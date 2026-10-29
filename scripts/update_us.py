"""Nightly update for every US-listed stock, using the Finnhub free plan.

1. Gets the full list of US common stocks, ADRs and REITs on NYSE, Nasdaq, NYSE American,
   NYSE Arca and Cboe BZX.
2. Updates today's price for all of them (about 1 call per stock).
3. With the time left, refreshes fundamentals (profile + metrics, 2 calls per stock),
   oldest first, so every stock gets refreshed every night or two.

The free plan allows about 60 calls a minute, so this stays at roughly 54.
"""
import argparse
import time

import requests

from common import Supabase, env, finnhub_sector, log, now_iso, num

API = "https://finnhub.io/api/v1"
EXCHANGES = {
    "XNYS": "NYSE",
    "XNAS": "NASDAQ",
    "XASE": "NYSE American",
    "ARCX": "NYSE Arca",
    "BATS": "Cboe BZX",
}
TYPES = {"Common Stock", "ADR", "REIT"}


class Finnhub:
    def __init__(self, key, per_minute=54):
        self.s = requests.Session()
        self.s.headers["X-Finnhub-Token"] = key
        self.gap = 60.0 / per_minute
        self.last = 0.0
        self.calls = 0

    def get(self, path, **params):
        for attempt in range(4):
            wait = self.gap - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            try:
                r = self.s.get(API + path, params=params, timeout=30)
            except requests.RequestException as e:
                log(f"  network error on {path} {params}: {e}")
                time.sleep(5)
                continue
            if r.status_code == 429:
                log("  rate limited, waiting 60s")
                time.sleep(60)
                continue
            if r.status_code in (401, 403):
                if path == "/stock/symbol":
                    raise SystemExit(f"Finnhub refused the key ({r.status_code}). Check the FINNHUB_API_KEY secret.")
                return None  # endpoint not included in the free plan for this symbol
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            try:
                return r.json()
            except ValueError:
                return None
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-minutes", type=float, default=300,
                    help="Stop starting new work after this many minutes.")
    ap.add_argument("--limit", type=int, default=0, help="Only process this many stocks (for testing).")
    args = ap.parse_args()
    deadline = time.monotonic() + args.budget_minutes * 60

    fh = Finnhub(env("FINNHUB_API_KEY"))
    db = Supabase()

    # 1. The list of US stocks -------------------------------------------------------
    listing = fh.get("/stock/symbol", exchange="US") or []
    stocks = {x["symbol"]: x for x in listing
              if x.get("type") in TYPES and x.get("mic") in EXCHANGES and x.get("symbol")}
    log(f"Finnhub lists {len(listing)} US symbols; {len(stocks)} are stocks on the main exchanges.")
    if len(stocks) < 2000:
        raise SystemExit("The symbol list looks incomplete, so nothing was changed. Try again later.")

    symbols = sorted(stocks)
    if args.limit:
        symbols = symbols[:args.limit]

    existing = {r["symbol"] for r in db.select("symbol", {"region": "eq.US"})}
    new_rows = [{
        "symbol": s,
        "name": (stocks[s].get("description") or s).title(),
        "exchange": EXCHANGES[stocks[s]["mic"]],
        "region": "US",
        "currency": stocks[s].get("currency") or "USD",
    } for s in symbols if s not in existing]
    if new_rows:
        db.upsert(new_rows)
        log(f"Added {len(new_rows)} new stocks.")

    if not args.limit:
        gone = existing - set(symbols)
        if gone:
            db.delete_symbols(gone)
            log(f"Removed {len(gone)} stocks that are no longer listed.")

    # 2. Prices ----------------------------------------------------------------------
    log(f"Updating prices for {len(symbols)} stocks …")
    batch, done = [], 0
    for i, sym in enumerate(symbols, 1):
        if time.monotonic() > deadline:
            log("Time budget used up during prices.")
            break
        q = fh.get("/quote", symbol=sym)
        if q and num(q.get("c")):
            batch.append({
                "symbol": sym,
                "region": "US",  # every upsert row carries region (NOT NULL), even when only updating
                "price": num(q["c"]),
                "change_pct": num(q.get("dp"), digits=3),
                "quote_updated_at": now_iso(),
            })
        if len(batch) >= 250:
            db.upsert(batch)
            done += len(batch)
            batch = []
            log(f"  prices: {i}/{len(symbols)}")
    if batch:
        db.upsert(batch)
        done += len(batch)
    log(f"Prices updated for {done} stocks.")

    # 3. Fundamentals, oldest first ----------------------------------------------------
    order = [r["symbol"] for r in db.select(
        "symbol", {"region": "eq.US"}, order="fundamentals_updated_at.asc.nullsfirst")]
    wanted = set(symbols)
    order = [s for s in order if s in wanted]
    log(f"Refreshing fundamentals, oldest first ({len(order)} candidates) …")
    batch, done = [], 0
    for sym in order:
        if time.monotonic() > deadline - 30:
            log("Time budget used up; the rest continue next run.")
            break
        prof = fh.get("/stock/profile2", symbol=sym) or {}
        met = (fh.get("/stock/metric", symbol=sym, metric="all") or {}).get("metric") or {}
        pe = num(met.get("peTTM") or met.get("peBasicExclExtraTTM"), digits=2)
        cap_m = prof.get("marketCapitalization") if prof.get("currency") in (None, "USD") else None
        row = {
            "symbol": sym,
            "region": "US",
            "name": prof.get("name") or None,
            "industry": prof.get("finnhubIndustry") or None,
            "sector": finnhub_sector(prof.get("finnhubIndustry")),
            "market_cap_usd_b": num(cap_m or met.get("marketCapitalization"), scale=0.001, digits=3),
            "pe": pe if pe and pe > 0 else None,
            "dividend_yield": num(met.get("dividendYieldIndicatedAnnual") or met.get("currentDividendYieldTTM"), digits=3),
            "return_1y": num(met.get("52WeekPriceReturnDaily"), digits=2),
            "beta": num(met.get("beta"), digits=3),
            "revenue_growth": num(met.get("revenueGrowthTTMYoy"), digits=2),
            "net_margin": num(met.get("netProfitMarginTTM"), digits=2),
            "high_52w": num(met.get("52WeekHigh")),
            "low_52w": num(met.get("52WeekLow")),
            "fundamentals_updated_at": now_iso(),
        }
        if row["name"] is None:
            row.pop("name")  # keep the name from the symbol list
        batch.append(row)
        if len(batch) >= 100:
            db.upsert(batch)
            done += len(batch)
            batch = []
            log(f"  fundamentals: {done}")
    if batch:
        db.upsert(batch)
        done += len(batch)
    log(f"Fundamentals refreshed for {done} stocks. Finnhub calls used: {fh.calls}.")


if __name__ == "__main__":
    main()
