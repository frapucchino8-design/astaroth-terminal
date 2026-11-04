"""World equity indices via Yahoo Finance. These are index SPOT values, not futures.

For Switzerland and the Eurozone this gives you SMI, DAX and Euro Stoxx 50 for free
in near real time, which is the closest reliable thing to Eurex futures without a
paid feed. See update_eurex.py for an attempt at the actual Eurex contracts.
"""
import argparse
import time

import yfinance as yf

from common import Supabase, log, now_iso, num

# Yahoo index symbol, long name, region, currency
INDICES = [
    # US
    ("^GSPC", "S&P 500", "US", "USD"),
    ("^DJI", "Dow Jones Industrial Average", "US", "USD"),
    ("^IXIC", "NASDAQ Composite", "US", "USD"),
    ("^NDX", "NASDAQ 100", "US", "USD"),
    ("^RUT", "Russell 2000", "US", "USD"),
    ("^VIX", "CBOE Volatility Index", "US", "USD"),
    ("^MID", "S&P MidCap 400", "US", "USD"),
    ("^SP600", "S&P SmallCap 600", "US", "USD"),

    # Switzerland
    ("^SSMI", "SMI", "Switzerland", "CHF"),
    ("^SMIM", "SMI MID", "Switzerland", "CHF"),
    ("^SLI", "SLI Swiss Leader Index", "Switzerland", "CHF"),
    ("^SPI", "Swiss Performance Index", "Switzerland", "CHF"),

    # Europe
    ("^STOXX50E", "Euro Stoxx 50", "Europe", "EUR"),
    ("^STOXX", "Stoxx Europe 600", "Europe", "EUR"),
    ("^GDAXI", "DAX", "Europe", "EUR"),
    ("^MDAXI", "MDAX", "Europe", "EUR"),
    ("^SDAXI", "SDAX", "Europe", "EUR"),
    ("^TECDAX", "TecDAX", "Europe", "EUR"),
    ("^FTSE", "FTSE 100", "Europe", "GBP"),
    ("^FTMC", "FTSE 250", "Europe", "GBP"),
    ("^FCHI", "CAC 40", "Europe", "EUR"),
    ("^AEX", "AEX", "Europe", "EUR"),
    ("^IBEX", "IBEX 35", "Europe", "EUR"),
    ("FTSEMIB.MI", "FTSE MIB", "Europe", "EUR"),
    ("^OMX", "OMX Stockholm 30", "Europe", "SEK"),
    ("^OSEAX", "Oslo All-Share", "Europe", "NOK"),
    ("^BFX", "BEL 20", "Europe", "EUR"),
    ("^ATX", "ATX Austria", "Europe", "EUR"),

    # Asia / Pacific
    ("^N225", "Nikkei 225", "Asia", "JPY"),
    ("^TOPX", "TOPIX", "Asia", "JPY"),
    ("^HSI", "Hang Seng", "Asia", "HKD"),
    ("000001.SS", "Shanghai Composite", "Asia", "CNY"),
    ("399001.SZ", "Shenzhen Component", "Asia", "CNY"),
    ("^STI", "Straits Times", "Asia", "SGD"),
    ("^KS11", "KOSPI", "Asia", "KRW"),
    ("^TWII", "Taiwan Weighted", "Asia", "TWD"),
    ("^BSESN", "BSE SENSEX", "Asia", "INR"),
    ("^NSEI", "Nifty 50", "Asia", "INR"),
    ("^AXJO", "ASX 200", "Asia", "AUD"),
    ("^AORD", "ASX All Ordinaries", "Asia", "AUD"),
    ("^NZ50", "NZX 50", "Asia", "NZD"),

    # Americas (ex-US)
    ("^GSPTSE", "S&P/TSX Composite", "Americas", "CAD"),
    ("^BVSP", "Bovespa", "Americas", "BRL"),
    ("^MXX", "IPC Mexico", "Americas", "MXN"),
    ("^MERV", "Merval", "Americas", "ARS"),

    # Africa / Middle East
    ("^TA125.TA", "Tel Aviv 125", "MEA", "ILS"),

    # Rates (10y yields)
    ("^TNX", "US 10-Year Yield", "US", "USD"),
    ("^TYX", "US 30-Year Yield", "US", "USD"),
    ("^FVX", "US 5-Year Yield", "US", "USD"),
    ("^IRX", "US 13-Week Yield", "US", "USD"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fast", action="store_true",
                    help="Prices only — skip 1-year history. For the 5-minute workflow.")
    args = ap.parse_args()

    db = Supabase()
    rows_in = INDICES[:args.limit] if args.limit else INDICES
    rows, skipped = [], 0
    for i, (sym, name, region, cur) in enumerate(rows_in, 1):
        try:
            fi = yf.Ticker(sym).fast_info
            price = float(fi["last_price"])
            prev = float(fi.get("previous_close") or 0) or None
            hi = num(fi.get("year_high")); lo = num(fi.get("year_low"))
        except Exception:
            skipped += 1
            continue
        row = {
            "symbol": sym,
            "name": name,
            "exchange": "Index",
            "region": region,
            "asset_class": "index",
            "currency": cur,
            "sector": "Index",
            "industry": "Index",
            "price": round(price, 4),
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "quote_updated_at": now_iso(),
        }
        if not args.fast:
            y1 = None
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
            log(f"  indices {i}/{len(rows_in)}")
        if len(rows) >= 40:
            db.upsert(rows); rows = []
        time.sleep(0.1 if args.fast else 0.3)

    if rows:
        db.upsert(rows)
    log(f"Updated {len(rows_in) - skipped} indices, skipped {skipped}.")

    if not args.limit and not args.fast:
        gone = db.delete_asset_class("index", {r[0] for r in INDICES})
        if gone:
            log(f"Removed {gone} indices no longer on the list.")


if __name__ == "__main__":
    main()
