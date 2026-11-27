"""Comprehensive fundamentals for the screeners, straight from Yahoo.

This is the authoritative fundamentals source. It reads Yahoo's quoteSummary
endpoint directly — the same data behind the "Key Statistics" page — and fills
every column the screeners use, so a filter like "PEG under 1" actually has a
number for (almost) every stock instead of one in two.

Why not yfinance: on a CI runner yfinance's cookie/crumb handshake often returns
empty `info`, which is exactly why coverage used to be patchy. We do the crumb
handshake once ourselves and hit the JSON endpoint directly, which is far more
reliable at scale.

What it writes, per stock (whatever Yahoo has):
  valuation   pe, forward_pe, pb, ps, ev_ebitda, book_value_ps
  dividend    dividend_yield                     (growth/streak: update_dividends.py)
  growth      eps_growth, revenue_growth (annual), earnings_growth_q, revenue_growth_q,
              eps_growth_3y / 5y / 10y
  quality     net_margin, gross_margin, operating_margin, roe, roa, roic, receivable_days
  balance     current_ratio, quick_ratio, debt_to_equity, debt_to_assets,
              cash_usd_m, cash_pct_assets, ebitda_usd_m
  size/share  market_cap_usd_b, shares_outstanding_m, revenue_ps, fcf_ps
  risk        beta, short_pct_float, high_52w, low_52w

It runs in slices, oldest-first, and picks up where it stopped:

    python update_fundamentals.py --limit 300
    python update_fundamentals.py --region Switzerland
    python update_fundamentals.py --stale-days 2 --deadline 11000

Columns live in migration_fundamentals.sql and migration_fundamentals2.sql. Any
that haven't been created yet are detected and simply left out of the write, so
nothing fails before the migration is run.

Balance-sheet figures come from Yahoo's fundamentals-timeseries feed (the data
behind the Financials tab), because the old quoteSummary balanceSheetHistory
module now returns only dates.

Progress is tracked in its own column, deep_fundamentals_at
(migration_deep_fundamentals.sql). It used to share fundamentals_updated_at with
update_us.py, which stamps every stock several times a day, so this script
always found "nothing stale" and wrote nothing. Names with no balance-sheet data
yet always go first.

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, like the other scripts.
"""
import argparse
import datetime as _dt
import time

import requests

from common import Supabase, log, now_iso, num, to_usd

TAX = 0.21   # assumed cash tax rate for the ROIC numerator

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
MODULES = ("summaryDetail,defaultKeyStatistics,financialData,price,"
           "incomeStatementHistory,balanceSheetHistory")

# Columns added by each migration. We probe which exist and never write one that
# doesn't, so a missing migration degrades to "fewer columns", never a failure.
CORE_COLS = ["pe", "pb", "dividend_yield", "beta", "revenue_growth", "eps_growth",
             "net_margin", "high_52w", "low_52w", "market_cap_usd_b"]
EXTRA1 = ["fcf_ps", "revenue_ps", "shares_outstanding_m", "roic", "receivable_days",
          "debt_to_assets", "cash_usd_m", "cash_pct_assets",
          "eps_growth_3y", "eps_growth_5y", "eps_growth_10y"]
EXTRA2 = ["roe", "roa", "gross_margin", "operating_margin", "current_ratio",
          "quick_ratio", "debt_to_equity", "forward_pe", "ps", "ev_ebitda",
          "earnings_growth_q", "revenue_growth_q", "ebitda_usd_m",
          "book_value_ps", "short_pct_float"]


# ---------------------------------------------------------------------------
# Yahoo quoteSummary client
# ---------------------------------------------------------------------------
class Yahoo:
    """One crumb/cookie handshake, then direct quoteSummary reads."""

    def __init__(self):
        self.s = requests.Session()
        # Accept must stay generic: with "application/json" Yahoo's getcrumb answers
        # 406 Not Acceptable, no crumb comes back and every read is empty.
        self.s.headers.update({"User-Agent": _UA, "Accept": "*/*"})
        self.crumb = None

    def _bootstrap(self):
        # A GET to the consumer site sets the A1/A3 cookies the crumb is tied to.
        for url in ("https://fc.yahoo.com/", "https://finance.yahoo.com/"):
            try:
                self.s.get(url, timeout=15)
            except requests.RequestException:
                pass
        for host in _HOSTS:
            try:
                r = self.s.get(f"https://{host}/v1/test/getcrumb", timeout=15)
            except requests.RequestException:
                continue
            txt = (r.text or "").strip()
            if r.status_code == 200 and txt and "<" not in txt and "Too Many" not in txt:
                self.crumb = txt
                return True
        return False

    def summary(self, sym):
        """The quoteSummary result object for one symbol, or None."""
        if self.crumb is None and not self._bootstrap():
            return None
        params = {"modules": MODULES, "crumb": self.crumb}
        for attempt in range(4):
            host = _HOSTS[attempt % 2]
            try:
                r = self.s.get(f"https://{host}/v10/finance/quoteSummary/{sym}",
                               params=params, timeout=25)
            except requests.RequestException:
                time.sleep(1.2 * (attempt + 1))
                continue
            if r.status_code == 200:
                try:
                    res = ((r.json().get("quoteSummary") or {}).get("result")) or []
                except ValueError:
                    return None
                return res[0] if res else None
            if r.status_code in (401, 403):
                # Crumb went stale — refresh once and retry.
                self.crumb = None
                if not self._bootstrap():
                    return None
                params["crumb"] = self.crumb
                continue
            if r.status_code == 404:
                return None                    # not a Yahoo symbol
            if r.status_code == 429:
                time.sleep(6 * (attempt + 1))
                continue
            time.sleep(1.2 * (attempt + 1))
        return None


TS_TYPES = ("TotalAssets", "StockholdersEquity", "TotalDebt", "AccountsReceivable",
            "CashAndCashEquivalents", "CashCashEquivalentsAndShortTermInvestments",
            "CurrentAssets", "CurrentLiabilities", "Inventory", "InvestedCapital",
            "EBIT", "OperatingIncome", "NetIncome", "TotalRevenue", "FreeCashFlow",
            "OrdinarySharesNumber")


def timeseries(yahoo, sym):
    """{field: [(year, value)] newest-first} of annual statement lines, or {}."""
    if yahoo.crumb is None and not yahoo._bootstrap():
        return {}
    now = int(time.time())
    params = {"type": ",".join("annual" + t for t in TS_TYPES),
              "period1": now - 12 * 365 * 86400, "period2": now, "crumb": yahoo.crumb}
    for attempt in range(3):
        host = _HOSTS[attempt % 2]
        try:
            r = yahoo.s.get(f"https://{host}/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}",
                            params=params, timeout=25)
        except requests.RequestException:
            time.sleep(1.2 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                results = (r.json().get("timeseries") or {}).get("result") or []
            except ValueError:
                return {}
            out = {}
            for res in results:
                t = ((res.get("meta") or {}).get("type") or [None])[0]
                pts = []
                for v in res.get(t) or []:
                    if not v:
                        continue
                    val = num(((v.get("reportedValue") or {}).get("raw")))
                    year = (v.get("asOfDate") or "")[:4]
                    if val is not None and year.isdigit():
                        pts.append((int(year), val))
                if t and pts:
                    out[t[len("annual"):]] = sorted(pts, reverse=True)
            return out
        if r.status_code == 429:
            time.sleep(6 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            yahoo.crumb = None
            if not yahoo._bootstrap():
                return {}
            params["crumb"] = yahoo.crumb
            continue
        return {}
    return {}


# ---------------------------------------------------------------------------
# Pulling values out of the nested {raw, fmt} shapes
# ---------------------------------------------------------------------------
def g(mod, *keys):
    """A numeric field from a module, unwrapping Yahoo's {raw: ...} boxes."""
    if not isinstance(mod, dict):
        return None
    for key in keys:
        v = mod.get(key)
        if isinstance(v, dict):
            v = v.get("raw")
        n = num(v)
        if n is not None:
            return n
    return None


def pct(mod, *keys):
    """A fraction field (0.18) returned as a percent (18.0)."""
    v = g(mod, *keys)
    return None if v is None else round(v * 100, 2)


def annual_series(statements, *field_names):
    """[(year, value)] newest-first from a quoteSummary statement list."""
    out = []
    for st in statements or []:
        if not isinstance(st, dict):
            continue
        val = None
        for f in field_names:
            val = g(st, f)
            if val is not None:
                break
        if val is None:
            continue
        end = st.get("endDate")
        year = None
        if isinstance(end, dict):
            # endDate.fmt looks like "2025-12-31"; its raw is a unix timestamp.
            fmt = end.get("fmt")
            if fmt and len(fmt) >= 4 and fmt[:4].isdigit():
                year = int(fmt[:4])
            elif end.get("raw"):
                year = _dt.datetime.utcfromtimestamp(end["raw"]).year
        out.append((year, val))
    out.sort(key=lambda t: (t[0] or 0), reverse=True)
    return out


def cagr(new, old, years):
    """Compound annual growth, percent. None when it can't be computed."""
    try:
        new, old, years = float(new), float(old), float(years)
    except (TypeError, ValueError):
        return None
    if years <= 0 or old == 0:
        return None
    if (new < 0) != (old < 0):      # a sign change makes growth meaningless
        return None
    if new < 0 and old < 0:
        new, old = -new, -old
        try:
            return round(((old / new) ** (1 / years) - 1) * -100, 2)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return round(((new / old) ** (1 / years) - 1) * 100, 2)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def growth_1y(series):
    """Newest-over-previous year growth, percent."""
    if len(series) < 2:
        return None
    (ny, nv), (oy, ov) = series[0], series[1]
    span = (ny - oy) if (ny and oy) else 1
    return cagr(nv, ov, span or 1)


def growth_windows(series):
    """3/5/10-year CAGR from an annual series (as many as the data allows)."""
    out = {"eps_growth_3y": None, "eps_growth_5y": None, "eps_growth_10y": None}
    if len(series) < 2 or series[0][0] is None:
        return out
    newest_year, newest = series[0]
    for window, key in ((3, "eps_growth_3y"), (5, "eps_growth_5y"), (10, "eps_growth_10y")):
        best = None
        for year, v in series[1:]:
            if year is None:
                continue
            span = newest_year - year
            # only a span that really covers the window — a 3-year figure must not
            # be passed off as the 5- or 10-year one
            if span >= window and (best is None or span < best[0]):
                best = (span, v)
        if best:
            out[key] = cagr(newest, best[1], best[0])
    return out


# ---------------------------------------------------------------------------
# Turn one quoteSummary result into a row of columns
# ---------------------------------------------------------------------------
def build_row(res, currency, ts=None):
    sd = res.get("summaryDetail") or {}
    ks = res.get("defaultKeyStatistics") or {}
    fd = res.get("financialData") or {}
    pr = res.get("price") or {}
    inc = (res.get("incomeStatementHistory") or {}).get("incomeStatementHistory") or []
    bs = (res.get("balanceSheetHistory") or {}).get("balanceSheetStatements") or []

    fx = to_usd(currency) or 1.0
    # Statement figures are in the company's reporting currency, which can differ
    # from the trading currency (ADRs, some Swiss listings report in USD/EUR).
    fin_cur = (fd.get("financialCurrency") or currency or "USD")
    if isinstance(fin_cur, dict):
        fin_cur = fin_cur.get("raw") or currency
    fx_fin = (to_usd(fin_cur) if fin_cur != currency else fx) or fx
    ts = ts or {}
    latest = lambda k: (ts.get(k) or [(None, None)])[0][1]
    rec = {}

    def put(col, val):
        if val is not None:
            rec[col] = val

    # ---- valuation ----
    put("pe", g(sd, "trailingPE") or g(ks, "trailingPE"))
    put("forward_pe", g(sd, "forwardPE") or g(ks, "forwardPE"))
    put("pb", g(ks, "priceToBook"))
    put("ps", g(sd, "priceToSalesTrailing12Months"))
    put("ev_ebitda", g(ks, "enterpriseToEbitda"))
    put("book_value_ps", g(ks, "bookValue"))

    # ---- dividend yield (growth & streak are update_dividends.py's job) ----
    dy = g(sd, "dividendYield")
    if dy is None:
        dy = g(sd, "trailingAnnualDividendYield")
    if dy is not None:
        # Yahoo gives a fraction (0.032). Guard the odd feed that already sends 3.2.
        put("dividend_yield", round(dy * 100, 3) if dy < 1 else round(dy, 3))

    # ---- margins & returns on capital ----
    put("net_margin", pct(fd, "profitMargins") or pct(ks, "profitMargins"))
    put("gross_margin", pct(fd, "grossMargins"))
    put("operating_margin", pct(fd, "operatingMargins"))
    put("roe", pct(fd, "returnOnEquity"))
    put("roa", pct(fd, "returnOnAssets"))

    # ---- liquidity / leverage ----
    put("current_ratio", g(fd, "currentRatio"))
    put("quick_ratio", g(fd, "quickRatio"))
    put("debt_to_equity", g(fd, "debtToEquity"))   # already a percent in Yahoo's feed

    # ---- risk / range ----
    put("beta", g(sd, "beta") or g(ks, "beta"))
    put("high_52w", g(sd, "fiftyTwoWeekHigh"))
    put("low_52w", g(sd, "fiftyTwoWeekLow"))
    put("short_pct_float", pct(ks, "shortPercentOfFloat"))

    # ---- size / per share ----
    cap = g(pr, "marketCap") or g(sd, "marketCap") or g(ks, "marketCap")
    if cap is not None:
        put("market_cap_usd_b", round(cap * fx / 1e9, 3))
    shares = g(ks, "sharesOutstanding") or g(ks, "impliedSharesOutstanding")
    if shares:
        put("shares_outstanding_m", round(shares / 1e6, 3))
    ebitda = g(fd, "ebitda")
    if ebitda is not None:
        put("ebitda_usd_m", round(ebitda * fx / 1e6, 2))

    revenue = g(fd, "totalRevenue")
    if revenue is None:
        rev_series_now = annual_series(inc, "totalRevenue")
        revenue = rev_series_now[0][1] if rev_series_now else None

    rps = g(fd, "revenuePerShare")
    if rps is None and revenue and shares:
        rps = revenue / shares
    put("revenue_ps", None if rps is None else round(rps, 4))

    fcf = g(fd, "freeCashflow")
    if fcf is None:
        ocf = g(fd, "operatingCashflow")
        # capex isn't in these modules; operating cash flow alone is a usable floor
        fcf = ocf
    if fcf is not None and shares:
        put("fcf_ps", round(fcf / shares, 4))

    # ---- quarterly growth ----
    put("earnings_growth_q", pct(ks, "earningsQuarterlyGrowth") or pct(fd, "earningsGrowth"))
    put("revenue_growth_q", pct(fd, "revenueGrowth"))

    # ---- annual growth (what PEG uses) + multi-year windows ----
    ni_series = annual_series(inc, "netIncome")
    rev_series = annual_series(inc, "totalRevenue")
    eg = growth_1y(ni_series)
    if eg is None:
        eg = pct(ks, "earningsQuarterlyGrowth")
    put("eps_growth", eg)
    rg = growth_1y(rev_series)
    if rg is None:
        rg = pct(fd, "revenueGrowth")
    put("revenue_growth", rg)
    for k, v in growth_windows(ni_series).items():
        put(k, v)

    # ---- balance sheet ----
    # Newest annual statement from the timeseries feed; the old quoteSummary
    # balanceSheetHistory module is only a fallback (Yahoo now leaves it empty).
    first = lambda *names: next((v for _, v in annual_series(bs, *names)), None)
    total_assets = latest("TotalAssets") or first("totalAssets")
    equity = latest("StockholdersEquity") or first("totalStockholderEquity", "stockholdersEquity")
    receivables = latest("AccountsReceivable")
    if receivables is None:
        receivables = first("netReceivables", "accountsReceivable")
    total_debt = g(fd, "totalDebt")
    if total_debt is None:
        total_debt = latest("TotalDebt")
    cash = g(fd, "totalCash")
    if cash is None:
        cash = latest("CashCashEquivalentsAndShortTermInvestments") or latest("CashAndCashEquivalents") \
            or first("cash", "cashAndCashEquivalents")
    stmt_revenue = latest("TotalRevenue") or revenue

    if total_assets and total_debt is not None:
        put("debt_to_assets", round(total_debt / total_assets * 100, 2))
    if total_assets and cash is not None:
        put("cash_pct_assets", round(cash / total_assets * 100, 2))
    if cash is not None:
        put("cash_usd_m", round(cash * fx_fin / 1e6, 2))
    if stmt_revenue and receivables is not None:
        put("receivable_days", round(receivables / stmt_revenue * 365, 1))

    # Liquidity and leverage when financialData has none (common outside the US)
    ca, cl, inv = latest("CurrentAssets"), latest("CurrentLiabilities"), latest("Inventory")
    if "current_ratio" not in rec and ca and cl:
        put("current_ratio", round(ca / cl, 3))
    if "quick_ratio" not in rec and ca and cl:
        put("quick_ratio", round((ca - (inv or 0)) / cl, 3))
    if "debt_to_equity" not in rec and total_debt is not None and equity and equity > 0:
        put("debt_to_equity", round(total_debt / equity * 100, 2))
    if "roe" not in rec and equity and equity > 0 and latest("NetIncome") is not None:
        put("roe", round(latest("NetIncome") / equity * 100, 2))
    if "roa" not in rec and total_assets and latest("NetIncome") is not None:
        put("roa", round(latest("NetIncome") / total_assets * 100, 2))
    if "book_value_ps" not in rec and equity and shares:
        put("book_value_ps", round(equity * fx_fin / fx / shares, 4))
    if "fcf_ps" not in rec and latest("FreeCashFlow") is not None and shares:
        put("fcf_ps", round(latest("FreeCashFlow") * fx_fin / fx / shares, 4))

    # ROIC = EBIT after an assumed tax, over invested capital (debt + equity − cash)
    ebit = latest("EBIT") or latest("OperatingIncome") or first("ebit", "operatingIncome")
    invested = None
    if equity and total_debt is not None:
        invested = equity + total_debt - (cash or 0)
    if (not invested or invested <= 0) and latest("InvestedCapital"):
        invested = latest("InvestedCapital")
    if ebit is not None and invested and invested > 0:
        put("roic", round(ebit * (1 - TAX) / invested * 100, 2))

    # Multi-year earnings growth: the timeseries reaches further back than the
    # quoteSummary income history, so prefer it when it has more years.
    ni_ts = ts.get("NetIncome") or []
    if len(ni_ts) > len(ni_series):
        for k, v in growth_windows(ni_ts).items():
            put(k, v)

    # ---- reporting currency ≠ trading currency (ADRs such as TSM, BABA) ----
    # Yahoo then divides a USD price by per-share figures in TWD/CNY/…, and its
    # enterprise value is in the reporting currency, so P/B, P/S, EV/EBITDA and the
    # per-share numbers come out wildly wrong. Rebuild them from the market cap
    # (trading currency) and the statements converted into the trading currency.
    price = g(pr, "regularMarketPrice") or g(fd, "currentPrice")
    n_eff = cap / price if cap and price else None   # shares in listing units
    if n_eff and fin_cur == currency:
        # Same currency: Yahoo's ratios are fine, but its share count can cover only
        # one share class (Roche counts just its bearer shares), so per-share
        # figures use market cap ÷ price instead.
        if fcf is not None:
            put("fcf_ps", round(fcf / n_eff, 4))
        if revenue:
            put("revenue_ps", round(revenue / n_eff, 4))
    if fin_cur != currency and n_eff and fx_fin and fx:
        k = fx_fin / fx                      # reporting → trading currency
        if equity and equity > 0:
            put("pb", round(cap / (equity * k), 3))
            put("book_value_ps", round(equity * k / n_eff, 4))
        if revenue:
            put("ps", round(cap / (revenue * k), 4))
            put("revenue_ps", round(revenue * k / n_eff, 4))
        if ebitda and ebitda > 0:
            ev = cap + ((total_debt or 0) - (cash or 0)) * k
            put("ev_ebitda", round(ev / (ebitda * k), 3))
        if fcf is not None:
            put("fcf_ps", round(fcf * k / n_eff, 4))

    return rec


# ---------------------------------------------------------------------------
# Which of the optional columns actually exist in this database
# ---------------------------------------------------------------------------
def existing_columns(db, candidates):
    try:
        r = db.s.get(f"{db.url}/stocks",
                     params={"select": ",".join(candidates), "limit": "1"}, timeout=30)
        if r.status_code < 300:
            return set(candidates)
    except requests.RequestException:
        pass
    ok = set()
    for c in candidates:
        try:
            r = db.s.get(f"{db.url}/stocks", params={"select": c, "limit": "1"}, timeout=20)
            if r.status_code < 300:
                ok.add(c)
        except requests.RequestException:
            pass
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="", help="Only this region, e.g. US or Switzerland.")
    ap.add_argument("--limit", type=int, default=0, help="Stop after this many instruments.")
    ap.add_argument("--deadline", type=float, default=0, help="Seconds before saving what we have and stopping.")
    ap.add_argument("--stale-days", type=int, default=0,
                    help="Only names whose fundamentals are older than this many days (0 = all).")
    ap.add_argument("--sleep", type=float, default=0.6, help="Pause between names, to stay under Yahoo's limits.")
    args = ap.parse_args()

    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    db = Supabase()
    filters = {"asset_class": "in.(stock,reit)"}
    if args.region:
        filters["region"] = f"eq.{args.region}"
    # Progress lives in deep_fundamentals_at. fundamentals_updated_at is also
    # stamped by update_us.py several times a day, so using it made every stock
    # look fresh and this script did nothing.
    stamp = "deep_fundamentals_at" if existing_columns(db, ["deep_fundamentals_at"]) else None
    if not stamp:
        log("  deep_fundamentals_at isn't in the database yet — run "
            "supabase/migration_deep_fundamentals.sql. Until then every stock counts as stale.")
    cols = "symbol,currency,region" + (f",{stamp}" if stamp else "")
    rows = db.select(cols, filters, order=f"{stamp}.asc.nullsfirst" if stamp else "symbol")
    if args.stale_days and stamp:
        cutoff = time.time() - args.stale_days * 86400
        def is_stale(r):
            ts = r.get(stamp)
            if not ts:
                return True
            try:
                return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < cutoff
            except (ValueError, AttributeError):
                return True
        rows = [r for r in rows if is_stale(r)]
    # Names that still have no balance-sheet data go first.
    try:
        bare = {r["symbol"] for r in db.select("symbol", {**filters, "debt_to_assets": "is.null"})}
    except RuntimeError:
        bare = set()
    if bare:
        rows = [r for r in rows if r["symbol"] in bare] + [r for r in rows if r["symbol"] not in bare]
        log(f"  {len(bare)} have no balance-sheet data yet — doing those first.")
    if args.limit:
        rows = rows[: args.limit]

    log(f"{len(rows)} instruments to update.")
    if not rows:
        return

    allowed = set(CORE_COLS) | existing_columns(db, EXTRA1 + EXTRA2)
    missing = (set(EXTRA1) | set(EXTRA2)) - allowed
    if missing:
        have2 = allowed & set(EXTRA2)
        if not have2:
            log("  The new columns (roe, roa, current_ratio, forward_pe, ps …) aren't in the "
                "database yet — run supabase/migration_fundamentals2.sql to switch on those screeners.")
        else:
            log(f"  {len(missing)} optional column(s) not in the database yet; leaving them out: "
                + ", ".join(sorted(missing)))

    yahoo = Yahoo()
    batch, saved, skipped = [], 0, 0
    for i, row in enumerate(rows, 1):
        if out_of_time():
            log(f"  deadline reached after {i - 1}/{len(rows)}")
            break
        sym = row["symbol"]
        res = yahoo.summary(sym)
        if not res:
            skipped += 1
            if i % 50 == 0:
                log(f"  {i}/{len(rows)} (no data for {sym})")
            time.sleep(args.sleep)
            continue

        currency = row.get("currency") or ((res.get("price") or {}).get("currency")) or "USD"
        rec = build_row(res, currency, timeseries(yahoo, sym))
        rec = {k: v for k, v in rec.items() if k in allowed}
        if not rec:
            skipped += 1
            time.sleep(args.sleep)
            continue
        rec["symbol"] = sym
        rec["region"] = row.get("region") or "US"
        rec["fundamentals_updated_at"] = now_iso()
        if stamp:
            rec[stamp] = now_iso()
        batch.append(rec)

        if len(batch) >= 40:
            db.upsert(batch)
            saved += len(batch)
            batch = []
            log(f"  {i}/{len(rows)} — {saved} saved")
        elif i % 50 == 0:
            log(f"  {i}/{len(rows)}")
        time.sleep(args.sleep)

    if batch:
        db.upsert(batch)
        saved += len(batch)
    log(f"Saved {saved} instruments, skipped {skipped}, in {time.monotonic() - started:.0f}s.")


if __name__ == "__main__":
    main()
