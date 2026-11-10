"""Top crypto coins via the CoinGecko free API — no key needed.

CoinGecko allows about 30 calls per minute without a key. One call brings back
the top 250 by market cap with price, 24-hour change, 1-year change, market cap,
volume and all the market data we need, so this script makes just a handful of
calls.

Along with price and cap it writes the three figures the crypto screeners need:
circulating supply, maximum supply and 24-hour traded value. Those live in
circulating_supply_m, max_supply_m and volume_24h_usd_m — run
supabase/migration_fundamentals.sql once before using this version. If the
columns aren't there, the script says so plainly and stops before writing
anything, rather than destroying data and failing later. Pass --no-extras to
load prices without the new columns while you get around to running it.

Any failure prints a single-line reason prefixed with "FATAL:" right before the
non-zero exit, so the Actions log tells you exactly what went wrong.
"""
import argparse
import sys
import time
import traceback

import requests

from common import Supabase, log, now_iso, num

API = "https://api.coingecko.com/api/v3"
MIN_COINS = 50          # fewer than this means the API gave us nothing useful
UA = "astaroth-terminal/1.0"


def die(reason):
    """Print one clear fatal line, then exit with code 1."""
    log("")
    log("=" * 68)
    log(f"FATAL: {reason}")
    log("=" * 68)
    sys.exit(1)


def get(path, **params):
    """GET a CoinGecko endpoint with retries for rate limits and network blips."""
    last = None
    for attempt in range(4):
        try:
            r = requests.get(API + path, params=params, timeout=30,
                             headers={"Accept": "application/json", "User-Agent": UA})
            last = r
            if r.status_code == 429:
                wait = 60 * (attempt + 1)
                log(f"  rate limit, waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                log(f"  {path} HTTP {r.status_code}, retrying in {5 * (attempt + 1)}s")
                time.sleep(5 * (attempt + 1))
                continue
            if r.ok:
                try:
                    return r.json()
                except ValueError:
                    log(f"  {path} returned non-JSON ({len(r.text)} bytes)")
                    return None
            log(f"  {path} HTTP {r.status_code}: {r.text[:200]}")
            return None
        except requests.RequestException as e:
            log(f"  {path} network error: {e}")
            time.sleep(5)
    if last is not None and last.status_code == 429:
        die("CoinGecko kept rate-limiting us. Try again later, or get a free "
            "demo key and add it to the request headers (x-cg-demo-api-key).")
    return None


def millions(value):
    """A raw coin count as millions, keeping small supplies readable."""
    n = num(value)
    return None if n is None else round(n / 1e6, 6)


def is_missing_column(err_text):
    """Does this error look like 'column X does not exist'?"""
    t = (err_text or "").lower()
    extras = ("circulating_supply_m", "max_supply_m", "volume_24h_usd_m")
    if any(c in t for c in extras):
        return True
    return "column" in t and ("does not exist" in t or "schema cache" in t or "pgrst204" in t)


def upsert(db, rows, no_extras):
    """Write a batch, turning a missing-column error into a FATAL with a fix."""
    try:
        db.upsert(rows)
    except Exception as e:
        if is_missing_column(str(e)) and not no_extras:
            die("The crypto columns aren't in the database yet. "
                "Run supabase/migration_fundamentals.sql once in the Supabase SQL "
                "editor, then run this workflow again. To load prices now without "
                "the new columns, run with --no-extras.\n"
                f"Original error: {str(e)[:400]}")
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=250, help="How many coins to track (default 250).")
    ap.add_argument("--no-extras", action="store_true",
                    help="Skip supply and volume columns (useful if the migration hasn't been run).")
    args = ap.parse_args()

    # 1) Supabase credentials — common.env() already exits on missing, with the
    # same style of message. Catch its SystemExit so we can wrap it ourselves.
    try:
        db = Supabase()
    except SystemExit as e:
        die(str(e) or "Supabase credentials are missing. Check the SUPABASE_URL "
                      "and SUPABASE_SERVICE_ROLE_KEY secrets in the repo settings.")

    # 2) Fetch the top coins.
    pages, per = (args.top + 249) // 250, 250
    all_coins = []
    for page in range(1, pages + 1):
        data = get("/coins/markets", vs_currency="usd", order="market_cap_desc", per_page=per,
                   page=page, sparkline="false", price_change_percentage="24h,1y") or []
        all_coins += data
        time.sleep(2.5)
        if len(data) < per:
            break
    log(f"CoinGecko returned {len(all_coins)} coins.")

    # 3) Sanity rail. Fewer than MIN_COINS means the API gave us nothing useful;
    # continuing would call delete_asset_class with an empty kept-set and wipe
    # every crypto row.
    if len(all_coins) < MIN_COINS:
        die(f"CoinGecko returned {len(all_coins)} coins (expected at least {MIN_COINS}). "
            "Refusing to continue, because the delete-what's-missing pass at the end "
            "would then wipe every crypto row. Try again in a few minutes.")

    # 4) Shape the rows.
    #
    # CoinGecko occasionally returns two different coins that share a symbol
    # (SOL, BNB, HYPE and so on — one famous, one a tiny imitator). Building
    # a ticker as SYMBOL-USD would then put two rows with the same primary key
    # into the same upsert batch, and Postgres refuses with:
    #   "ON CONFLICT DO UPDATE command cannot affect row a second time"
    # CoinGecko returns coins ordered by market cap, so keeping only the first
    # time we see a symbol keeps the one you've actually heard of.
    rows_batch, kept, skipped, deduped = [], set(), 0, 0
    for c in all_coins[:args.top]:
        sym = (c.get("symbol") or "").upper()
        if not sym:
            skipped += 1
            continue
        ticker = f"{sym}-USD"
        if ticker in kept:
            deduped += 1
            continue
        price = num(c.get("current_price"))
        if not price:
            skipped += 1
            continue
        kept.add(ticker)
        cap = num(c.get("market_cap"))
        row = {
            "symbol": ticker,
            "name": c.get("name") or sym,
            "exchange": "CoinGecko",
            "region": "Global",
            "asset_class": "crypto",
            "currency": "USD",
            "sector": "Crypto",
            "industry": "Cryptocurrency",
            "summary": None,
            "price": price,
            "change_pct": num(c.get("price_change_percentage_24h"), digits=3),
            "market_cap_usd_b": round(cap / 1e9, 4) if cap else None,
            "return_1y": num(c.get("price_change_percentage_1y_in_currency"), digits=2),
            "high_52w": num(c.get("ath")),
            "low_52w": num(c.get("atl")),
            "quote_updated_at": now_iso(),
            "fundamentals_updated_at": now_iso(),
        }
        if not args.no_extras:
            vol = num(c.get("total_volume"))
            row.update({
                "circulating_supply_m": millions(c.get("circulating_supply")),
                # null max_supply is meaningful — the coin is uncapped (Ethereum, Dogecoin)
                "max_supply_m": millions(c.get("max_supply")),
                "volume_24h_usd_m": round(vol / 1e6, 3) if vol else None,
            })
        rows_batch.append(row)
        if len(rows_batch) >= 100:
            upsert(db, rows_batch, args.no_extras)
            rows_batch = []

    if rows_batch:
        upsert(db, rows_batch, args.no_extras)

    log(f"Updated {len(kept)} crypto rows (skipped {skipped}, dropped {deduped} duplicate-symbol imitators).")

    # 5) Remove coins that have fallen out of the top list. Guarded by the
    # MIN_COINS check above — we only reach here with a healthy batch.
    try:
        gone = db.delete_asset_class("crypto", kept)
    except Exception as e:
        log(f"  housekeeping delete failed: {e}")
        gone = 0
    if gone:
        log(f"Removed {gone} coins no longer in the top.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Unexpected error — print the traceback, then the one-line FATAL.
        traceback.print_exc()
        die("Unhandled error above. See the traceback for the exact line.")
