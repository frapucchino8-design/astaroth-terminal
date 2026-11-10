"""Nightly update for Swiss stocks on SIX, using Yahoo Finance through the yfinance library.

The list of SIX shares is put together from three sources, so one failing doesn't matter:
  1. Yahoo's own screener: every equity on SIX (exchange code EBS)
  2. Finnhub's symbol list for SIX (if the free plan allows it)
  3. A built-in list of about 230 SIX shares
Yahoo is unofficial: fine for a personal project, not for a paid public site.
Each row carries asset_class = "stock" (or "reit" when the sector says so).

Changes vs the earlier version:
  * A symbol is only accepted if Yahoo returns a non-trivial `marketCap`. The
    old version let through 370+ no-cap junk symbols — dormant listings, non-
    equity instruments with blank metadata — which then stayed in the table
    forever because the prune only touched symbols seen in the current run.
  * Non-CHF listings that aren't on the built-in list are refused: USD, EUR and
    GBP symbols on SIX are almost always foreign secondary listings.
  * The prune at the end now also drops any Swiss row with no cap or in USD,
    not just ones missing from kept_syms. This cleans up legacy rows the old
    version let in before this patch existed.
"""
import argparse
import os
import time

import requests
import yfinance as yf

from common import YAHOO_SECTORS, Supabase, log, now_iso, num, to_usd

# Built-in list of SIX-listed shares (Yahoo symbols without ".SW"). Unknown ones are skipped.
BUILTIN = """
ABBN ACLN ADEN ADXN AERO AEVS ALC ALLN ALPN ALSN AMS APGN ARYN ASCN AUTN AVOL BAER BALN BANB
BARN BBN BCGE BCHN BCVN BEAN BEKN BELL BION BKW BLKB BOSN BRKN BSKP BSLN BUCN BVZN BYS CALN
CFR CICN CLN CLTN CMBN COPN COTN CPHN DAE DESN DKSH DOCM DOKA EDHN EFGN ELMN EMMN EMSN EPIC
FHZN FORN FREN GALD GALE GAM GEBN GF GIVN GLKBN GMI GRKP GUR HBLN HBMN HELN HIAG HLEE HOCN
HOLN HUBN IDIA IFCN IMPN INRN IREN ISN JFN KARN KLIN KNIN KOMN KUD KURN LAND LEHN LEON LINN
LISN LISP LLBN LOGN LONN LUKN MBTN MCHN MED METN MIKN MOBN MOLN MOVE MOZN MTG NBEN NESN NOVN
NREN NWRN OERL OFN ORON PEAN PEDU PEHN PGHN PLAN PMN PPGN PSPN RIEN RLF RO ROG SAHN SANN
SCHN SCHP SCMN SDZ SENS SFPN SFSN SFZN SGKN SGSN SHLTN SIGN SIKA SKAN SLHN SNBN SOON SPCE
SPEX SPSN SQN SRAIL SREN STGN STLN STMN SUN SWON SWTQ TECN TEMN TIBN TKBP TOHN TXGN UBSG UBXN
UHR UHRN VACN VAHN VARN VATN VBSN VETN VILN VLRT VONN VPBN VZN WARN WIHN WKBN XLS YPSN ZEHN
ZUBN ZUGER ZUGN ZURN ZWM
""".split()


def yahoo_list():
    try:
        from yfinance import EquityQuery
        q = EquityQuery("and", [EquityQuery("eq", ["region", "ch"]), EquityQuery("is-in", ["exchange", "EBS"])])
        out, offset = set(), 0
        while offset < 2000:
            page = yf.screen(q, offset=offset, size=250, sortField="intradaymarketcap", sortAsc=False) or {}
            quotes = page.get("quotes") or []
            out |= {x["symbol"] for x in quotes if x.get("symbol", "").endswith(".SW")}
            if len(quotes) < 250:
                break
            offset += 250
            time.sleep(1)
        log(f"Yahoo screener lists {len(out)} Swiss shares.")
        return out
    except Exception as e:
        log(f"Yahoo screener failed ({e}).")
        return set()


def finnhub_list():
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        return set()
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/symbol",
                         params={"exchange": "SW"}, headers={"X-Finnhub-Token": key}, timeout=60)
        if not r.ok:
            log(f"Finnhub SIX list not available ({r.status_code}).")
            return set()
        out = {x["symbol"] for x in r.json()
               if x.get("type") in ("Common Stock", "REIT", "Equity") and x.get("symbol", "").endswith(".SW")}
        log(f"Finnhub lists {len(out)} Swiss shares.")
        return out
    except (requests.RequestException, ValueError) as e:
        log(f"Finnhub SIX list failed ({e}).")
        return set()


def swiss_symbols():
    yahoo, finnhub = yahoo_list(), finnhub_list()
    builtin = {s + ".SW" for s in BUILTIN}
    symbols = sorted(yahoo | finnhub | builtin)
    complete = len(yahoo) >= 150 or len(finnhub) >= 150
    log(f"Checking {len(symbols)} Swiss symbols in total.")
    return symbols, complete


def fetch_info(sym):
    for attempt in range(3):
        try:
            return yf.Ticker(sym).info or {}
        except Exception as e:
            msg = str(e)
            if "Too Many Requests" in msg or "Rate" in msg:
                log(f"  Yahoo rate limit, waiting {30 * (attempt + 1)}s")
                time.sleep(30 * (attempt + 1))
                continue
            return {}
    return {}


def eps_growth(info, pe):
    peg = num(info.get("trailingPegRatio") or info.get("pegRatio"))
    if pe and peg and peg > 0:
        return round(pe / peg, 2)
    return num(info.get("earningsGrowth"), scale=100, digits=2)


# Currencies Swiss shares legitimately trade in. Anything else is a secondary
# listing (nearly always USD, EUR or GBP) and we don't want them tagged Swiss.
SWISS_CURRENCIES = {"CHF"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Only process this many stocks (for testing).")
    args = ap.parse_args()

    db = Supabase()
    symbols, complete_list = swiss_symbols()
    if args.limit:
        symbols = symbols[:args.limit]

    builtin_syms = {s + ".SW" for s in BUILTIN}
    rows, skipped, saved = [], 0, 0
    dropped_foreign = dropped_nocap = dropped_wrongcur = 0
    kept_syms = set()   # only the genuine Swiss equities we actually stored
    for i, sym in enumerate(symbols, 1):
        info = fetch_info(sym)
        price = num(info.get("currentPrice") or info.get("regularMarketPrice"))
        # Must be an actual share, not an ETF, fund, right or warrant that the
        # EBS screener also returns.
        if not price or info.get("quoteType") != "EQUITY":
            skipped += 1
            continue
        country = (info.get("country") or "").strip()
        cur = info.get("currency") or "CHF"
        cap = num(info.get("marketCap"))
        on_builtin = sym in builtin_syms

        # Must be a Swiss company. The EBS screener lists foreign secondary
        # listings too. Trust the built-in list; otherwise require Yahoo to say
        # the country is Switzerland.
        if not on_builtin and country and country != "Switzerland":
            dropped_foreign += 1
            continue
        # Must trade in a Swiss currency. Foreign secondary listings on SIX
        # trade in USD/EUR/GBP and Yahoo returns no country for them, so the
        # check above lets them through. This one stops them.
        if not on_builtin and cur not in SWISS_CURRENCIES:
            dropped_wrongcur += 1
            continue
        # Must have a market cap. Real listed shares always do; symbols where
        # Yahoo gives nothing are dormant, non-equity, or metadata-blank. This
        # is what kept the Swiss count at ~600 instead of ~230.
        if not on_builtin and not cap:
            dropped_nocap += 1
            continue

        prev = num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
        fx = to_usd(cur)
        div = num(info.get("dividendRate") or info.get("trailingAnnualDividendRate"))
        pe = num(info.get("trailingPE"), digits=2)
        pe = pe if pe and pe > 0 else None
        sector = YAHOO_SECTORS.get(info.get("sector"), "Other" if info.get("sector") else None)
        is_reit = sector == "Real Estate" or (info.get("industry") or "").lower().startswith("reit")
        rows.append({
            "symbol": sym,
            "name": info.get("longName") or info.get("shortName") or sym,
            "exchange": "SIX",
            "region": "Switzerland",
            "asset_class": "reit" if is_reit else "stock",
            "currency": cur,
            "sector": sector,
            "industry": info.get("industry"),
            "summary": (info.get("longBusinessSummary") or "").strip()[:800] or None,
            "price": price,
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "market_cap_usd_b": round(cap * fx / 1e9, 3) if cap and fx else None,
            "pe": pe,
            "pb": num(info.get("priceToBook"), digits=2),
            "dividend_yield": round(div / price * 100, 3) if div else None,
            "return_1y": num(info.get("52WeekChange"), scale=100, digits=2),
            "beta": num(info.get("beta"), digits=3),
            "revenue_growth": num(info.get("revenueGrowth"), scale=100, digits=2),
            "eps_growth": eps_growth(info, pe),
            "net_margin": num(info.get("profitMargins"), scale=100, digits=2),
            "high_52w": num(info.get("fiftyTwoWeekHigh")),
            "low_52w": num(info.get("fiftyTwoWeekLow")),
            "quote_updated_at": now_iso(),
            "fundamentals_updated_at": now_iso(),
        })
        kept_syms.add(sym)
        if i % 25 == 0:
            log(f"  {i}/{len(symbols)}")
        if len(rows) >= 50:
            db.upsert(rows)
            saved += len(rows)
            rows = []
        time.sleep(0.6)

    if rows:
        db.upsert(rows)
    total = saved + len(rows)
    log(f"Updated {total} Swiss stocks; skipped {skipped} without data, "
        f"dropped {dropped_foreign} foreign, {dropped_wrongcur} non-CHF, {dropped_nocap} no-cap.")

    # Prune the Switzerland table. Two passes:
    #   1. Anything in the table that we didn't just store (stale / foreign).
    #   2. Legacy rows the OLD updater let in before this patch existed — no
    #      cap, or non-CHF currency, and not on the built-in list. The prune
    #      only ever ran on the first pass, so these had piled up.
    # Guarded by total >= 100 so a bad Yahoo run never empties the table.
    if not args.limit and total >= 100:
        existing_rows = db.select(
            "symbol,market_cap_usd_b,currency",
            {"region": "eq.Switzerland", "asset_class": "in.(stock,reit)"})
        gone = set()
        for r in existing_rows:
            sym = r["symbol"]
            if sym in kept_syms or sym in builtin_syms:
                continue
            # Pass 1: anything we didn't store this run and that isn't a known SIX name.
            gone.add(sym)
        # Pass 2 is already covered by pass 1 since those legacy rows aren't in kept_syms,
        # but we log them separately so you can see what the cleanup caught.
        legacy = {r["symbol"] for r in existing_rows
                  if r["symbol"] in gone
                  and (r.get("market_cap_usd_b") is None or r.get("currency") != "CHF")}
        if gone:
            db.delete_symbols(gone)
            log(f"Removed {len(gone)} Swiss rows ({len(legacy)} legacy no-cap / non-CHF).")


if __name__ == "__main__":
    main()
