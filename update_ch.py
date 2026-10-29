"""Nightly update for Swiss stocks on SIX, using Yahoo Finance through the yfinance library.

The list of SIX shares comes from Finnhub's free symbol list (one call). If that fails,
a built-in list of the larger Swiss companies is used instead.
Yahoo is unofficial: fine for a personal project, not for a paid public site.
"""
import argparse
import os
import time

import requests
import yfinance as yf

from common import YAHOO_SECTORS, Supabase, log, now_iso, num

# Used only if Finnhub's SIX list can't be loaded. Larger SIX companies, Yahoo symbols.
FALLBACK = """
NESN NOVN ROG RO UBSG ABBN ZURN CFR LONN SIKA GIVN ALC HOLN SREN PGHN SCMN SLHN GEBN KNIN
LOGN SOON STMN BAER BALN HELN SGSN TEMN SCHP SCHN LISN LISP EMSN BARN SPSN PSPN DKSH BCVN
VATN BUCN GALE SUN CLN AMS BANB ADEN BEAN COTN FHZN HUBN IFCN LAND MOBN OERL SRAIL SIGN
SQN TECN UHR UHRN VACN ZEHN YPSN SFSN GF ARYN AUTN BKW BSLN DOKA EFGN FORN KARN KOMN LEHN
LEON MBTN VZN
""".split()


def swiss_symbols():
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if key:
        try:
            r = requests.get("https://finnhub.io/api/v1/stock/symbol",
                             params={"exchange": "SW"}, headers={"X-Finnhub-Token": key}, timeout=60)
            if r.ok:
                rows = [x for x in r.json()
                        if x.get("type") in ("Common Stock", "REIT", "Equity") and x.get("symbol", "").endswith(".SW")]
                if len(rows) >= 100:
                    log(f"Finnhub lists {len(rows)} Swiss shares.")
                    return sorted({x["symbol"] for x in rows}), True
                log(f"Finnhub returned only {len(rows)} Swiss shares; using the built-in list.")
            else:
                log(f"Finnhub symbol list failed ({r.status_code}); using the built-in list.")
        except requests.RequestException as e:
            log(f"Finnhub symbol list failed ({e}); using the built-in list.")
    return [s + ".SW" for s in FALLBACK], False


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="Only process this many stocks (for testing).")
    args = ap.parse_args()

    db = Supabase()
    symbols, complete_list = swiss_symbols()
    if args.limit:
        symbols = symbols[:args.limit]

    rows, skipped = [], 0
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
            "pe": pe if pe and pe > 0 else None,
            "dividend_yield": round(div / price * 100, 3) if div else None,
            "return_1y": num(info.get("52WeekChange"), scale=100, digits=2),
            "beta": num(info.get("beta"), digits=3),
            "revenue_growth": num(info.get("revenueGrowth"), scale=100, digits=2),
            "net_margin": num(info.get("profitMargins"), scale=100, digits=2),
            "high_52w": num(info.get("fiftyTwoWeekHigh")),
            "low_52w": num(info.get("fiftyTwoWeekLow")),
            "quote_updated_at": now_iso(),
            "fundamentals_updated_at": now_iso(),
        })
        if i % 25 == 0:
            log(f"  {i}/{len(symbols)}")
        time.sleep(0.6)

    if rows:
        db.upsert(rows)
    log(f"Updated {len(rows)} Swiss stocks, skipped {skipped} without data.")

    if complete_list and not args.limit and len(rows) >= 100:
        existing = {r["symbol"] for r in db.select("symbol", {"region": "eq.Switzerland"})}
        gone = existing - {r["symbol"] for r in rows}
        if gone:
            db.delete_symbols(gone)
            log(f"Removed {len(gone)} Swiss stocks that are no longer listed.")


if __name__ == "__main__":
    main()
