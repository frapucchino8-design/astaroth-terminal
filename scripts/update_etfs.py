"""US and Swiss ETFs, discovered from the exchange listing rather than hand-listed.

This used to work from a hardcoded list of about 170 well-known US ETFs. That is
why the screener only ever had ~200 of them, while Finnhub lists roughly 6,600
ETPs on the main US exchanges.

Now the universe comes from the listing itself, screened by how much the thing
actually trades, because most of those 6,600 are dead: hundreds of ETPs do only
a few thousand dollars a day and nobody can buy them in any size. The screen is
median daily dollar volume over the last month (MIN_DOLLAR_VOL, default
$250,000), which keeps roughly 1,500-2,000 real funds and drops the rest.

The curated list below is always kept, whatever its volume, so the familiar
names can never fall out.

Order of work, so a run that is cut short still helps:
  1. ETFs not yet in the database at all;
  2. then the ones whose figures are oldest.

    python scripts/update_etfs.py                  # the usual run
    python scripts/update_etfs.py --all            # skip the volume screen
    python scripts/update_etfs.py --limit 30       # a quick look
    python scripts/update_etfs.py --dry-run        # discover and screen, write nothing

Yahoo is unofficial: fine for a personal project, not for a paid public site.
"""
import argparse
import time

import requests
import yfinance as yf

from common import Supabase, env, log, now_iso, num, to_usd

FINNHUB = "https://finnhub.io/api/v1"
# The venues an ETF actually lists on. Arca and Cboe BZX carry most of them.
EXCHANGES = {"ARCX": "NYSE Arca", "BATS": "Cboe BZX", "XNAS": "NASDAQ", "XNYS": "NYSE", "XASE": "NYSE American"}
ETP_TYPES = {"ETP", "ETF"}          # Finnhub says ETP; ETF is accepted in case that changes
MIN_DOLLAR_VOL = 250_000            # median $ traded a day over the last month
VOL_CHUNK = 60                      # tickers per yfinance history call
# Delete rails, same idea as update_us.py: a bad listing must not empty the table.
MAX_DROP_FRACTION = 0.10
MIN_DROP_ALLOWED = 40

# Always included, whatever the volume screen says.
US_ETFS = """
SPY VOO IVV VTI QQQ QQQM SPLG DIA IWM MDY VTV VUG VV MGK MGV IWF IWD IWR IJS IJT IJR IJH ITOT RSP
VEA VXUS VWO IEFA IEMG EFA EEM VEU VNQI SCHE SCHF SCHX SCHA SCHG SCHV VBR VXF SCHD VYM VIG DVY NOBL
HDV SPHD SDY FDL DGRO DGRW VPU VDC VDE VOX VCR VFH VIS VAW VHT VGT MTUM USMV SPLV QUAL
XLF XLE XLK XLV XLI XLY XLP XLU XLB XLRE XLC
SMH SOXX XSD KRE KBE XHB ITB XME GDX GDXJ SIL TAN LIT URA ICLN KWEB CQQQ
VNQ SCHH REM MORT PFF PGX PGF PFFA
AGG BND BNDX VTIP TIP VGSH VGIT VGLT GOVT SHY IEF TLT IEI BIL SGOV SCHO SCHP SCHZ
LQD VCIT VCSH HYG JNK EMB PCY EMLC VWOB
GLD IAU SLV PPLT PALL CPER WOOD USO UNG BNO DBO DBA DBC CORN WEAT SOYB CANE SGG JO
JEPI JEPQ DIVO QYLD RYLD XYLD AMLP MLPX AMZA
ARKK ARKQ ARKG ARKW ARKF ARKX FXI MCHI EWJ EWG EWU EWC EWA EWY EWT EWW EWZ INDA EWH EWS EWL EZU EPOL
TQQQ SQQQ UPRO SOXL FAS FAZ SPXL SPXS TNA TZA SDS SSO QID QLD
VXX UVXY SVIX UVIX
SHV SHYG USFR TBLL FLOT FLRN
""".split()

CH_ETFS = """
SMMCHA.SW CHSPI.SW EEMM.SW XMME.SW EUNA.SW IWRD.SW SPGL.SW VWRL.SW VUSA.SW VEUR.SW IEMM.SW EUNK.SW
CSSPX.SW CSPXJ.SW CSBGC0.SW CSBGC3.SW CSBGU3.SW CSBGU7.SW CSAGG.SW CBND.SW
""".split()


def discover_us(key):
    """Every ETP on the main US exchanges, from the Finnhub listing."""
    if not key:
        log("  no FINNHUB_API_KEY, so sticking to the built-in list")
        return {}
    try:
        r = requests.get(f"{FINNHUB}/stock/symbol", params={"exchange": "US"},
                         headers={"X-Finnhub-Token": key}, timeout=60)
    except requests.RequestException as e:
        log(f"  couldn't reach Finnhub ({e}); sticking to the built-in list")
        return {}
    if not r.ok:
        log(f"  Finnhub listing HTTP {r.status_code}; sticking to the built-in list")
        return {}
    try:
        listing = r.json()
    except ValueError:
        log("  Finnhub listing wasn't JSON; sticking to the built-in list")
        return {}
    found = {x["symbol"]: EXCHANGES[x["mic"]] for x in listing
             if x.get("symbol") and x.get("type") in ETP_TYPES and x.get("mic") in EXCHANGES}
    log(f"  Finnhub lists {len(listing)} US symbols; {len(found)} are ETPs on the main exchanges.")
    if len(found) < 500:
        log(f"  only {len(found)} ETPs found — that looks wrong, so the built-in list is used instead.")
        return {}
    return found


def liquid_only(symbols, minimum):
    """Keep the ones that actually trade. Returns {symbol: median $ volume a day}."""
    keep, checked = {}, 0
    for i in range(0, len(symbols), VOL_CHUNK):
        part = symbols[i:i + VOL_CHUNK]
        try:
            df = yf.download(" ".join(part), period="1mo", interval="1d", progress=False,
                             group_by="ticker", threads=True, auto_adjust=False)
        except Exception as e:
            log(f"  volume batch {i // VOL_CHUNK} failed ({str(e)[:60]}) — keeping it rather than dropping it")
            keep.update({s: None for s in part})
            continue
        for s in part:
            checked += 1
            try:
                col = df[s] if len(part) > 1 else df
                closes, vols = col["Close"].dropna(), col["Volume"].dropna()
                n = min(len(closes), len(vols))
                if not n:
                    continue
                dollars = sorted(float(closes.iloc[k]) * float(vols.iloc[k]) for k in range(n))
                med = dollars[n // 2]
                if med >= minimum:
                    keep[s] = round(med)
            except (KeyError, IndexError, ValueError):
                continue
        if (i // VOL_CHUNK) % 10 == 0:
            log(f"  volume screen: {checked}/{len(symbols)} checked, {len(keep)} kept so far")
        time.sleep(0.5)
    return keep


def fetch_info(sym):
    for attempt in range(3):
        try:
            return yf.Ticker(sym).info or {}
        except Exception as e:
            msg = str(e)
            if "Too Many Requests" in msg or "Rate" in msg:
                log(f"  Yahoo rate limit, waiting {30 * (attempt + 1)}s")
                time.sleep(30 * (attempt + 1))
                continue
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="Skip the volume screen and take every ETP.")
    ap.add_argument("--min-volume", type=float, default=MIN_DOLLAR_VOL,
                    help=f"Median daily dollar volume to qualify (default {MIN_DOLLAR_VOL:,}).")
    ap.add_argument("--deadline", type=float, default=0, help="Seconds before saving what we have and stopping.")
    ap.add_argument("--dry-run", action="store_true", help="Discover and screen, write nothing.")
    ap.add_argument("--no-delete", action="store_true", help="Never remove anything.")
    args = ap.parse_args()
    started = time.monotonic()
    out_of_time = lambda: args.deadline and (time.monotonic() - started) > args.deadline

    db = None if args.dry_run else Supabase()

    # 1. Who is out there ----------------------------------------------------
    log("Working out the ETF universe …")
    discovered = discover_us(env("FINNHUB_API_KEY", ""))
    curated = {s: "NYSE Arca/BATS" for s in US_ETFS}
    universe = {**discovered, **curated}                      # curated wins on exchange naming
    log(f"{len(universe)} US candidates ({len(discovered)} discovered, {len(curated)} built-in).")

    # 2. Which of them actually trade ----------------------------------------
    if args.all or not discovered:
        chosen = sorted(universe)
        log(f"No volume screen; taking all {len(chosen)}.")
    else:
        only_found = sorted(set(discovered) - set(curated))
        log(f"Screening {len(only_found)} of them on volume (over ${args.min_volume:,.0f} a day) …")
        liquid = liquid_only(only_found, args.min_volume)
        chosen = sorted(set(liquid) | set(curated))
        log(f"{len(liquid)} passed, plus {len(curated)} built-in = {len(chosen)} ETFs to update.")

    targets = [(s, "US", universe.get(s, "NYSE Arca/BATS"), "USD") for s in chosen] \
            + [(s, "Switzerland", "SIX", "CHF") for s in sorted(set(CH_ETFS))]

    # 3. Oldest and newest first, so a short run still gets somewhere ---------
    if not args.dry_run:
        try:
            have = {r["symbol"]: r.get("fundamentals_updated_at") or ""
                    for r in db.select("symbol,fundamentals_updated_at", {"asset_class": "eq.etf"})}
        except Exception as e:
            log(f"  couldn't read what's already stored ({str(e)[:60]}); carrying on in symbol order")
            have = {}
        targets.sort(key=lambda t: (t[0] in have, have.get(t[0], "")))
        fresh = sum(1 for t in targets if t[0] not in have)
        if fresh:
            log(f"{fresh} of them are new, and go first.")
    if args.limit:
        targets = targets[:args.limit]

    # 4. The details ---------------------------------------------------------
    rows, done, skipped = [], 0, 0
    for i, (sym, region, exch, default_cur) in enumerate(targets, 1):
        if out_of_time():
            log(f"  deadline reached after {i - 1}/{len(targets)} — the rest continue next run.")
            break
        info = fetch_info(sym)
        qt = info.get("quoteType")
        price = num(info.get("regularMarketPrice") or info.get("navPrice") or info.get("previousClose"))
        if not price or qt not in (None, "ETF", "MUTUALFUND"):
            skipped += 1
            if i % 100 == 0:
                log(f"  {i}/{len(targets)} (skipped {skipped})")
            continue
        cur = info.get("currency") or default_cur
        prev = num(info.get("regularMarketPreviousClose") or info.get("previousClose"))
        fx = to_usd(cur) or 1.0
        aum = num(info.get("totalAssets"))
        exp_ratio = info.get("netExpenseRatio") or info.get("annualReportExpenseRatio")
        yld = info.get("yield") or info.get("trailingAnnualDividendYield")
        name = info.get("longName") or info.get("shortName") or sym
        cat = info.get("category") or info.get("fundFamily") or "ETF"
        rows.append({
            "symbol": sym,
            "name": name,
            "exchange": exch,
            "region": region,
            "asset_class": "etf",
            "currency": cur,
            "sector": "Fund",
            "industry": cat,
            "summary": (info.get("longBusinessSummary") or "").strip()[:800] or None,
            "price": price,
            "change_pct": round((price / prev - 1) * 100, 3) if prev else None,
            "market_cap_usd_b": round(aum * fx / 1e9, 3) if aum and fx else None,
            "dividend_yield": num(yld, scale=100 if yld and yld < 1 else 1, digits=3),
            "return_1y": num(info.get("52WeekChange"), scale=100, digits=2),
            "beta": num(info.get("beta3Year") or info.get("beta"), digits=3),
            "expense_ratio": num(exp_ratio, scale=100 if exp_ratio and exp_ratio < 1 else 1, digits=3),
            "high_52w": num(info.get("fiftyTwoWeekHigh")),
            "low_52w": num(info.get("fiftyTwoWeekLow")),
            "quote_updated_at": now_iso(),
            "fundamentals_updated_at": now_iso(),
        })
        done += 1
        if i % 100 == 0:
            log(f"  {i}/{len(targets)} ({done} stored, {skipped} skipped)")
        if len(rows) >= 50 and not args.dry_run:
            db.upsert(rows)
            rows = []
        time.sleep(0.4)

    if rows and not args.dry_run:
        db.upsert(rows)
    log(f"{'Would store' if args.dry_run else 'Stored'} {done} ETFs, skipped {skipped}.")

    # 5. Housekeeping --------------------------------------------------------
    # The keep-set is the WHOLE universe, not just what passed the volume
    # screen — an ETF that went quiet this month should stop being refreshed,
    # not be deleted. Capped, so a bad listing can't clear the table.
    if args.dry_run or args.limit or args.no_delete:
        return
    if not discovered:
        # The universe collapsed back to the built-in list, so almost every
        # stored ETF would look "no longer listed". Deleting on that basis
        # would chip away at real funds every run, a few dozen at a time.
        log("Housekeeping skipped: the listing wasn't available, so there is nothing to compare against.")
        return
    keep = set(universe) | set(CH_ETFS) | {s for s, *_ in targets}
    try:
        current = {r["symbol"] for r in db.select("symbol", {"asset_class": "eq.etf"})}
    except Exception as e:
        log(f"  housekeeping skipped: {str(e)[:80]}")
        return
    gone = current - keep
    if not gone:
        return
    cap = max(MIN_DROP_ALLOWED, int(len(current) * MAX_DROP_FRACTION))
    if len(gone) > cap:
        log("")
        log("=" * 68)
        log(f"REFUSING TO DELETE: {len(gone)} of {len(current)} ETF rows are no longer in the "
            f"listing (the limit for one run is {cap}).")
        log(f"A few of them: {', '.join(sorted(gone)[:20])}")
        log("Nothing was deleted.")
        log("=" * 68)
        return
    try:
        db.delete_symbols(gone)
        log(f"Removed {len(gone)} ETFs no longer listed: {', '.join(sorted(gone)[:20])}"
            f"{' …' if len(gone) > 20 else ''}")
    except Exception as e:
        log(f"  delete failed: {str(e)[:80]}")


if __name__ == "__main__":
    main()
