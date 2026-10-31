"""Quick price refresh, every 15 minutes during market hours.

Stocks:  Yahoo's screener returns up to 250 quotes per request, so every US and Swiss
         share is refreshed in about 30 requests.
ETFs and futures: the screener doesn't cover them, so their last two daily closes are
         pulled in bulk with yf.download (a few hundred symbols per request batch).

Only price fields are touched; the nightly jobs still fill in P/E, growth, margins,
expense ratios and so on. Yahoo quotes are free but usually about 15 minutes delayed.
"""
import time

import yfinance as yf
from yfinance import EquityQuery

from common import Supabase, log, now_iso, num

# Main US exchanges on Yahoo: Nasdaq (3 tiers), NYSE, NYSE American, NYSE Arca, Cboe BZX. No OTC.
US_EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS"]


def raw(v):
    """Yahoo sometimes wraps numbers as {"raw": 1.2, "fmt": "1.20"}."""
    return v.get("raw") if isinstance(v, dict) else v


def screen_all(query, label):
    quotes, offset = [], 0
    while offset < 12000:
        page = None
        for attempt in range(3):
            try:
                page = yf.screen(query, offset=offset, size=250, sortField="intradaymarketcap", sortAsc=False) or {}
                break
            except Exception as e:
                log(f"  {label}: request failed ({e}); retrying")
                time.sleep(10 * (attempt + 1))
        if page is None:
            break
        batch = page.get("quotes") or []
        quotes += batch
        if len(batch) < 250:
            break
        offset += 250
        time.sleep(0.5)
    log(f"{label}: {len(quotes)} quotes from Yahoo.")
    return quotes


def clean(row):
    """Leave out missing values, so a gap in Yahoo's data never wipes a value we already have."""
    return {k: v for k, v in row.items() if v is not None}


def fx_to_usd(currency):
    if currency == "USD":
        return 1.0
    try:
        return float(yf.Ticker(f"{currency}USD=X").history(period="5d")["Close"].dropna().iloc[-1])
    except Exception:
        return None


def fund_prices(symbols, chunk=150):
    """Last close and previous close for ETFs and futures, in bulk."""
    now, rows = now_iso(), []
    symbols = sorted(symbols)
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        try:
            data = yf.download(part, period="5d", interval="1d", group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
        except Exception as e:
            log(f"  funds: download failed ({e}); skipping this batch")
            continue
        for sym in part:
            try:
                frame = data[sym] if len(part) > 1 else data
                closes = frame["Close"].dropna()
            except Exception:
                continue
            if closes.empty:
                continue
            price = num(float(closes.iloc[-1]))
            prev = num(float(closes.iloc[-2])) if len(closes) > 1 else None
            if not price:
                continue
            rows.append(clean({
                "symbol": sym,
                "price": price,
                "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
                "quote_updated_at": now,
            }))
        time.sleep(1)
    log(f"Funds and futures: {len(rows)} prices.")
    return rows


def main():
    db = Supabase()
    try:
        existing = {r["symbol"]: (r.get("asset_class") or "stock") for r in db.select("symbol,region,asset_class")}
    except RuntimeError:
        log("No asset_class column yet — run supabase/setup-update.sql. Falling back to stocks only.")
        existing = {r["symbol"]: "stock" for r in db.select("symbol,region")}
    log(f"{len(existing)} instruments in the database.")
    now = now_iso()

    us = screen_all(EquityQuery("and", [EquityQuery("eq", ["region", "us"]),
                                        EquityQuery("is-in", ["exchange", *US_EXCHANGES])]), "US")
    ch = screen_all(EquityQuery("and", [EquityQuery("eq", ["region", "ch"]),
                                        EquityQuery("is-in", ["exchange", "EBS"])]), "Switzerland")
    chf_usd = fx_to_usd("CHF")

    rows, new_swiss = [], 0
    for q in us:
        sym = (q.get("symbol") or "").replace("-", ".")   # Yahoo BRK-B = Finnhub BRK.B
        price = num(raw(q.get("regularMarketPrice")))
        if existing.get(sym) not in ("stock", "reit") or not price:
            continue                                       # the nightly job decides which US stocks exist
        cap = num(raw(q.get("marketCap")))
        rows.append(clean({"symbol": sym, "region": "US", "price": price,
                           "change_pct": num(raw(q.get("regularMarketChangePercent")), digits=3),
                           "market_cap_usd_b": round(cap / 1e9, 3) if cap else None,
                           "high_52w": num(raw(q.get("fiftyTwoWeekHigh"))), "low_52w": num(raw(q.get("fiftyTwoWeekLow"))),
                           "quote_updated_at": now}))
    for q in ch:
        sym = q.get("symbol") or ""
        price = num(raw(q.get("regularMarketPrice")))
        if not sym.endswith(".SW") or not price or existing.get(sym, "stock") not in ("stock", "reit"):
            continue
        cap = num(raw(q.get("marketCap")))
        cur = q.get("currency") or "CHF"
        rate = chf_usd if cur == "CHF" else fx_to_usd(cur)
        row = {"symbol": sym, "region": "Switzerland", "price": price,
               "change_pct": num(raw(q.get("regularMarketChangePercent")), digits=3),
               "market_cap_usd_b": round(cap * rate / 1e9, 3) if cap and rate else None,
               "high_52w": num(raw(q.get("fiftyTwoWeekHigh"))), "low_52w": num(raw(q.get("fiftyTwoWeekLow"))),
               "quote_updated_at": now}
        if sym not in existing:                            # a Swiss share we didn't have yet: add it
            row.update({"name": q.get("longName") or q.get("shortName") or sym, "exchange": "SIX",
                        "currency": cur, "asset_class": "stock"})
            new_swiss += 1
        rows.append(clean(row))

    funds = [s for s, ac in existing.items() if ac in ("etf", "future")]
    if funds:
        rows += fund_prices(funds)

    if not rows:
        raise SystemExit("Yahoo returned no usable quotes, so nothing was updated.")
    db.upsert(rows)
    log(f"Updated prices for {len(rows)} instruments ({new_swiss} new Swiss shares added).")


if __name__ == "__main__":
    main()
