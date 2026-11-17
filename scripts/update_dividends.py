"""Fill the dividend columns the Dividend screeners use.

Dividend growth over one, three, five and ten years, and the dividend streak:
how many years in a row the dividend per share was raised.

update_fundamentals.py also works these out, but it pulls a full balance sheet
and income statement for every name, so it takes a few seconds each and rarely
gets through the whole database in one run. This script asks Yahoo for nothing
but the dividend history, skips everything that pays no dividend, and fetches
several names at once, so the whole list is usually done in a few minutes.

    python update_dividends.py                      # every dividend payer
    python update_dividends.py --region Switzerland
    python update_dividends.py --stale-days 30 --deadline 1800
    python update_dividends.py --all                # include names with no yield

It is resumable: --stale-days skips anything updated recently, and --deadline
saves what it has and stops cleanly, so a scheduled run never gets cut off
halfway with nothing written.

Run supabase/migration_dividends.sql first — it adds the four columns and the
dividends_updated_at stamp this script writes. Without it every write fails on
a missing column, and the script says so and stops.

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, like the other scripts.
"""
import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from common import Supabase, log, now_iso

try:
    from update_fundamentals import dividends_by_year, dividend_growth_5y, dividend_extras
except ImportError:
    sys.exit("update_fundamentals.py must sit next to this script — it holds the dividend maths.")

COLUMNS = ("dividend_growth_1y", "dividend_growth_3y", "dividend_growth_5y",
           "dividend_growth_10y", "dividend_streak_years")


def check_columns(db):
    """Stop early with a clear message if migration_dividends.sql hasn't been run."""
    try:
        r = db.s.get(f"{db.url}/stocks",
                     params={"select": ",".join(COLUMNS + ("dividends_updated_at",)), "limit": "1"},
                     timeout=30)
    except Exception as e:
        sys.exit(f"Couldn't reach Supabase: {e}")
    if r.status_code >= 300:
        sys.exit("The dividend columns aren't in the database yet. Run "
                 "supabase/migration_dividends.sql once in the Supabase SQL editor, then run this "
                 f"again.\nSupabase said: {r.text[:300]}")


def pick_rows(db, args):
    filters = {"asset_class": "in.(stock,reit,etf)"}
    if args.region:
        filters["region"] = f"eq.{args.region}"
    rows = db.select("symbol,region,dividend_yield,dividends_updated_at", filters, order="symbol")

    if not args.all:
        # A name with no yield pays nothing, so there is no dividend history to read.
        # Anything already carrying a streak is kept, so a cut is picked up too.
        rows = [r for r in rows
                if (r.get("dividend_yield") or 0) > 0 or r.get("dividends_updated_at")]
    if args.stale_days:
        import datetime as _dt
        cutoff = time.time() - args.stale_days * 86400

        def is_stale(r):
            ts = r.get("dividends_updated_at")
            if not ts:
                return True
            try:
                return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < cutoff
            except (ValueError, AttributeError):
                return True
        rows = [r for r in rows if is_stale(r)]
    if args.limit:
        rows = rows[: args.limit]
    return rows


def record_for(yf, sym, region):
    """One instrument's dividend figures, or None when there is nothing to say."""
    try:
        by_year = dividends_by_year(yf.Ticker(sym))
    except Exception:
        return None
    rec = {"symbol": sym, "region": region or "US", "dividends_updated_at": now_iso()}
    if by_year:
        g5 = dividend_growth_5y(by_year)
        if g5 is not None:
            rec["dividend_growth_5y"] = g5
        rec.update(dividend_extras(by_year))
    else:
        # Pays nothing, or Yahoo has no record: say so rather than leaving a stale
        # streak from an earlier run standing.
        rec["dividend_streak_years"] = 0
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="", help="Only this region, e.g. US or Switzerland.")
    ap.add_argument("--limit", type=int, default=0, help="Stop after this many instruments.")
    ap.add_argument("--deadline", type=float, default=0, help="Seconds before saving what we have and stopping.")
    ap.add_argument("--stale-days", type=int, default=0,
                    help="Only names whose dividends are older than this many days (0 = all).")
    ap.add_argument("--all", action="store_true", help="Include names with no dividend yield.")
    ap.add_argument("--workers", type=int, default=4, help="Parallel Yahoo requests (default 4; keep it modest).")
    args = ap.parse_args()

    try:
        import yfinance as yf
    except ImportError:
        raise SystemExit("yfinance isn't installed. pip install -r requirements.txt")

    db = Supabase()
    check_columns(db)
    rows = pick_rows(db, args)
    log(f"{len(rows)} instruments to update.")
    if not rows:
        return

    started = time.monotonic()
    stop = threading.Event()
    lock = threading.Lock()
    done = {"n": 0, "skipped": 0}
    batch, saved = [], 0

    def work(row):
        if stop.is_set():
            return None
        rec = record_for(yf, row["symbol"], row.get("region"))
        with lock:
            done["n"] += 1
            if rec is None:
                done["skipped"] += 1
            if done["n"] % 100 == 0:
                log(f"  {done['n']}/{len(rows)}")
            if args.deadline and (time.monotonic() - started) > args.deadline and not stop.is_set():
                stop.set()
                log(f"  deadline reached after {done['n']}/{len(rows)} — saving what we have")
        return rec

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for rec in pool.map(work, rows):
            if rec is None:
                continue
            batch.append(rec)
            if len(batch) >= 200:
                db.upsert(batch)
                saved += len(batch)
                batch = []
                log(f"  {saved} saved")

    if batch:
        db.upsert(batch)
        saved += len(batch)

    log(f"Saved {saved} instruments, skipped {done['skipped']}, in {time.monotonic() - started:.0f}s.")
    if saved:
        log("The Dividend screeners on the site pick this up on the next page load.")


if __name__ == "__main__":
    main()
