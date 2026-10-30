"""Nightly update for Swiss stocks on SIX, using Yahoo Finance through the yfinance library.

The list of SIX shares is put together from three sources, so one failing doesn't matter:
  1. Yahoo's own screener: every equity on SIX (exchange code EBS)
  2. Finnhub's symbol list for SIX (if the free plan allows it)
  3. A built-in list of about 230 SIX shares
Yahoo is unofficial: fine for a personal project, not for a paid public site.
"""
import argparse
import os
import time

import requests
import yfinance as yf

from common import YAHOO_SECTORS, Supabase, log, now_iso, num

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
    """Every equity Yahoo lists on SIX, using its screener (up to 250 per page)."""
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


FX = {"USD": 1.0}


def to_usd(currency):
    """Exchange rate from a currency to USD, from Yahoo (e.g. CHFUSD=X)."""
    if currency not in FX:
        try:
            hist = yf.Ticker(f"{currency}USD=X").history(period="5d")
            FX[currency] = float(hist["Close"].dropna().iloc[-1])
        except Exception:
            FX[currency] = None
    return FX[currency]


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
    """Yearly EPS growth in %, for PEG. Prefer the growth implied by Yahoo's PEG ratio
    (based on analysts' 5-year estimates); otherwise use the latest year-on-year earnings growth."""
    peg = num(info.get("trailingPegRatio") or info.get("pegRatio"))
    if pe and peg and peg > 0:
        return round(pe / peg, 2)
    return num(info.get("earningsGrowth"), scale=100, digits=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Only process this many stocks (for testing).")
    args = ap.parse_args()

    db = Supabase()
    symbols, complete_list = swiss_symbols()
    if args.limit:
        symbols = symbols[:args.limit]

    rows, skipped, saved = [], 0, 0
    for i, sym in enumerate(symbols, 1):
        info = fetch_info(sym)
        price = num(info.get("currentPrice") or info.get("regularMarketPrice"))
        if not price or info.get("quoteType") not in (None, "EQUITY"):
            skipped += 1
            continue
        cur = info.get("currency") or "CHF"
        prev = num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
        fx = to_usd(cur)
        cap = num(info.get("marketCap"))
        div = num(info.get("dividendRate") or info.get("trailingAnnualDividendRate"))
        pe = num(info.get("trailingPE"), digits=2)
        pe = pe if pe and pe > 0 else None
        rows.append({
            "symbol": sym,
            "name": info.get("longName") or info.get("shortName") or sym,
            "exchange": "SIX",
            "region": "Switzerland",
            "currency": cur,
            "sector": YAHOO_SECTORS.get(info.get("sector"), "Other" if info.get("sector") else None),
            "industry": info.get("industry"),
            "price": price,
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "market_cap_usd_b": round(cap * fx / 1e9, 3) if cap and fx else None,
            "pe": pe,
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
        if i % 25 == 0:
            log(f"  {i}/{len(symbols)}")
        if len(rows) >= 50:  # save as we go, so a crash late in the run keeps earlier work
            db.upsert(rows)
            saved += len(rows)
            rows = []
        time.sleep(0.6)

    if rows:
        db.upsert(rows)
    total = saved + len(rows)
    log(f"Updated {total} Swiss stocks, skipped {skipped} without data.")

    if complete_list and not args.limit and total >= 100:
        existing = {r["symbol"] for r in db.select("symbol", {"region": "eq.Switzerland"})}
        gone = existing - set(symbols)
        if gone:
            db.delete_symbols(gone)
            log(f"Removed {len(gone)} Swiss stocks that are no longer listed.")


if __name__ == "__main__":
    main()
