"""Twenty years of weekly closes for the major indices and the instruments people use.

Fills the price_history table (run supabase/migration_history.sql once first).
The website reads it for the Compare block, the backtest lab, strategies and
the returns spreadsheet on each stock page. Anything not in the table keeps an
illustrative history on the site, so this can grow over time.

What it covers, in this order:
  1. the major indices (S&P 500, Nasdaq 100, Dow, Russell 2000, SMI, Euro Stoxx 50,
     DAX, FTSE 100, Nikkei 225);
  2. every symbol in someone's vault or portfolio (the held_symbols view);
  3. the largest instruments by market cap (--top, default 200).

The first run fetches the full twenty years. After that it only asks Yahoo for
the weeks since the last stored date (minus a two-week overlap, so a late
correction gets picked up). Weekly bars keep the table small: about 1,040 rows
per symbol for twenty years.

Any failure prints a single-line reason prefixed with "FATAL:" before exiting.
"""
import argparse
import re
import sys
import time
import traceback
from datetime import date, timedelta

import requests
import yfinance as yf

from common import Supabase, log

BENCHMARKS = ["^GSPC", "^NDX", "^DJI", "^RUT", "^SSMI", "^STOXX50E", "^GDAXI", "^FTSE", "^N225"]
YEARS = 20
OVERLAP_DAYS = 14
CHUNK = 40          # tickers per yfinance call
UPSERT = 1000       # rows per POST


def die(reason):
    log("")
    log("=" * 68)
    log(f"FATAL: {reason}")
    log("=" * 68)
    sys.exit(1)


def missing_table(text):
    t = (text or "").lower()
    return "price_history" in t and ("does not exist" in t or "schema cache" in t or "pgrst205" in t or "42p01" in t)


def get_all(db, path, params):
    """Read every row of a table or view, 1000 at a time."""
    out, start = [], 0
    while True:
        r = db.s.get(f"{db.url}/{path}", params=params, timeout=60,
                     headers={"Range-Unit": "items", "Range": f"{start}-{start + 999}"})
        if r.status_code >= 300:
            raise RuntimeError(f"{path} ({r.status_code}): {r.text[:300]}")
        page = r.json()
        out.extend(page)
        if len(page) < 1000:
            return out
        start += 1000


def held_symbols(db):
    try:
        return {r["symbol"] for r in get_all(db, "held_symbols", {"select": "symbol"}) if r.get("symbol")}
    except RuntimeError as e:
        log(f"  held_symbols not readable ({str(e)[:120]}) — carrying on without it")
        return set()


def top_symbols(db, n):
    if n <= 0:
        return []
    r = db.s.get(f"{db.url}/stocks", timeout=60, params={
        "select": "symbol", "market_cap_usd_b": "not.is.null",
        "asset_class": "not.in.(preferred,future,fx)",
        "order": "market_cap_usd_b.desc", "limit": str(n)})
    if r.status_code >= 300:
        log(f"  couldn't read the largest instruments ({r.status_code}) — skipping that part")
        return []
    return [row["symbol"] for row in r.json() if row.get("symbol")]


def last_dates(db):
    """symbol -> last stored date."""
    try:
        rows = get_all(db, "price_history_last", {"select": "symbol,last_d"})
    except RuntimeError as e:
        if missing_table(str(e)) or "price_history_last" in str(e):
            die("The price_history table isn't in the database yet. Run "
                "supabase/migration_history.sql once in the Supabase SQL editor, "
                f"then run this workflow again.\nOriginal error: {str(e)[:300]}")
        raise
    return {r["symbol"]: date.fromisoformat(r["last_d"]) for r in rows if r.get("last_d")}


def yahoo_symbol(s):
    """Class shares are BRK.B in the database but BRK-B on Yahoo."""
    return re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1-\2", s)


def fetch_weekly(symbols, start):
    """{symbol: [(date, close), ...]} of weekly closes from `start`."""
    out = {}
    ysyms = {s: yahoo_symbol(s) for s in symbols}
    try:
        df = yf.download(" ".join(ysyms.values()), start=start.isoformat(), interval="1wk",
                         progress=False, group_by="ticker", threads=True, auto_adjust=True)
    except Exception as e:
        log(f"  download failed for {len(symbols)} tickers: {e}")
        return out
    if df is None or df.empty:
        return out
    for s in symbols:
        try:
            try:
                col = df[ysyms[s]]
            except KeyError:
                if len(symbols) > 1:
                    continue
                col = df
            closes = col["Close"]
            if hasattr(closes, "columns"):        # single ticker, still a two-level frame
                closes = closes.iloc[:, 0]
            closes = closes.dropna()
        except (KeyError, ValueError):
            continue
        pts = []
        for ts, v in closes.items():
            v = float(v)
            if v > 0 and v == v:
                pts.append((ts.date(), round(v, 6)))
        if pts:
            out[s] = pts
    return out


def upsert_history(db, rows):
    for i in range(0, len(rows), UPSERT):
        batch = rows[i:i + UPSERT]
        r = None
        for attempt in range(5):
            try:
                r = db.s.post(f"{db.url}/price_history?on_conflict=symbol,d", json=batch, timeout=120,
                              headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                log(f"  upsert network error ({e.__class__.__name__}), retry {attempt + 1}/5")
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code < 500:
                break
            time.sleep(5 * (attempt + 1))
        if r is None:
            raise RuntimeError("price_history upsert failed: every attempt timed out.")
        if r.status_code >= 300:
            if missing_table(r.text):
                die("The price_history table isn't in the database yet. Run "
                    "supabase/migration_history.sql once, then run this workflow again.")
            raise RuntimeError(f"price_history upsert failed ({r.status_code}): {r.text[:400]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=200, help="Also cover the N largest instruments (default 200, 0 = none).")
    ap.add_argument("--only", default="", help="Comma-separated symbols to (re)load, nothing else.")
    ap.add_argument("--full", action="store_true", help="Ignore what's stored and fetch the full twenty years.")
    ap.add_argument("--deadline", type=float, default=0, help="Seconds before stopping cleanly.")
    args = ap.parse_args()
    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    try:
        db = Supabase()
    except SystemExit as e:
        die(str(e) or "Supabase credentials are missing.")

    have = last_dates(db)
    if args.only:
        symbols = [s.strip() for s in args.only.split(",") if s.strip()]
    else:
        held = held_symbols(db)
        top = top_symbols(db, args.top)
        symbols = list(dict.fromkeys(BENCHMARKS + sorted(held) + top))
        log(f"{len(symbols)} symbols: {len(BENCHMARKS)} indices, {len(held)} held, {len(top)} largest.")

    today = date.today()
    full_start = today - timedelta(days=int(365.25 * YEARS))
    # Group by start date so one yfinance call covers many tickers.
    groups = {}
    for s in symbols:
        last = None if args.full else have.get(s)
        start = full_start if last is None else max(full_start, last - timedelta(days=OVERLAP_DAYS))
        groups.setdefault(start, []).append(s)

    total_rows, done, empty = 0, 0, []
    for start, syms in sorted(groups.items()):
        for i in range(0, len(syms), CHUNK):
            if out_of_time():
                log(f"Deadline reached — stopping after {done} symbols. The next run carries on.")
                break
            part = syms[i:i + CHUNK]
            got = fetch_weekly(part, start)
            rows, seen = [], set()
            for s in part:
                pts = got.get(s)
                if not pts:
                    empty.append(s)
                    continue
                for d, v in pts:
                    key = (s, d)
                    if key in seen:          # a duplicate week would make the upsert fail
                        continue
                    seen.add(key)
                    rows.append({"symbol": s, "d": d.isoformat(), "close": v})
            if rows:
                upsert_history(db, rows)
            total_rows += len(rows)
            done += len(part)
            log(f"  {done}/{len(symbols)} symbols · {total_rows:,} rows written")
            time.sleep(1)

    if empty:
        log(f"Yahoo had nothing for {len(empty)}: {', '.join(empty[:25])}{' …' if len(empty) > 25 else ''}")
    if not total_rows and not have:
        die("Nothing was written and the table is empty. Yahoo may be blocking the runner — "
            "try again in an hour.")
    log(f"Done: {total_rows:,} rows across {done} symbols in {time.monotonic() - started:.0f}s.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        die("Unhandled error above. See the traceback for the exact line.")
