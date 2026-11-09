# fix_swiss.py — paste contents from Claude chat he"""Clean up the Switzerland region so the count reflects reality.

The Swiss count has been stuck around 600 because the SIX screener also returns
foreign secondary listings, funds, ETFs, rights and warrants, and older runs
stored all of them with region = "Switzerland". The real SIX share universe is
roughly 230–250 names.

This script fixes it in two passes, cheapest first:

  Pass 1 — no network. Anything obviously not a Swiss share goes straight away:
           a symbol that isn't ".SW", an asset_class that isn't stock/reit,
           or a name that reads as a fund, ETF, tracker, right or warrant.

  Pass 2 — Yahoo, only for the rows pass 1 didn't settle, and only for the ones
           not on the built-in list of known SIX shares. A row survives if Yahoo
           says quoteType is EQUITY and country is Switzerland.

Nothing is deleted unless you ask. Run it to see the plan, then run it again
with --apply:

    python fix_swiss.py                 # report only (default)
    python fix_swiss.py --apply         # actually delete
    python fix_swiss.py --apply --no-network   # pass 1 only, no Yahoo calls
    python fix_swiss.py --reassign      # move foreign listings to their own
                                        # region instead of deleting them

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment, exactly
like the nightly scripts.
"""
import argparse
import re
import sys
import time

from common import Supabase, log

# The built-in list of SIX-listed shares, kept in step with update_ch.py.
# A symbol on this list is trusted without a Yahoo lookup.
BUILTIN = """
ABBN ACLN ADEN ADXN AERO AEVS ALC ALLN ALPN ALSN AMS APGN ARYN ASCN AUTN AVOL BAER BALN BANB
BARN BBN BCGE BCHN BCVN BEAN BEKN BELL BION BKW BLKB BOSN BRKN BSKP BSLN BUCN BVZN BYS CALN
CFR CICN CLN CLTN CMBN COPN COTN CPHN DAE DESN DKSH DOCM DOKA EDHN EFGN ELMN EMMN EMSN EPIC
FHZN FORN FREN GALD GALE GAM GEBN GF GIVN GLKBN GMI GRKP GUR HBLN HBMN HELN HIAG HLEE HOCN
HOLN HUBN IDIA IFCN IMPN INRN IREN ISN JFN KARN KLIN KNIN KOMN KUD KURN LAND LEHN LEON LINN
LISN LISP LLBN LOGN LONN LUKN MBTN MCHN MED METN MIKN MOBN MOLN MOVE MOZN MTG NBEN NESN NOVN
NREN NWRN OERL OFN ORON PEAN PEDU PEHN PGHN PLAN PMN PPGN PSPN RIEN RLF RO ROG SAHN SANN
SCHN SCHP SCMN SDZ SENS SFPN SFSN SFZN SGKN SGSN SHLTN SIGN SIKA SKAN SLHN SNBN SOON SPCE
SPEX SPSN SQN SRAIL STGN STLN STMN SUN SWON SWTQ TECN TEMN TIBN TKBP TOHN TXGN UBSG UBXN
UHR UHRN VACN VAHN VARN VATN VBSN VETN VILN VLRT VONN VPBN VZN WARN WIHN WKBN XLS YPSN ZEHN
ZUBN ZUGER ZUGN ZURN ZWM
""".split()
BUILTIN_SW = {s + ".SW" for s in BUILTIN}

# Words in a name that mean "not an ordinary share".
NOT_A_SHARE = re.compile(
    r"\b(etf|etc|etp|fund|sicav|sicaf|index|tracker|ucits|certificate|warrant|"
    r"call|put|rights?|bond|note|notes|trust unit|units?|lev(eraged)?|short\s|"
    r"x\d+\s|turbo|mini[- ]future|participation certificate)\b",
    re.I,
)
# Share classes that are fine even though the name looks odd.
OK_CLASSES = ("stock", "reit")


def classify_offline(row):
    """Return (verdict, reason). verdict is 'keep', 'drop' or 'check'."""
    sym = (row.get("symbol") or "").strip()
    name = (row.get("name") or "").strip()
    ac = (row.get("asset_class") or "").strip().lower()
    exch = (row.get("exchange") or "").strip()

    if not sym.endswith(".SW"):
        return "drop", f"symbol isn't a SIX listing ({sym or 'blank'})"
    if ac and ac not in OK_CLASSES:
        return "drop", f"asset_class is {ac}, not a share"
    if NOT_A_SHARE.search(name):
        return "drop", f"name reads as a fund/derivative ({name[:60]})"
    if exch and exch not in ("SIX", "EBS", "SWX", ""):
        return "drop", f"exchange is {exch}, not SIX"
    if sym in BUILTIN_SW:
        return "keep", "on the built-in SIX list"
    return "check", "needs a Yahoo check"


def yahoo_verdict(symbol, retries=3):
    """'keep', 'drop' or 'unknown' — whether Yahoo calls this a Swiss equity."""
    try:
        import yfinance as yf
    except ImportError:
        return "unknown", "yfinance isn't installed"
    for attempt in range(retries):
        try:
            info = yf.Ticker(symbol).info or {}
        except Exception as e:
            msg = str(e)
            if "Too Many Requests" in msg or "Rate" in msg:
                time.sleep(20 * (attempt + 1))
                continue
            return "unknown", f"Yahoo error ({msg[:60]})"
        qt = (info.get("quoteType") or "").upper()
        country = (info.get("country") or "").strip()
        if not qt and not country:
            return "unknown", "Yahoo returned nothing"
        if qt and qt != "EQUITY":
            return "drop", f"Yahoo quoteType is {qt}"
        if country and country != "Switzerland":
            return "drop", f"Yahoo country is {country}"
        return "keep", "Yahoo says Swiss equity"
    return "unknown", "Yahoo rate-limited"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Actually change the database. Without it, nothing is written.")
    ap.add_argument("--no-network", action="store_true", help="Skip the Yahoo pass; keep anything pass 1 couldn't settle.")
    ap.add_argument("--reassign", action="store_true",
                    help="Instead of deleting foreign listings, move them to region 'Europe' so they stay searchable.")
    ap.add_argument("--limit", type=int, default=0, help="Only check this many ambiguous rows (for a quick look).")
    args = ap.parse_args()

    db = Supabase()
    rows = db.select("symbol,name,exchange,asset_class,region,price", {"region": "eq.Switzerland"})
    log(f"Switzerland currently holds {len(rows)} rows.")
    if not rows:
        log("Nothing to do.")
        return

    keep, drop, check = [], [], []
    for row in rows:
        verdict, why = classify_offline(row)
        (keep if verdict == "keep" else drop if verdict == "drop" else check).append((row, why))

    log(f"Pass 1 (offline): {len(keep)} clearly Swiss shares, {len(drop)} clearly not, {len(check)} need a check.")

    if check and not args.no_network:
        todo = check[: args.limit] if args.limit else check
        log(f"Pass 2: asking Yahoo about {len(todo)} symbols (about {len(todo) * 0.7:.0f}s) …")
        for i, (row, _) in enumerate(todo, 1):
            verdict, why = yahoo_verdict(row["symbol"])
            if verdict == "drop":
                drop.append((row, why))
            else:
                # 'keep' and 'unknown' both stay — we never delete on a failed lookup
                keep.append((row, why))
            if i % 25 == 0:
                log(f"  {i}/{len(todo)}")
            time.sleep(0.6)
        # anything we didn't get to stays
        for row, why in check[len(todo):]:
            keep.append((row, "not checked, kept"))
    else:
        for row, why in check:
            keep.append((row, "no network pass, kept"))

    log("")
    log(f"Result: {len(keep)} genuine Swiss shares, {len(drop)} to remove.")
    log("")
    if drop:
        log("These would go:")
        for row, why in sorted(drop, key=lambda x: x[0]["symbol"])[:80]:
            log(f"  {row['symbol']:<14} {(row.get('name') or '')[:42]:<44} — {why}")
        if len(drop) > 80:
            log(f"  … and {len(drop) - 80} more")

    if not args.apply:
        log("")
        log("Nothing was written. Re-run with --apply to make these changes.")
        return

    # Safety rail: never empty the table on a bad run.
    if len(keep) < 100:
        sys.exit(f"Refusing to apply: only {len(keep)} rows would survive, which looks wrong. "
                 "Check the output above, or run with --no-network to use pass 1 only.")

    symbols = [row["symbol"] for row, _ in drop]
    if args.reassign:
        log(f"Moving {len(symbols)} rows to region 'Europe' …")
        for i in range(0, len(symbols), 100):
            part = symbols[i:i + 100]
            db.upsert([{"symbol": s, "region": "Europe"} for s in part])
        log("Done. They stay searchable, just not as Swiss.")
    else:
        log(f"Deleting {len(symbols)} rows …")
        db.delete_symbols(symbols)
        log("Done.")

    left = db.select("symbol", {"region": "eq.Switzerland"})
    log(f"Switzerland now holds {len(left)} rows.")


if __name__ == "__main__":
    main()re
