"""Nightly update for every US-listed stock and REIT, using the Finnhub free plan.

1. Gets the full list of US common stocks, ADRs and REITs on NYSE, Nasdaq, NYSE American,
   NYSE Arca and Cboe BZX.
2. Updates today's price for all of them (about 1 call per stock).
3. With the time left, refreshes fundamentals (profile + metrics, 2 calls per stock),
   oldest first, so every stock gets refreshed every night or two.

The free plan allows about 60 calls a minute, so this stays at roughly 54.
Each row carries asset_class = "stock" or "reit".

A stock is removed only when Finnhub has stopped listing the symbol entirely,
and never more than 2% of the table in one run. See the delete step in main().
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

# Delisting rails. A real night of delistings is a handful of symbols; these
# stop a bad listing from emptying the table. See the delete step in main().
MAX_DROP_FRACTION = 0.02        # at most 2% of the US rows in one run
MIN_DROP_ALLOWED = 50           # ... but always allow a small clear-out


def asset_class_of(type_, industry):
    """REIT by listing type OR by Finnhub industry; everything else is a stock."""
    if type_ == "REIT":
        return "reit"
    if industry and "real estate" in industry.lower():
        return "reit"
    if industry and "reit" in industry.lower():
        return "reit"
    return "stock"


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
                return None
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
    ap.add_argument("--no-delete", action="store_true",
                    help="Never remove anything, whatever the listing says.")
    ap.add_argument("--check", action="store_true",
                    help="Print what Finnhub's listing looks like and stop. Touches nothing.")
    args = ap.parse_args()
    deadline = time.monotonic() + args.budget_minutes * 60

    fh = Finnhub(env("FINNHUB_API_KEY"))
    db = None if args.check else Supabase()

    # 1. The list of US stocks ------------------------------------------------
    listing = fh.get("/stock/symbol", exchange="US") or []
    # Every symbol Finnhub still lists, whatever it calls it. THIS is the set
    # that decides whether something has been delisted — see the delete below.
    listed_all = {x.get("symbol") for x in listing if x.get("symbol")}
    stocks = {x["symbol"]: x for x in listing
              if x.get("type") in TYPES and x.get("mic") in EXCHANGES and x.get("symbol")
              and not x["symbol"].endswith(".SW")}
    log(f"Finnhub lists {len(listing)} US symbols; {len(stocks)} are stocks on the main exchanges.")
    types_seen = {}
    for x in listing:
        types_seen[x.get("type") or "(blank)"] = types_seen.get(x.get("type") or "(blank)", 0) + 1
    log("  listing types: " + ", ".join(f"{k}={v}" for k, v in
                                        sorted(types_seen.items(), key=lambda kv: -kv[1])[:8]))
    if args.check:
        mics = {}
        for x in listing:
            mics[x.get("mic") or "(blank)"] = mics.get(x.get("mic") or "(blank)", 0) + 1
        log("")
        log("Exchanges (MIC) in the listing, most common first:")
        for k, v in sorted(mics.items(), key=lambda kv: -kv[1])[:15]:
            log(f"    {k:<10} {v:>7}   {'KEPT as ' + EXCHANGES[k] if k in EXCHANGES else 'ignored'}")
        log("")
        log("Types in the listing, most common first:")
        for k, v in sorted(types_seen.items(), key=lambda kv: -kv[1])[:15]:
            log(f"    {k:<24} {v:>7}   {'KEPT' if k in TYPES else 'ignored'}")
        log("")
        log("A few well-known symbols, exactly as Finnhub describes them:")
        by_sym = {x.get("symbol"): x for x in listing}
        for sym in ("AAPL", "MSFT", "JPM", "KO", "O", "PLD", "BRK.B", "F", "T"):
            x = by_sym.get(sym)
            log(f"    {sym:<8} " + (f"type={x.get('type')!r:<18} mic={x.get('mic')!r}" if x else "NOT IN THE LISTING AT ALL"))
        log("")
        log(f"VERDICT: {len(listing)} listed, {len(stocks)} pass the type/exchange filter.")
        log("  About 10,000 passing  -> the listing is healthy; a normal run restores the missing rows.")
        log("  Far fewer than that   -> the type or MIC labels above have changed; TYPES / EXCHANGES")
        log("                           at the top of this file need the new spelling.")
        return

    if len(listing) < 5000 or len(stocks) < 5000:
        raise SystemExit(
            f"The symbol list looks incomplete ({len(listing)} listed, {len(stocks)} usable; "
            "expected roughly 11,000 and 10,000). Nothing was changed — try again later. "
            "If the types above have changed names, TYPES at the top of this file needs updating.")

    symbols = sorted(stocks)
    if args.limit:
        symbols = symbols[:args.limit]

    # Housekeeping (adding newly listed stocks, dropping delisted ones) is wrapped
    # in try/except so a Supabase blip here never blocks prices and fundamentals.
    try:
        # NB: include asset_class in the select so legacy rows with asset_class = NULL
        # are also treated as existing — otherwise every nightly run would try to
        # re-insert all 11k symbols as "new".
        existing_rows = db.select("symbol,asset_class", {"region": "eq.US"})
        existing = {r["symbol"] for r in existing_rows
                    if r.get("asset_class") in (None, "stock", "reit")}

        new_rows = [{
            "symbol": s,
            "name": (stocks[s].get("description") or s).title(),
            "exchange": EXCHANGES[stocks[s]["mic"]],
            "region": "US",
            "asset_class": asset_class_of(stocks[s].get("type"), None),
            "currency": stocks[s].get("currency") or "USD",
        } for s in symbols if s not in existing]
        if new_rows:
            db.upsert(new_rows)
            log(f"Added {len(new_rows)} new stocks.")
            # A row with no price is invisible on the website, so price the
            # new ones first. After a big restore that is the difference
            # between seeing them tonight and seeing them in three days.
            fresh = {r["symbol"] for r in new_rows}
            symbols = [s2 for s2 in symbols if s2 in fresh] + [s2 for s2 in symbols if s2 not in fresh]

        # ------------------------------------------------------------------
        # Delisting. Two rails, because this step once deleted about 5,000
        # live stocks in a single run:
        #
        #   1. A row goes only when Finnhub has stopped listing the symbol
        #      ALTOGETHER. It used to go whenever it failed the type/exchange
        #      filter above — so the day Finnhub relabelled a type or moved a
        #      venue's MIC, every affected stock was deleted even though it was
        #      still trading and still in the listing.
        #   2. Never more than MAX_DROP_FRACTION of the table in one run. A
        #      real night of delistings is a handful of symbols; anything
        #      bigger is the data source misbehaving, so it stops and says so.
        # ------------------------------------------------------------------
        gone = existing - listed_all
        if args.limit or args.no_delete:
            if gone:
                log(f"Not deleting anything this run ({len(gone)} would have gone).")
        elif gone:
            cap = max(MIN_DROP_ALLOWED, int(len(existing) * MAX_DROP_FRACTION))
            if len(gone) > cap:
                log("")
                log("=" * 68)
                log(f"REFUSING TO DELETE: {len(gone)} of {len(existing)} US rows are missing "
                    f"from Finnhub's listing (the limit for one run is {cap}).")
                log("That is almost always a bad listing, not thousands of delistings overnight.")
                log(f"A few of them: {', '.join(sorted(gone)[:20])}")
                log("Nothing was deleted. Prices and fundamentals continue as normal.")
                log("=" * 68)
            else:
                try:
                    db.delete_symbols(gone)
                    log(f"Removed {len(gone)} stocks that are no longer listed: "
                        f"{', '.join(sorted(gone)[:20])}{' …' if len(gone) > 20 else ''}")
                except Exception as e:
                    log(f"Delisting step failed ({len(gone)} to drop): {e}. Prices and "
                        f"fundamentals continue; next run will retry the delete.")
    except Exception as e:
        log(f"Symbol-list housekeeping failed: {e}. Continuing to prices and fundamentals.")

    # 2. Prices ---------------------------------------------------------------
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
                "region": "US",
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

    # 3. Fundamentals, oldest first ------------------------------------------
    order = [r["symbol"] for r in db.select(
        "symbol", {"region": "eq.US", "asset_class": "in.(stock,reit)"},
        order="fundamentals_updated_at.asc.nullsfirst")]
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
        industry = prof.get("finnhubIndustry")
        is_reit = asset_class_of(stocks[sym].get("type"), industry) == "reit"
        row = {
            "symbol": sym,
            "region": "US",
            "asset_class": "reit" if is_reit else "stock",
            "name": prof.get("name") or None,
            "industry": industry or None,
            "sector": finnhub_sector(industry),
            "summary": None,  # Finnhub free doesn't carry company descriptions
            "market_cap_usd_b": num(cap_m or met.get("marketCapitalization"), scale=0.001, digits=3),
            "pe": pe if pe and pe > 0 else None,
            "pb": num(met.get("pbAnnual") or met.get("pbQuarterly"), digits=2),
            # FFO/AFFO aren't on the Finnhub free plan; left null for REITs — the website hides those columns when empty.
            "dividend_yield": num(met.get("dividendYieldIndicatedAnnual") or met.get("currentDividendYieldTTM"), digits=3),
            "return_1y": num(met.get("52WeekPriceReturnDaily"), digits=2),
            "beta": num(met.get("beta"), digits=3),
            "revenue_growth": num(met.get("revenueGrowthTTMYoy"), digits=2),
            "eps_growth": next((v for v in (num(met.get(k), digits=2) for k in
                               ("epsGrowth5Y", "epsGrowth3Y", "epsGrowthTTMYoy")) if v is not None), None),
            "net_margin": num(met.get("netProfitMarginTTM"), digits=2),
            "high_52w": num(met.get("52WeekHigh")),
            "low_52w": num(met.get("52WeekLow")),
            "fundamentals_updated_at": now_iso(),
        }
        if row["name"] is None:
            row.pop("name")
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
