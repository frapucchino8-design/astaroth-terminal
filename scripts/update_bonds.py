"""Sovereign bond yields, for the Bonds section under Public.

Each maturity is one row in the stocks table with asset_class 'bond':

    symbol      US-10Y            (country-maturity; also the price_history key)
    name        United States 10Y
    region      US                (the site groups the curve by this)
    sector      10Y               (the maturity label)
    price       the yield, in percent
    change_pct  the day's move, in percentage points (0.05 = up 5 basis points)

No migration is needed: 'bond' is already in the asset_class constraint that
migration_asset_classes.sql installs, and every column above already exists.

Two sources:

  Treasury  the official US Daily Par Yield Curve — the whole curve (1M … 30Y)
            in one request, no key. The primary US source.
  Yahoo     four US index symbols. Only a fallback, for the rare case the
            Treasury feed is unreachable.
  FRED      10-year benchmark yields for the other countries (Switzerland,
            Germany, the UK, Japan, France, Italy, Canada and more), from the
            Fed's keyless CSV download — no API key needed.

It also writes weekly closes into price_history, so the "yield over time" chart
in the site has something to draw. That needs migration_history.sql, which the
price-history job already uses; without it the yields still update and only the
history is skipped.

    python update_bonds.py
    python update_bonds.py --no-history
    python update_bonds.py --years 6

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, like the other scripts.
"""
import argparse
import csv
import datetime as dt
import io
import time
import xml.etree.ElementTree as ET

import requests

from common import Supabase, log, now_iso, num

# ---------------------------------------------------------------------------
# Yahoo: the four US Treasury yield indices. Yahoo quotes these as the yield
# itself (4.12 means 4.12%), which is exactly what we store.
# ---------------------------------------------------------------------------
YAHOO = [
    ("^IRX", "US", "United States", "USD", "3M", 0.25),
    ("^FVX", "US", "United States", "USD", "5Y", 5),
    ("^TNX", "US", "United States", "USD", "10Y", 10),
    ("^TYX", "US", "United States", "USD", "30Y", 30),
]

# ---------------------------------------------------------------------------
# FRED: daily constant-maturity series. The US set is the full curve; the other
# countries publish a 10-year benchmark (monthly for some, which is fine for a
# curve chart). Add a line here and it appears in the site on the next run.
# ---------------------------------------------------------------------------
FRED = [
    # series id,   region,        country name,     currency, label, years
    ("DGS1MO",     "US",          "United States",  "USD", "1M",  1 / 12),
    ("DGS3MO",     "US",          "United States",  "USD", "3M",  0.25),
    ("DGS6MO",     "US",          "United States",  "USD", "6M",  0.5),
    ("DGS1",       "US",          "United States",  "USD", "1Y",  1),
    ("DGS2",       "US",          "United States",  "USD", "2Y",  2),
    ("DGS3",       "US",          "United States",  "USD", "3Y",  3),
    ("DGS5",       "US",          "United States",  "USD", "5Y",  5),
    ("DGS7",       "US",          "United States",  "USD", "7Y",  7),
    ("DGS10",      "US",          "United States",  "USD", "10Y", 10),
    ("DGS20",      "US",          "United States",  "USD", "20Y", 20),
    ("DGS30",      "US",          "United States",  "USD", "30Y", 30),
    ("IRLTLT01CHM156N", "Switzerland", "Switzerland", "CHF", "10Y", 10),
    ("IRLTLT01DEM156N", "Germany",     "Germany",     "EUR", "10Y", 10),
    ("IRLTLT01GBM156N", "UK",          "United Kingdom", "GBP", "10Y", 10),
    ("IRLTLT01JPM156N", "Japan",       "Japan",       "JPY", "10Y", 10),
    ("IRLTLT01FRM156N", "France",      "France",      "EUR", "10Y", 10),
    ("IRLTLT01ITM156N", "Italy",       "Italy",       "EUR", "10Y", 10),
    ("IRLTLT01CAM156N", "Canada",      "Canada",      "CAD", "10Y", 10),
]

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# ---------------------------------------------------------------------------
# US Treasury: the official Daily Par Yield Curve. One request returns the whole
# curve — every maturity, every business day — with no API key. This is the
# primary US source; Yahoo's four index symbols are only a fallback, because
# fetching maturities one symbol at a time leaves gaps when some calls fail.
# ---------------------------------------------------------------------------
TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/"
                "interest-rates/pages/xml")
TREASURY_FIELDS = [
    ("BC_1MONTH", "1M", 1 / 12), ("BC_2MONTH", "2M", 2 / 12), ("BC_3MONTH", "3M", 0.25),
    ("BC_4MONTH", "4M", 4 / 12), ("BC_6MONTH", "6M", 0.5),   ("BC_1YEAR", "1Y", 1),
    ("BC_2YEAR", "2Y", 2),       ("BC_3YEAR", "3Y", 3),       ("BC_5YEAR", "5Y", 5),
    ("BC_7YEAR", "7Y", 7),       ("BC_10YEAR", "10Y", 10),    ("BC_20YEAR", "20Y", 20),
    ("BC_30YEAR", "30Y", 30),
]


def symbol_for(region, label):
    return f"{region}-{label}"


def row_for(region, country, currency, label, yield_pct, change_pp, hi=None, lo=None):
    row = {
        "symbol": symbol_for(region, label),
        "name": f"{country} {label}",
        "asset_class": "bond",
        "region": region,
        "sector": label,
        "currency": currency,
        "exchange": "Government",
        "price": round(float(yield_pct), 4),
        "quote_updated_at": now_iso(),
        "fundamentals_updated_at": now_iso(),
    }
    if change_pp is not None:
        row["change_pct"] = round(float(change_pp), 4)
    if hi is not None:
        row["high_52w"] = round(float(hi), 4)
    if lo is not None:
        row["low_52w"] = round(float(lo), 4)
    return row


# A browser-like User-Agent — both sources below serve CI runners more happily with one.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------------------
# US Treasury
# ---------------------------------------------------------------------------
def _treasury_year(year, session):
    """{date: {label: yield}} for one calendar year of the par yield curve."""
    params = {"data": "daily_treasury_yield_curve", "field_tdr_date_value": str(year)}
    try:
        r = session.get(TREASURY_URL, params=params, headers={"User-Agent": _UA}, timeout=45)
    except requests.RequestException as e:
        log(f"  Treasury {year}: {e.__class__.__name__}")
        return {}
    if r.status_code != 200:
        log(f"  Treasury {year}: HTTP {r.status_code}")
        return {}
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log(f"  Treasury {year}: parse {e.__class__.__name__}")
        return {}
    local = lambda tag: tag.rsplit("}", 1)[-1]          # ignore the Atom/OData namespaces
    by_date = {}
    for props in root.iter():
        if local(props.tag) != "properties":
            continue
        fields = {local(c.tag): (c.text or "").strip() for c in props}
        ds = fields.get("NEW_DATE") or fields.get("Date") or ""
        try:
            d = dt.date.fromisoformat(ds[:10])
        except ValueError:
            continue
        row = {}
        for col, label, _yrs in TREASURY_FIELDS:
            v = fields.get(col, "")
            if v in ("", "N/A"):
                continue
            try:
                row[label] = round(float(v), 4)
            except ValueError:
                continue
        if row:
            by_date[d] = row
    return by_date


def from_treasury(years, want_history):
    """[(row, [(date, yield)])] for the whole US curve, from the Treasury feed."""
    end = dt.date.today()
    start_year = end.year - int(max(1, years)) if want_history else end.year
    merged = {}
    with requests.Session() as session:
        for y in range(start_year, end.year + 1):
            merged.update(_treasury_year(y, session))
            time.sleep(0.3)
        # Guarantee a second date for the day-change, even in the first days of January.
        if sum(1 for d in merged if d.year == end.year) < 2:
            merged.update(_treasury_year(end.year - 1, session))
    if not merged:
        return []
    dates = sorted(merged)
    out = []
    for col, label, _yrs in TREASURY_FIELDS:
        points = [(d, merged[d][label]) for d in dates if label in merged[d]]
        if not points:
            continue
        latest = points[-1][1]
        prev = points[-2][1] if len(points) > 1 else None
        year_ago = [v for d, v in points if (end - d).days <= 366]
        out.append((
            row_for("US", "United States", "USD", label, latest,
                    None if prev is None else latest - prev,
                    max(year_ago) if year_ago else None,
                    min(year_ago) if year_ago else None),
            points,
        ))
        log(f"  Treasury → US-{label}: {latest:.2f}%")
    return out


# ---------------------------------------------------------------------------
# Yahoo (fallback for the US curve)
# ---------------------------------------------------------------------------
# yfinance's own cookie/crumb handshake is what fails on CI runners (empty frames,
# 401s), so we skip it and read the public chart endpoint directly — no crumb needed.
_YH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
}
_YH_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


def _yahoo_chart(sym, years, session):
    """Weekly closes for one Yahoo symbol via the public chart endpoint.

    Returns (points, live) where points is [(date, value)] oldest-first and live
    is the latest quote from meta, or (None, None) if every attempt failed.
    """
    path = f"/v8/finance/chart/{sym}?range={int(years)}y&interval=1wk"
    last_err = "no response"
    for attempt in range(3):
        host = _YH_HOSTS[attempt % len(_YH_HOSTS)]
        try:
            r = session.get(f"https://{host}{path}", headers=_YH_HEADERS, timeout=30)
        except requests.RequestException as e:
            last_err = e.__class__.__name__
        else:
            if r.status_code == 200:
                try:
                    res = (r.json().get("chart") or {}).get("result") or []
                    if not res:
                        last_err = "empty result"
                    else:
                        res = res[0]
                        ts = res.get("timestamp") or []
                        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
                        pts = []
                        for t, v in zip(ts, closes):
                            if v is None:
                                continue
                            try:
                                fv = float(v)
                            except (TypeError, ValueError):
                                continue
                            if fv == fv and fv > 0:
                                pts.append((dt.datetime.utcfromtimestamp(t).date(), round(fv, 4)))
                        live = num((res.get("meta") or {}).get("regularMarketPrice"))
                        if pts:
                            return pts, (live if live and live > 0 else None)
                        last_err = "no usable points"
                except (ValueError, KeyError, TypeError) as e:
                    last_err = f"parse {e.__class__.__name__}"
            else:
                last_err = f"HTTP {r.status_code}"
        time.sleep(1.5 * (attempt + 1))
    log(f"  {sym}: {last_err}")
    return None, None


def from_yahoo(years):
    """[(row, [(date, yield)])] for the US curve points Yahoo carries."""
    out = []
    with requests.Session() as session:
        for sym, region, country, currency, label, _yrs in YAHOO:
            points, live = _yahoo_chart(sym, years, session)
            if not points:
                continue

            latest = points[-1][1]
            prev = points[-2][1] if len(points) > 1 else None
            # The live quote is fresher than the weekly bar, when it is there.
            if live is not None:
                prev = latest if abs(live - latest) > 1e-9 else prev
                latest = round(float(live), 4)

            year_ago = [v for d, v in points if (dt.date.today() - d).days <= 366]
            out.append((
                row_for(region, country, currency, label, latest,
                        None if prev is None else latest - prev,
                        max(year_ago) if year_ago else None,
                        min(year_ago) if year_ago else None),
                points,
            ))
            log(f"  {sym} → {symbol_for(region, label)}: {latest:.2f}%")
            time.sleep(0.4)
    return out


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------
def fred_series(series_id, start):
    """Observations for one FRED series via the keyless CSV download — no API key."""
    params = {"id": series_id, "cosd": start.isoformat()}
    try:
        r = requests.get(FRED_CSV_URL, params=params, headers={"User-Agent": _UA}, timeout=45)
    except requests.RequestException as e:
        log(f"  {series_id}: {e.__class__.__name__}")
        return []
    if r.status_code >= 300:
        log(f"  {series_id}: HTTP {r.status_code}")
        return []
    out = []
    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    for row in rows[1:]:                     # first row is the header
        if len(row) < 2:
            continue
        ds, val = row[0].strip(), row[1].strip()
        if val in ("", ".", "NA", "n/a"):
            continue
        try:
            out.append((dt.date.fromisoformat(ds), round(float(val), 4)))
        except (ValueError, TypeError):
            continue
    return out


def from_fred(years):
    start = dt.date.today() - dt.timedelta(days=int(years * 365.25))
    out = []
    for series_id, region, country, currency, label, _yrs in FRED:
        points = fred_series(series_id, start)
        if len(points) < 2:
            continue
        latest, prev = points[-1][1], points[-2][1]
        year_ago = [v for d, v in points if (dt.date.today() - d).days <= 366]
        out.append((
            row_for(region, country, currency, label, latest, latest - prev,
                    max(year_ago) if year_ago else None,
                    min(year_ago) if year_ago else None),
            points,
        ))
        log(f"  {series_id} → {symbol_for(region, label)}: {latest:.2f}%")
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def write_history(db, series):
    """Weekly points into price_history, the same table the charts already read."""
    rows = []
    for symbol, points in series:
        seen = set()
        for day, value in points:
            # one point a week is plenty for a yield chart
            key = (day.isocalendar().year, day.isocalendar().week)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"symbol": symbol, "d": day.isoformat(), "close": value})
    if not rows:
        return 0
    saved = 0
    for i in range(0, len(rows), 1000):
        batch = rows[i:i + 1000]
        try:
            r = db.s.post(f"{db.url}/price_history?on_conflict=symbol,d", json=batch, timeout=120,
                          headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        except requests.RequestException as e:
            log(f"  history: {e.__class__.__name__}, skipping the rest")
            return saved
        if r.status_code >= 300:
            if "price_history" in r.text and ("does not exist" in r.text or "schema cache" in r.text):
                log("  price_history isn't in the database yet — run supabase/migration_history.sql "
                    "to get the yield-over-time charts. Yields themselves are saved.")
                return saved
            log(f"  history upsert failed ({r.status_code}): {r.text[:200]}")
            return saved
        saved += len(batch)
    return saved


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=float, default=5, help="How far back to fetch history (default 5).")
    ap.add_argument("--no-history", action="store_true", help="Yields only; don't touch price_history.")
    ap.add_argument("--fred-only", action="store_true", help="Skip the US sources (Treasury / Yahoo).")
    args = ap.parse_args()

    db = Supabase()
    started = time.monotonic()

    collected = []            # [(row, points)]
    if not args.fred_only:
        log("US Treasury: the official par yield curve …")
        us = from_treasury(args.years, not args.no_history)
        if not us:
            log("Treasury feed unreachable — falling back to Yahoo's index points.")
            us = from_yahoo(args.years)
        collected += us

    log("FRED: curves for the other countries …")
    collected += from_fred(args.years)

    if not collected:
        # A transient outage (rate-limited runner, 403s) shouldn't redline the whole
        # pipeline — the next scheduled run picks it up. Exit clean, just loudly.
        log("Nothing was fetched this run — every source was unreachable. "
            "Leaving existing yields in place; the next run will retry.")
        return

    # The first source to carry a symbol wins: Treasury is authoritative for the US
    # curve, and FRED only adds the countries Treasury doesn't cover.
    by_symbol = {}
    for row, points in collected:
        by_symbol.setdefault(row["symbol"], (row, points))

    rows = [row for row, _ in by_symbol.values()]
    db.upsert(rows)
    log(f"Saved {len(rows)} bond yields across {len({r['region'] for r in rows})} countries.")

    if not args.no_history:
        n = write_history(db, [(sym, pts) for sym, (_row, pts) in by_symbol.items()])
        if n:
            log(f"Saved {n} history points.")

    log(f"Done in {time.monotonic() - started:.0f}s. The Bonds section under Public picks this up on the next page load.")


if __name__ == "__main__":
    main()
