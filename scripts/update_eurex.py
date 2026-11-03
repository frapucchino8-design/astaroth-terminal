"""Eurex futures — best-effort scraper of Deutsche Börse's free MiFID II delayed data.

Deutsche Börse publishes 15-minute-delayed pre- and post-trade data for every Eurex
contract, free of charge, under its MiFIR Art. 13 (1) obligation. The file service
updates once a minute and files stay available until midnight the next business day:
    https://mfs.deutsche-boerse.com/DEUR-posttrade

The exact file-naming inside the service isn't publicly documented, so this script
tries several patterns and falls back to the public view-service JSON used by
mvs.deutsche-boerse.com. It only inserts rows it can read with confidence.

Terms: Deutsche Börse's free delayed data is for personal and informational use.
Check mds.deutsche-boerse.com/mds-en/disclaimer before you redistribute it on a
public page. For a public commercial site, you need a paid Eurex licence.
"""
import argparse
import datetime as dt
import gzip
import io
import json
import time

import requests

from common import Supabase, log, now_iso, num

MFS_BASE = "https://mfs.deutsche-boerse.com/DEUR-posttrade"
MVS_API = "https://mvs.deutsche-boerse.com/api"  # view-service backend, same data in a different shape

# One row per contract we want. The script looks up each by product_id (Eurex) + ISIN.
EUREX = [
    # product_id, long name, ISIN (underlying index/bond), region, currency
    ("FSMI", "SMI Future", "CH0008616432", "Switzerland", "CHF"),
    ("FSMM", "SMI MID Future", "CH0019396990", "Switzerland", "CHF"),
    ("FSLI", "SLI Future", "CH0030252883", "Switzerland", "CHF"),
    ("FESX", "Euro Stoxx 50 Future", "EU0009658145", "Europe", "EUR"),
    ("FDAX", "DAX Future", "DE0008469008", "Europe", "EUR"),
    ("FDXM", "Mini-DAX Future", "DE0008469008", "Europe", "EUR"),
    ("FMDX", "MDAX Future", "DE0008467416", "Europe", "EUR"),
    ("FTDX", "TecDAX Future", "DE0007203275", "Europe", "EUR"),
    ("FSTX", "Stoxx Europe 50 Future", "EU0009658152", "Europe", "EUR"),
    ("FXXP", "Stoxx Europe 600 Future", "EU0009658202", "Europe", "EUR"),
    ("FSMI", "SMI Future", "CH0008616432", "Switzerland", "CHF"),
    # Bund family
    ("FGBL", "Euro-Bund Future", "DE0001102333", "Europe", "EUR"),
    ("FGBM", "Euro-Bobl Future", "DE0001141828", "Europe", "EUR"),
    ("FGBS", "Euro-Schatz Future", "DE0001104867", "Europe", "EUR"),
    ("FGBX", "Euro-Buxl Future", "DE0001102580", "Europe", "EUR"),
    ("FBTP", "Long BTP Future", "IT0005506919", "Europe", "EUR"),
    ("FOAT", "Long OAT Future", "FR0014004301", "Europe", "EUR"),
]


def fetch_json(url, timeout=30):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "astaroth-terminal/1.0"})
        if not r.ok:
            return None
        ct = r.headers.get("Content-Type", "")
        body = r.content
        if ct.startswith("application/gzip") or url.endswith(".gz"):
            body = gzip.decompress(body)
        try:
            return json.loads(body)
        except ValueError:
            # Some endpoints wrap JSON in a tiny HTML page or an array of objects per line.
            text = body.decode("utf-8", errors="replace").strip()
            if text.startswith("["):
                return json.loads(text)
            if "\n" in text:
                return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]
            return None
    except (requests.RequestException, ValueError, OSError):
        return None


def try_sources(product_id, isin):
    """Try a few URLs for one contract. Returns (price, change_pct, high, low) or None."""
    today = dt.datetime.utcnow().strftime("%Y%m%d")
    candidates = [
        # 1. The view-service's own JSON endpoint (same data mvs.deutsche-boerse.com displays).
        f"{MVS_API}/instruments/XEUR/{product_id}/summary",
        f"{MVS_API}/instruments/{isin}/XEUR/summary",
        # 2. The file service, by product_id. The exact filename isn't documented, so
        #    these are good-faith guesses — they'll silently fall through if wrong.
        f"{MFS_BASE}/{today}/{product_id}.json.gz",
        f"{MFS_BASE}/{product_id}/latest.json",
        f"{MFS_BASE}/{product_id}.json",
    ]
    for url in candidates:
        data = fetch_json(url)
        if not data:
            continue
        # Normalise one of several possible shapes.
        if isinstance(data, dict):
            for key in ("summary", "trade", "lastTrade", "data"):
                if isinstance(data.get(key), dict):
                    data = data[key]
                    break
            price = num(data.get("lastPrice") or data.get("price") or data.get("last"))
            prev = num(data.get("previousClose") or data.get("prevClose") or data.get("settlementPrice"))
            hi = num(data.get("dayHigh") or data.get("high"))
            lo = num(data.get("dayLow") or data.get("low"))
            if price:
                return price, (round((price / prev - 1) * 100, 3) if prev else None), hi, lo, url
        elif isinstance(data, list) and data:
            # Pick the most recent entry by timestamp.
            try:
                data.sort(key=lambda d: d.get("timestamp") or d.get("ts") or "")
                last = data[-1]
                price = num(last.get("price") or last.get("lastPrice"))
                if price:
                    return price, None, None, None, url
            except (AttributeError, TypeError):
                pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print what would be stored, don't write to Supabase.")
    args = ap.parse_args()

    rows, misses = [], []
    for pid, name, isin, region, cur in EUREX:
        res = try_sources(pid, isin)
        if not res:
            misses.append(pid); log(f"  {pid}: no data from any endpoint"); continue
        price, chpct, hi, lo, src = res
        log(f"  {pid}: {price} ({chpct}%) from {src.rsplit('/', 2)[-1]}")
        rows.append({
            "symbol": pid,
            "name": name,
            "exchange": "EUREX",
            "region": region,
            "asset_class": "future",
            "currency": cur,
            "sector": "Equity Index" if pid.startswith(("FS", "FE", "FD", "FM", "FT", "FX")) else "Rates",
            "industry": "Equity Index" if pid.startswith(("FS", "FE", "FD", "FM", "FT", "FX")) else "Rates",
            "price": price,
            "change_pct": chpct,
            "high_52w": hi,
            "low_52w": lo,
            "quote_updated_at": now_iso(),
            "fundamentals_updated_at": now_iso(),
        })
        time.sleep(0.5)

    log(f"Got {len(rows)} Eurex contracts, missed {len(misses)}: {misses}")
    if args.dry_run:
        print(json.dumps(rows, indent=2))
        return
    if rows:
        Supabase().upsert(rows)
        log(f"Stored {len(rows)} Eurex contracts.")
    else:
        log("No Eurex data stored this run — the exact file-service URL couldn't be read. "
            "The Yahoo index spot values (^SSMI, ^GDAXI, ^STOXX50E) from update_indices.py "
            "cover the same underlyings.")


if __name__ == "__main__":
    main()
