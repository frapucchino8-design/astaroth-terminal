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

  Yahoo   the US Treasury curve, through four index symbols. No key, always on.
  FRED    full curves for several countries, and a long history. Free, but it
          needs an API key: sign up at fred.stlouisfed.org/docs/api/api_key.html
          and put it in the FRED_API_KEY GitHub secret. Without the key this
          script quietly skips everything but the US Yahoo points.

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
import datetime as dt
import os
import time

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

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


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


# ---------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------
# A browser-like header set. yfinance's own cookie/crumb handshake is what fails
# on CI runners (empty frames, 401s), so we skip it and read the public chart
# endpoint directly — it needs no crumb, just a User-Agent.
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
def fred_series(key, series_id, start):
    params = {"series_id": series_id, "api_key": key, "file_type": "json",
              "observation_start": start.isoformat()}
    try:
        r = requests.get(FRED_URL, params=params, timeout=45)
    except requests.RequestException as e:
        log(f"  {series_id}: {e.__class__.__name__}")
        return []
    if r.status_code == 400 and "api_key" in r.text:
        raise SystemExit("FRED rejected the API key. Check the FRED_API_KEY secret.")
    if r.status_code >= 300:
        log(f"  {series_id}: HTTP {r.status_code}")
        return []
    out = []
    for o in r.json().get("observations", []):
        if o.get("value") in (None, ".", ""):
            continue
        try:
            out.append((dt.date.fromisoformat(o["date"]), round(float(o["value"]), 4)))
        except (ValueError, TypeError):
            continue
    return out


def from_fred(key, years):
    start = dt.date.today() - dt.timedelta(days=int(years * 365.25))
    out = []
    for series_id, region, country, currency, label, _yrs in FRED:
        points = fred_series(key, series_id, start)
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
    ap.add_argument("--fred-only", action="store_true", help="Skip Yahoo.")
    args = ap.parse_args()

    db = Supabase()
    started = time.monotonic()

    collected = []            # [(row, points)]
    if not args.fred_only:
        log("Yahoo: the US Treasury curve …")
        collected += from_yahoo(args.years)

    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        log("FRED: full curves …")
        collected += from_fred(key, args.years)
    else:
        log("No FRED_API_KEY set, so only the US points from Yahoo are updated. "
            "A free key from fred.stlouisfed.org adds Switzerland, Germany, the UK, Japan and more.")

    if not collected:
        # A transient Yahoo outage (rate-limited runner, 403s) shouldn't redline the
        # whole pipeline — the next scheduled run picks it up. Exit clean, just loudly.
        log("Nothing was fetched this run — every source was unreachable. "
            "Leaving existing yields in place; the next run will retry.")
        return

    # FRED wins where both carry the same maturity: it is the official source.
    by_symbol = {}
    for row, points in collected:
        by_symbol[row["symbol"]] = (row, points)

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
