"""Shared helpers for the nightly stock update: Supabase access and sector mapping."""
import os
import sys
import time
from datetime import datetime, timezone

import requests


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
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
        if key.startswith("eyJ"):  # legacy service_role key (a JWT); new sb_secret_ keys go in apikey only
            self.s.headers["Authorization"] = f"Bearer {key}"

    def _check(self, r, what):
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {what} failed ({r.status_code}): {r.text[:500]}")

    def upsert(self, rows, chunk=500):
        """Insert or update rows by symbol. Only the columns present in a row are written."""
        groups = {}
        for row in rows:  # PostgREST needs the same columns in every row of one request
            groups.setdefault(tuple(sorted(row)), []).append(row)
        for batch_rows in groups.values():
            for i in range(0, len(batch_rows), chunk):
                batch = batch_rows[i:i + chunk]
                for attempt in range(3):
                    r = self.s.post(
                        f"{self.url}/stocks?on_conflict=symbol",
                        json=batch,
                        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                        timeout=60,
                    )
                    if r.status_code < 500:
                        break
                    time.sleep(5 * (attempt + 1))
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

# Finnhub gives an industry ("Banking", "Semiconductors" ...). Matched by keyword, first hit wins.
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
