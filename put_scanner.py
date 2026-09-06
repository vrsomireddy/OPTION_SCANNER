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
from dataclasses import asdict, dataclass
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


def get_next_earnings(tk: yf.Ticker) -> date | None:
    """Find the company's next earnings announcement date, if known."""
    today = date.today()
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
                dates = [d if isinstance(d, date) else pd.Timestamp(d).date() for d in ed]
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
    """Check one stock and return every put option worth considering for it."""
    tk = yf.Ticker(symbol)
    spot, avg_vol = get_spot_and_volume(tk)
    if spot is None:
        return []
    if C.MAX_STOCK_PRICE and spot > C.MAX_STOCK_PRICE:
        log.info("%s: skip, price %.2f > %.0f", symbol, spot, C.MAX_STOCK_PRICE)
        return []
    if avg_vol and avg_vol < C.MIN_AVG_STOCK_VOLUME:
        log.info("%s: skip, avg volume %.0f too low", symbol, avg_vol)
        return []

    earnings = get_next_earnings(tk) if C.SKIP_EARNINGS else None

    try:
        expirations = tk.options
    except Exception as e:
        log.warning("%s: no option chain (%s)", symbol, e)
        return []

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
            continue
        if puts is None or puts.empty:
            continue

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
                )
            )
    return out


def score(cands: list[PutCandidate]) -> list[PutCandidate]:
    """Rate and rank each candidate by how attractive a trade it is, best first."""
    for c in cands:
        y = min(c.annualized_return, 0.60) / 0.60
        s = c.prob_otm
        cu = min(c.cushion_pct, 0.20) / 0.20
        c.score = round(C.W_YIELD * y + C.W_SAFETY * s + C.W_CUSHION * cu, 4)
    cands.sort(key=lambda c: c.score, reverse=True)
    return cands


def pick_top(cands: list[PutCandidate]) -> list[PutCandidate]:
    """Select the day's best suggestions, limiting how many come from the same stock."""
    seen: dict[str, int] = {}
    picks = []
    for c in cands:
        if seen.get(c.ticker, 0) >= C.MAX_PER_TICKER:
            continue
        seen[c.ticker] = seen.get(c.ticker, 0) + 1
        picks.append(c)
        if len(picks) >= C.TOP_N:
            break
    return picks


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def to_markdown(picks: list[PutCandidate], scanned: int, total_cands: int, run_ts: datetime) -> str:
    """Turn the day's picks into a readable report."""
    lines = [
        f"# Cash-Secured Put Suggestions — {run_ts.strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        f"Universe: {scanned} Nasdaq-100 names scanned · {total_cands} contracts passed filters · "
        f"top {len(picks)} shown. Data: Yahoo Finance (may be ~15 min delayed). "
        "**Suggestions only — nothing is executed.**",
        "",
        f"Filters: |Δ| {C.DELTA_MIN:.2f}–{C.DELTA_MAX:.2f} · DTE {C.DTE_MIN}–{C.DTE_MAX} · "
        f"≥{C.MIN_ANNUALIZED_RETURN:.0%} annualized · OI ≥ {C.MIN_OPEN_INTEREST} · "
        f"spread ≤ {C.MAX_SPREAD_PCT:.0%}"
        + (" · earnings skipped" if C.SKIP_EARNINGS else ""),
        "",
    ]
    if not picks:
        lines.append("_No contracts met the criteria today._")
        return "\n".join(lines)

    lines += [
        "| # | Ticker | Spot | Exp | DTE | Strike | Bid | Δ | P(OTM) | Cushion | Ann. Ret | Premium/ct | Collateral | Breakeven | Score |",
        "|---|--------|------|-----|-----|--------|-----|---|--------|---------|----------|------------|------------|-----------|-------|",
    ]
    for i, c in enumerate(picks, 1):
        lines.append(
            f"| {i} | **{c.ticker}** | {c.spot:.2f} | {c.expiration} | {c.dte} | {c.strike:.2f} | "
            f"{c.bid:.2f} | {c.delta:.2f} | {c.prob_otm:.0%} | {c.cushion_pct:.1%} | "
            f"{c.annualized_return:.1%} | ${c.premium_per_contract:,.0f} | ${c.collateral:,.0f} | "
            f"{c.breakeven:.2f} | {c.score:.3f} |"
        )
    lines += ["", "### Trade descriptions", ""]
    for i, c in enumerate(picks, 1):
        lines.append(
            f"{i}. **SELL TO OPEN 1 {c.ticker} {c.expiration} {c.strike:.0f}P @ {c.bid:.2f} (limit)** — "
            f"collect ~${c.premium_per_contract:,.0f} against ${c.collateral:,.0f} cash. "
            f"IV {c.iv:.0%}, OI {c.open_interest:,}, spread {c.spread_pct:.1%}. "
            f"Assigned below {c.strike:.2f}; breakeven {c.breakeven:.2f} "
            f"({c.cushion_pct:.1%} below spot)."
            + (f" Next earnings {c.earnings_date} (after expiry)." if c.earnings_date else "")
        )
    lines += ["", "_Not financial advice. Verify quotes with your broker before trading._"]
    return "\n".join(lines)


def to_sms(picks: list[PutCandidate], run_ts: datetime) -> str:
    """Condense the day's picks into a short text-message-friendly summary."""
    if not picks:
        return f"Put scan {run_ts:%m/%d}: no qualifying contracts."
    parts = [f"Put scan {run_ts:%m/%d}:"]
    for c in picks:
        parts.append(f"{c.ticker} {c.expiration[5:]} {c.strike:.0f}P bid {c.bid:.2f} "
                     f"d{abs(c.delta):.2f} {c.annualized_return:.0%}ann")
    return "\n".join(parts)


def send_email(subject: str, body: str, to_addr: str, subtype: str = "plain") -> None:
    """Send the report to someone's inbox by email."""
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")
    if not (user and pw):
        log.warning("SMTP_USER/SMTP_PASS not set; skipping send to %s", to_addr)
        return
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
    """Run the full daily scan: check every stock, rank the results, and deliver the report."""
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

    for sym in C.UNIVERSE:
        try:
            found = scan_ticker(sym, today)
            scanned += 1
            log.info("%s: %d candidates", sym, len(found))
            all_cands.extend(found)
        except Exception as e:
            log.error("%s: unexpected error: %s", sym, e)
        time.sleep(0.3)  # be polite to Yahoo

    ranked = score(all_cands)
    picks = pick_top(ranked)

    md = to_markdown(picks, scanned, len(ranked), run_ts)
    print(md)

    out_dir = Path(__file__).resolve().parent / C.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"puts_{run_ts:%Y-%m-%d}.md"
    out_file.write_text(md)
    if ranked:
        pd.DataFrame([asdict(c) for c in ranked]).to_csv(out_dir / f"all_candidates_{run_ts:%Y-%m-%d}.csv", index=False)
    log.info("wrote %s", out_file)

    subject = f"Put suggestions {run_ts:%Y-%m-%d}: " + (", ".join(c.ticker for c in picks) or "none")
    if os.getenv("EMAIL_TO"):
        try:
            send_email(subject, md, os.getenv("EMAIL_TO"))
        except Exception as e:
            log.error("email failed: %s", e)
    if os.getenv("SMS_TO"):
        try:
            send_email("", to_sms(picks, run_ts), os.getenv("SMS_TO"))
        except Exception as e:
            log.error("sms failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
