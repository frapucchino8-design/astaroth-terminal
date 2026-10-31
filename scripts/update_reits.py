"""Weekly FFO and AFFO for REITs, from Yahoo's financial statements.

No free data source publishes FFO or AFFO directly — they are NAREIT definitions that
REITs report in their own supplements, not in the standard financial statements. So this
rebuilds FFO from the statements Yahoo does give:

    FFO  = net income + real-estate depreciation and amortisation
    AFFO = FFO - recurring capital spending

Two honest caveats, both reflected in how the site labels these numbers:

  * The NAREIT definition also strips out gains and losses on property sales. Yahoo's
    income statement doesn't break those out reliably, so a REIT that sold buildings in
    the period will show FFO that is too high. Figures are best read as an approximation.
  * Recurring ("maintenance") capex cannot be separated from development capex in the
    cash-flow statement — spending to keep a building earning looks identical to spending
    on a new one. This uses the common 15% rule of thumb, so AFFO here is an estimate.

Run it weekly: there are only a few hundred REITs and the numbers move once a quarter.
"""
import argparse
import time

import yfinance as yf

from common import Supabase, log, now_iso, num

# What the rows are called in Yahoo's statements, newest naming first.
NET_INCOME_KEYS = ["Net Income", "NetIncome", "Net Income Common Stockholders",
                   "Net Income From Continuing Operation Net Minority Interest"]
DA_KEYS = ["Depreciation And Amortization", "Depreciation Amortization Depletion",
           "DepreciationAndAmortization", "Reconciled Depreciation",
           "Depreciation & Amortization", "Depreciation"]
AFFO_HAIRCUT = 0.85   # recurring capex rule of thumb: AFFO is about 85% of FFO


def pick(frame, keys):
    """Latest value for the first of `keys` that the statement actually has."""
    if frame is None or getattr(frame, "empty", True):
        return None
    for key in keys:
        if key in frame.index:
            row = frame.loc[key].dropna()
            if len(row):
                return float(row.iloc[0])
    return None


def ttm(frame, keys, quarters=4):
    """Trailing twelve months: the last four quarters added up, when they are all there."""
    if frame is None or getattr(frame, "empty", True):
        return None
    for key in keys:
        if key in frame.index:
            row = frame.loc[key].dropna()
            if len(row) >= quarters:
                return float(row.iloc[:quarters].sum())
    return None


def reit_row(sym):
    t = yf.Ticker(sym)
    try:
        info = t.info or {}
    except Exception:
        info = {}
    shares = num(info.get("sharesOutstanding"))
    if not shares:
        return None

    try:
        qi, qc = t.quarterly_income_stmt, t.quarterly_cashflow
    except Exception:
        qi = qc = None
    net = ttm(qi, NET_INCOME_KEYS)
    da = ttm(qc, DA_KEYS)

    if net is None or da is None:           # fall back to the last full year
        try:
            ai, ac = t.income_stmt, t.cashflow
        except Exception:
            ai = ac = None
        net = net if net is not None else pick(ai, NET_INCOME_KEYS)
        da = da if da is not None else pick(ac, DA_KEYS)

    if net is None or da is None or da <= 0:
        return None

    ffo = net + da
    if ffo <= 0:                            # a loss big enough to swallow depreciation
        return {"symbol": sym, "ffo_ps": None, "affo_ps": None, "fundamentals_updated_at": now_iso()}

    ffo_ps = ffo / shares
    return {
        "symbol": sym,
        "ffo_ps": round(ffo_ps, 4),
        "affo_ps": round(ffo_ps * AFFO_HAIRCUT, 4),
        "fundamentals_updated_at": now_iso(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-minutes", type=float, default=60, help="Stop starting new work after this many minutes.")
    ap.add_argument("--limit", type=int, default=0, help="Only process this many REITs (for testing).")
    args = ap.parse_args()
    deadline = time.monotonic() + args.budget_minutes * 60

    db = Supabase()
    try:
        reits = [r["symbol"] for r in db.select(
            "symbol", {"asset_class": "eq.reit"}, order="fundamentals_updated_at.asc.nullsfirst")]
    except RuntimeError:
        raise SystemExit("No asset_class column yet — run supabase/setup-update.sql first.")

    if args.limit:
        reits = reits[:args.limit]
    log(f"{len(reits)} REITs to refresh.")

    batch, done, skipped = [], 0, 0
    for i, sym in enumerate(reits, 1):
        if time.monotonic() > deadline:
            log("Time budget used up; the rest continue next run.")
            break
        row = reit_row(sym)
        if row is None:
            skipped += 1
        else:
            batch.append(row)
        if len(batch) >= 50:
            db.upsert(batch)
            done += len(batch)
            batch = []
            log(f"  {done} written ({i}/{len(reits)} checked)")
        time.sleep(0.6)

    if batch:
        db.upsert(batch)
        done += len(batch)
    log(f"FFO/AFFO written for {done} REITs; {skipped} had no usable statements.")


if __name__ == "__main__":
    main()
