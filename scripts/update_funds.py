"""Nightly update for ETFs and futures.

ETFs:    the universe comes from Finnhub's US symbol list (type "ETP"), plus the Swiss
         ETFs Yahoo lists on SIX, plus a built-in core list. Details (expense ratio,
         distribution yield, 1-year return, beta, AUM, category) come from Yahoo.
Futures: a fixed list of the main CME/CBOT/NYMEX/COMEX/ICE contracts.

Both are written to the same `stocks` table with asset_class = 'etf' / 'future'.
Details are refreshed oldest-first inside a time budget, so every fund gets
refreshed every night or two without blowing past Yahoo's tolerance.

Run supabase/setup-update.sql once before the first run (it adds asset_class
and expense_ratio).
"""
import argparse
import os
import time

import requests
import yfinance as yf

from common import YAHOO_SECTORS, Supabase, log, now_iso, num

# ---------------------------------------------------------------------------
# Futures: symbol -> (name, group, exchange)
# ---------------------------------------------------------------------------
FUTURES = {
    "ES=F": ("E-mini S&P 500", "Index", "CME"),
    "NQ=F": ("E-mini Nasdaq 100", "Index", "CME"),
    "YM=F": ("E-mini Dow", "Index", "CBOT"),
    "RTY=F": ("E-mini Russell 2000", "Index", "CME"),
    "VX=F": ("VIX Futures", "Index", "CFE"),
    "CL=F": ("Crude Oil WTI", "Energy", "NYMEX"),
    "BZ=F": ("Brent Crude", "Energy", "ICE"),
    "NG=F": ("Natural Gas", "Energy", "NYMEX"),
    "RB=F": ("RBOB Gasoline", "Energy", "NYMEX"),
    "HO=F": ("Heating Oil", "Energy", "NYMEX"),
    "GC=F": ("Gold", "Metals", "COMEX"),
    "SI=F": ("Silver", "Metals", "COMEX"),
    "HG=F": ("Copper", "Metals", "COMEX"),
    "PL=F": ("Platinum", "Metals", "NYMEX"),
    "PA=F": ("Palladium", "Metals", "NYMEX"),
    "ZC=F": ("Corn", "Agriculture", "CBOT"),
    "ZW=F": ("Wheat", "Agriculture", "CBOT"),
    "ZS=F": ("Soybeans", "Agriculture", "CBOT"),
    "ZM=F": ("Soybean Meal", "Agriculture", "CBOT"),
    "ZL=F": ("Soybean Oil", "Agriculture", "CBOT"),
    "KC=F": ("Coffee", "Agriculture", "ICE"),
    "SB=F": ("Sugar", "Agriculture", "ICE"),
    "CC=F": ("Cocoa", "Agriculture", "ICE"),
    "CT=F": ("Cotton", "Agriculture", "ICE"),
    "LE=F": ("Live Cattle", "Agriculture", "CME"),
    "HE=F": ("Lean Hogs", "Agriculture", "CME"),
    "ZB=F": ("30-Year T-Bond", "Rates", "CBOT"),
    "ZN=F": ("10-Year T-Note", "Rates", "CBOT"),
    "ZF=F": ("5-Year T-Note", "Rates", "CBOT"),
    "ZT=F": ("2-Year T-Note", "Rates", "CBOT"),
    "6E=F": ("Euro FX", "Currencies", "CME"),
    "6J=F": ("Japanese Yen", "Currencies", "CME"),
    "6B=F": ("British Pound", "Currencies", "CME"),
    "6S=F": ("Swiss Franc", "Currencies", "CME"),
    "6C=F": ("Canadian Dollar", "Currencies", "CME"),
    "6A=F": ("Australian Dollar", "Currencies", "CME"),
    "DX=F": ("US Dollar Index", "Currencies", "ICE"),
    "BTC=F": ("Bitcoin", "Crypto", "CME"),
    "ETH=F": ("Ether", "Crypto", "CME"),
}

# Core ETFs that must always be present, even if a list source fails.
CORE_ETFS = """
SPY VOO IVV VTI QQQ QQQM DIA IWM IWB IWD IWF VTV VUG VB VO MDY RSP SCHX SCHB ITOT
XLK XLF XLE XLV XLY XLP XLI XLU XLB XLRE XLC SMH SOXX IGV IBB XBI ITA XOP XME KRE
VEA VWO IEFA IEMG EFA EEM VXUS ACWI IEUR EWJ EWZ EWG EWU EWL INDA MCHI FXI
AGG BND TLT IEF SHY LQD HYG JNK TIP BNDX EMB MUB VCIT VCSH SGOV BIL
GLD IAU SLV PDBC DBC USO UNG GLDM
SCHD VYM DVY SDY NOBL HDV SPHD VIG DGRO JEPI JEPQ QYLD
ARKK ARKG ARKW ICLN TAN LIT BOTZ ROBO HACK CIBR SKYY FINX
VNQ SCHH IYR REET
USMV SPLV QUAL MTUM VLUE SIZE EFAV ACWV
BIL SHV GOVT VGIT VGLT FLOT
""".split()

SWISS_ETFS = ["CHSPI.SW", "CSSMI.SW", "SMICHA.SW", "CHDVD.SW", "SPICHA.SW", "WRDUSA.SW", "CSBGC0.SW"]

ETF_CATEGORY_WORDS = [
    ("Bonds", ["bond", "treasury", "credit", "aggregate", "municipal", "fixed income", "government"]),
    ("Commodity", ["gold", "silver", "commodit", "oil", "gas", "metal", "crude"]),
    ("Dividend", ["dividend", "income", "yield"]),
    ("International", ["international", "emerging", "developed", "world", "europe", "japan", "china", "global", "ex-us"]),
    ("Sector", ["sector", "energy", "financial", "health", "technolog", "utilit", "industrial", "staples", "discretionary", "semiconductor", "real estate"]),
    ("Thematic", ["innovation", "clean", "robot", "cyber", "cloud", "ai ", "blockchain", "thematic"]),
    ("Broad market", ["s&p", "total", "russell", "nasdaq", "dow", "msci", "core", "market"]),
]


def etf_category(name, cat):
    text = ((cat or "") + " " + (name or "")).lower()
    for label, words in ETF_CATEGORY_WORDS:
        if any(w in text for w in words):
            return label
    return "Other"


def finnhub_etfs():
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        return set()
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/symbol", params={"exchange": "US"},
                         headers={"X-Finnhub-Token": key}, timeout=90)
        if not r.ok:
            log(f"Finnhub US list not available ({r.status_code}).")
            return set()
        out = {x["symbol"] for x in r.json()
               if x.get("type") == "ETP" and x.get("symbol") and "." not in x["symbol"]}
        log(f"Finnhub lists {len(out)} US ETFs.")
        return out
    except (requests.RequestException, ValueError) as e:
        log(f"Finnhub ETF list failed ({e}).")
        return set()


def fetch_info(sym, tries=3):
    for attempt in range(tries):
        try:
            return yf.Ticker(sym).info or {}
        except Exception as e:
            if "Too Many Requests" in str(e) or "Rate" in str(e):
                log(f"  Yahoo rate limit, waiting {30 * (attempt + 1)}s")
                time.sleep(30 * (attempt + 1))
                continue
            return {}
    return {}


def etf_row(sym, info):
    price = num(info.get("navPrice") or info.get("regularMarketPrice") or info.get("previousClose"))
    if not price:
        return None
    prev = num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
    aum = num(info.get("totalAssets"))
    exp = info.get("netExpenseRatio")
    exp = num(exp) if exp is not None else num(info.get("annualReportExpenseRatio"), scale=100, digits=3)
    name = info.get("longName") or info.get("shortName") or sym
    return {
        "symbol": sym,
        "name": name,
        "asset_class": "etf",
        "exchange": "SIX" if sym.endswith(".SW") else (info.get("exchange") or "US"),
        "region": "Switzerland" if sym.endswith(".SW") else "US",
        "currency": info.get("currency") or ("CHF" if sym.endswith(".SW") else "USD"),
        "sector": etf_category(name, info.get("category")),
        "industry": info.get("category"),
        "price": price,
        "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
        "market_cap_usd_b": round(aum / 1e9, 3) if aum else None,
        "pe": num(info.get("trailingPE"), digits=2),
        "dividend_yield": num(info.get("yield"), scale=100, digits=3) or num(info.get("dividendYield"), digits=3),
        "return_1y": num(info.get("52WeekChange"), scale=100, digits=2),
        "beta": num(info.get("beta3Year") or info.get("beta"), digits=3),
        "expense_ratio": exp,
        "high_52w": num(info.get("fiftyTwoWeekHigh")),
        "low_52w": num(info.get("fiftyTwoWeekLow")),
        "quote_updated_at": now_iso(),
        "fundamentals_updated_at": now_iso(),
    }


def future_row(sym, info):
    name, group, exchange = FUTURES[sym]
    price = num(info.get("regularMarketPrice") or info.get("previousClose"))
    if not price:
        return None
    prev = num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
    return {
        "symbol": sym,
        "name": name,
        "asset_class": "future",
        "exchange": exchange,
        "region": "US",
        "currency": info.get("currency") or "USD",
        "sector": group,
        "price": price,
        "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
        "high_52w": num(info.get("fiftyTwoWeekHigh")),
        "low_52w": num(info.get("fiftyTwoWeekLow")),
        "quote_updated_at": now_iso(),
        "fundamentals_updated_at": now_iso(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-minutes", type=float, default=90, help="Stop starting new work after this many minutes.")
    ap.add_argument("--min-aum-m", type=float, default=50, help="Drop ETFs smaller than this (millions of USD).")
    ap.add_argument("--limit", type=int, default=0, help="Only process this many ETFs (for testing).")
    args = ap.parse_args()
    deadline = time.monotonic() + args.budget_minutes * 60

    db = Supabase()

    # 1. Futures ------------------------------------------------------------------
    rows = []
    for sym in FUTURES:
        row = future_row(sym, fetch_info(sym))
        if row:
            rows.append(row)
        time.sleep(0.4)
    if rows:
        db.upsert(rows)
    log(f"Updated {len(rows)} futures contracts.")

    # 2. The ETF universe ---------------------------------------------------------
    universe = set(CORE_ETFS) | set(SWISS_ETFS) | finnhub_etfs()
    log(f"{len(universe)} ETF symbols to consider.")

    # Refresh oldest-first, so a nightly budget still covers everything over time.
    known = {r["symbol"]: r.get("fundamentals_updated_at") or ""
             for r in db.select("symbol,fundamentals_updated_at", {"asset_class": "eq.etf"})}
    order = sorted(universe, key=lambda s: (known.get(s, ""), s))
    if args.limit:
        order = order[:args.limit]

    batch, saved, dropped = [], 0, []
    for i, sym in enumerate(order, 1):
        if time.monotonic() > deadline:
            log("Time budget used up; the rest continue next run.")
            break
        row = etf_row(sym, fetch_info(sym))
        if row is None:
            if sym in known:
                dropped.append(sym)
            continue
        aum_b = row.get("market_cap_usd_b")
        if aum_b is not None and aum_b * 1000 < args.min_aum_m:
            if sym in known:
                dropped.append(sym)      # shrank below the floor: stop carrying it
            continue
        batch.append(row)
        if len(batch) >= 50:
            db.upsert(batch)
            saved += len(batch)
            batch = []
            log(f"  ETFs: {saved} saved ({i}/{len(order)} checked)")
        time.sleep(0.5)

    if batch:
        db.upsert(batch)
        saved += len(batch)
    log(f"Updated {saved} ETFs.")

    if dropped and not args.limit:
        db.delete_symbols(dropped)
        log(f"Removed {len(dropped)} ETFs that are gone or below the size floor.")


if __name__ == "__main__":
    main()
