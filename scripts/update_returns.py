"""Fill the price-change columns the Price change screener uses.

Price change over one, two, three and six months, since the start of the year,
and over two and three years. The 1-year figure already exists as return_1y,
written by the price scripts, so this one leaves it alone.

Everything comes from one bulk Yahoo call per chunk of tickers — weekly closes
for the last four years — so a few thousand instruments take a couple of
minutes, not hours. The end point of every window is the live `price` already in
the database, so the figures agree with what the site shows.

    python update_returns.py
    python update_returns.py --region US --chunk 150
    python update_returns.py --stale-days 7 --deadline 1500

Run supabase/migration_returns.sql first, or every write fails on a missing
column (the script checks and says so).

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, like the other scripts.
"""
import argparse
import datetime as dt
import re
import sys
import time

from common import Supabase, log, now_iso

COLUMNS = ("return_1m", "return_2m", "return_3m", "return_6m", "return_ytd",
           "return_2y", "return_3y", "returns_updated_at")

# column → how far back to look, in days. YTD is handled separately.
WINDOWS = (("return_1m", 30), ("return_2m", 61), ("return_3m", 91),
           ("return_6m", 182), ("return_2y", 730), ("return_3y", 1095))

# A close this far either side of the target date still counts. Weekly data means
# the nearest point can be a few days off; a gap larger than this (a holiday
# stretch, a halt, a listing younger than the window) leaves the figure empty
# rather than quietly measuring the wrong span.
TOLERANCE_DAYS = 12


def yahoo_symbol(s):
    """Class shares are BRK.B in the database but BRK-B on Yahoo."""
    return re.sub(r"^([A-Z]+)\.([A-Z])$", r"\1-\2", s)


def check_columns(db):
    try:
        r = db.s.get(f"{db.url}/stocks", params={"select": ",".join(COLUMNS), "limit": "1"}, timeout=30)
    except Exception as e:
        sys.exit(f"Couldn't reach Supabase: {e}")
    if r.status_code >= 300:
        sys.exit("The price-change columns aren't in the database yet. Run "
                 "supabase/migration_returns.sql once in the Supabase SQL editor, then run this "
                 f"again.\nSupabase said: {r.text[:300]}")


def pick_rows(db, args):
    filters = {}
    if args.region:
        filters["region"] = f"eq.{args.region}"
    if args.asset_class:
        filters["asset_class"] = f"eq.{args.asset_class}"
    rows = db.select("symbol,region,price,returns_updated_at", filters, order="symbol")
    rows = [r for r in rows if r.get("price")]          # no price, no price change
    if args.stale_days:
        cutoff = time.time() - args.stale_days * 86400

        def is_stale(r):
            ts = r.get("returns_updated_at")
            if not ts:
                return True
            try:
                return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < cutoff
            except (ValueError, AttributeError):
                return True
        rows = [r for r in rows if is_stale(r)]
    if args.limit:
        rows = rows[: args.limit]
    return rows


def closes_for(yf, symbols):
    """{symbol: [(date, close)] oldest first} — one download for the whole chunk."""
    out = {}
    ysyms = {s: yahoo_symbol(s) for s in symbols}
    try:
        df = yf.download(" ".join(ysyms.values()), period="4y", interval="1wk",
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
            if hasattr(closes, "columns"):          # single ticker, still a two-level frame
                closes = closes.iloc[:, 0]
            closes = closes.dropna()
        except (KeyError, ValueError):
            continue
        pts = []
        for ts, v in closes.items():
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v > 0 and v == v:
                pts.append((ts.date(), v))
        if len(pts) >= 2:
            out[s] = pts
    return out


def close_near(pts, target):
    """The close nearest `target`, or None when the nearest is too far away."""
    best, gap = None, None
    for day, v in pts:
        g = abs((day - target).days)
        if gap is None or g < gap:
            best, gap = v, g
    return best if gap is not None and gap <= TOLERANCE_DAYS else None


def returns_for(pts, price, today):
    """Every window as a percentage change, skipping the ones we can't measure."""
    out = {}
    for col, days in WINDOWS:
        base = close_near(pts, today - dt.timedelta(days=days))
        if base:
            out[col] = round((price / base - 1) * 100, 2)
    # Year to date: against the last close of December, within a fortnight of New Year.
    ytd_base = close_near(pts, dt.date(today.year - 1, 12, 31))
    if ytd_base:
        out["return_ytd"] = round((price / ytd_base - 1) * 100, 2)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="", help="Only this region, e.g. US or Switzerland.")
    ap.add_argument("--asset-class", default="", help="Only this asset class, e.g. stock or etf.")
    ap.add_argument("--limit", type=int, default=0, help="Stop after this many instruments.")
    ap.add_argument("--chunk", type=int, default=120, help="Tickers per Yahoo download (default 120).")
    ap.add_argument("--deadline", type=float, default=0, help="Seconds before saving what we have and stopping.")
    ap.add_argument("--stale-days", type=int, default=0,
                    help="Only names whose returns are older than this many days (0 = all).")
    ap.add_argument("--sleep", type=float, default=1.0, help="Pause between chunks.")
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
    today = dt.date.today()
    saved = skipped = 0

    for i in range(0, len(rows), max(1, args.chunk)):
        if args.deadline and (time.monotonic() - started) > args.deadline:
            log(f"  deadline reached after {i}/{len(rows)}")
            break
        chunk = rows[i: i + args.chunk]
        by_symbol = closes_for(yf, [r["symbol"] for r in chunk])
        batch = []
        for r in chunk:
            pts = by_symbol.get(r["symbol"])
            if not pts:
                skipped += 1
                continue
            try:
                price = float(r["price"])
            except (TypeError, ValueError):
                skipped += 1
                continue
            vals = returns_for(pts, price, today)
            if not vals:
                skipped += 1
                continue
            vals.update({"symbol": r["symbol"], "region": r.get("region") or "US",
                         "returns_updated_at": now_iso()})
            batch.append(vals)
        if batch:
            db.upsert(batch)
            saved += len(batch)
        log(f"  {min(i + args.chunk, len(rows))}/{len(rows)} — {saved} saved")
        time.sleep(args.sleep)

    log(f"Saved {saved} instruments, skipped {skipped}, in {time.monotonic() - started:.0f}s.")
    if saved:
        log("The Price change screener on the site picks this up on the next page load.")


if __name__ == "__main__":
    main()
