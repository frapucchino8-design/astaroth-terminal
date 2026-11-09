"""Fill in the extra fundamentals the new screeners use.

Covers free cash flow per share, revenue per share, shares outstanding, ROIC,
receivable days, debt/assets, cash and cash as a share of assets, five-year
dividend growth, and earnings growth over three, five and ten years.

Everything comes from Yahoo through yfinance: `info` for the headline figures,
and the yearly balance sheet / income statement / dividend history for the ones
that need several years. That makes it slow — a few seconds per name — so it is
built to run in slices and pick up where it stopped:

    python update_fundamentals.py --region US --limit 300
    python update_fundamentals.py --region Switzerland
    python update_fundamentals.py --stale-days 30 --deadline 18000

Run supabase/migration_fundamentals.sql first, or every write will fail on a
missing column.

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, like the other scripts.
"""
import argparse
import math
import time

from common import Supabase, log, now_iso, num, to_usd

TAX = 0.21   # assumed cash tax rate for the ROIC numerator


def cagr(new, old, years):
    """Compound annual growth, in percent. None when it can't be computed."""
    try:
        new, old, years = float(new), float(old), float(years)
    except (TypeError, ValueError):
        return None
    if years <= 0 or old == 0:
        return None
    # A sign change makes a growth rate meaningless.
    if (new < 0) != (old < 0):
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


def first_row(frame, *names):
    """Pull a row out of a yfinance statement frame by any of several labels."""
    if frame is None or getattr(frame, "empty", True):
        return None
    index = {str(i).strip().lower(): i for i in frame.index}
    for n in names:
        key = n.strip().lower()
        if key in index:
            return frame.loc[index[key]]
    return None


def series_values(row):
    """A statement row as [(year, value)], newest first, skipping blanks."""
    if row is None:
        return []
    out = []
    for col, val in row.items():
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if v != v:
            continue
        year = getattr(col, "year", None)
        out.append((year, v))
    return out


def eps_growth_windows(tk):
    """EPS growth over 3, 5 and 10 years, from the yearly income statement."""
    out = {"eps_growth_3y": None, "eps_growth_5y": None, "eps_growth_10y": None}
    try:
        inc = tk.income_stmt
    except Exception:
        return out
    eps_row = first_row(inc, "Diluted EPS", "Basic EPS")
    vals = series_values(eps_row)
    if len(vals) < 2:
        # Fall back to net income / shares, which is close enough for a growth rate.
        ni = series_values(first_row(inc, "Net Income", "Net Income Common Stockholders"))
        sh = series_values(first_row(inc, "Diluted Average Shares", "Basic Average Shares"))
        if len(ni) >= 2 and len(sh) >= 2:
            per = {y: v for y, v in sh if v}
            vals = [(y, v / per[y]) for y, v in ni if y in per]
    if len(vals) < 2:
        return out
    vals.sort(key=lambda t: (t[0] or 0), reverse=True)
    newest_year, newest = vals[0]
    for window, key in ((3, "eps_growth_3y"), (5, "eps_growth_5y"), (10, "eps_growth_10y")):
        # the oldest point no further back than `window` years
        best = None
        for year, v in vals[1:]:
            if year is None or newest_year is None:
                continue
            span = newest_year - year
            if 0 < span <= window and (best is None or span > best[0]):
                best = (span, v)
        if best:
            out[key] = cagr(newest, best[1], best[0])
    return out


def dividend_growth_5y(tk):
    """Five-year growth in dividends per share, from the payment history."""
    try:
        divs = tk.dividends
    except Exception:
        return None
    if divs is None or len(divs) == 0:
        return None
    by_year = {}
    for ts, amount in divs.items():
        try:
            by_year.setdefault(ts.year, 0.0)
            by_year[ts.year] += float(amount)
        except (TypeError, ValueError, AttributeError):
            continue
    years = sorted(by_year)
    if len(years) < 3:
        return None
    # ignore the current year, which is usually incomplete
    import datetime as _dt
    this_year = _dt.date.today().year
    years = [y for y in years if y < this_year]
    if len(years) < 3:
        return None
    newest = years[-1]
    target = [y for y in years if newest - y <= 5 and newest - y >= 3]
    if not target:
        target = years[:1]
    oldest = min(target)
    return cagr(by_year[newest], by_year[oldest], newest - oldest)


def balance_sheet_metrics(tk, info, price, currency):
    """Debt/assets, cash, cash share of assets, receivable days, ROIC."""
    out = {}
    total_assets = total_debt = cash = receivables = None
    try:
        bs = tk.balance_sheet
        total_assets = next((v for _, v in series_values(first_row(bs, "Total Assets"))), None)
        total_debt = next((v for _, v in series_values(first_row(bs, "Total Debt"))), None)
        cash = next((v for _, v in series_values(first_row(
            bs, "Cash Cash Equivalents And Short Term Investments",
            "Cash And Cash Equivalents"))), None)
        receivables = next((v for _, v in series_values(first_row(
            bs, "Accounts Receivable", "Receivables"))), None)
        equity = next((v for _, v in series_values(first_row(
            bs, "Stockholders Equity", "Total Equity Gross Minority Interest"))), None)
    except Exception:
        equity = None

    # info is often more current than the statements
    total_debt = total_debt if total_debt is not None else num(info.get("totalDebt"))
    cash = cash if cash is not None else num(info.get("totalCash"))

    if total_assets and total_debt is not None:
        out["debt_to_assets"] = round(total_debt / total_assets * 100, 2)
    if total_assets and cash is not None:
        out["cash_pct_assets"] = round(cash / total_assets * 100, 2)
    if cash is not None:
        fx = to_usd(currency) or 1.0
        out["cash_usd_m"] = round(cash * fx / 1e6, 2)

    revenue = num(info.get("totalRevenue"))
    if revenue is None:
        try:
            revenue = next((v for _, v in series_values(first_row(tk.income_stmt, "Total Revenue"))), None)
        except Exception:
            revenue = None
    if revenue and receivables is not None:
        out["receivable_days"] = round(receivables / revenue * 365, 1)

    # ROIC = EBIT after an assumed tax rate, over invested capital
    ebit = num(info.get("ebit")) or num(info.get("ebitda"))
    if ebit is None:
        try:
            ebit = next((v for _, v in series_values(first_row(tk.income_stmt, "EBIT", "Operating Income"))), None)
        except Exception:
            ebit = None
    if ebit is not None and equity and total_debt is not None:
        invested = equity + total_debt - (cash or 0)
        if invested and invested > 0:
            out["roic"] = round(ebit * (1 - TAX) / invested * 100, 2)
    return out, revenue


def per_share(info, revenue, shares):
    out = {}
    if shares:
        out["shares_outstanding_m"] = round(shares / 1e6, 3)
    fcf = num(info.get("freeCashflow"))
    if fcf is None:
        ocf, capex = num(info.get("operatingCashflow")), num(info.get("capitalExpenditures"))
        if ocf is not None and capex is not None:
            fcf = ocf + capex          # capex comes through negative
    if fcf is not None and shares:
        out["fcf_ps"] = round(fcf / shares, 4)
    rps = num(info.get("revenuePerShare"))
    if rps is None and revenue and shares:
        rps = revenue / shares
    if rps is not None:
        out["revenue_ps"] = round(rps, 4)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="", help="Only this region, e.g. US or Switzerland.")
    ap.add_argument("--limit", type=int, default=0, help="Stop after this many instruments.")
    ap.add_argument("--deadline", type=float, default=0, help="Seconds before saving what we have and stopping.")
    ap.add_argument("--stale-days", type=int, default=0,
                    help="Only names whose fundamentals are older than this many days (0 = all).")
    ap.add_argument("--sleep", type=float, default=0.8, help="Pause between names, to stay under Yahoo's limits.")
    args = ap.parse_args()

    try:
        import yfinance as yf
    except ImportError:
        raise SystemExit("yfinance isn't installed. pip install -r requirements.txt")

    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    db = Supabase()
    filters = {"asset_class": "in.(stock,reit)"}
    if args.region:
        filters["region"] = f"eq.{args.region}"
    rows = db.select("symbol,currency,price,region,fundamentals_updated_at", filters, order="symbol")
    if args.stale_days:
        cutoff = time.time() - args.stale_days * 86400
        def is_stale(r):
            ts = r.get("fundamentals_updated_at")
            if not ts:
                return True
            try:
                import datetime as _dt
                return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < cutoff
            except (ValueError, AttributeError):
                return True
        rows = [r for r in rows if is_stale(r)]
    if args.limit:
        rows = rows[: args.limit]

    log(f"{len(rows)} instruments to update.")
    if not rows:
        return

    batch, saved, skipped = [], 0, 0
    for i, row in enumerate(rows, 1):
        if out_of_time():
            log(f"  deadline reached after {i - 1}/{len(rows)}")
            break
        sym = row["symbol"]
        try:
            tk = yf.Ticker(sym)
            info = tk.info or {}
        except Exception as e:
            skipped += 1
            if i % 25 == 0:
                log(f"  {i}/{len(rows)} (skipped {sym}: {str(e)[:40]})")
            continue
        if not info:
            skipped += 1
            continue

        shares = num(info.get("sharesOutstanding")) or num(info.get("impliedSharesOutstanding"))
        currency = row.get("currency") or info.get("currency") or "USD"
        bs, revenue = balance_sheet_metrics(tk, info, row.get("price"), currency)

        rec = {"symbol": sym, "region": row.get("region") or "US"}
        rec.update(bs)
        rec.update(per_share(info, revenue, shares))
        rec.update(eps_growth_windows(tk))
        dg = dividend_growth_5y(tk)
        if dg is not None:
            rec["dividend_growth_5y"] = dg
        rec["fundamentals_updated_at"] = now_iso()

        # Only write if we actually learned something beyond the keys we always set.
        if len(rec) > 3:
            batch.append(rec)
        else:
            skipped += 1

        if len(batch) >= 40:
            db.upsert(batch)
            saved += len(batch)
            batch = []
            log(f"  {i}/{len(rows)} — {saved} saved")
        elif i % 25 == 0:
            log(f"  {i}/{len(rows)}")
        time.sleep(args.sleep)

    if batch:
        db.upsert(batch)
        saved += len(batch)
    log(f"Saved {saved} instruments, skipped {skipped}, in {time.monotonic() - started:.0f}s.")


if __name__ == "__main__":
    main()# update_fundamentals.py — paste contents from Claude chat here
