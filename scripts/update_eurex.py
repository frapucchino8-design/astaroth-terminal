"""Eurex futures via Stooq — end-of-day CSV, one call per contract.

Stooq hosts continuous Eurex futures under <lowercase-product-code>.f, with two
free CSV endpoints:
    https://stooq.com/q/l/?s=fsmi.f&f=sd2t2ohlcv&h&e=csv   -> today's quote
    https://stooq.com/q/d/l/?s=fsmi.f&i=d                  -> daily history
We fetch both: the quote for today's price and OHLC, the history for yesterday's
close (so we can compute change_pct) and for 52-week high/low and 1-year return.

Stooq updates after the Eurex close, so running this hourly or once an evening is
plenty. On the fast 5-minute workflow it's harmless — it just rewrites the same
EOD numbers until the next close.

Stooq's terms allow personal use. For a public commercial site, you need a paid
Eurex licence.
"""
import argparse
import csv
import io
import time

import requests

from common import Supabase, log, now_iso, num

QUOTE = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcvn&h&e=csv"
HISTORY = "https://stooq.com/q/d/l/?s={sym}&i=d"
UA = {"User-Agent": "Mozilla/5.0 (astaroth-terminal)"}

# Stooq symbol, long name, Eurex group, currency, region.
# All symbols follow "<lowercase-eurex-product-code>.f" — verified for fsmi.f.
EUREX = [
    # Equity-index futures
    ("fsmi.f",  "SMI Future",                    "Equity Index", "CHF", "Switzerland"),
    ("fsmm.f",  "SMI MID Future",                "Equity Index", "CHF", "Switzerland"),
    ("fsli.f",  "SLI Future",                    "Equity Index", "CHF", "Switzerland"),
    ("fsmd.f",  "SMI Dividend Future",           "Equity Index", "CHF", "Switzerland"),
    ("fdax.f",  "DAX Future",                    "Equity Index", "EUR", "Europe"),
    ("fdxm.f",  "Mini-DAX Future",               "Equity Index", "EUR", "Europe"),
    ("fmdx.f",  "MDAX Future",                   "Equity Index", "EUR", "Europe"),
    ("ftdx.f",  "TecDAX Future",                 "Equity Index", "EUR", "Europe"),
    ("fesx.f",  "Euro Stoxx 50 Future",          "Equity Index", "EUR", "Europe"),
    ("fstx.f",  "Stoxx Europe 50 Future",        "Equity Index", "EUR", "Europe"),
    ("fxxp.f",  "Stoxx Europe 600 Future",       "Equity Index", "EUR", "Europe"),
    ("fsto.f",  "Stoxx Europe 600 Oil & Gas",    "Equity Index", "EUR", "Europe"),
    ("fstb.f",  "Stoxx Europe 600 Banks",        "Equity Index", "EUR", "Europe"),
    # Fixed-income futures
    ("fgbl.f",  "Euro-Bund Future",              "Rates",        "EUR", "Europe"),
    ("fgbm.f",  "Euro-Bobl Future",              "Rates",        "EUR", "Europe"),
    ("fgbs.f",  "Euro-Schatz Future",            "Rates",        "EUR", "Europe"),
    ("fgbx.f",  "Euro-Buxl Future",              "Rates",        "EUR", "Europe"),
    ("fbtp.f",  "Long BTP Future",               "Rates",        "EUR", "Europe"),
    ("fbts.f",  "Short BTP Future",              "Rates",        "EUR", "Europe"),
    ("foat.f",  "Long OAT Future",               "Rates",        "EUR", "Europe"),
    ("fbon.f",  "Buoni del Tesoro Future",       "Rates",        "EUR", "Europe"),
    # Volatility
    ("fvs.f",   "VSTOXX Future",                 "Volatility",   "EUR", "Europe"),
]


def fetch_csv(url, retries=2, timeout=12):
    """One GET, retried only on a network error — a clean non-OK reply is final,
    so a wrong ticker costs one request instead of three plus back-off."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            return r.text if r.ok else None
        except requests.RequestException as e:
            log(f"  network error on {url}: {e}")
            if attempt == retries - 1:
                return None
            time.sleep(2)
    return None


def parse_quote(text):
    """Returns {symbol, date, time, open, high, low, close, volume, name} or None."""
    if not text:
        return None
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return None
    row = rows[0]
    close = num(row.get("Close"))
    return None if close is None else {
        "date": row.get("Date"), "time": row.get("Time"),
        "open": num(row.get("Open")), "high": num(row.get("High")),
        "low": num(row.get("Low")), "close": close,
        "volume": num(row.get("Volume")), "name": row.get("Name"),
    }


def parse_history(text):
    """Returns the daily close series as a list of (date, close), oldest first."""
    if not text:
        return []
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        c = num(row.get("Close"))
        if c is not None and row.get("Date"):
            out.append((row["Date"], c))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fast", action="store_true",
                    help="Price only — skip history for 52w high/low and 1-year return.")
    ap.add_argument("--deadline", type=float, default=0,
                    help="Seconds. Stop fetching after this long and save what we have, "
                         "so the job finishes inside its runner timeout. 0 = no limit.")
    args = ap.parse_args()
    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    rows, misses = [], []
    for stooq_sym, name, group, cur, region in EUREX:
        if out_of_time():
            log(f"Deadline of {args.deadline:.0f}s reached — saving {len(rows)} contracts "
                f"and leaving the rest for the next run.")
            break
        q = parse_quote(fetch_csv(QUOTE.format(sym=stooq_sym)))
        if not q:
            misses.append(stooq_sym); log(f"  {stooq_sym}: no quote"); continue
        price = q["close"]

        change_pct = hi52 = lo52 = y1 = None
        if not args.fast:
            hist = parse_history(fetch_csv(HISTORY.format(sym=stooq_sym)))
            if hist:
                # previous close = second-to-last row
                if len(hist) >= 2 and hist[-2][1]:
                    change_pct = round((price / hist[-2][1] - 1) * 100, 3)
                closes = [c for _, c in hist[-260:]]  # ~1 trading year
                if closes:
                    hi52, lo52 = max(closes), min(closes)
                    y1 = round((price / closes[0] - 1) * 100, 2) if len(closes) > 20 else None

        # Eurex product code is the file name minus the .f, uppercased.
        sym = stooq_sym.replace(".f", "").upper()
        log(f"  {sym}: {price} {cur} ({change_pct if change_pct is not None else '?'}%)")
        row = {
            "symbol": sym,
            "name": name,
            "exchange": "EUREX",
            "region": region,
            "asset_class": "future",
            "currency": cur,
            "sector": group,
            "industry": group,
            "price": price,
            "change_pct": change_pct,
            "quote_updated_at": now_iso(),
        }
        if not args.fast:
            row.update({"high_52w": hi52, "low_52w": lo52, "return_1y": y1,
                        "fundamentals_updated_at": now_iso()})
        rows.append(row)
        time.sleep(0.25 if args.fast else 1.0)  # be polite to a free third-party service

    log(f"Got {len(rows)} Eurex contracts in {time.monotonic() - started:.0f}s, "
        f"missed {len(misses)}: {misses}")
    if args.dry_run:
        import json; print(json.dumps(rows, indent=2)); return
    if rows:
        Supabase().upsert(rows)
        log(f"Stored {len(rows)} Eurex contracts.")
    else:
        log("No Eurex data stored. Check whether any Stooq ticker changed by opening "
            "https://stooq.com/q/?s=<ticker> in a browser.")


if __name__ == "__main__":
    main()
