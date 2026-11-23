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
            if r.status_code == 200 and r.text and "," in r.text and not r.text.lstrip().startswith("<"):
                return r.text.replace("\x00", "")
            if r.status_code == 200:
                log(f"  {ticker}: iShares sent a web page, not the CSV")
                return None
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
        # Normalise. DictReader collects any columns beyond the header into a list
        # under the None key, and missing ones come back as None — handle both.
        r = {}
        for k, v in row.items():
            if k is None:
                continue
            if isinstance(v, list):
                v = v[0] if v else ""
            r[k.strip()] = (v or "").strip()

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


# ---------------------------------------------------------------------------
# SPDR fallback. BlackRock's holdings CSV link now returns an HTML page, so the
# iShares path above often gets nothing. State Street publishes every SPDR
# fund's daily holdings as a plain .xlsx — SPBO (investment-grade corporates)
# and SPHY (high yield) together are ~4,000 US company bonds. They carry no
# price or yield, so both are worked out here from market value, par, coupon
# and maturity.
# ---------------------------------------------------------------------------
SPDR = [("SPBO", "IG"), ("SPHY", "HY")]
SPDR_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{t}.xlsx"
_ISSUER_CUT = re.compile(r"\s+(?:SR|SENIOR|SECURED|SUBORD\w*|SUB|UNSECURED|COMPANY|GLOBAL|NOTES?|BONDS?|"
                         r"144A|REGS|MTN|FIXED|VAR|JR|\d{2}/\d{2})\b.*$")


def ytm_from_price(price, coupon, maturity_iso, today=None):
    """Yield to maturity (%) for a semi-annual bond; price per 100 of par. Bisection."""
    if not price or price <= 0 or coupon is None or not maturity_iso:
        return None
    today = today or dt.date.today()
    years = (dt.date.fromisoformat(maturity_iso) - today).days / 365.25
    if years <= 0.05:
        return None
    n = max(1, round(years * 2))
    c = coupon / 2

    def pv(y):
        r = y / 200
        if abs(r) < 1e-12:
            return c * n + 100
        return c * (1 - (1 + r) ** -n) / r + 100 * (1 + r) ** -n

    lo, hi = -5.0, 80.0
    if not (pv(hi) <= price <= pv(lo)):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if pv(mid) > price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


def fetch_spdr(ticker, grade):
    try:
        import openpyxl
    except ImportError:
        log("  openpyxl isn't installed — add it to scripts/requirements.txt.")
        return []
    try:
        r = requests.get(SPDR_URL.format(t=ticker.lower()), headers={"User-Agent": _UA}, timeout=60)
    except requests.RequestException as e:
        log(f"  {ticker}: {e.__class__.__name__}")
        return []
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        log(f"  {ticker}: HTTP {r.status_code}, not a spreadsheet")
        return []
    ws = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    head = next((i for i, row in enumerate(rows) if row and row[0] == "Name"), None)
    if head is None:
        return []
    cols = {str(v).strip(): i for i, v in enumerate(rows[head]) if v}
    get = lambda row, k: row[cols[k]] if k in cols and cols[k] < len(row) else None
    out = []
    for row in rows[head + 1:]:
        name = (get(row, "Name") or "").strip() if isinstance(get(row, "Name"), str) else ""
        isin = get(row, "Identifier")
        if not name or not isin or "MONEY MARKET" in name.upper() or "CASH" == name.upper():
            continue
        coupon = to_float(get(row, "Coupon"))
        par, mv = to_float(get(row, "Par Value")), to_float(get(row, "Market Value"))
        mat = to_date(str(get(row, "Maturity") or ""))
        price = round(mv / par * 100, 3) if par and mv else None
        if price is not None and not (5 < price < 250):
            price = None
        out.append({
            "id": str(isin), "isin": str(isin), "name": name,
            "issuer": (_ISSUER_CUT.sub("", name).strip() or name).title(),
            "sector": None, "grade": grade, "coupon": coupon, "maturity": mat,
            "price": price, "ytm": ytm_from_price(price, coupon, mat),
            "duration": None, "currency": get(row, "Local Currency") or "USD",
            "source_etf": ticker, "weight": to_float(get(row, "Weight")),
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

    # iShares came back empty for a grade → take that grade from SPDR instead.
    have = {r["grade"] for r in by_id.values()}
    for ticker, grade in SPDR:
        if grade in have or (args.only and args.only.upper() not in (ticker, "LQD" if grade == "IG" else "HYG")):
            continue
        log(f"{ticker}: iShares had no {grade} bonds — using SPDR's holdings …")
        rows = fetch_spdr(ticker, grade)
        log(f"  {ticker}: parsed {len(rows)} bonds.")
        for row in rows:
            by_id.setdefault(row["id"], row)
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
