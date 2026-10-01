"""Unit tests for put_scanner.py. All network calls (yfinance, SMTP) are mocked."""
import logging
import math
import smtplib
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import config as C
import put_scanner as ps


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class Chain:
    def __init__(self, puts):
        self.puts = puts


class FakeTicker:
    """Stands in for yfinance's Ticker so tests never hit the network."""

    def __init__(
        self,
        ticker="TEST",
        fast_info=None,
        fast_info_raises=False,
        history_df=None,
        history_raises=False,
        earnings_df=None,
        earnings_raises=False,
        calendar=None,
        calendar_raises=False,
        options=(),
        option_chains=None,
        options_raises=False,
    ):
        self.ticker = ticker
        self._fast_info = fast_info
        self._fast_info_raises = fast_info_raises
        self._history_df = history_df
        self._history_raises = history_raises
        self._earnings_df = earnings_df
        self._earnings_raises = earnings_raises
        self._calendar = calendar
        self._calendar_raises = calendar_raises
        self._options = options
        self._option_chains = option_chains or {}
        self._options_raises = options_raises
        self.history_calls = 0

    @property
    def fast_info(self):
        if self._fast_info_raises:
            raise RuntimeError("fast_info failed")
        return self._fast_info

    def history(self, period=None, auto_adjust=None):
        self.history_calls += 1
        if self._history_raises:
            raise RuntimeError("history failed")
        return self._history_df if self._history_df is not None else pd.DataFrame()

    def get_earnings_dates(self, limit=8):
        if self._earnings_raises:
            raise RuntimeError("earnings failed")
        return self._earnings_df if self._earnings_df is not None else pd.DataFrame()

    @property
    def calendar(self):
        if self._calendar_raises:
            raise RuntimeError("calendar failed")
        return self._calendar

    @property
    def options(self):
        if self._options_raises:
            raise RuntimeError("no chain")
        return self._options

    def option_chain(self, exp_str):
        return self._option_chains[exp_str]


class Bar:
    def __init__(self, close):
        self.close = close


class StockTicker:
    def __init__(self, contract, price=100.0, close=float("nan"), av_volume=5_000_000.0):
        self.contract = contract
        self.price = price
        self.close = close
        self.avVolume = av_volume

    def marketPrice(self):
        return self.price


class PutTicker:
    def __init__(self, contract, bid=1.0, ask=1.1, iv=0.30, delta=-0.20, volume=50):
        self.contract = contract
        self.bid = bid
        self.ask = ask
        self.volume = volume
        self.modelGreeks = None if delta is None else SimpleNamespace(impliedVol=iv, delta=delta)


class FakeIB:
    """Stands in for an IBKR connection so tests never open a socket."""

    def __init__(self, ivs=(), qualify=True, raises=False, stock=None, chains=None, quotes=None,
                 unlisted=(), secdef_raises=False, mktdata_raises=False):
        self.ivs = list(ivs)
        self.qualify = qualify
        self.raises = raises
        self.stock = stock or {}
        self.chains = chains if chains is not None else []
        self.quotes = quotes or {}
        self.unlisted = set(unlisted)
        self.secdef_raises = secdef_raises
        self.mktdata_raises = mktdata_raises
        self.requests = []
        self.cancelled = []
        self.batches = []
        self.data_type = None
        self.disconnected = False

    def qualifyContracts(self, *contracts):
        if self.raises:
            raise RuntimeError("pacing violation")
        if not self.qualify:
            return []
        out = []
        for c in contracts:
            if c.secType == "OPT" and (c.lastTradeDateOrContractMonth, c.strike) in self.unlisted:
                out.append(None)
            else:
                c.conId = 1
                out.append(c)
        return out

    def reqHistoricalData(self, contract, **kwargs):
        self.requests.append((contract.symbol, kwargs["whatToShow"]))
        return [Bar(v) for v in self.ivs]

    def reqSecDefOptParams(self, symbol, exchange, sec_type, con_id):
        if self.secdef_raises:
            raise RuntimeError("secdef failed")
        return self.chains

    def reqMktData(self, contract, generic="", snapshot=False, regulatory=False):
        if self.mktdata_raises:
            raise RuntimeError("no market data permissions")
        if contract.secType == "OPT":
            self.batches.append(contract.strike)
            key = (contract.lastTradeDateOrContractMonth, contract.strike)
            return PutTicker(contract, **self.quotes.get(key, {}))
        return StockTicker(contract, **self.stock)

    def cancelMktData(self, contract):
        self.cancelled.append(contract)

    def sleep(self, seconds):
        pass

    def reqMarketDataType(self, data_type):
        self.data_type = data_type

    def disconnect(self):
        self.disconnected = True


def chain(expirations, strikes, exchange="SMART"):
    """Describe one listed option chain the way IBKR reports it."""
    return SimpleNamespace(exchange=exchange, tradingClass="AAA", multiplier="100",
                           expirations=set(expirations), strikes=list(strikes))


def make_candidate(**overrides):
    base = dict(
        ticker="AAA", spot=100.0, expiration="2026-10-01", dte=30, strike=90.0, bid=1.0,
        ask=1.1, mid=1.05, iv=0.3, delta=-0.2, open_interest=100, volume=50,
        spread_pct=0.05, cushion_pct=0.10, premium_per_contract=100.0, collateral=9000.0,
        annualized_return=0.20, breakeven=89.0, prob_otm=0.80, earnings_date=None,
    )
    base.update(overrides)
    return ps.PutCandidate(**base)


# ---------------------------------------------------------------------------
# get_next_earnings
# ---------------------------------------------------------------------------
def test_get_next_earnings_from_earnings_dates():
    today = date.today()
    idx = pd.to_datetime([today - timedelta(days=5), today + timedelta(days=10), today + timedelta(days=3)])
    tk = FakeTicker(earnings_df=pd.DataFrame({"x": [1, 2, 3]}, index=idx))
    assert ps.get_next_earnings(tk) == today + timedelta(days=3)


def test_get_next_earnings_no_future_dates_in_earnings_dates_falls_back_to_calendar():
    today = date.today()
    idx = pd.to_datetime([today - timedelta(days=5)])
    tk = FakeTicker(
        earnings_df=pd.DataFrame({"x": [1]}, index=idx),
        calendar={"Earnings Date": [today + timedelta(days=7)]},
    )
    assert ps.get_next_earnings(tk) == today + timedelta(days=7)


def test_get_next_earnings_earnings_dates_raises_calendar_list():
    today = date.today()
    tk = FakeTicker(earnings_raises=True, calendar={"Earnings Date": [today + timedelta(days=1)]})
    assert ps.get_next_earnings(tk) == today + timedelta(days=1)


def test_get_next_earnings_calendar_single_non_list_value():
    today = date.today()
    tk = FakeTicker(earnings_raises=True, calendar={"Earnings Date": today + timedelta(days=2)})
    assert ps.get_next_earnings(tk) == today + timedelta(days=2)


def test_get_next_earnings_calendar_string_date_is_parsed():
    today = date.today()
    target = (today + timedelta(days=4)).isoformat()
    tk = FakeTicker(earnings_raises=True, calendar={"Earnings Date": [target]})
    assert ps.get_next_earnings(tk) == today + timedelta(days=4)


def test_get_next_earnings_calendar_not_a_dict():
    tk = FakeTicker(earnings_raises=True, calendar=None)
    assert ps.get_next_earnings(tk) is None


def test_get_next_earnings_calendar_raises_too():
    tk = FakeTicker(earnings_raises=True, calendar_raises=True)
    assert ps.get_next_earnings(tk) is None


def test_get_next_earnings_calendar_has_no_future_dates():
    today = date.today()
    tk = FakeTicker(earnings_raises=True, calendar={"Earnings Date": [today - timedelta(days=1)]})
    assert ps.get_next_earnings(tk) is None


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------
@pytest.fixture
def score_config(monkeypatch):
    """Fixed weights and bands so score arithmetic can be checked by hand."""
    for name, value in [("W_RICHNESS", 0.4), ("W_YIELD", 0.2), ("W_SAFETY", 0.3), ("W_CUSHION", 0.1),
                        ("DELTA_MIN", 0.15), ("DELTA_MAX", 0.25)]:
        monkeypatch.setattr(C, name, value)


def test_score_ranks_best_first_and_clamps_caps(score_config):
    """Combine richness, yield, safety and cushion, clamping each part to its cap."""
    low = make_candidate(ticker="LOW", iv_percentile=0.5, annualized_return=0.10, delta=-0.25, cushion_pct=0.05)
    high = make_candidate(ticker="HIGH", iv_percentile=1.0, annualized_return=1.0, delta=-0.10, cushion_pct=0.50)

    ranked = ps.score([low, high])
    assert [c.ticker for c in ranked] == ["HIGH", "LOW"]
    # HIGH is past every cap, so each part clamps to 1.0.
    assert ranked[0].score == 1.0
    assert ranked[1].score == round(0.4 * 0.5 + 0.2 * (0.10 / 0.60) + 0.3 * 0.0 + 0.1 * (0.05 / 0.20), 4)


def test_score_prefers_rich_premium_over_raw_volatility(score_config):
    """Rank a stock with unusually high option prices above one that is simply always volatile."""
    always_wild = make_candidate(ticker="WILD", iv=0.90, iv_percentile=0.10, annualized_return=0.40)
    unusually_rich = make_candidate(ticker="RICH", iv=0.40, iv_percentile=0.90, annualized_return=0.20)
    assert [c.ticker for c in ps.score([always_wild, unusually_rich])] == ["RICH", "WILD"]


def test_score_spreads_safety_across_the_delta_band(score_config):
    """Give full safety credit at the low end of the delta band and none at the high end."""
    safest, riskiest = ps.score([make_candidate(ticker="S", delta=-0.15), make_candidate(ticker="R", delta=-0.25)])
    assert round(safest.score - riskiest.score, 4) == 0.3


def test_score_treats_unknown_richness_as_no_credit(score_config):
    """Score candidates saved before richness was recorded without breaking the ranking."""
    assert ps.score([make_candidate(delta=-0.25, cushion_pct=0.0, annualized_return=0.0)])[0].score == 0.0


def test_score_with_zero_width_delta_band(score_config, monkeypatch):
    """Give full safety credit when the delta band is a single value."""
    monkeypatch.setattr(C, "DELTA_MIN", 0.25)
    assert ps.score([make_candidate(delta=-0.25, cushion_pct=0.0, annualized_return=0.0)])[0].score == 0.3


# ---------------------------------------------------------------------------
# pick_top
# ---------------------------------------------------------------------------
def test_pick_top_limits_per_ticker_and_total(monkeypatch):
    monkeypatch.setattr(C, "MAX_PER_TICKER", 1)
    monkeypatch.setattr(C, "TOP_N", 2)
    cands = [
        make_candidate(ticker="AAA"),
        make_candidate(ticker="AAA"),  # dropped: already have one AAA
        make_candidate(ticker="BBB"),
        make_candidate(ticker="CCC"),  # dropped: TOP_N reached
    ]
    picks = ps.pick_top(cands)
    assert [c.ticker for c in picks] == ["AAA", "BBB"]


# ---------------------------------------------------------------------------
# to_markdown / to_sms
# ---------------------------------------------------------------------------
def test_to_markdown_no_picks():
    run_ts = datetime(2026, 9, 6, 9, 30, tzinfo=UTC)
    md = ps.to_markdown([], scanned=10, total_cands=0, run_ts=run_ts)
    assert "No contracts met the criteria today" in md
    assert "10 Nasdaq-100 names scanned" in md


def test_to_markdown_with_picks_includes_table_and_earnings_note():
    run_ts = datetime(2026, 9, 6, 9, 30, tzinfo=UTC)
    c1 = make_candidate(ticker="AAA", earnings_date="2026-11-01")
    c2 = make_candidate(ticker="BBB", earnings_date=None)
    md = ps.to_markdown([c1, c2], scanned=2, total_cands=2, run_ts=run_ts)
    assert "**AAA**" in md and "**BBB**" in md
    assert "Next earnings 2026-11-01" in md
    assert md.count("Next earnings") == 1
    assert "SELL TO OPEN 1 AAA" in md


def test_to_sms_no_picks():
    run_ts = datetime(2026, 9, 6, 9, 30)
    assert "no qualifying contracts" in ps.to_sms([], run_ts)


def test_to_sms_with_picks():
    run_ts = datetime(2026, 9, 6, 9, 30)
    c = make_candidate(ticker="AAA", expiration="2026-10-01", strike=90.0, bid=1.0, delta=-0.2, annualized_return=0.2)
    out = ps.to_sms([c], run_ts)
    assert "AAA" in out
    assert "90P" in out
    assert "d0.20" in out


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------
def test_send_email_missing_credentials_skips_send(monkeypatch):
    """Reject requested delivery when credentials are missing."""
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    called = {"smtp": False}

    class BoomSMTP:
        def __init__(self, *a, **k):
            called["smtp"] = True

    monkeypatch.setattr(smtplib, "SMTP", BoomSMTP)
    with pytest.raises(ValueError, match="SMTP_USER/SMTP_PASS"):
        ps.send_email("subj", "body", "to@example.com")
    assert called["smtp"] is False


def test_send_email_success(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASS", "secret")

    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("init", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            calls.append(("starttls",))

        def login(self, user, pw):
            calls.append(("login", user, pw))

        def sendmail(self, frm, to, msg):
            calls.append(("sendmail", frm, to))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    ps.send_email("subj", "body", "to@example.com")

    kinds = [c[0] for c in calls]
    assert kinds == ["init", "starttls", "login", "sendmail"]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
@pytest.fixture
def main_env(monkeypatch, tmp_path):
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    monkeypatch.setattr(C, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(C, "UNIVERSE", ["AAA", "BBB"])
    monkeypatch.delenv("EMAIL_TO", raising=False)
    monkeypatch.delenv("SMS_TO", raising=False)
    monkeypatch.setattr(C, "IV_MIN_DAYS", 3)
    monkeypatch.setattr(ps, "connect_ibkr", lambda: FakeIB(ivs=[0.2, 0.3, 0.4, 0.5, 0.45]))
    return tmp_path


def test_main_test_email_without_env_returns_error(monkeypatch, main_env):
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py", "--test-email"])
    assert ps.main() == 1


def test_main_test_email_success(monkeypatch, main_env):
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py", "--test-email"])
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    sent = []
    monkeypatch.setattr(ps, "send_email", lambda *a, **k: sent.append(a))
    assert ps.main() == 0
    assert len(sent) == 1


def test_main_test_email_auth_error(monkeypatch, main_env):
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py", "--test-email"])
    monkeypatch.setenv("EMAIL_TO", "to@example.com")

    def boom(*a, **k):
        raise smtplib.SMTPAuthenticationError(535, b"bad creds")

    monkeypatch.setattr(ps, "send_email", boom)
    assert ps.main() == 1


def test_main_test_email_generic_error(monkeypatch, main_env):
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py", "--test-email"])
    monkeypatch.setenv("EMAIL_TO", "to@example.com")

    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(ps, "send_email", boom)
    assert ps.main() == 1


def test_main_full_run_writes_reports_and_sends_notifications(monkeypatch, main_env, capsys):
    """Keep available picks and label partial failures in every delivery."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.setenv("SMS_TO", "+15551234567")

    def fake_scan(symbol, today, ib):
        if symbol == "AAA":
            return [make_candidate(ticker="AAA")]
        raise RuntimeError("yahoo hiccup")  # exercises the per-symbol error handling

    sent = []
    monkeypatch.setattr(ps, "scan_ticker", fake_scan)
    monkeypatch.setattr(ps, "send_email", lambda *a, **k: sent.append(a))

    assert ps.main() == 1
    out = capsys.readouterr().out
    assert "AAA" in out
    assert "INCOMPLETE SCAN" in out
    assert sent[0][0].startswith("INCOMPLETE:")
    assert "INCOMPLETE SCAN" in sent[1][1]

    md_files = list(main_env.glob("puts_*.md"))
    csv_files = list(main_env.glob("all_candidates_*.csv"))
    assert len(md_files) == 1
    assert len(csv_files) == 1
    assert len(sent) == 2  # one email, one "sms" (sent via send_email too)


def test_main_full_run_no_candidates_writes_empty_csv(monkeypatch, main_env):
    """Write an empty candidate table when no contracts qualify."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today, ib: [])

    assert ps.main() == 0
    assert pd.read_csv(next(main_env.glob("all_candidates_*.csv"))).empty
    assert len(list(main_env.glob("puts_*.md"))) == 1


def test_main_notification_failures_return_error(monkeypatch, main_env):
    """Report failed notifications through the exit status."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.setenv("SMS_TO", "+15551234567")
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today, ib: [])

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ps, "send_email", boom)
    assert ps.main() == 1


@pytest.mark.parametrize("value", [datetime(2020, 1, 5), pd.Timestamp("2020-01-05", tz="UTC")])
def test_earnings_normalizes_datetime_and_uses_scan_date(value):
    """Recognize calendar timestamps relative to the requested scan day."""
    tk = FakeTicker(calendar={"Earnings Date": [value]})
    assert ps.get_next_earnings(tk, date(2020, 1, 1)) == date(2020, 1, 5)


@pytest.mark.parametrize("strike", [92.5, 92.25, 92.125])
def test_reports_preserve_fractional_strikes(strike):
    """Keep the exact fractional strike in trade descriptions and texts."""
    candidate = make_candidate(strike=strike)
    run_ts = datetime(2026, 9, 13)
    assert f"{strike}P" in ps.to_markdown([candidate], 1, 1, run_ts)
    assert f"{strike}P" in ps.to_sms([candidate], run_ts)


def test_email_check_missing_credentials_fails(monkeypatch, main_env):
    """Fail email verification without attempting delivery when credentials are missing."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py", "--test-email"])
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    assert ps.main() == 1


def test_empty_rerun_replaces_previous_csv(monkeypatch, main_env):
    """Remove old candidates from the daily table when a rerun finds none."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today, ib: [make_candidate(ticker=symbol)])
    assert ps.main() == 0
    csv = next(main_env.glob("all_candidates_*.csv"))
    assert len(pd.read_csv(csv)) == 2
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today, ib: [])
    assert ps.main() == 0
    assert pd.read_csv(csv).empty
    assert "No contracts met" in next(main_env.glob("puts_*.md")).read_text()


def test_total_outage_is_not_a_successful_empty_scan(monkeypatch, main_env, capsys):
    """Label a total data outage as incomplete in reports and texts."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setenv("SMS_TO", "to@example.com")
    sent = []
    monkeypatch.setattr(ps, "send_email", lambda *args: sent.append(args))

    def fail(symbol, today, ib):
        """Represent a stock whose data could not be fetched."""
        raise ps.ScanDataError("stock price unavailable")

    monkeypatch.setattr(ps, "scan_ticker", fail)
    assert ps.main() == 1
    report = capsys.readouterr().out
    assert "0 Nasdaq-100 names scanned" in report
    assert "INCOMPLETE SCAN" in report
    assert "No contracts met the criteria today" not in report
    assert "INCOMPLETE SCAN" in sent[0][1]


def test_skipped_stocks_are_reported_separately(monkeypatch, main_env, capsys):
    """Distinguish intentional exclusions from completed scans and data failures."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])

    def skip(symbol, today, ib):
        """Represent a stock excluded by the price limit."""
        raise ps.ScanSkipped("stock price exceeds limit")

    monkeypatch.setattr(ps, "scan_ticker", skip)
    assert ps.main() == 0
    report = capsys.readouterr().out
    assert "Stocks skipped before contract evaluation: 2" in report
    assert "INCOMPLETE" not in report


def test_alternatives_keep_rank_order_and_exclude_all_top_names(monkeypatch):
    """Select distinct alternative names without changing the original ranking."""
    monkeypatch.setattr(C, "ALTERNATIVE_N", 2)
    ranked = [make_candidate(ticker=name) for name in ["AAA", "AAA", "BBB", "BBB", "CCC", "DDD"]]
    assert [c.ticker for c in ps.pick_alternatives(ranked, ranked[:1])] == ["BBB", "CCC"]
    monkeypatch.setattr(C, "ALTERNATIVE_N", 0)
    assert ps.pick_alternatives(ranked, ranked[:1]) == []
    monkeypatch.setattr(C, "ALTERNATIVE_N", 5)
    assert [c.ticker for c in ps.pick_alternatives(ranked, ranked[:1])] == ["BBB", "CCC", "DDD"]


def test_change_labels_and_same_contract_comparison():
    """Distinguish new names and changed contracts without comparing unrelated premiums."""
    old = make_candidate(bid=1, score=0.6)
    other_contract = make_candidate(strike=85, bid=0.5, score=0.4)
    history = ps.PreviousScan(date(2026, 9, 10), [old, other_contract], [old])
    repeated = ps.describe_change(make_candidate(bid=1.2, score=0.7), history)
    assert "Repeat contract" in repeated
    assert "bid change +0.20, score change +0.1000" in repeated
    assert "Potentially stale" not in repeated
    changed = ps.describe_change(make_candidate(strike=85, bid=0.7, score=0.5), history)
    assert "Changed contract" in changed
    assert "bid change +0.20" in changed
    unavailable = ps.describe_change(make_candidate(strike=86), history)
    assert "changes unavailable" in unavailable
    assert "New ticker" in ps.describe_change(make_candidate(ticker="BBB"), history)
    assert "No previous report" in ps.describe_change(old, None)


def test_unchanged_quote_warns_about_dte_only_return_increase():
    """Explain a rising annualized return when the same quote is reused the next day."""
    old = make_candidate(dte=30, annualized_return=0.20)
    history = ps.PreviousScan(date(2026, 9, 10), [old], [old])
    note = ps.describe_change(make_candidate(dte=29, annualized_return=0.207), history)
    assert "Potentially stale" in note
    assert "no better quoted premium" in note
    assert "return rose" not in ps.describe_change(old, history)
    assert "Potentially stale" not in ps.describe_change(make_candidate(ask=1.2), history)


def test_load_previous_scan_uses_recorded_selections_and_ignores_today(tmp_path):
    """Use actual earlier selections even when today's output limits differ."""
    rows = [dict(asdict(make_candidate(ticker=name)), report_group=group)
            for name, group in [("AAA", ""), ("BBB", "top"), ("CCC", "alternative")]]
    pd.DataFrame(rows).to_csv(tmp_path / "all_candidates_2026-09-10.csv", index=False)
    (tmp_path / "all_candidates_2026-09-11.csv").write_text("ignored same-day file")
    (tmp_path / "all_candidates_2026-09-12.csv").write_text("ignored future file")
    previous = ps.load_previous_scan(tmp_path, date(2026, 9, 11))
    assert previous.day == date(2026, 9, 10)
    assert [c.ticker for c in previous.displayed] == ["BBB", "CCC"]
    assert len(previous.candidates) == 3


def test_load_previous_scan_supports_legacy_files_and_skips_corrupt_history(tmp_path, monkeypatch):
    """Recover older results when a newer history file cannot be used."""
    monkeypatch.setattr(C, "TOP_N", 1)
    rows = [asdict(make_candidate(ticker=name)) for name in ["AAA", "BBB"]]
    pd.DataFrame(rows).to_csv(tmp_path / "all_candidates_2026-09-08.csv", index=False)
    (tmp_path / "all_candidates_2026-09-09.csv").write_text("invalid\nvalue\n")
    pd.DataFrame([asdict(make_candidate(bid=float("nan")))]).to_csv(
        tmp_path / "all_candidates_2026-09-10.csv", index=False,
    )
    previous = ps.load_previous_scan(tmp_path, date(2026, 9, 11))
    assert previous.day == date(2026, 9, 8)
    assert [c.ticker for c in previous.displayed] == ["AAA"]


def test_empty_previous_scan_is_a_valid_baseline(tmp_path):
    """Keep an empty prior day instead of falling back to older recommendations."""
    pd.DataFrame(columns=list(asdict(make_candidate()))).to_csv(
        tmp_path / "all_candidates_2026-09-10.csv", index=False,
    )
    previous = ps.load_previous_scan(tmp_path, date(2026, 9, 11))
    assert previous.candidates == []
    assert "New ticker" in ps.describe_change(make_candidate(), previous)


def test_main_reports_alternatives_changes_and_persists_selections(monkeypatch, main_env, capsys):
    """Carry prior-day comparisons through reports, notifications, and saved results."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setattr(C, "TOP_N", 1)
    monkeypatch.setattr(C, "ALTERNATIVE_N", 1)
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today, ib: [make_candidate(ticker=symbol, dte=29)])
    previous_day = date.today() - timedelta(days=1)
    pd.DataFrame([asdict(make_candidate(ticker="AAA", dte=30))]).to_csv(
        main_env / f"all_candidates_{previous_day}.csv", index=False,
    )
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.setenv("SMS_TO", "sms@example.com")
    sent = []
    monkeypatch.setattr(ps, "send_email", lambda *args: sent.append(args))
    assert ps.main() == 0
    report = capsys.readouterr().out
    assert f"Compared with {previous_day}" in report
    assert "Repeat contract" in report
    assert "Potentially stale" in report
    assert "Alternative names" in report
    assert "New ticker in recommendations" in report
    assert "Potentially stale" in sent[0][1]
    assert "Potentially stale" in sent[1][1]
    assert "Alternatives: BBB" in sent[1][1]
    saved = pd.read_csv(main_env / f"all_candidates_{date.today()}.csv")
    assert saved.report_group.tolist() == ["top", "alternative"]
    assert saved.comparison_date.tolist() == [str(previous_day)] * 2
    assert "Repeat contract" in saved.change_note.iloc[0]


# ---------------------------------------------------------------------------
# IV percentile and portfolio limits
# ---------------------------------------------------------------------------
def test_iv_percentile_compares_today_with_past_year(monkeypatch):
    """Place today's implied vol among the stock's earlier daily values."""
    monkeypatch.setattr(C, "IV_MIN_DAYS", 3)
    ib = FakeIB(ivs=[0.2, 0.3, 0.4, float("nan"), 0.6, 0.35])
    assert ps.get_iv_percentile(ib, "AAA") == 0.5
    assert ib.requests == [("AAA", "OPTION_IMPLIED_VOLATILITY")]


def test_iv_percentile_short_history_is_a_skip(monkeypatch):
    """Skip recently listed stocks instead of reporting a data failure."""
    monkeypatch.setattr(C, "IV_MIN_DAYS", 10)
    with pytest.raises(ps.ScanSkipped, match="too little implied volatility history"):
        ps.get_iv_percentile(FakeIB(ivs=[0.2, 0.3]), "AAA")


@pytest.mark.parametrize("ib", [FakeIB(ivs=[]), FakeIB(ivs=[0.3] * 5, qualify=False), FakeIB(raises=True)])
def test_iv_percentile_unavailable(ib):
    """Report no percentile when IBKR has no history, no matching stock, or errors."""
    assert ps.get_iv_percentile(ib, "AAA") is None


def test_add_iv_percentile_labels_candidates_and_failures(monkeypatch):
    """Attach the percentile to every contract and fail the stock when it cannot be found."""
    monkeypatch.setattr(C, "IV_MIN_DAYS", 3)
    found = [make_candidate(), make_candidate(strike=85)]
    ps.add_iv_percentile(FakeIB(ivs=[0.2, 0.3, 0.4, 0.5, 0.45]), "AAA", found)
    assert [c.iv_percentile for c in found] == [0.75, 0.75]
    ps.add_iv_percentile(None, "AAA", [])   # no candidates, nothing to look up
    with pytest.raises(ps.ScanDataError, match="IBKR not connected"):
        ps.add_iv_percentile(None, "AAA", found)
    with pytest.raises(ps.ScanDataError, match="unavailable from IBKR"):
        ps.add_iv_percentile(FakeIB(), "AAA", found)


def test_connect_ibkr_is_read_only_and_skips_account_data(monkeypatch):
    """Connect without order permissions or downloading positions, orders, or balances."""
    calls = {}

    class FakeClient:
        def connect(self, host, port, **kwargs):
            calls.update(kwargs, host=host, port=port)

        def reqMarketDataType(self, data_type):
            calls["data_type"] = data_type

    monkeypatch.setattr(ps, "IB", FakeClient)
    assert isinstance(ps.connect_ibkr(), FakeClient)
    assert calls["readonly"] is True
    assert calls["fetchFields"] == ps.StartupFetch(0)
    assert (calls["host"], calls["port"]) == (C.IBKR_HOST, C.IBKR_PORT)
    assert calls["data_type"] == C.IBKR_MARKET_DATA_TYPE


def test_connect_ibkr_failure_returns_none(monkeypatch):
    """Carry on without IBKR when the gateway is not running."""
    class DownClient:
        def connect(self, *a, **k):
            raise ConnectionRefusedError("refused")

    monkeypatch.setattr(ps, "IB", DownClient)
    assert ps.connect_ibkr() is None


def test_main_without_ibkr_marks_stocks_with_candidates_incomplete(monkeypatch, main_env, capsys):
    """Never rank picks without IV history; label the run incomplete instead."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setattr(ps, "connect_ibkr", lambda: None)
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today, ib: [make_candidate(ticker=symbol)])
    assert ps.main() == 1
    report = capsys.readouterr().out
    assert "IBKR not connected" in report
    assert "INCOMPLETE SCAN" in report


def test_main_disconnects_from_ibkr(monkeypatch, main_env):
    """Close the IBKR connection when the scan finishes."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    ib = FakeIB(ivs=[0.2, 0.3, 0.4, 0.5])
    monkeypatch.setattr(ps, "connect_ibkr", lambda: ib)
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today, ib: [make_candidate(ticker=symbol)])
    assert ps.main() == 0
    assert ib.disconnected


def test_sector_group_limit_applies_across_picks_and_alternatives(monkeypatch):
    """Show at most the configured number of names from one sector group in the whole report."""
    monkeypatch.setattr(C, "SECTOR_GROUPS", {"Chips": ["AAA", "BBB", "CCC"]})
    monkeypatch.setattr(C, "MAX_PER_GROUP", 1)
    monkeypatch.setattr(C, "MAX_PER_TICKER", 2)
    monkeypatch.setattr(C, "TOP_N", 3)
    monkeypatch.setattr(C, "ALTERNATIVE_N", 3)
    ranked = [make_candidate(ticker=t) for t in ["AAA", "AAA", "BBB", "DDD", "CCC", "EEE", "FFF"]]
    picks = ps.pick_top(ranked)
    assert [c.ticker for c in picks] == ["AAA", "AAA", "DDD"]   # second contract of a shown name is still allowed
    assert [c.ticker for c in ps.pick_alternatives(ranked, picks)] == ["EEE", "FFF"]


def test_report_shows_richness_and_limits(monkeypatch):
    """Explain the richness figure and active portfolio limits in the report."""
    monkeypatch.setattr(C, "MAX_COLLATERAL", 30_000)
    monkeypatch.setattr(C, "SECTOR_GROUPS", {"Semiconductors": ["AAA"]})
    monkeypatch.setattr(C, "MAX_PER_GROUP", 1)
    md = ps.to_markdown([make_candidate(iv=0.3, iv_percentile=0.82)], 1, 1, datetime(2026, 9, 30))
    assert "collateral ≤ $30,000" in md
    assert "≤1 Semiconductors" in md
    assert "IV 30%, higher than on 82% of past-year days" in md
    assert "| 82% |" in md


def test_previous_scan_loads_files_written_before_richness_existed(tmp_path):
    """Keep comparing against older reports that lack the new volatility columns."""
    legacy = {k: v for k, v in asdict(make_candidate(ticker="OLD")).items() if k not in ps.OPTIONAL_FIELDS}
    pd.DataFrame([dict(legacy, report_group="top")]).to_csv(tmp_path / "all_candidates_2026-09-10.csv", index=False)
    previous = ps.load_previous_scan(tmp_path, date(2026, 9, 11))
    assert [c.ticker for c in previous.displayed] == ["OLD"]
    assert math.isnan(previous.candidates[0].iv_percentile)


# ---------------------------------------------------------------------------
# IBKR market data and scan_ticker
# ---------------------------------------------------------------------------
TODAY = date(2026, 9, 30)
EXP = "20261030"          # 30 days out, IBKR format
EXP_ISO = "2026-10-30"


@pytest.fixture
def loose_config(monkeypatch):
    """Wide-open filters so scan tests can isolate one condition at a time."""
    for name, value in [("MAX_STOCK_PRICE", None), ("MIN_AVG_STOCK_VOLUME", 0), ("SKIP_EARNINGS", False),
                        ("DTE_MIN", 1), ("DTE_MAX", 60), ("MIN_BID", 0.10), ("MIN_OPEN_INTEREST", 10),
                        ("MAX_SPREAD_PCT", 0.5), ("DELTA_MIN", 0.0), ("DELTA_MAX", 1.0), ("MIN_ANNUALIZED_RETURN", 0.0),
                        ("MAX_COLLATERAL", None), ("EXCLUDE_TICKERS", []), ("STRIKE_WINDOW", 0.30),
                        ("IBKR_BATCH_SIZE", 90), ("IBKR_QUOTE_TIMEOUT", 0)]:
        monkeypatch.setattr(C, name, value)
    return C


def install_yahoo(monkeypatch, open_interest=None, **ticker_kwargs):
    """Serve open interest (and optionally earnings) from a fake Yahoo ticker."""
    rows = open_interest if open_interest is not None else {90.0: 100}
    puts = pd.DataFrame([{"strike": k, "openInterest": v} for k, v in rows.items()])
    tk = FakeTicker(option_chains={EXP_ISO: Chain(puts)}, **ticker_kwargs)
    monkeypatch.setattr(ps.yf, "Ticker", lambda symbol: tk)
    return tk


def scan(monkeypatch, ib=None, open_interest=None, **ticker_kwargs):
    """Run one stock through the scanner against fake IBKR and Yahoo data."""
    install_yahoo(monkeypatch, open_interest, **ticker_kwargs)
    ib = ib or FakeIB(chains=[chain([EXP], [90.0])])
    return ps.scan_ticker("AAA", TODAY, ib)


def test_scan_accepts_a_qualifying_put(monkeypatch, loose_config):
    """Combine IBKR prices and greeks with Yahoo open interest into one suggestion."""
    [c] = scan(monkeypatch)
    assert (c.ticker, c.expiration, c.dte, c.strike) == ("AAA", EXP_ISO, 30, 90.0)
    assert (c.bid, c.ask, c.iv, c.delta, c.open_interest, c.volume) == (1.0, 1.1, 0.3, -0.2, 100, 50)
    assert (c.premium_per_contract, c.collateral, c.breakeven, c.cushion_pct) == (100.0, 9000.0, 89.0, 0.10)


@pytest.mark.parametrize("quote,open_interest", [
    ({"bid": 0.05}, None),                       # below MIN_BID
    ({"bid": 0.0, "ask": 1.0}, None),            # no bid
    ({"bid": 1.0, "ask": 0.5}, None),            # ask below bid
    ({"bid": 1.0, "ask": 5.0}, None),            # spread too wide
    ({"delta": None}, None),                     # no greeks from IBKR
    ({}, {90.0: 1}),                             # too little open interest
    ({}, {85.0: 1000}),                          # open interest missing for this strike
])
def test_scan_rejects_puts_that_fail_a_filter(monkeypatch, loose_config, quote, open_interest):
    """Drop puts whose price, greeks, or open interest do not meet the filters."""
    ib = FakeIB(chains=[chain([EXP], [90.0])], quotes={(EXP, 90.0): quote})
    assert scan(monkeypatch, ib, open_interest) == []


def test_scan_rejects_delta_and_return_outside_limits(monkeypatch, loose_config):
    """Apply the delta band and the minimum annualized return."""
    monkeypatch.setattr(C, "DELTA_MIN", 0.5)
    assert scan(monkeypatch) == []
    monkeypatch.setattr(C, "DELTA_MIN", 0.0)
    monkeypatch.setattr(C, "MIN_ANNUALIZED_RETURN", 10.0)
    assert scan(monkeypatch) == []


def test_put_contracts_limit_expirations_strikes_and_collateral(monkeypatch, loose_config):
    """Price only listed puts in the day window, just below the stock price, within the cash limit."""
    monkeypatch.setattr(C, "MAX_COLLATERAL", 8_600)
    ib = FakeIB(chains=[chain([EXP, "20261231"], [60.0, 80.0, 85.0, 86.0, 90.0, 100.0, 110.0]),
                        chain([EXP], [75.0], exchange="CBOE")],
                unlisted={(EXP, 85.0)})
    stock = ps.get_stock(ib, "AAA")
    got = ps.get_put_contracts(ib, stock, 100.0, lambda exp: exp == EXP)
    # 60 is outside the 30% window, 90+ need too much cash or are not below spot, 85 is not listed, CBOE is ignored.
    assert [(c.lastTradeDateOrContractMonth, c.strike) for c in got] == [(EXP, 80.0), (EXP, 86.0)]
    assert ps.get_put_contracts(ib, stock, 100.0, lambda exp: False) == []


def test_scan_skips_expirations_outside_window_or_spanning_earnings(monkeypatch, loose_config):
    """Leave out expirations that are too near, too far, or after the next earnings report."""
    monkeypatch.setattr(C, "DTE_MIN", 31)
    assert scan(monkeypatch) == []
    monkeypatch.setattr(C, "DTE_MIN", 1)
    monkeypatch.setattr(C, "SKIP_EARNINGS", True)
    earnings = pd.DataFrame({"x": [1]}, index=pd.to_datetime([TODAY + timedelta(days=10)]))
    assert scan(monkeypatch, earnings_df=earnings) == []
    later = pd.DataFrame({"x": [1]}, index=pd.to_datetime([TODAY + timedelta(days=40)]))
    assert scan(monkeypatch, earnings_df=later)[0].earnings_date == "2026-11-09"


def test_scan_quotes_in_batches_and_cancels_every_request(monkeypatch, loose_config):
    """Stay under IBKR's live-quote limit and release every quote after reading it."""
    monkeypatch.setattr(C, "IBKR_BATCH_SIZE", 2)
    ib = FakeIB(chains=[chain([EXP], [80.0, 85.0, 90.0])])
    scan(monkeypatch, ib, {80.0: 100, 85.0: 100, 90.0: 100})
    assert ib.batches == [80.0, 85.0, 90.0]
    assert len([c for c in ib.cancelled if c.secType == "OPT"]) == 3


@pytest.mark.parametrize("ib,error", [
    (None, "IBKR not connected"),
    (FakeIB(qualify=False), "stock not found at IBKR"),
    (FakeIB(raises=True), "stock not found at IBKR"),
    (FakeIB(stock={"price": float("nan")}), "stock price unavailable"),
    (FakeIB(mktdata_raises=True), "stock price unavailable"),
    (FakeIB(secdef_raises=True), "option chain unavailable"),
    (FakeIB(chains=[chain([EXP], [90.0])], quotes={(EXP, 90.0): {"bid": 0.0, "ask": 0.0}}), "no option quotes"),
])
def test_scan_reports_data_failures(monkeypatch, loose_config, ib, error):
    """Mark a stock as failed when IBKR cannot supply its required data."""
    install_yahoo(monkeypatch)
    with pytest.raises(ps.ScanDataError, match=error):
        ps.scan_ticker("AAA", TODAY, ib)


@pytest.mark.parametrize("volume", [None, float("nan"), 0.0])
def test_scan_rejects_unknown_required_volume(monkeypatch, loose_config, volume):
    """Exclude stocks whose required trading volume cannot be verified."""
    monkeypatch.setattr(C, "MIN_AVG_STOCK_VOLUME", 1_000_000)
    with pytest.raises(ps.ScanDataError, match="volume unavailable"):
        scan(monkeypatch, FakeIB(stock={"av_volume": volume}))


def test_scan_rejects_unknown_earnings(monkeypatch, loose_config):
    """Exclude stocks when the enabled earnings check cannot be completed."""
    monkeypatch.setattr(C, "SKIP_EARNINGS", True)
    with pytest.raises(ps.ScanDataError, match="earnings date unavailable"):
        scan(monkeypatch)


@pytest.mark.parametrize("setup,ib,reason", [
    ({"EXCLUDE_TICKERS": ["AAA"]}, None, "excluded in config"),
    ({"MAX_STOCK_PRICE": 50}, FakeIB(), "stock price exceeds limit"),
    ({"MIN_AVG_STOCK_VOLUME": 10_000_000}, FakeIB(), "average stock volume below limit"),
    ({}, FakeIB(chains=[chain([EXP], [90.0], exchange="CBOE")]), "no listed option expirations"),
])
def test_scan_skips_stocks_by_rule(monkeypatch, loose_config, setup, ib, reason):
    """Skip stocks excluded by configuration or with no SMART-routed options, without failing the run."""
    for name, value in setup.items():
        monkeypatch.setattr(C, name, value)
    install_yahoo(monkeypatch)
    with pytest.raises(ps.ScanSkipped, match=reason):
        ps.scan_ticker("AAA", TODAY, ib)


def test_spot_falls_back_to_close_when_no_live_price(monkeypatch):
    """Use the last close when the market is shut and no live price is available."""
    monkeypatch.setattr(C, "IBKR_QUOTE_TIMEOUT", 0)
    ib = FakeIB(stock={"price": float("nan"), "close": 99.5})
    assert ps.get_spot_and_volume(ib, ps.get_stock(ib, "AAA")) == (99.5, 5_000_000.0)


def test_open_interest_skips_expirations_yahoo_cannot_supply():
    """Keep open interest from expirations that loaded and ignore missing values."""
    puts = pd.DataFrame([{"strike": 90.0, "openInterest": 700}, {"strike": 85.0, "openInterest": float("nan")}])

    class PartlyBroken(FakeTicker):
        def option_chain(self, exp_str):
            if exp_str == "2026-11-06":
                raise RuntimeError("yahoo hiccup")
            return Chain(puts)

    got = ps.get_open_interest(PartlyBroken(), {EXP_ISO, "2026-11-06"})
    assert got == {(EXP_ISO, 90.0): 700}


def test_missing_strike_notices_are_hidden_from_logs():
    """Hide IBKR's expected notices about strikes that are not listed, but keep other errors."""
    f = ps.HideMissingStrikes()

    def record(msg):
        return logging.LogRecord("ib_async.wrapper", logging.ERROR, "", 0, msg, None, None)

    assert not f.filter(record("Error 200, reqId 11: No security definition has been found"))
    assert not f.filter(record("Unknown contract: Option(symbol='AAA')"))
    assert f.filter(record("Error 354: Requested market data is not subscribed"))


def test_put_quotes_wait_for_prices_and_greeks_to_arrive(monkeypatch, loose_config):
    """Keep listening until each put's price and greeks have arrived, then stop early."""
    monkeypatch.setattr(C, "IBKR_QUOTE_TIMEOUT", 30)
    ib = FakeIB(chains=[chain([EXP], [90.0])], quotes={(EXP, 90.0): {"delta": None}})
    waits = []

    def arrive(seconds):
        """Deliver the greeks on the first wait."""
        waits.append(seconds)
        ticker.modelGreeks = SimpleNamespace(impliedVol=0.3, delta=-0.2)

    ib.sleep = arrive
    contracts = ps.get_put_contracts(ib, ps.get_stock(ib, "AAA"), 100.0, lambda exp: True)
    real_req = ib.reqMktData

    def capture(*args):
        """Remember the ticker so the fake can update it while waiting."""
        nonlocal ticker
        ticker = real_req(*args)
        return ticker

    ticker = None
    ib.reqMktData = capture
    [q] = ps.get_put_quotes(ib, contracts)
    assert (q.delta, len(waits)) == (-0.2, 1)
