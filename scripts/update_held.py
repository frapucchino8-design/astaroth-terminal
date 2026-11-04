"""Refresh only the instruments somebody actually holds.

Reads the `held_symbols` view (every symbol in any vault or portfolio) and
refreshes just those prices. On a database with 11,000 US stocks but 200 symbols
people care about, this turns a 50-minute sweep into about 20 seconds — so it can
run on the 5-minute workflow while the full nightly job keeps everything else
current.

Needs supabase/social.sql to have been run (it creates the view).
"""
import argparse
import time

import requests
import yfinance as yf

from common import Supabase, env, log, now_iso, num

FINNHUB = "https://finnhub.io/api/v1"


def held_symbols(db):
    """Every symbol in someone's vault or portfolio."""
    r = db.s.get(f"{db.url}/held_symbols", params={"select": "symbol"}, timeout=60)
    if r.status_code == 404 or (r.status_code == 400 and "held_symbols" in r.text):
        raise SystemExit("The held_symbols view is missing — run supabase/social.sql first.")
    db._check(r, "held_symbols")
    return sorted({row["symbol"] for row in r.json() if row.get("symbol")})


def region_of(db, symbols):
    """Map symbol -> region, so each upsert row can carry the NOT NULL column."""
    out = {}
    for i in range(0, len(symbols), 150):
        part = symbols[i:i + 150]
        quoted = ",".join('"' + s.replace('"', '') + '"' for s in part)
        r = db.s.get(f"{db.url}/stocks", params={"select": "symbol,region", "symbol": f"in.({quoted})"}, timeout=60)
        db._check(r, "regions")
        for row in r.json():
            out[row["symbol"]] = row.get("region") or "US"
    return out


def yahoo_batch(symbols, chunk=50, out_of_time=None):
    """Latest close + previous close for many symbols at once."""
    out = {}
    for i in range(0, len(symbols), chunk):
        if out_of_time and out_of_time():
            log(f"  deadline reached after {i}/{len(symbols)}")
            break
        part = symbols[i:i + chunk]
        try:
            df = yf.download(" ".join(part), period="5d", interval="1d",
                             progress=False, group_by="ticker", threads=True, auto_adjust=False)
        except Exception as e:
            log(f"  batch {i // chunk} failed: {e}")
            continue
        for s in part:
            try:
                col = df[s] if len(part) > 1 else df
                closes = col["Close"].dropna()
                if not len(closes):
                    continue
                out[s] = (float(closes.iloc[-1]), float(closes.iloc[-2]) if len(closes) >= 2 else None)
            except (KeyError, IndexError, ValueError):
                continue
        time.sleep(1)
    return out


def finnhub_quotes(symbols, key, out_of_time=None, per_minute=54):
    """Fall back to Finnhub for US symbols Yahoo didn't return."""
    s = requests.Session()
    s.headers["X-Finnhub-Token"] = key
    gap, last, out = 60.0 / per_minute, 0.0, {}
    for sym in symbols:
        if out_of_time and out_of_time():
            break
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        try:
            r = s.get(f"{FINNHUB}/quote", params={"symbol": sym}, timeout=20)
            if not r.ok:
                continue
            q = r.json()
            if num(q.get("c")):
                out[sym] = (float(q["c"]), float(q.get("pc") or 0) or None)
        except (requests.RequestException, ValueError):
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deadline", type=float, default=0, help="Seconds before saving what we have and stopping.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-finnhub", action="store_true", help="Yahoo only.")
    args = ap.parse_args()
    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    db = Supabase()
    symbols = held_symbols(db)
    if args.limit:
        symbols = symbols[:args.limit]
    if not symbols:
        log("Nobody holds anything yet — nothing to refresh.")
        return
    log(f"{len(symbols)} symbols are in someone's vault or portfolio.")

    regions = region_of(db, symbols)
    quotes = yahoo_batch(symbols, out_of_time=out_of_time)
    log(f"Yahoo returned {len(quotes)}/{len(symbols)}.")

    missing = [s for s in symbols if s not in quotes and "." not in s and "=" not in s and "-" not in s]
    key = env("FINNHUB_API_KEY", "")
    if missing and key and not args.no_finnhub and not out_of_time():
        log(f"Trying Finnhub for {len(missing)} Yahoo didn't have …")
        quotes.update(finnhub_quotes(missing, key, out_of_time))

    rows = []
    for sym, (price, prev) in quotes.items():
        if not price:
            continue
        rows.append({
            "symbol": sym,
            "region": regions.get(sym, "US"),
            "price": num(price),
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "quote_updated_at": now_iso(),
        })
    if rows:
        db.upsert(rows)
    log(f"Refreshed {len(rows)} held instruments in {time.monotonic() - started:.0f}s.")


if __name__ == "__main__":
    main()
