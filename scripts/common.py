"""Shared helpers for the nightly data update: Supabase access, sector mapping, FX rates."""
import os
import sys
import time
from datetime import datetime, timezone

import requests


def env(name, default=None):
    value = os.environ.get(name, "").strip()
    if not value:
        if default is not None:
            return default
        sys.exit(f"Missing {name}. Add it under GitHub → Settings → Secrets and variables → Actions.")
    return value


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def num(value, scale=1.0, digits=4):
    """Turn an API value into a float, or None if it is missing or not a number."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f * scale, digits)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class Supabase:
    """Minimal client for the PostgREST API, using the service_role key."""

    def __init__(self):
        self.url = env("SUPABASE_URL").rstrip("/") + "/rest/v1"
        key = env("SUPABASE_SERVICE_ROLE_KEY")
        self.s = requests.Session()
        self.s.headers.update({"apikey": key, "Content-Type": "application/json"})
        if key.startswith("eyJ"):
            self.s.headers["Authorization"] = f"Bearer {key}"

    def _check(self, r, what):
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {what} failed ({r.status_code}): {r.text[:500]}")

    def upsert(self, rows, chunk=100):
        """Insert or update rows by symbol. Only the columns present in a row are written."""
        groups = {}
        for row in rows:
            groups.setdefault(tuple(sorted(row)), []).append(row)
        for batch_rows in groups.values():
            for i in range(0, len(batch_rows), chunk):
                batch = batch_rows[i:i + chunk]
                r = None
                for attempt in range(5):
                    try:
                        r = self.s.post(
                            f"{self.url}/stocks?on_conflict=symbol",
                            json=batch,
                            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                            timeout=120,
                        )
                    except (requests.exceptions.ReadTimeout,
                            requests.exceptions.ConnectionError) as e:
                        log(f"  upsert network error ({e.__class__.__name__}), retry {attempt + 1}/5")
                        time.sleep(5 * (attempt + 1))
                        continue
                    if r.status_code < 500:
                        break
                    time.sleep(5 * (attempt + 1))
                if r is None:
                    raise RuntimeError("Supabase upsert failed: all 5 attempts timed out or lost the connection.")
                self._check(r, "upsert")

    def select(self, columns, filters=None, order=None):
        """Read every matching row, 1000 at a time."""
        out, start, step = [], 0, 1000
        params = {"select": columns, **(filters or {})}
        if order:
            params["order"] = order
        while True:
            r = self.s.get(
                f"{self.url}/stocks",
                params=params,
                headers={"Range-Unit": "items", "Range": f"{start}-{start + step - 1}"},
                timeout=60,
            )
            self._check(r, "select")
            page = r.json()
            out.extend(page)
            if len(page) < step:
                return out
            start += step

    def delete_symbols(self, symbols, chunk=100):
        symbols = list(symbols)
        for i in range(0, len(symbols), chunk):
            part = symbols[i:i + chunk]
            quoted = ",".join('"' + s.replace('"', '') + '"' for s in part)
            r = self.s.delete(f"{self.url}/stocks", params={"symbol": f"in.({quoted})"}, timeout=60)
            self._check(r, "delete")

    def delete_asset_class(self, asset_class, keep):
        """Remove rows of this asset_class whose symbol isn't in `keep` — housekeeping."""
        keep = set(keep)
        rows = self.select("symbol", {"asset_class": f"eq.{asset_class}"})
        gone = [r["symbol"] for r in rows if r["symbol"] not in keep]
        if gone:
            self.delete_symbols(gone)
        return len(gone)


# ---------------------------------------------------------------------------
# Sectors: both providers are mapped onto the screener's 11 sectors.
# ---------------------------------------------------------------------------
YAHOO_SECTORS = {
    "Technology": "Technology",
    "Communication Services": "Communication",
    "Consumer Cyclical": "Consumer Disc.",
    "Consumer Defensive": "Consumer Staples",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Energy": "Energy",
    "Industrials": "Industrials",
    "Basic Materials": "Materials",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
}

FINNHUB_RULES = [
    ("Real Estate", ["real estate", "reit"]),
    ("Utilities", ["utilit"]),
    ("Energy", ["energy", "oil", "gas"]),
    ("Health Care", ["pharma", "biotech", "health", "life sciences", "medical"]),
    ("Financials", ["bank", "financ", "insurance", "capital markets"]),
    ("Communication", ["media", "telecom", "entertainment"]),
    ("Technology", ["semiconductor", "technology", "communications", "software", "electronic"]),
    ("Consumer Staples", ["beverage", "food", "tobacco", "consumer products", "household", "personal"]),
    ("Consumer Disc.", ["retail", "automobile", "auto ", "hotel", "restaurant", "leisure", "textile",
                        "apparel", "luxury", "consumer services", "distributors", "homebuild"]),
    ("Materials", ["chemical", "metal", "mining", "packaging", "paper", "forest", "materials"]),
    ("Industrials", ["aerospace", "defense", "airline", "machinery", "industrial", "logistic", "transport",
                     "road", "rail", "marine", "commercial services", "professional services",
                     "construction", "building", "trading companies", "electrical equipment"]),
]


def finnhub_sector(industry):
    text = (industry or "").lower()
    if not text or text == "n/a":
        return None
    for sector, words in FINNHUB_RULES:
        if any(w in text for w in words):
            return sector
    return "Other"


# ---------------------------------------------------------------------------
# FX rates (quoted vs USD). Fed by update_fx.py but cached here for other scripts.
# ---------------------------------------------------------------------------
_FX_CACHE = {"USD": 1.0}


def to_usd(currency, yf_fallback=True):
    """Current rate from a currency to USD. Cached in-process."""
    if not currency:
        return None
    if currency in _FX_CACHE:
        return _FX_CACHE[currency]
    # Try ECB first (no API key, open data)
    try:
        r = requests.get(
            f"https://api.frankfurter.app/latest?from={currency}&to=USD",
            timeout=10,
        )
        if r.ok:
            rate = r.json().get("rates", {}).get("USD")
            if rate:
                _FX_CACHE[currency] = float(rate)
                return _FX_CACHE[currency]
    except requests.RequestException:
        pass
    if yf_fallback:
        try:
            import yfinance as yf
            hist = yf.Ticker(f"{currency}USD=X").history(period="5d")
            _FX_CACHE[currency] = float(hist["Close"].dropna().iloc[-1])
            return _FX_CACHE[currency]
        except Exception:
            pass
    _FX_CACHE[currency] = None
    return None
