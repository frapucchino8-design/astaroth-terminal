"""Individual corporate bonds for the Public section, from iShares holdings.

Downloads the full holdings of two big iShares bond ETFs — LQD (investment grade)
and HYG (high yield) — which BlackRock publishes as CSV, and stores each bond:
issuer, coupon, maturity, price, yield to maturity, sector and grade. Together
they're a few thousand real US corporate bonds, no API key.

    python update_corpbonds.py
    python update_corpbonds.py --only LQD

Run supabase/migration_corporate_bonds.sql once first. Needs SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY.
"""
import argparse
import csv
import datetime as dt
import io
import re
import time

import requests

from common import Supabase, log, now_iso, num

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ticker, iShares product id, grade. The long numeric path segment is a fixed
# token iShares accepts for any fund's holdings download.
ETFS = [
    ("LQD", "239566", "IG"),   # iShares iBoxx $ Investment Grade Corporate Bond ETF
    ("HYG", "239565", "HY"),   # iShares iBoxx $ High Yield Corporate Bond ETF
]
URL = ("https://www.ishares.com/us/products/{pid}/fund/1467271812596.ajax"
       "?fileType=csv&fileName={ticker}_holdings&dataType=fund")

DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%d/%b/%Y", "%m/%d/%Y", "%d-%b-%Y")


def to_float(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_date(v):
    s = (v or "").strip()
    if not s or s in ("-", "N/A"):
        return None
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def issuer_of(name):
    """Best-effort issuer from an iShares bond name like 'AT&T INC 3.5 06/01/2041'."""
    if not name:
        return None
    # drop a trailing "<coupon> <date>" tail
    m = re.match(r"^(.*?)\s+\d+(?:\.\d+)?\s+\d", name.strip())
    return (m.group(1) if m else name).strip() or name


def fetch_csv(ticker, pid):
    url = URL.format(pid=pid, ticker=ticker)
    headers = {"User-Agent": _UA, "Accept": "text/csv,*/*"}
    last = "no response"
    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, timeout=45)
        except requests.RequestException as e:
            last = e.__class__.__name__
        else:
            if r.status_code == 200 and r.text and "," in r.text:
                return r.text
            last = f"HTTP {r.status_code}"
        time.sleep(2 * (attempt + 1))
    log(f"  {ticker}: couldn't download holdings ({last})")
    return None


def parse_holdings(text, grade, source):
    """Rows from one iShares holdings CSV. Skips the metadata header lines."""
    lines = text.splitlines()
    # The holdings table starts at the row whose header names the bond columns.
    start = None
    for i, line in enumerate(lines):
        low = line.lower()
        if "name" in low and ("cusip" in low or "coupon" in low) and "sector" in low:
            start = i
            break
    if start is None:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    out = []
    for row in reader:
        # normalise keys (strip, drop the trailing "(%)" etc. so lookups are stable)
        r = { (k or "").strip(): (v or "").strip() for k, v in row.items() }

        def g(*names):
            for n in names:
                for key in r:
                    if key.lower() == n.lower():
                        return r[key]
            return ""

        asset = g("Asset Class")
        if asset and "bond" not in asset.lower() and "fixed income" not in asset.lower():
            continue   # skip cash, derivatives, FX legs
        cusip = g("CUSIP") or g("ISIN")
        name = g("Name")
        if not cusip or cusip in ("-", "N/A") or not name:
            continue
        coupon = to_float(g("Coupon (%)", "Coupon"))
        ytm = to_float(g("YTM (%)", "YTM", "Yield to Worst (%)"))
        price = to_float(g("Price"))
        mat = to_date(g("Maturity"))
        out.append({
            "id": cusip,
            "isin": g("ISIN") or None,
            "name": name,
            "issuer": issuer_of(name),
            "sector": g("Sector") or None,
            "grade": grade,
            "coupon": coupon,
            "maturity": mat,
            "price": price,
            "ytm": ytm,
            "duration": to_float(g("Duration", "Effective Duration")),
            "currency": g("Market Currency", "Currency") or "USD",
            "source_etf": source,
            "weight": to_float(g("Weight (%)", "Weight")),
            "updated_at": now_iso(),
        })
    return out


def upsert(db, rows):
    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        r = db.s.post(f"{db.url}/corporate_bonds?on_conflict=id", json=batch, timeout=120,
                      headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        if r.status_code >= 300:
            if "corporate_bonds" in r.text and ("does not exist" in r.text or "schema cache" in r.text):
                raise SystemExit("The corporate_bonds table isn't in the database yet. "
                                 "Run supabase/migration_corporate_bonds.sql once, then run this again.")
            raise SystemExit(f"Saving corporate bonds failed ({r.status_code}): {r.text[:300]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="Just one ETF, e.g. LQD or HYG.")
    args = ap.parse_args()

    db = Supabase()
    by_id = {}
    for ticker, pid, grade in ETFS:
        if args.only and ticker.upper() != args.only.upper():
            continue
        log(f"{ticker}: downloading holdings …")
        text = fetch_csv(ticker, pid)
        if not text:
            continue
        rows = parse_holdings(text, grade, ticker)
        log(f"  {ticker}: parsed {len(rows)} bonds.")
        for row in rows:
            by_id[row["id"]] = row     # de-dupe across ETFs by CUSIP
        time.sleep(1)

    rows = list(by_id.values())
    if not rows:
        log("Nothing was fetched — iShares may be unreachable this run. Leaving the "
            "existing rows in place; the next run will retry.")
        return
    upsert(db, rows)
    ig = sum(1 for r in rows if r["grade"] == "IG")
    log(f"Saved {len(rows)} corporate bonds ({ig} investment grade, {len(rows) - ig} high yield).")


if __name__ == "__main__":
    main()
