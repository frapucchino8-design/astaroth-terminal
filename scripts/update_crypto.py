"""Top crypto coins via the CoinGecko free API — no key needed.

CoinGecko allows about 30 calls per minute without a key. One call brings back the
top 250 by market cap with price, 24-hour change, 1-year change, market cap, volume
and all the market data we need, so this script makes just a handful of calls.
"""
import argparse
import time

import requests

from common import Supabase, log, now_iso, num

API = "https://api.coingecko.com/api/v3"


def get(path, **params):
    for attempt in range(4):
        try:
            r = requests.get(API + path, params=params, timeout=30,
                             headers={"Accept": "application/json", "User-Agent": "astaroth-terminal/1.0"})
            if r.status_code == 429:
                wait = 60 * (attempt + 1); log(f"  rate limit, waiting {wait}s"); time.sleep(wait); continue
            if r.ok:
                return r.json()
            log(f"  {path} HTTP {r.status_code}")
            return None
        except requests.RequestException as e:
            log(f"  {path} network error: {e}"); time.sleep(5)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=250, help="How many coins to track (default 250).")
    args = ap.parse_args()

    db = Supabase()
    pages, per = (args.top + 249) // 250, 250
    all_coins, kept = [], set()
    for page in range(1, pages + 1):
        data = get("/coins/markets", vs_currency="usd", order="market_cap_desc", per_page=per,
                   page=page, sparkline="false", price_change_percentage="24h,1y") or []
        all_coins += data
        time.sleep(2.5)
        if len(data) < per:
            break
    log(f"CoinGecko returned {len(all_coins)} coins.")

    rows = []
    for c in all_coins[:args.top]:
        sym = (c.get("symbol") or "").upper()
        if not sym:
            continue
        ticker = f"{sym}-USD"
        kept.add(ticker)
        price = num(c.get("current_price"))
        if not price:
            continue
        cap = num(c.get("market_cap"))
        rows.append({
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
        })
        if len(rows) >= 100:
            db.upsert(rows); rows = []

    if rows:
        db.upsert(rows)
    log(f"Updated {len(kept)} crypto rows.")

    gone = db.delete_asset_class("crypto", kept)
    if gone:
        log(f"Removed {gone} coins no longer in the top.")


if __name__ == "__main__":
    main()
