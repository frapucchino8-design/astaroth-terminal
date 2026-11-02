"""Shared helpers for the nightly stock update: Supabase access and sector mapping."""
import os
import sys
import time
from datetime import datetime, timezone

import requests
from urllib3.util.retry import Retry


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

    def __init__(self, timeout=180):
        self.url = env("SUPABASE_URL").rstrip("/") + "/rest/v1"
        self.timeout = timeout          # 60s was too tight for large writes
        key = env("SUPABASE_SERVICE_ROLE_KEY")
        self.s = requests.Session()
        self.s.headers.update({"apikey": key, "Content-Type": "application/json"})
        if key.startswith("eyJ"):  # legacy service_role key (a JWT); new sb_secret_ keys go in apikey only
            self.s.headers["Authorization"] = f"Bearer {key}"
        # Reconnect rather than reusing a pooled socket the other end has quietly dropped.
        adapter = requests.adapters.HTTPAdapter(
            max_retries=Retry(total=3, connect=3, read=0, backoff_factor=2,
                              status_forcelist=[], allowed_methods=None),
            pool_connections=4, pool_maxsize=4)
        self.s.mount("https://", adapter)

    def _check(self, r, what):
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {what} failed ({r.status_code}): {r.text[:500]}")

    def _send(self, method, path, what, tries=5, timeout=None, **kw):
        """One request, retried on both server errors and network failures.

        The old version only retried on HTTP 5xx, so a read timeout — which raises out of
        requests rather than returning a response — ended the whole run. Timeouts are the
        common failure here: Supabase occasionally takes more than a minute to acknowledge a
        large write, especially when another job is writing at the same time.

        Retrying a write is safe because every write we make is idempotent: upsert merges on
        the symbol, and delete is by symbol too. If the server did apply a write we never
        heard back about, doing it again changes nothing.
        """
        timeout = timeout or self.timeout
        last = None
        for attempt in range(tries):
            try:
                r = self.s.request(method, f"{self.url}/{path}", timeout=timeout, **kw)
            except requests.RequestException as e:
                last = e
                wait = min(60, 5 * 2 ** attempt)
                log(f"  {what}: {type(e).__name__}, retrying in {wait}s ({attempt + 1}/{tries})")
                time.sleep(wait)
                continue
            if r.status_code < 500:
                self._check(r, what)
                return r
            last = RuntimeError(f"{r.status_code}: {r.text[:200]}")
            wait = min(60, 5 * 2 ** attempt)
            log(f"  {what}: server said {r.status_code}, retrying in {wait}s ({attempt + 1}/{tries})")
            time.sleep(wait)
        raise RuntimeError(f"Supabase {what} failed after {tries} tries: {last}")

    def upsert(self, rows, chunk=200):
        """Insert or update rows by symbol. Only the columns present in a row are written."""
        groups = {}
        for row in rows:  # PostgREST needs the same columns in every row of one request
            groups.setdefault(tuple(sorted(row)), []).append(row)
        for batch_rows in groups.values():
            for i in range(0, len(batch_rows), chunk):
                self._send(
                    "POST", "stocks?on_conflict=symbol", "upsert",
                    json=batch_rows[i:i + chunk],
                    headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                )

    def select(self, columns, filters=None, order=None):
        """Read every matching row, 1000 at a time."""
        out, start, step = [], 0, 1000
        params = {"select": columns, **(filters or {})}
        if order:
            params["order"] = order
        while True:
            r = self._send("GET", "stocks", "select", params=params,
                           headers={"Range-Unit": "items", "Range": f"{start}-{start + step - 1}"})
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
            self._send("DELETE", "stocks", "delete", params={"symbol": f"in.({quoted})"})


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
