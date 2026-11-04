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


def batch_download(symbols, chunk=50, out_of_time=None):
    """Fetch latest close + previous close for many symbols in one call using
    yf.download. Much faster than fast_info per symbol: ~1 HTTP round-trip per 50
    symbols instead of per symbol. Returns {symbol: (price, prev)}.

    out_of_time: optional callable; when it returns True we stop early and hand
    back whatever we already have, so the caller still saves a partial refresh."""
    out = {}
    for i in range(0, len(symbols), chunk):
        if out_of_time and out_of_time():
            log(f"  deadline reached after {i}/{len(symbols)} symbols — "
                f"returning {len(out)} quotes; next tick continues.")
            break
        part = symbols[i:i + chunk]
        try:
            df = yf.download(" ".join(part), period="5d", interval="1d",
                             progress=False, group_by="ticker", threads=True, auto_adjust=False)
        except Exception as e:
            log(f"  batch {i // chunk} failed, falling back per symbol: {e}")
            for s in part:
                if out_of_time and out_of_time():
                    break
                p, pv = fast(s)
                if p:
                    out[s] = (p, pv)
            continue
        for s in part:
            try:
                col = df[s] if (len(part) > 1) else df
                closes = col["Close"].dropna()
                if len(closes) == 0:
                    continue
                price = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else None
                out[s] = (price, prev)
            except (KeyError, IndexError, ValueError):
                continue
        time.sleep(1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--deadline", type=float, default=0,
                    help="Seconds. Stop fetching after this long and save what we have, "
                         "so the job finishes inside its runner timeout. 0 = no limit.")
    args = ap.parse_args()
    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    db = Supabase()
    symbols = sorted({r["symbol"] for r in db.select(
        "symbol", {"region": "eq.Switzerland", "asset_class": "in.(stock,reit)"})})
    if args.limit:
        symbols = symbols[:args.limit]
    if not symbols:
        log("No Swiss stocks in the database yet — run update_ch.py first.")
        return

    log(f"Refreshing prices for {len(symbols)} Swiss stocks (batched) …")
    quotes = batch_download(symbols, out_of_time=out_of_time)
    log(f"  got {len(quotes)} / {len(symbols)} quotes")

    rows, saved = [], 0
    for sym, (price, prev) in quotes.items():
        rows.append({
            "symbol": sym,
            "region": "Switzerland",
            "price": num(price),
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "quote_updated_at": now_iso(),
        })
        if len(rows) >= 100:
            try:
                db.upsert(rows); saved += len(rows)
            except Exception as e:
                log(f"  batch upsert failed: {e}")
            rows = []
    if rows:
        try:
            db.upsert(rows); saved += len(rows)
        except Exception as e:
            log(f"  final upsert failed: {e}")
    log(f"Refreshed {saved} Swiss prices, {len(symbols) - len(quotes)} without data, "
        f"in {time.monotonic() - started:.0f}s.")


if __name__ == "__main__":
    main()
