"""Eurex futures via Stooq, with loud diagnostics.

Why this script may find nothing when stooq.com works fine in your browser:
Stooq is known to refuse datacenter IP ranges, and GitHub Actions runners sit in
one. From a runner you then get an HTML error page (or an empty body) instead of
CSV, and every contract looks like "no quote".

So this script does three things rather than failing silently:
  1. For each contract it tries several symbol spellings (fsmi.f, fsmi, f.fsmi …).
  2. If the CSV endpoint gives nothing usable it scrapes Stooq's own quote page.
  3. It PRINTS what actually came back — status, length, first line — so the log
     tells you whether the ticker is wrong or the IP is blocked.

Run `python scripts/update_eurex.py --probe` to test one contract and dump the
raw response without touching Supabase.
"""
import argparse
import csv
import io
import re
import sys
import time

import requests

from common import Supabase, log, now_iso, num

CSV_URL = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
PAGE_URL = "https://stooq.com/q/?s={sym}"
HEADERS = {
    # A plain requests UA is an instant refusal on many sites; mimic a browser.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/csv,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://stooq.com/",
}

# Eurex product code -> (long name, group, currency, region).
# The script tries "<code>.f", "<code>", and "f.<code>" for each.
EUREX = [
    ("fsmi", "SMI Future",                 "Equity Index", "CHF", "Switzerland"),
    ("fsmm", "SMI MID Future",             "Equity Index", "CHF", "Switzerland"),
    ("fsli", "SLI Future",                 "Equity Index", "CHF", "Switzerland"),
    ("fdax", "DAX Future",                 "Equity Index", "EUR", "Europe"),
    ("fdxm", "Mini-DAX Future",            "Equity Index", "EUR", "Europe"),
    ("fmdx", "MDAX Future",                "Equity Index", "EUR", "Europe"),
    ("ftdx", "TecDAX Future",              "Equity Index", "EUR", "Europe"),
    ("fesx", "Euro Stoxx 50 Future",       "Equity Index", "EUR", "Europe"),
    ("fstx", "Stoxx Europe 50 Future",     "Equity Index", "EUR", "Europe"),
    ("fxxp", "Stoxx Europe 600 Future",    "Equity Index", "EUR", "Europe"),
    ("fgbl", "Euro-Bund Future",           "Rates",        "EUR", "Europe"),
    ("fgbm", "Euro-Bobl Future",           "Rates",        "EUR", "Europe"),
    ("fgbs", "Euro-Schatz Future",         "Rates",        "EUR", "Europe"),
    ("fgbx", "Euro-Buxl Future",           "Rates",        "EUR", "Europe"),
    ("fbtp", "Long BTP Future",            "Rates",        "EUR", "Europe"),
    ("foat", "Long OAT Future",            "Rates",        "EUR", "Europe"),
    ("fvs",  "VSTOXX Future",              "Volatility",   "EUR", "Europe"),
]

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_diag = {"requests": 0, "html_back": 0, "empty": 0, "blocked_hint": False}


def get(url, timeout=12):
    """One GET. Returns (status, text) or (None, None) on a network error."""
    _diag["requests"] += 1
    try:
        r = SESSION.get(url, timeout=timeout)
        return r.status_code, r.text
    except requests.RequestException as e:
        log(f"    network error: {e}")
        return None, None


def looks_like_csv(text):
    return bool(text) and text.lstrip()[:7].lower().startswith("symbol")


def from_csv(text):
    """Parse Stooq's one-row quote CSV. Returns dict or None."""
    if not looks_like_csv(text):
        return None
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return None
    row = rows[0]
    close = num(row.get("Close"))
    if close is None:
        return None
    return {"close": close, "open": num(row.get("Open")), "high": num(row.get("High")),
            "low": num(row.get("Low")), "date": row.get("Date"), "src": "csv"}


def from_page(text):
    """Last resort: pull the last price out of Stooq's HTML quote page."""
    if not text or "<" not in text:
        return None
    # Stooq renders the last price inside an id="aq_<sym>_c2" span, among others.
    for pat in (r'id="aq_[a-z0-9_.]+_c2"[^>]*>([0-9][0-9\s,.]*)<',
                r'<b[^>]*id="aq_[a-z0-9_.]+_c3"[^>]*>([0-9][0-9\s,.]*)<',
                r'Last[^0-9]{0,40}([0-9][0-9,]*\.?[0-9]*)'):
        m = re.search(pat, text, re.I)
        if m:
            v = num(m.group(1).replace(",", "").replace(" ", ""))
            if v:
                return {"close": v, "open": None, "high": None, "low": None,
                        "date": None, "src": "page"}
    return None


def describe(status, text):
    """One short line saying what actually came back — the whole point of this script."""
    if status is None:
        return "no response"
    body = (text or "").strip()
    head = body.splitlines()[0][:110] if body else ""
    kind = "csv" if looks_like_csv(body) else ("html" if body[:1] == "<" else "text")
    if kind == "html":
        _diag["html_back"] += 1
        low = body.lower()
        if any(w in low for w in ("denied", "forbidden", "blocked", "robot", "captcha", "too many")):
            _diag["blocked_hint"] = True
    if not body:
        _diag["empty"] += 1
    return f"HTTP {status}, {len(body)}B, {kind}: {head!r}"


def fetch_contract(code, verbose=False):
    """Try every spelling and both endpoints. Returns (quote, symbol_used) or (None, None)."""
    for sym in (f"{code}.f", code, f"f.{code}"):
        status, text = get(CSV_URL.format(sym=sym))
        q = from_csv(text)
        if verbose or q is None:
            log(f"    csv  {sym:10} -> {describe(status, text)}")
        if q:
            return q, sym
        status, text = get(PAGE_URL.format(sym=sym))
        q = from_page(text)
        if verbose or q is None:
            log(f"    page {sym:10} -> {describe(status, text)}")
        if q:
            return q, sym
        time.sleep(0.4)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="Diagnose one contract: dump raw responses, write nothing.")
    ap.add_argument("--probe-code", default="fsmi", help="Which contract to probe (default fsmi).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fast", action="store_true", help="Accepted for symmetry; this script is already price-only.")
    ap.add_argument("--deadline", type=float, default=0,
                    help="Seconds. Stop fetching after this long and save what we have.")
    args = ap.parse_args()

    if args.probe:
        log(f"Probing '{args.probe_code}' against Stooq from this runner …")
        q, sym = fetch_contract(args.probe_code, verbose=True)
        log(f"RESULT: {q} via {sym}")
        if not q:
            log("")
            log("Nothing parsed. Read the lines above:")
            log("  * 'html' bodies where CSV was expected  -> Stooq served a page, not data.")
            log("    If it mentions robots/denied/too many, this runner's IP is being refused;")
            log("    the tickers are fine and the fix is a different source, not a different symbol.")
            log("  * 'HTTP 404' for every spelling          -> the symbol really is wrong.")
            log("  * 'no response'                          -> network or DNS blocked outbound.")
        return

    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    rows, misses = [], []
    for code, name, group, cur, region in EUREX:
        if out_of_time():
            log(f"Deadline reached — saving {len(rows)} and leaving the rest.")
            break
        log(f"  {code}:")
        q, sym = fetch_contract(code)
        if not q:
            misses.append(code)
            continue
        price = q["close"]
        log(f"    -> {price} {cur} via {sym} ({q['src']})")
        rows.append({
            "symbol": code.upper(),
            "name": name,
            "exchange": "EUREX",
            "region": region,
            "asset_class": "future",
            "currency": cur,
            "sector": group,
            "industry": group,
            "price": price,
            "high_52w": q.get("high"),
            "low_52w": q.get("low"),
            "quote_updated_at": now_iso(),
        })
        time.sleep(0.3)

    log("")
    log(f"SUMMARY: {len(rows)} contracts read, {len(misses)} missed {misses}")
    log(f"  requests made: {_diag['requests']}, html-instead-of-csv: {_diag['html_back']}, "
        f"empty bodies: {_diag['empty']}")
    if not rows:
        if _diag["blocked_hint"] or _diag["html_back"] >= _diag["requests"] / 2:
            log("  DIAGNOSIS: Stooq served HTML to every request. This runner's IP is almost")
            log("  certainly being refused — the same URLs work from your own browser. Stooq")
            log("  will not work from GitHub Actions; we need a different Eurex source.")
        else:
            log("  DIAGNOSIS: symbols not found. Run with --probe to see the raw replies.")
        sys.exit(0)   # a data gap is not a build failure

    if args.dry_run:
        import json; print(json.dumps(rows, indent=2)); return
    Supabase().upsert(rows)
    log(f"Stored {len(rows)} Eurex contracts.")


if __name__ == "__main__":
    main()
