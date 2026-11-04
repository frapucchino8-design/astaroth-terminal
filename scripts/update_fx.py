"""FX rates against USD, using the Frankfurter API (ECB reference rates, free, no key).

One call brings back every currency the ECB publishes, so this script makes one
request and stores one row per currency pair as XXXUSD=X.
"""
import argparse
import time

import requests

from common import Supabase, log, now_iso, num

FRANKFURTER = "https://api.frankfurter.app"
CURRENCY_NAMES = {
    "EUR": "Euro", "GBP": "British Pound", "CHF": "Swiss Franc", "JPY": "Japanese Yen",
    "AUD": "Australian Dollar", "CAD": "Canadian Dollar", "NZD": "New Zealand Dollar",
    "SEK": "Swedish Krona", "NOK": "Norwegian Krone", "DKK": "Danish Krone",
    "ISK": "Icelandic Krona", "PLN": "Polish Zloty", "CZK": "Czech Koruna",
    "HUF": "Hungarian Forint", "RON": "Romanian Leu", "BGN": "Bulgarian Lev",
    "TRY": "Turkish Lira", "ILS": "Israeli Shekel", "ZAR": "South African Rand",
    "CNY": "Chinese Yuan", "HKD": "Hong Kong Dollar", "SGD": "Singapore Dollar",
    "KRW": "South Korean Won", "INR": "Indian Rupee", "IDR": "Indonesian Rupiah",
    "MYR": "Malaysian Ringgit", "PHP": "Philippine Peso", "THB": "Thai Baht",
    "MXN": "Mexican Peso", "BRL": "Brazilian Real",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true",
                    help="Prices only — skip the per-currency 1-year history. For the 5-minute workflow.")
    args = ap.parse_args()
    db = Supabase()

    try:
        latest = requests.get(f"{FRANKFURTER}/latest?from=USD", timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        raise SystemExit(f"Frankfurter unreachable: {e}")

    base, date, rates = latest.get("base"), latest.get("date"), latest.get("rates") or {}
    if base != "USD" or not rates:
        raise SystemExit(f"Unexpected response: {latest}")

    # 1-year change for each currency, one call per currency (small set, polite 1s gap).
    # Skipped on the fast path — the nightly run refreshes it.
    one_year = {}
    if not args.fast:
        from datetime import date as _d, timedelta
        start = (_d.today() - timedelta(days=365)).isoformat()
        for cur in rates:
            try:
                h = requests.get(f"{FRANKFURTER}/{start}..?from=USD&to={cur}", timeout=30).json()
                series = h.get("rates", {})
                if series:
                    first = next(iter(sorted(series)))
                    y0 = series[first].get(cur)
                    if y0:
                        one_year[cur] = round((rates[cur] / y0 - 1) * 100, 2)
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1)

    rows, kept = [], set()
    for cur, rate_from_usd in rates.items():
        # Store as foreign/USD (so 1 EUR → ~1.08 USD, like Yahoo's EURUSD=X).
        price = round(1.0 / float(rate_from_usd), 6)
        sym = f"{cur}USD=X"
        kept.add(sym)
        row = {
            "symbol": sym,
            "name": f"{CURRENCY_NAMES.get(cur, cur)} / US Dollar",
            "exchange": "ECB",
            "region": "Global",
            "asset_class": "fx",
            "currency": "USD",
            "sector": "FX",
            "industry": "FX",
            "price": price,
            "quote_updated_at": now_iso(),
        }
        if not args.fast:
            row["return_1y"] = one_year.get(cur)
            row["fundamentals_updated_at"] = now_iso()
        rows.append(row)

    db.upsert(rows)
    log(f"Updated {len(rows)} FX pairs (ECB rates as of {date}).")

    if not args.fast:
        gone = db.delete_asset_class("fx", kept)
        if gone:
            log(f"Removed {gone} FX pairs no longer published.")


if __name__ == "__main__":
    main()
