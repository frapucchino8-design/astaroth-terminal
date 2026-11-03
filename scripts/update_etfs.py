"""ETFs via Yahoo Finance. A built-in list of ~170 US-listed ETFs and a handful of
Swiss-listed ETFs, with price, AUM, expense ratio, yield and 1-year return.

Yahoo is unofficial: fine for a personal project, not for a paid public site.
"""
import argparse
import time

import yfinance as yf

from common import Supabase, log, now_iso, num, to_usd

# US-listed ETFs. One line per theme, so adding or removing is easy.
US_ETFS = """
SPY VOO IVV VTI QQQ QQQM SPLG DIA IWM MDY VTV VUG VV MGK MGV IWF IWD IWR IJS IJT IJR IJH ITOT RSP
VEA VXUS VWO IEFA IEMG EFA EEM VEU VNQI SCHE SCHF SCHX SCHA SCHG SCHV VBR VXF SCHD VYM VIG DVY NOBL
HDV SPHD SDY FDL DGRO DGRW VPU VDC VDE VOX VCR VFH VIS VAW VHT VGT MTUM USMV SPLV QUAL
XLF XLE XLK XLV XLI XLY XLP XLU XLB XLRE XLC
SMH SOXX XSD KRE KBE XHB ITB XME GDX GDXJ SIL TAN LIT URA ICLN KWEB CQQQ
VNQ VNQI SCHH REM MORT PFF PGX PGF PFFA
AGG BND BNDX VTIP TIP VGSH VGIT VGLT GOVT SHY IEF TLT IEI BIL SGOV SCHO SCHP SCHZ
LQD VCIT VCSH HYG JNK EMB PCY EMLC VWOB
GLD IAU SLV PPLT PALL CPER WOOD USO UNG BNO DBO DBA DBC CORN WEAT SOYB CANE SGG JO
JEPI JEPQ DIVO QYLD RYLD XYLD AMLP MLPX AMZA
ARKK ARKQ ARKG ARKW ARKF ARKX FXI MCHI EWJ EWG EWU EWC EWA EWY EWT EWW EWZ INDA EWH EWS EWL EZU EPOL
TQQQ SQQQ UPRO SOXL FAS FAZ SPXL SPXS TNA TZA SDS SSO QID QLD
VXX UVXY SVIX UVIX
BIL SGOV SHV SHYG USFR TBLL FLOT FLRN
"""

CH_ETFS = """
SMMCHA.SW CHSPI.SW EEMM.SW XMME.SW EUNA.SW IWRD.SW SPGL.SW VWRL.SW VUSA.SW VEUR.SW IEMM.SW EUNK.SW
CSSPX.SW CSPXJ.SW CSBGC0.SW CSBGC3.SW CSBGU3.SW CSBGU7.SW CSAGG.SW CBND.SW
""".split()


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
    args = ap.parse_args()

    db = Supabase()
    us = sorted(set(US_ETFS.split()))
    ch = sorted(set(CH_ETFS))
    all_etfs = [(s, "US", "NYSE Arca/BATS", "USD") for s in us] + [(s, "Switzerland", "SIX", "CHF") for s in ch]
    if args.limit:
        all_etfs = all_etfs[:args.limit]

    rows, skipped = [], 0
    for i, (sym, region, exch, default_cur) in enumerate(all_etfs, 1):
        info = fetch_info(sym)
        qt = info.get("quoteType")
        price = num(info.get("regularMarketPrice") or info.get("navPrice") or info.get("previousClose"))
        if not price or qt not in (None, "ETF", "MUTUALFUND"):
            skipped += 1
            if i % 20 == 0:
                log(f"  {i}/{len(all_etfs)} (skipped {skipped})")
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
        if i % 20 == 0:
            log(f"  {i}/{len(all_etfs)}")
        if len(rows) >= 50:
            db.upsert(rows)
            rows = []
        time.sleep(0.5)

    if rows:
        db.upsert(rows)
    log(f"Updated {len(all_etfs) - skipped} ETFs, skipped {skipped}.")

    # Housekeeping: drop ETFs that left the list.
    if not args.limit:
        gone = db.delete_asset_class("etf", {s for s, *_ in all_etfs})
        if gone:
            log(f"Removed {gone} ETFs no longer on the list.")


if __name__ == "__main__":
    main()
