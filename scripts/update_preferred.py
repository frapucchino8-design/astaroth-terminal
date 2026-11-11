"""US preferred shares, via Yahoo through yfinance.

Preferred shares sit between bonds and ordinary equity: a fixed dividend (the
coupon) set against a face value (par, usually $25 in the US), and no vote. The
screener treats them as their own asset class, so "Preferred" appears beside
Stocks and ETFs once this has run.

There is no free, complete list of preferred tickers anywhere, so this works from
a curated list of the larger, liquid US issues below. To add your own, drop the
Yahoo symbol into TICKERS — the format is the common ticker, a hyphen, then P
and the series letter, so Bank of America's series L is BAC-PL.

Par value and coupon are not in Yahoo's data as fields, so:
  * the coupon is read out of the security's name, which nearly always carries it
    ("... 6.00% Non-Cumulative Preferred, Series CC" → 6.00)
  * par is inferred from the price: a few hundred dollars or more means a $1,000
    issue, otherwise $25. Both are written only when they can be worked out.

Run supabase/migration_fundamentals.sql first — it adds par_value, coupon_pct
and callable.

    python scripts/update_preferred.py                 # the whole list
    python scripts/update_preferred.py --limit 20      # a quick look
    python scripts/update_preferred.py --dry-run       # fetch and print, write nothing

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY, like the other scripts.
"""
import argparse
import logging
import re
import time

from common import Supabase, log, now_iso, num

# Curated list of liquid US preferred shares, grouped by issuer. Yahoo format.
TICKERS = """
BAC-PB BAC-PE BAC-PK BAC-PL BAC-PM BAC-PN BAC-PO BAC-PP BAC-PQ BAC-PS
JPM-PC JPM-PD JPM-PJ JPM-PK JPM-PL JPM-PM
WFC-PA WFC-PC WFC-PD WFC-PL WFC-PY WFC-PZ
C-PJ C-PK C-PN
GS-PA GS-PC GS-PD GS-PK
MS-PA MS-PE MS-PF MS-PI MS-PK MS-PL MS-PO MS-PP
USB-PA USB-PH USB-PP USB-PQ USB-PR USB-PS
PNC-PP PNC-PQ PNC-PR
TFC-PI TFC-PO TFC-PR
SCHW-PD SCHW-PJ
ALLY-PB ALLY-PC
AIG-PA
MET-PA MET-PE MET-PF
PRU-PA
ATH-PA ATH-PB ATH-PC ATH-PD ATH-PE
NEE-PR
DUK-PA
SO-PB
AEP-PB
CMS-PB CMS-PC
DTE-PB
NI-PB
SR-PA
PSA-PF PSA-PG PSA-PH PSA-PI PSA-PJ PSA-PK PSA-PL PSA-PM PSA-PN PSA-PO PSA-PP PSA-PQ PSA-PR PSA-PS
O-PA
SPG-PJ
VNO-PL VNO-PM VNO-PN VNO-PO
KIM-PL KIM-PM KIM-PN
REG-PA
AHH-PA
DLR-PJ DLR-PK DLR-PL
NSA-PA NSA-PB
CUBE-PA
EPR-PC EPR-PE EPR-PG
STAG-PC
WPC-PA
T-PA T-PC
VZ-PA
CHTR-PA
F-PB F-PC F-PD
GM-PB
RITM-PA RITM-PB RITM-PC RITM-PD
AGNC-PC AGNC-PD AGNC-PE AGNC-PF AGNC-PG
NLY-PF NLY-PG NLY-PI
STWD-PA
TWO-PA TWO-PB TWO-PC
ARR-PC
CIM-PA CIM-PB CIM-PC CIM-PD
MFA-PB MFA-PC
NYMT-PD NYMT-PE NYMT-PF NYMT-PG
""".split()

# "6.00%", "6 1/2%", "4.875 %" anywhere in the name
COUPON_RE = re.compile(r"(\d{1,2})(?:\s+(\d)/(\d)|[.,](\d{1,4}))?\s*%")
CALL_HINT = re.compile(r"\bnon[- ]?callable\b", re.I)


def coupon_from_name(name):
    """Pull the stated rate out of a preferred's name. None if it isn't there."""
    if not name:
        return None
    m = COUPON_RE.search(name)
    if not m:
        return None
    whole = float(m.group(1))
    if m.group(2) and m.group(3):          # "6 1/2%"
        try:
            whole += float(m.group(2)) / float(m.group(3))
        except (ValueError, ZeroDivisionError):
            pass
    elif m.group(4):                        # "6.125%"
        whole = float(f"{m.group(1)}.{m.group(4)}")
    # Rates outside this band are almost certainly something else in the name.
    return round(whole, 4) if 0.5 <= whole <= 25 else None


def par_from_price(price):
    """US preferreds are $25 or $1,000 par. The price tells you which."""
    if not price:
        return None
    if price > 200:
        return 1000.0
    if price > 60:
        return 100.0
    return 25.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="Only this many tickers.")
    ap.add_argument("--sleep", type=float, default=0.7, help="Pause between names.")
    ap.add_argument("--dry-run", action="store_true", help="Fetch and print; write nothing.")
    ap.add_argument("--deadline", type=float, default=0, help="Seconds before saving what we have and stopping.")
    args = ap.parse_args()

    try:
        import yfinance as yf
    except ImportError:
        raise SystemExit("yfinance isn't installed. pip install -r scripts/requirements.txt")

    # yfinance prints its own "HTTP Error 404" line for every ticker Yahoo has
    # never heard of, which buries the real output. They are counted and listed
    # at the end instead, so the list below can be pruned.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    tickers = TICKERS[: args.limit] if args.limit else TICKERS
    log(f"Checking {len(tickers)} preferred issues.")

    db = None if args.dry_run else Supabase()
    rows, kept, skipped, missing = [], set(), 0, []

    for i, sym in enumerate(tickers, 1):
        if out_of_time():
            log(f"  deadline reached after {i - 1}/{len(tickers)}")
            break
        try:
            info = yf.Ticker(sym).info or {}
        except Exception as e:
            skipped += 1
            missing.append(sym)
            log(f"  {sym}: {str(e)[:50]}")
            time.sleep(args.sleep)
            continue

        price = num(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose"))
        if not price:
            skipped += 1
            missing.append(sym)
            time.sleep(args.sleep)
            continue

        name = info.get("longName") or info.get("shortName") or sym
        prev = num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
        par = par_from_price(price)
        coupon = coupon_from_name(name)
        # The dividend is the coupon on par; the yield is that against the price.
        annual = (coupon / 100 * par) if (coupon and par) else num(info.get("dividendRate"))
        dy = round(annual / price * 100, 3) if (annual and price) else num(info.get("dividendYield"), scale=100, digits=3)

        row = {
            "symbol": sym,
            "name": name,
            "exchange": info.get("exchange") or "NYSE",
            "region": "US",
            "asset_class": "pref",
            # Yahoo's sector for a preferred is usually the issuer's, which is
            # what we want; most preferred issuers are banks, hence the default.
            "sector": info.get("sector") or "Financials",
            "currency": info.get("currency") or "USD",
            "industry": info.get("industry") or "Preferred stock",
            "summary": (info.get("longBusinessSummary") or "").strip()[:800] or None,
            "price": price,
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "dividend_yield": dy,
            "return_1y": num(info.get("52WeekChange"), scale=100, digits=2),
            "beta": num(info.get("beta"), digits=3),
            "high_52w": num(info.get("fiftyTwoWeekHigh")),
            "low_52w": num(info.get("fiftyTwoWeekLow")),
            "par_value": par,
            "coupon_pct": coupon,
            # Nearly every US preferred is callable after five years; only mark
            # 'No' when the name says so outright, and leave the rest unknown.
            "callable": "No" if CALL_HINT.search(name) else "Yes",
            "quote_updated_at": now_iso(),
            "fundamentals_updated_at": now_iso(),
        }
        rows.append(row)
        kept.add(sym)

        if args.dry_run:
            log(f"  {sym:<10} {name[:44]:<46} {price:>9.2f}  par {par}  coupon {coupon}  yield {dy}")
        elif len(rows) >= 50:
            db.upsert(rows)
            rows = []
            log(f"  {i}/{len(tickers)} saved")
        elif i % 20 == 0:
            log(f"  {i}/{len(tickers)}")
        time.sleep(args.sleep)

    if rows and not args.dry_run:
        db.upsert(rows)

    log(f"{'Would write' if args.dry_run else 'Wrote'} {len(kept)} preferred issues, skipped {skipped}.")
    if missing:
        log(f"Yahoo had no quote for {len(missing)} of them — these have been called, renamed or "
            f"never existed, and can be taken out of TICKERS at the top of this file:")
        log("    " + " ".join(sorted(missing)))

    # Clear out issues that have been called or delisted, but only on a full run.
    if not args.dry_run and not args.limit and not args.deadline and len(kept) > 30:
        gone = db.delete_asset_class("pref", kept)
        if gone:
            log(f"Removed {gone} preferred rows no longer listed.")


if __name__ == "__main__":
    main()
