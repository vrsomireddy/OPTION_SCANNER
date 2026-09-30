#!/usr/bin/env python3
"""
Daily cash-secured put scanner for Nasdaq-100 tech names.

Suggestions only. This script never connects to a broker and never places orders.

Pipeline
  1. Pull fresh quotes + option chains from Yahoo Finance (yfinance).
  2. Filter underlyings by price and liquidity.
  3. For each expiration in the DTE window (optionally skipping earnings),
     pull the put chain and compute Black-Scholes delta from implied vol.
  4. Keep puts in the delta band with acceptable spread/OI and a bid that
     clears the annualized-return hurdle on cash collateral.
  5. Score, rank, keep the best contract per ticker, print top N,
     write a markdown report, and optionally email/SMS it.
"""

from __future__ import annotations

import logging
import math
import os
import smtplib
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
import yfinance as yf
from scipy.stats import norm

import config as C

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("put_scanner")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class PutCandidate:
    ticker: str
    spot: float
    expiration: str
    dte: int
    strike: float
    bid: float
    ask: float
    mid: float
    iv: float
    delta: float
    open_interest: int
    volume: int
    spread_pct: float
    cushion_pct: float          # (spot - strike) / spot
    premium_per_contract: float  # bid * 100
    collateral: float           # strike * 100
    annualized_return: float    # bid / strike * 365 / dte
    breakeven: float            # strike - bid
    prob_otm: float             # 1 - |delta|
    earnings_date: str | None
    score: float = 0.0
    hv: float = float("nan")            # stock's realized volatility over HV_LOOKBACK
    iv_hv_ratio: float = float("nan")   # iv / hv; above 1 means options price in more movement than usual


class ScanDataError(Exception):
    """Identify a stock whose required data could not be verified."""


class ScanSkipped(Exception):
    """Identify a stock excluded before its contracts were evaluated."""


@dataclass
class PreviousScan:
    day: date
    candidates: list[PutCandidate]
    displayed: list[PutCandidate]


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------
def bs_put_delta(spot: float, strike: float, t_years: float, r: float, iv: float) -> float:
    """Estimate how sensitive an option's price is to stock moves, used to gauge how risky a put is."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        return float("nan")
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    return norm.cdf(d1) - 1.0


# ---------------------------------------------------------------------------
# Data fetch helpers (each wrapped so one bad ticker never kills the run)
# ---------------------------------------------------------------------------
def get_spot_and_volume(tk: yf.Ticker) -> tuple[float | None, float | None]:
    """Look up a stock's current price and typical daily trading volume."""
    try:
        fi = tk.fast_info
        spot = float(fi["last_price"])
        avg_vol = float(fi.get("ten_day_average_volume") or fi.get("three_month_average_volume") or 0)
        if not avg_vol:
            hist = tk.history(period="10d", auto_adjust=False)
            avg_vol = float(hist["Volume"].mean()) if not hist.empty else 0.0
        if spot and spot > 0:
            return spot, (avg_vol or None)
    except Exception:
        pass
    try:
        hist = tk.history(period="10d", auto_adjust=False)
        if hist.empty:
            return None, None
        return float(hist["Close"].iloc[-1]), float(hist["Volume"].mean())
    except Exception as e:
        log.warning("%s: could not get price (%s)", tk.ticker, e)
        return None, None


def get_realized_vol(tk: yf.Ticker) -> float | None:
    """Measure how much the stock has actually moved over the past year, skipping recently listed stocks."""
    try:
        hist = tk.history(period=C.HV_LOOKBACK, auto_adjust=True)
        closes = hist["Close"].dropna()
        returns = (closes / closes.shift(1)).apply(math.log).dropna()
    except Exception as e:
        log.warning("%s: could not get price history (%s)", tk.ticker, e)
        return None
    if returns.empty:
        return None
    if len(returns) < C.HV_MIN_DAYS:
        raise ScanSkipped("too little price history for realized volatility")
    hv = float(returns.std() * math.sqrt(252))
    return hv if math.isfinite(hv) and hv > 0 else None


def get_next_earnings(tk: yf.Ticker, today: date | None = None) -> date | None:
    """Find the company's next known earnings announcement on or after the scan day."""
    today = today if today is not None else date.today()
    try:
        df = tk.get_earnings_dates(limit=8)
        if df is not None and not df.empty:
            dates = [d.date() for d in df.index if d.date() >= today]
            if dates:
                return min(dates)
    except Exception:
        pass
    try:
        cal = tk.calendar
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed:
                ed = ed if isinstance(ed, list) else [ed]
                dates = [pd.Timestamp(d).date() for d in ed]
                dates = [d for d in dates if d >= today]
                if dates:
                    return min(dates)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def scan_ticker(symbol: str, today: date) -> list[PutCandidate]:
    """Find qualifying puts for one stock or explain why it could not be evaluated."""
    if symbol in C.EXCLUDE_TICKERS:
        raise ScanSkipped("excluded in config")
    tk = yf.Ticker(symbol)
    spot, avg_vol = get_spot_and_volume(tk)
    if spot is None or not math.isfinite(spot) or spot <= 0:
        raise ScanDataError("stock price unavailable")
    if C.MAX_STOCK_PRICE and spot > C.MAX_STOCK_PRICE:
        log.info("%s: skip, price %.2f > %.0f", symbol, spot, C.MAX_STOCK_PRICE)
        raise ScanSkipped("stock price exceeds limit")
    if C.MIN_AVG_STOCK_VOLUME > 0 and (avg_vol is None or not math.isfinite(avg_vol) or avg_vol <= 0):
        raise ScanDataError("average stock volume unavailable")
    if avg_vol is not None and avg_vol < C.MIN_AVG_STOCK_VOLUME:
        log.info("%s: skip, avg volume %.0f too low", symbol, avg_vol)
        raise ScanSkipped("average stock volume below limit")

    earnings = get_next_earnings(tk, today) if C.SKIP_EARNINGS else None
    if C.SKIP_EARNINGS and earnings is None:
        raise ScanDataError("next earnings date unavailable")

    try:
        expirations = tk.options
    except Exception as e:
        log.warning("%s: no option chain (%s)", symbol, e)
        raise ScanDataError("option expirations unavailable") from e
    if not expirations:
        raise ScanSkipped("no listed option expirations")

    hv = get_realized_vol(tk)
    if hv is None:
        raise ScanDataError("realized volatility unavailable")

    out: list[PutCandidate] = []
    for exp_str in expirations:
        exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp - today).days
        if dte < C.DTE_MIN or dte > C.DTE_MAX:
            continue
        if earnings and today <= earnings <= exp:
            log.info("%s: skip %s, spans earnings %s", symbol, exp_str, earnings)
            continue

        try:
            puts = tk.option_chain(exp_str).puts
        except Exception as e:
            log.warning("%s %s: chain fetch failed (%s)", symbol, exp_str, e)
            raise ScanDataError(f"option chain unavailable for {exp_str}") from e
        if puts is None or puts.empty:
            raise ScanDataError(f"put chain empty for {exp_str}")

        t_years = dte / 365.0
        for row in puts.itertuples(index=False):
            strike = float(row.strike)
            bid = float(row.bid) if not pd.isna(row.bid) else 0.0
            ask = float(row.ask) if not pd.isna(row.ask) else 0.0
            iv = float(row.impliedVolatility) if not pd.isna(row.impliedVolatility) else 0.0
            oi = int(row.openInterest) if not pd.isna(row.openInterest) else 0
            vol = int(row.volume) if not pd.isna(row.volume) else 0

            if strike >= spot:            # OTM puts only
                continue
            if C.MAX_COLLATERAL and strike * 100 > C.MAX_COLLATERAL:
                continue
            if bid < C.MIN_BID or ask <= 0 or ask < bid:
                continue
            if oi < C.MIN_OPEN_INTEREST:
                continue
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid if mid > 0 else 1.0
            if spread_pct > C.MAX_SPREAD_PCT:
                continue

            delta = bs_put_delta(spot, strike, t_years, C.RISK_FREE_RATE, iv)
            if math.isnan(delta):
                continue
            adelta = abs(delta)
            if adelta < C.DELTA_MIN or adelta > C.DELTA_MAX:
                continue

            ann = (bid / strike) * (365.0 / dte)
            if ann < C.MIN_ANNUALIZED_RETURN:
                continue

            out.append(
                PutCandidate(
                    ticker=symbol,
                    spot=round(spot, 2),
                    expiration=exp_str,
                    dte=dte,
                    strike=strike,
                    bid=bid,
                    ask=ask,
                    mid=round(mid, 2),
                    iv=round(iv, 4),
                    delta=round(delta, 3),
                    open_interest=oi,
                    volume=vol,
                    spread_pct=round(spread_pct, 4),
                    cushion_pct=round((spot - strike) / spot, 4),
                    premium_per_contract=round(bid * 100, 2),
                    collateral=round(strike * 100, 2),
                    annualized_return=round(ann, 4),
                    breakeven=round(strike - bid, 2),
                    prob_otm=round(1 - adelta, 3),
                    earnings_date=earnings.isoformat() if earnings else None,
                    hv=round(hv, 4),
                    iv_hv_ratio=round(iv / hv, 3),
                )
            )
    return out


def score(cands: list[PutCandidate]) -> list[PutCandidate]:
    """Rate and rank each candidate by how attractive a trade it is, best first."""
    band = C.DELTA_MAX - C.DELTA_MIN
    for c in cands:
        ratio = c.iv_hv_ratio if math.isfinite(c.iv_hv_ratio) else 1.0   # unknown earns no richness credit
        rich = min(max((ratio - 1.0) / (C.IV_HV_FULL_CREDIT - 1.0), 0.0), 1.0)
        y = min(c.annualized_return, 0.60) / 0.60
        s = min(max((C.DELTA_MAX - abs(c.delta)) / band, 0.0), 1.0) if band > 0 else 1.0
        cu = min(c.cushion_pct, 0.20) / 0.20
        c.score = round(C.W_RICHNESS * rich + C.W_YIELD * y + C.W_SAFETY * s + C.W_CUSHION * cu, 4)
    cands.sort(key=lambda c: c.score, reverse=True)
    return cands


def sector_group(ticker: str) -> str | None:
    """Name the configured sector group a stock belongs to, if any."""
    return next((name for name, members in C.SECTOR_GROUPS.items() if ticker in members), None)


def group_is_full(ticker: str, shown: list[PutCandidate]) -> bool:
    """Tell whether the stock's sector group already has its maximum number of names shown."""
    group = sector_group(ticker)
    if group is None:
        return False
    return len({c.ticker for c in shown if sector_group(c.ticker) == group}) >= C.MAX_PER_GROUP


def pick_top(cands: list[PutCandidate]) -> list[PutCandidate]:
    """Select the day's best suggestions, limiting repeats of the same stock or sector group."""
    seen: dict[str, int] = {}
    picks = []
    for c in cands:
        if seen.get(c.ticker, 0) >= C.MAX_PER_TICKER:
            continue
        if c.ticker not in seen and group_is_full(c.ticker, picks):
            continue
        seen[c.ticker] = seen.get(c.ticker, 0) + 1
        picks.append(c)
        if len(picks) >= C.TOP_N:
            break
    return picks


def pick_alternatives(ranked: list[PutCandidate], picks: list[PutCandidate]) -> list[PutCandidate]:
    """Choose additional qualifying stocks in score order without repeating a name or overfilling a sector group."""
    seen = {c.ticker for c in picks}
    alternatives = []
    for c in ranked:
        if len(alternatives) >= C.ALTERNATIVE_N:
            break
        if c.ticker not in seen and not group_is_full(c.ticker, picks + alternatives):
            alternatives.append(c)
            seen.add(c.ticker)
    return alternatives


def contract_key(c: PutCandidate) -> tuple[str, str, float]:
    """Identify a put by its stock, expiration, and exact strike."""
    return c.ticker, c.expiration, c.strike


OPTIONAL_FIELDS = {"hv", "iv_hv_ratio"}   # absent from reports written before these were recorded


def load_previous_scan(out_dir: Path, today: date) -> PreviousScan | None:
    """Read the latest usable earlier daily results for comparison."""
    for path in sorted(out_dir.glob("all_candidates_*.csv"), reverse=True):
        try:
            day = date.fromisoformat(path.stem.removeprefix("all_candidates_"))
            if day >= today:
                continue
            df = pd.read_csv(path)
            names = [field.name for field in fields(PutCandidate) if field.name in df or field.name not in OPTIONAL_FIELDS]
            records = df[names].to_dict("records")
            candidates = [PutCandidate(**row) for row in records]
            for c in candidates:
                if not all(math.isfinite(getattr(c, name)) for name in
                           ("spot", "strike", "bid", "ask", "dte", "score", "annualized_return")):
                    raise ValueError("invalid comparison values")
            if "report_group" in df:
                displayed = [c for c, group in zip(candidates, df["report_group"], strict=True)
                             if group in ("top", "alternative")]
            else:
                displayed = pick_top(candidates)
            return PreviousScan(day, candidates, displayed)
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("Skipping unreadable comparison history %s: %s", path.name, e)
    return None


def describe_change(c: PutCandidate, previous: PreviousScan | None) -> str:
    """Explain what changed and flag unchanged prices without claiming they are stale."""
    if previous is None:
        return "No previous report available for comparison."
    shown = [old for old in previous.displayed if old.ticker == c.ticker]
    if not shown:
        status = "New ticker in recommendations"
    elif any(contract_key(old) == contract_key(c) for old in shown):
        status = "Repeat contract"
    else:
        old = shown[0]
        status = f"Changed contract (previously {old.expiration} {old.strike:g}P)"
    old = next((old for old in previous.candidates if contract_key(old) == contract_key(c)), None)
    if old is None:
        return status + "; no prior quote for this exact contract, bid/score changes unavailable."
    note = f"{status}; same-contract bid change {c.bid - old.bid:+.2f}, score change {c.score - old.score:+.4f}."
    if (c.spot, c.bid, c.ask) == (old.spot, old.bid, old.ask):
        note += " Potentially stale: spot, bid and ask unchanged; freshness unverified."
        if c.dte < old.dte and c.annualized_return > old.annualized_return:
            note += " Annualized return rose as DTE fell with the same bid; no better quoted premium."
    return note


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def to_markdown(
    picks: list[PutCandidate], scanned: int, total_cands: int, run_ts: datetime,
    failures: dict[str, str] | None = None, skipped: dict[str, str] | None = None,
    alternatives: list[PutCandidate] | None = None, previous: PreviousScan | None = None,
) -> str:
    """Present ranked picks, alternatives, daily changes, and data limitations."""
    lines = [
        f"# Cash-Secured Put Suggestions - {run_ts.strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        f"Universe: {scanned} Nasdaq-100 names scanned · {total_cands} contracts passed filters · "
        f"top {len(picks)} shown. Data: Yahoo Finance (may be ~15 min delayed). "
        "**Suggestions only; nothing is executed.**",
        "",
        f"Filters: |Δ| {C.DELTA_MIN:.2f}–{C.DELTA_MAX:.2f} · DTE {C.DTE_MIN}–{C.DTE_MAX} · "
        f"≥{C.MIN_ANNUALIZED_RETURN:.0%} annualized · OI ≥ {C.MIN_OPEN_INTEREST} · "
        f"spread ≤ {C.MAX_SPREAD_PCT:.0%}"
        + (" · earnings skipped" if C.SKIP_EARNINGS else "")
        + (f" · collateral ≤ ${C.MAX_COLLATERAL:,.0f}" if C.MAX_COLLATERAL else "")
        + "".join(f" · ≤{C.MAX_PER_GROUP} {name}" for name in C.SECTOR_GROUPS),
        "",
    ]
    if skipped:
        lines += [f"Stocks skipped before contract evaluation: {len(skipped)}.", ""]
        lines += [f"- {symbol}: {reason}" for symbol, reason in skipped.items()]
        lines.append("")
    if failures:
        lines += [f"**INCOMPLETE SCAN: required data failed for {len(failures)} stocks.**", ""]
        lines += [f"- {symbol}: {reason}" for symbol, reason in failures.items()]
        lines.append("")
    if not picks:
        lines.append("_No qualifying contracts in the available data; the scan is incomplete._" if failures
                     else "_No contracts met the criteria today._")
        return "\n".join(lines)

    lines += [
        "| # | Ticker | Spot | Exp | DTE | Strike | Bid | Δ | P(OTM) | Cushion | IV/HV "
        "| Ann. Ret | Premium/ct | Collateral | Breakeven | Score |",
        "|---|--------|------|-----|-----|--------|-----|---|--------|---------|-------"
        "|----------|------------|------------|-----------|-------|",
    ]
    for i, c in enumerate(picks, 1):
        lines.append(
            f"| {i} | **{c.ticker}** | {c.spot:.2f} | {c.expiration} | {c.dte} | {c.strike:.2f} | "
            f"{c.bid:.2f} | {c.delta:.2f} | {c.prob_otm:.0%} | {c.cushion_pct:.1%} | {c.iv_hv_ratio:.2f} | "
            f"{c.annualized_return:.1%} | ${c.premium_per_contract:,.0f} | ${c.collateral:,.0f} | "
            f"{c.breakeven:.2f} | {c.score:.3f} |"
        )
    lines += ["", "### Trade descriptions", ""]
    for i, c in enumerate(picks, 1):
        lines.append(
            f"{i}. **SELL TO OPEN 1 {c.ticker} {c.expiration} {str(c.strike).removesuffix('.0')}P @ {c.bid:.2f} (limit)**: "
            f"collect ~${c.premium_per_contract:,.0f} against ${c.collateral:,.0f} cash. "
            f"IV {c.iv:.0%} vs {c.hv:.0%} realized ({c.iv_hv_ratio:.2f}x), OI {c.open_interest:,}, spread {c.spread_pct:.1%}. "
            f"Assigned below {c.strike:.2f}; breakeven {c.breakeven:.2f} "
            f"({c.cushion_pct:.1%} below spot)."
            + (f" Next earnings {c.earnings_date} (after expiry)." if c.earnings_date else "")
        )
    lines += ["", "### Changes since the previous report", ""]
    if previous:
        lines += [f"Compared with {previous.day.isoformat()}. Bid and score changes compare the exact same contract.", ""]
    lines += [f"- **{c.ticker} {c.expiration} {c.strike:g}P**: {describe_change(c, previous)}" for c in picks]
    lines += ["", "### Alternative names", ""]
    if alternatives:
        lines += [
            "Next qualifying names by score, excluding the top-pick tickers.", "",
            "| Ticker | Expiration | Strike | Bid | Collateral | Ann. Ret | Score | Changes |",
            "|--------|------------|--------|-----|------------|----------|-------|---------|",
        ]
        for c in alternatives:
            lines.append(
                f"| {c.ticker} | {c.expiration} | {c.strike:g} | {c.bid:.2f} | ${c.collateral:,.0f} | "
                f"{c.annualized_return:.1%} | {c.score:.4f} | {describe_change(c, previous)} |"
            )
    else:
        lines.append("No additional names selected.")
    lines += ["", "Unchanged quotes are a freshness warning, not proof of stale data. Quote timestamps are unavailable."]
    lines += ["", "_Not financial advice. Verify quotes with your broker before trading._"]
    return "\n".join(lines)


def to_sms(
    picks: list[PutCandidate], run_ts: datetime, previous: PreviousScan | None = None,
    alternatives: list[PutCandidate] | None = None,
) -> str:
    """Summarize picks, changes, and alternative names for text delivery."""
    if not picks:
        return f"Put scan {run_ts:%m/%d}: no qualifying contracts."
    parts = [f"Put scan {run_ts:%m/%d}:"]
    for c in picks:
        parts.append(f"{c.ticker} {c.expiration[5:]} {str(c.strike).removesuffix('.0')}P bid {c.bid:.2f} "
                     f"d{abs(c.delta):.2f} {c.annualized_return:.0%}ann")
        parts.append(describe_change(c, previous))
    if alternatives:
        parts.append("Alternatives: " + ", ".join(c.ticker for c in alternatives) + ". See full report for quotes and changes.")
    return "\n".join(parts)


def send_email(subject: str, body: str, to_addr: str, subtype: str = "plain") -> None:
    """Send the requested email or report why delivery could not be completed."""
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")
    if not (user and pw):
        raise ValueError("SMTP_USER/SMTP_PASS required for requested delivery")
    msg = MIMEText(body, subtype)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(user, [to_addr], msg.as_string())
    log.info("sent to %s", to_addr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    """Deliver ranked picks and alternatives with historical comparisons and run status."""
    run_ts = datetime.now().astimezone()
    if "--test-email" in sys.argv:
        to_addr = os.getenv("EMAIL_TO")
        if not to_addr:
            log.error("EMAIL_TO not set; nothing to test")
            return 1
        try:
            send_email(f"Put scanner test {run_ts:%Y-%m-%d %H:%M}",
                       "SMTP is configured correctly. Daily reports will arrive here.", to_addr)
        except smtplib.SMTPAuthenticationError as e:
            log.error("Gmail rejected the login: %s", e.smtp_error.decode(errors="replace").splitlines()[0])
            log.error("Use a Gmail App Password (myaccount.google.com/apppasswords), not your account password")
            return 1
        except Exception as e:
            log.error("email failed: %s", e)
            return 1
        return 0
    today = run_ts.date()
    all_cands: list[PutCandidate] = []
    scanned = 0
    failures: dict[str, str] = {}
    skipped: dict[str, str] = {}

    for sym in C.UNIVERSE:
        try:
            found = scan_ticker(sym, today)
            scanned += 1
            log.info("%s: %d candidates", sym, len(found))
            all_cands.extend(found)
        except ScanSkipped as e:
            skipped[sym] = str(e)
        except Exception as e:
            failures[sym] = str(e)
            log.error("%s: scan failed: %s", sym, e)
        time.sleep(0.3)  # be polite to Yahoo

    ranked = score(all_cands)
    picks = pick_top(ranked)
    alternatives = pick_alternatives(ranked, picks)
    out_dir = Path(__file__).resolve().parent / C.OUTPUT_DIR
    previous = load_previous_scan(out_dir, today)

    md = to_markdown(picks, scanned, len(ranked), run_ts, failures, skipped, alternatives, previous)
    print(md)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"puts_{run_ts:%Y-%m-%d}.md"
    out_file.write_text(md)
    groups = {contract_key(c): "top" for c in picks}
    groups.update({contract_key(c): "alternative" for c in alternatives})
    rows = [dict(asdict(c), report_group=groups.get(contract_key(c), ""), comparison_date=previous.day if previous else "",
                 change_note=describe_change(c, previous)) for c in ranked]
    pd.DataFrame(rows, columns=[field.name for field in fields(PutCandidate)] +
                 ["report_group", "comparison_date", "change_note"]).to_csv(
        out_dir / f"all_candidates_{run_ts:%Y-%m-%d}.csv", index=False,
    )
    log.info("wrote %s", out_file)

    subject = f"Put suggestions {run_ts:%Y-%m-%d}: " + (", ".join(c.ticker for c in picks) or "none")
    if failures:
        subject = "INCOMPLETE: " + subject
    delivery_failed = False
    if os.getenv("EMAIL_TO"):
        try:
            send_email(subject, md, os.getenv("EMAIL_TO"))
        except Exception as e:
            log.error("email failed: %s", e)
            delivery_failed = True
    if os.getenv("SMS_TO"):
        try:
            sms = to_sms(picks, run_ts, previous, alternatives)
            if failures:
                sms = f"INCOMPLETE SCAN: {len(failures)} stocks failed. " + (
                    sms if picks else "No qualifying contracts in available data."
                )
            send_email("", sms, os.getenv("SMS_TO"))
        except Exception as e:
            log.error("sms failed: %s", e)
            delivery_failed = True
    return 1 if failures or delivery_failed else 0


if __name__ == "__main__":
    sys.exit(main())
