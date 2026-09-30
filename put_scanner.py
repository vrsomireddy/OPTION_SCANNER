#!/usr/bin/env python3
"""
Daily cash-secured put scanner for Nasdaq-100 tech names.

Suggestions only. This script never places orders. Market data comes from
Interactive Brokers over a read-only API connection; only earnings dates come
from Yahoo Finance.

Pipeline
  1. Pull stock prices, volume, and option chains from IBKR.
  2. Filter underlyings by price and liquidity.
  3. For each expiration in the DTE window (optionally skipping earnings),
     request live put quotes with IBKR's implied vol, delta, and open interest.
  4. Keep puts in the delta band with acceptable spread/OI and a bid that
     clears the annualized-return hurdle on cash collateral.
  5. For stocks with candidates, look up where today's implied vol sits in
     its past year (IBKR IV percentile).
  6. Score, rank, keep the best contract per ticker, print top N,
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
from ib_async import IB, Option, StartupFetch, Stock

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
    iv_percentile: float = float("nan")  # share of past-year days with lower stock IV (IBKR); high = rich premium


class ScanDataError(Exception):
    """Identify a stock whose required data could not be verified."""


class ScanSkipped(Exception):
    """Identify a stock excluded before its contracts were evaluated."""


@dataclass
class PreviousScan:
    day: date
    candidates: list[PutCandidate]
    displayed: list[PutCandidate]


@dataclass
class PutQuote:
    expiration: str   # YYYY-MM-DD
    strike: float
    bid: float
    ask: float
    iv: float
    delta: float
    open_interest: int
    volume: int


def positive(x: float | None) -> bool:
    """Tell whether a market data value is a real, usable positive number."""
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


# ---------------------------------------------------------------------------
# Earnings dates (Yahoo Finance; IBKR only offers these as a paid add-on)
# ---------------------------------------------------------------------------
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
# IBKR market data (read-only; no order code in this project)
# ---------------------------------------------------------------------------
def connect_ibkr() -> IB | None:
    """Open a read-only market data connection to a running IB Gateway or TWS, if one is available."""
    ib = IB()
    try:
        ib.connect(C.IBKR_HOST, C.IBKR_PORT, clientId=C.IBKR_CLIENT_ID, timeout=C.IBKR_TIMEOUT,
                   readonly=True, fetchFields=StartupFetch(0))
        ib.reqMarketDataType(C.IBKR_MARKET_DATA_TYPE)
    except Exception as e:
        log.error("IBKR connection to %s:%s failed (%s); is IB Gateway/TWS running with the API enabled?",
                  C.IBKR_HOST, C.IBKR_PORT, e)
        return None
    return ib


def get_stock(ib: IB, symbol: str) -> Stock | None:
    """Find the stock's listing at IBKR."""
    try:
        stock = Stock(symbol, "SMART", "USD")
        return stock if ib.qualifyContracts(stock) and stock.conId else None
    except Exception as e:
        log.warning("%s: IBKR contract lookup failed (%s)", symbol, e)
        return None


def get_spot_and_volume(ib: IB, stock: Stock) -> tuple[float | None, float | None]:
    """Look up a stock's current price and IBKR's 90-day average daily trading volume."""
    try:
        ticker = ib.reqMktData(stock, "165", False, False)
        deadline = time.monotonic() + C.IBKR_QUOTE_TIMEOUT
        while time.monotonic() < deadline and not (positive(ticker.marketPrice()) and positive(ticker.avVolume)):
            ib.sleep(0.25)
        ib.cancelMktData(stock)
    except Exception as e:
        log.warning("%s: could not get price (%s)", stock.symbol, e)
        return None, None
    spot = ticker.marketPrice() if positive(ticker.marketPrice()) else ticker.close
    return (float(spot) if positive(spot) else None), (float(ticker.avVolume) if positive(ticker.avVolume) else None)


def get_put_contracts(ib: IB, stock: Stock, spot: float, keep_expiration) -> list[Option]:
    """List the listed puts worth pricing: expirations in the window and strikes just below the stock price."""
    params = [p for p in ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId) if p.exchange == "SMART"]
    if not params:
        raise ScanSkipped("no listed option expirations")
    low = spot * (1 - C.STRIKE_WINDOW)
    wanted = []
    for p in params:
        strikes = [k for k in p.strikes if low <= k < spot and not (C.MAX_COLLATERAL and k * 100 > C.MAX_COLLATERAL)]
        for exp in sorted(p.expirations):
            if keep_expiration(exp):
                wanted += [Option(stock.symbol, exp, k, "P", "SMART", tradingClass=p.tradingClass,
                                  multiplier=p.multiplier, currency="USD") for k in strikes]
    if not wanted:
        return []
    return [c for c in ib.qualifyContracts(*wanted) if c is not None and c.conId]


def quote_is_complete(ticker) -> bool:
    """Tell whether a put's price, greeks, and open interest have all arrived."""
    g = ticker.modelGreeks
    return (positive(ticker.bid) and positive(ticker.ask) and g is not None and g.delta is not None
            and math.isfinite(g.delta) and positive(ticker.putOpenInterest))


def get_put_quotes(ib: IB, contracts: list[Option]) -> list[PutQuote]:
    """Collect live prices, implied volatility, delta, and open interest for a set of puts."""
    quotes = []
    for i in range(0, len(contracts), C.IBKR_BATCH_SIZE):
        tickers = [ib.reqMktData(c, "101", False, False) for c in contracts[i:i + C.IBKR_BATCH_SIZE]]
        deadline = time.monotonic() + C.IBKR_QUOTE_TIMEOUT
        while time.monotonic() < deadline and not all(quote_is_complete(t) for t in tickers):
            ib.sleep(0.25)
        for t in tickers:
            ib.cancelMktData(t.contract)
            g = t.modelGreeks
            exp = t.contract.lastTradeDateOrContractMonth
            quotes.append(PutQuote(
                expiration=f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}",
                strike=float(t.contract.strike),
                bid=float(t.bid) if positive(t.bid) else 0.0,
                ask=float(t.ask) if positive(t.ask) else 0.0,
                iv=float(g.impliedVol) if g is not None and positive(g.impliedVol) else 0.0,
                delta=float(g.delta) if g is not None and g.delta is not None else float("nan"),
                open_interest=int(t.putOpenInterest) if positive(t.putOpenInterest) else 0,
                volume=int(t.volume) if positive(t.volume) else 0,
            ))
    return quotes


def get_iv_percentile(ib: IB, symbol: str) -> float | None:
    """Tell how today's implied volatility compares with the stock's past year, from 0 (lowest) to 1 (highest)."""
    try:
        contract = Stock(symbol, "SMART", "USD")
        if not ib.qualifyContracts(contract):
            return None
        bars = ib.reqHistoricalData(contract, endDateTime="", durationStr=C.IV_LOOKBACK, barSizeSetting="1 day",
                                    whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True)
        ivs = [b.close for b in bars or [] if b.close is not None and math.isfinite(b.close) and b.close > 0]
    except Exception as e:
        log.warning("%s: IBKR IV history failed (%s)", symbol, e)
        return None
    if not ivs:
        return None
    if len(ivs) < C.IV_MIN_DAYS:
        raise ScanSkipped("too little implied volatility history")
    today_iv, past = ivs[-1], ivs[:-1]
    return sum(v < today_iv for v in past) / len(past)


def add_iv_percentile(ib: IB | None, symbol: str, found: list[PutCandidate]) -> None:
    """Attach the stock's IV percentile to its candidates or explain why it could not be found."""
    if not found:
        return
    if ib is None:
        raise ScanDataError("IV history unavailable: IBKR not connected")
    pct = get_iv_percentile(ib, symbol)
    if pct is None:
        raise ScanDataError("IV history unavailable from IBKR")
    for c in found:
        c.iv_percentile = round(pct, 3)


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def scan_ticker(symbol: str, today: date, ib: IB | None) -> list[PutCandidate]:
    """Find qualifying puts for one stock or explain why it could not be evaluated."""
    if symbol in C.EXCLUDE_TICKERS:
        raise ScanSkipped("excluded in config")
    if ib is None:
        raise ScanDataError("IBKR not connected")
    stock = get_stock(ib, symbol)
    if stock is None:
        raise ScanDataError("stock not found at IBKR")
    spot, avg_vol = get_spot_and_volume(ib, stock)
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

    earnings = get_next_earnings(yf.Ticker(symbol), today) if C.SKIP_EARNINGS else None
    if C.SKIP_EARNINGS and earnings is None:
        raise ScanDataError("next earnings date unavailable")

    def keep_expiration(exp_str: str) -> bool:
        """Keep expirations inside the day window that finish before the next earnings report."""
        exp = datetime.strptime(exp_str, "%Y%m%d").date()
        if not C.DTE_MIN <= (exp - today).days <= C.DTE_MAX:
            return False
        if earnings and today <= earnings <= exp:
            log.info("%s: skip %s, spans earnings %s", symbol, exp_str, earnings)
            return False
        return True

    try:
        contracts = get_put_contracts(ib, stock, spot, keep_expiration)
    except ScanSkipped:
        raise
    except Exception as e:
        log.warning("%s: option chain lookup failed (%s)", symbol, e)
        raise ScanDataError("option chain unavailable") from e
    if not contracts:
        return []
    quotes = get_put_quotes(ib, contracts)
    if not any(q.bid > 0 or q.ask > 0 for q in quotes):
        raise ScanDataError("no option quotes from IBKR")

    out: list[PutCandidate] = []
    for q in quotes:
        dte = (datetime.strptime(q.expiration, "%Y-%m-%d").date() - today).days
        strike, bid, ask = q.strike, q.bid, q.ask
        if strike >= spot:            # OTM puts only
            continue
        if C.MAX_COLLATERAL and strike * 100 > C.MAX_COLLATERAL:
            continue
        if bid < C.MIN_BID or ask <= 0 or ask < bid:
            continue
        if q.open_interest < C.MIN_OPEN_INTEREST:
            continue
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        if spread_pct > C.MAX_SPREAD_PCT:
            continue
        if not math.isfinite(q.delta):
            continue
        adelta = abs(q.delta)
        if adelta < C.DELTA_MIN or adelta > C.DELTA_MAX:
            continue
        ann = (bid / strike) * (365.0 / dte)
        if ann < C.MIN_ANNUALIZED_RETURN:
            continue

        out.append(
            PutCandidate(
                ticker=symbol,
                spot=round(spot, 2),
                expiration=q.expiration,
                dte=dte,
                strike=strike,
                bid=bid,
                ask=ask,
                mid=round(mid, 2),
                iv=round(q.iv, 4),
                delta=round(q.delta, 3),
                open_interest=q.open_interest,
                volume=q.volume,
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
    band = C.DELTA_MAX - C.DELTA_MIN
    for c in cands:
        rich = c.iv_percentile if math.isfinite(c.iv_percentile) else 0.0   # unknown earns no richness credit
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


OPTIONAL_FIELDS = {"iv_percentile"}   # absent from reports written before these were recorded


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
        f"top {len(picks)} shown. Data: IBKR (earnings dates: Yahoo Finance). "
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
        "| # | Ticker | Spot | Exp | DTE | Strike | Bid | Δ | P(OTM) | Cushion | IV pct "
        "| Ann. Ret | Premium/ct | Collateral | Breakeven | Score |",
        "|---|--------|------|-----|-----|--------|-----|---|--------|---------|-------"
        "|----------|------------|------------|-----------|-------|",
    ]
    for i, c in enumerate(picks, 1):
        lines.append(
            f"| {i} | **{c.ticker}** | {c.spot:.2f} | {c.expiration} | {c.dte} | {c.strike:.2f} | "
            f"{c.bid:.2f} | {c.delta:.2f} | {c.prob_otm:.0%} | {c.cushion_pct:.1%} | {c.iv_percentile:.0%} | "
            f"{c.annualized_return:.1%} | ${c.premium_per_contract:,.0f} | ${c.collateral:,.0f} | "
            f"{c.breakeven:.2f} | {c.score:.3f} |"
        )
    lines += ["", "### Trade descriptions", ""]
    for i, c in enumerate(picks, 1):
        lines.append(
            f"{i}. **SELL TO OPEN 1 {c.ticker} {c.expiration} {str(c.strike).removesuffix('.0')}P @ {c.bid:.2f} (limit)**: "
            f"collect ~${c.premium_per_contract:,.0f} against ${c.collateral:,.0f} cash. "
            f"IV {c.iv:.0%}, higher than on {c.iv_percentile:.0%} of past-year days, OI {c.open_interest:,}, spread {c.spread_pct:.1%}. "
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

    ib = connect_ibkr()
    for sym in C.UNIVERSE:
        try:
            found = scan_ticker(sym, today, ib)
            add_iv_percentile(ib, sym, found)
            scanned += 1
            log.info("%s: %d candidates", sym, len(found))
            all_cands.extend(found)
        except ScanSkipped as e:
            skipped[sym] = str(e)
        except Exception as e:
            failures[sym] = str(e)
            log.error("%s: scan failed: %s", sym, e)
        time.sleep(0.3)  # be polite to Yahoo (earnings dates)
    if ib is not None:
        ib.disconnect()

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
