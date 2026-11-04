"""Futures contracts via Yahoo Finance.

Every CME / COMEX / NYMEX / CBOT / ICE contract Yahoo carries, plus Yahoo's
cash crypto pairs. For Eurex (FDAX, FSMI, FESX, FGBL …), see update_eurex.py.

Yahoo returns continuous front-month quotes under symbols like ES=F, GC=F.
"""
import argparse
import time

import yfinance as yf

from common import Supabase, log, now_iso, num

# Each row: Yahoo symbol, long name, group, exchange, currency, region.
FUTURES = [
    # Equity index
    ("ES=F", "E-mini S&P 500", "Equity Index", "CME", "USD", "US"),
    ("NQ=F", "E-mini Nasdaq-100", "Equity Index", "CME", "USD", "US"),
    ("YM=F", "E-mini Dow", "Equity Index", "CBOT", "USD", "US"),
    ("RTY=F", "E-mini Russell 2000", "Equity Index", "CME", "USD", "US"),
    ("MES=F", "Micro E-mini S&P 500", "Equity Index", "CME", "USD", "US"),
    ("MNQ=F", "Micro E-mini Nasdaq-100", "Equity Index", "CME", "USD", "US"),
    ("MYM=F", "Micro E-mini Dow", "Equity Index", "CBOT", "USD", "US"),
    ("M2K=F", "Micro E-mini Russell 2000", "Equity Index", "CME", "USD", "US"),
    ("EMD=F", "E-mini S&P MidCap 400", "Equity Index", "CME", "USD", "US"),
    ("NKD=F", "Nikkei 225 Dollar", "Equity Index", "CME", "USD", "Asia"),

    # Energy
    ("CL=F", "WTI Crude Oil", "Energy", "NYMEX", "USD", "Global"),
    ("BZ=F", "Brent Crude Oil", "Energy", "ICE", "USD", "Global"),
    ("NG=F", "Natural Gas", "Energy", "NYMEX", "USD", "US"),
    ("HO=F", "Heating Oil", "Energy", "NYMEX", "USD", "US"),
    ("RB=F", "RBOB Gasoline", "Energy", "NYMEX", "USD", "US"),
    ("MCL=F", "Micro WTI Crude", "Energy", "NYMEX", "USD", "Global"),

    # Metals
    ("GC=F", "Gold", "Metals", "COMEX", "USD", "Global"),
    ("SI=F", "Silver", "Metals", "COMEX", "USD", "Global"),
    ("HG=F", "Copper", "Metals", "COMEX", "USD", "Global"),
    ("PL=F", "Platinum", "Metals", "NYMEX", "USD", "Global"),
    ("PA=F", "Palladium", "Metals", "NYMEX", "USD", "Global"),
    ("MGC=F", "Micro Gold", "Metals", "COMEX", "USD", "Global"),
    ("SIL=F", "Micro Silver", "Metals", "COMEX", "USD", "Global"),

    # Grains
    ("ZC=F", "Corn", "Grains", "CBOT", "USD", "US"),
    ("ZS=F", "Soybeans", "Grains", "CBOT", "USD", "US"),
    ("ZW=F", "Wheat", "Grains", "CBOT", "USD", "US"),
    ("KE=F", "KC HRW Wheat", "Grains", "CBOT", "USD", "US"),
    ("ZL=F", "Soybean Oil", "Grains", "CBOT", "USD", "US"),
    ("ZM=F", "Soybean Meal", "Grains", "CBOT", "USD", "US"),
    ("ZO=F", "Oats", "Grains", "CBOT", "USD", "US"),
    ("ZR=F", "Rough Rice", "Grains", "CBOT", "USD", "US"),

    # Softs
    ("SB=F", "Sugar #11", "Softs", "ICE", "USD", "Global"),
    ("CC=F", "Cocoa", "Softs", "ICE", "USD", "Global"),
    ("KC=F", "Coffee", "Softs", "ICE", "USD", "Global"),
    ("CT=F", "Cotton", "Softs", "ICE", "USD", "Global"),
    ("OJ=F", "Orange Juice", "Softs", "ICE", "USD", "US"),
    ("LBR=F", "Lumber", "Softs", "CME", "USD", "US"),

    # Livestock
    ("LE=F", "Live Cattle", "Livestock", "CME", "USD", "US"),
    ("HE=F", "Lean Hogs", "Livestock", "CME", "USD", "US"),
    ("GF=F", "Feeder Cattle", "Livestock", "CME", "USD", "US"),

    # Rates
    ("ZB=F", "30-Year T-Bond", "Rates", "CBOT", "USD", "US"),
    ("ZN=F", "10-Year T-Note", "Rates", "CBOT", "USD", "US"),
    ("ZF=F", "5-Year T-Note", "Rates", "CBOT", "USD", "US"),
    ("ZT=F", "2-Year T-Note", "Rates", "CBOT", "USD", "US"),
    ("TN=F", "Ultra 10-Year T-Note", "Rates", "CBOT", "USD", "US"),
    ("UB=F", "Ultra T-Bond", "Rates", "CBOT", "USD", "US"),
    ("SR3=F", "3-Month SOFR", "Rates", "CME", "USD", "US"),

    # FX
    ("6E=F", "Euro FX", "FX", "CME", "USD", "Global"),
    ("6J=F", "Japanese Yen", "FX", "CME", "USD", "Global"),
    ("6B=F", "British Pound", "FX", "CME", "USD", "Global"),
    ("6A=F", "Australian Dollar", "FX", "CME", "USD", "Global"),
    ("6C=F", "Canadian Dollar", "FX", "CME", "USD", "Global"),
    ("6S=F", "Swiss Franc", "FX", "CME", "USD", "Global"),
    ("6N=F", "New Zealand Dollar", "FX", "CME", "USD", "Global"),
    ("6M=F", "Mexican Peso", "FX", "CME", "USD", "Global"),
    ("DX=F", "US Dollar Index", "FX", "ICE", "USD", "Global"),

    # Crypto futures
    ("BTC=F", "Bitcoin", "Crypto", "CME", "USD", "Global"),
    ("ETH=F", "Ether", "Crypto", "CME", "USD", "Global"),
    ("MBT=F", "Micro Bitcoin", "Crypto", "CME", "USD", "Global"),
    ("MET=F", "Micro Ether", "Crypto", "CME", "USD", "Global"),

    # Volatility
    ("VX=F", "CBOE VIX", "Volatility", "CFE", "USD", "US"),
]


def fetch_info(sym):
    try:
        return yf.Ticker(sym).info or {}
    except Exception:
        return {}


def fast(sym):
    try:
        fi = yf.Ticker(sym).fast_info
        return float(fi["last_price"]), float(fi.get("previous_close") or 0) or None
    except Exception:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fast", action="store_true",
                    help="Prices only — skip per-symbol info and 1-year history. For the 5-minute workflow.")
    args = ap.parse_args()

    db = Supabase()
    rows_in = FUTURES[:args.limit] if args.limit else FUTURES
    rows, skipped = [], 0

    for i, (sym, name, group, exch, cur, region) in enumerate(rows_in, 1):
        price, prev = fast(sym)
        info = {} if args.fast else (fetch_info(sym) if price else {})
        price = price or num(info.get("regularMarketPrice"))
        prev = prev or num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
        if not price:
            skipped += 1
            continue
        row = {
            "symbol": sym,
            "name": name,
            "exchange": exch,
            "region": region,
            "asset_class": "future",
            "currency": cur,
            "sector": group,
            "industry": group,
            "price": price,
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "quote_updated_at": now_iso(),
        }
        if not args.fast:
            hi, lo = num(info.get("fiftyTwoWeekHigh")), num(info.get("fiftyTwoWeekLow"))
            y1 = None
            if hi and lo:
                try:
                    hist = yf.Ticker(sym).history(period="1y")["Close"].dropna()
                    if len(hist) > 20:
                        y1 = round((price / float(hist.iloc[0]) - 1) * 100, 2)
                except Exception:
                    pass
            row.update({"return_1y": y1, "high_52w": hi, "low_52w": lo,
                        "fundamentals_updated_at": now_iso()})
        rows.append(row)
        if i % 10 == 0:
            log(f"  futures {i}/{len(rows_in)}")
        if len(rows) >= 40:
            db.upsert(rows); rows = []
        time.sleep(0.1 if args.fast else 0.4)

    if rows:
        db.upsert(rows)
    log(f"Updated {len(rows_in) - skipped} futures, skipped {skipped}.")

    # Housekeeping only on the full nightly run — the fast path never prunes.
    if not args.limit and not args.fast:
        gone = db.delete_asset_class("future", {r[0] for r in FUTURES})
        if gone:
            log(f"Removed {gone} futures no longer on the list.")


if __name__ == "__main__":
    main()
