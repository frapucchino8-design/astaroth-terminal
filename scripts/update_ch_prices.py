"""Prices-only refresh for Swiss stocks. Fast path for the 5-minute workflow.

Reads the Swiss symbols already in Supabase (so new listings land here only after
the nightly update_ch.py adds them) and uses Yahoo's fast_info endpoint — one quick
HTTP round-trip per symbol, no fundamentals.
"""
import argparse
import time

import yfinance as yf

from common import Supabase, log, now_iso, num


def fast(sym):
    """Return (price, previous_close) using Yahoo's fast_info."""
    for attempt in range(3):
        try:
            fi = yf.Ticker(sym).fast_info
            price = float(fi["last_price"])
            prev = float(fi.get("previous_close") or 0) or None
            return price, prev
        except Exception as e:
            msg = str(e)
            if "Too Many Requests" in msg or "Rate" in msg:
                time.sleep(20 * (attempt + 1))
                continue
            return None, None
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    db = Supabase()
    symbols = sorted({r["symbol"] for r in db.select(
        "symbol", {"region": "eq.Switzerland", "asset_class": "in.(stock,reit)"})})
    if args.limit:
        symbols = symbols[:args.limit]
    if not symbols:
        log("No Swiss stocks in the database yet — run update_ch.py first.")
        return

    log(f"Refreshing prices for {len(symbols)} Swiss stocks …")
    rows, done, missed = [], 0, 0
    for i, sym in enumerate(symbols, 1):
        price, prev = fast(sym)
        if not price:
            missed += 1
            continue
        rows.append({
            "symbol": sym,
            "region": "Switzerland",
            "price": num(price),
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "quote_updated_at": now_iso(),
        })
        if len(rows) >= 100:
            db.upsert(rows); done += len(rows); rows = []
            log(f"  {i}/{len(symbols)} saved")
        time.sleep(0.25)

    if rows:
        db.upsert(rows); done += len(rows)
    log(f"Refreshed {done} Swiss prices, {missed} without data.")


if __name__ == "__main__":
    main()
