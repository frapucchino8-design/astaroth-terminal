"""CNN Fear & Greed Index, for the Public section.

Reads CNN's public feed (the same data behind money.cnn.com/markets/fear-and-greed)
and stores the headline score, its rating, the prior readings, and the seven
component indicators in the market_indicators table.

    python update_feargreed.py

Run supabase/migration_indicators.sql once first. Needs SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY, like the other scripts. No API key.
"""
import json
import time

import requests

from common import Supabase, log, now_iso, num

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# CNN's seven components → the label we show. Keys are the feed's object names.
COMPONENTS = [
    ("market_momentum_sp125", "Market momentum", "S&P 500 vs its 125-day average"),
    ("stock_price_strength", "Stock price strength", "52-week highs vs lows on the NYSE"),
    ("stock_price_breadth", "Stock price breadth", "Volume in rising vs falling stocks"),
    ("put_call_options", "Put/call options", "Put volume vs call volume"),
    ("market_volatility_vix", "Market volatility", "The VIX vs its 50-day average"),
    ("safe_haven_demand", "Safe-haven demand", "Stock returns vs Treasury returns"),
    ("junk_bond_demand", "Junk bond demand", "Yield spread, junk vs investment grade"),
]


def rating_for(score):
    """CNN's own bands."""
    if score is None:
        return None
    if score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score <= 55:
        return "Neutral"
    if score <= 75:
        return "Greed"
    return "Extreme Greed"


def fetch():
    headers = {"User-Agent": _UA, "Accept": "application/json",
               "Origin": "https://edition.cnn.com", "Referer": "https://edition.cnn.com/"}
    last = "no response"
    for attempt in range(4):
        try:
            r = requests.get(URL, headers=headers, timeout=25)
        except requests.RequestException as e:
            last = e.__class__.__name__
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    last = "invalid JSON"
            else:
                last = f"HTTP {r.status_code}"
        time.sleep(2 * (attempt + 1))
    raise SystemExit(f"Couldn't fetch the Fear & Greed feed ({last}).")


def main():
    db = Supabase()
    data = fetch()

    fg = data.get("fear_and_greed") or {}
    score = num(fg.get("score"), digits=2)
    if score is None:
        raise SystemExit("The feed had no current score — leaving the stored value in place.")
    rating = (fg.get("rating") or rating_for(score) or "").title()

    comps = []
    for key, label, blurb in COMPONENTS:
        block = data.get(key) or {}
        cs = num(block.get("score"), digits=1)
        comps.append({
            "key": key, "label": label, "blurb": blurb,
            "score": cs, "rating": (block.get("rating") or rating_for(cs) or "").title(),
        })

    prev = {
        "close": num(fg.get("previous_close"), digits=1),
        "week": num(fg.get("previous_1_week"), digits=1),
        "month": num(fg.get("previous_1_month"), digits=1),
        "year": num(fg.get("previous_1_year"), digits=1),
    }

    # About a year of daily readings, for the history chart (one point per day).
    by_day = {}
    for pt in ((data.get("fear_and_greed_historical") or {}).get("data") or []):
        y = num(pt.get("y"), digits=1)
        if y is not None and pt.get("x"):
            x = int(pt["x"])
            by_day[x // 86_400_000] = [x, y]
    hist = [by_day[d] for d in sorted(by_day)][-380:]

    row = {
        "name": "fear_greed",
        "score": score,
        "rating": rating,
        "as_of": fg.get("timestamp") or now_iso(),
        "extra": {"components": comps, "prev": prev, "history": hist,
                  "source": "CNN Business"},
        "updated_at": now_iso(),
    }
    r = db.s.post(f"{db.url}/market_indicators?on_conflict=name", json=[row], timeout=60,
                  headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if r.status_code >= 300:
        if "market_indicators" in r.text and ("does not exist" in r.text or "schema cache" in r.text):
            raise SystemExit("The market_indicators table isn't in the database yet. "
                             "Run supabase/migration_indicators.sql once, then run this again.")
        raise SystemExit(f"Saving the Fear & Greed row failed ({r.status_code}): {r.text[:300]}")
    log(f"Fear & Greed: {score:.0f} ({rating}). Saved with {len(comps)} components "
        f"and {len(hist)} history points.")


if __name__ == "__main__":
    main()
