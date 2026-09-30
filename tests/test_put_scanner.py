"""Unit tests for put_scanner.py. All network calls (yfinance, SMTP) are mocked."""
import math
import smtplib
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

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


def make_put_row(**overrides):
    row = dict(strike=90.0, bid=1.0, ask=1.1, impliedVolatility=0.30, openInterest=100, volume=50)
    row.update(overrides)
    return row


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
# bs_put_delta
# ---------------------------------------------------------------------------
def test_bs_put_delta_normal_range():
    d = ps.bs_put_delta(100, 90, 30 / 365, 0.04, 0.30)
    assert -1.0 < d < 0.0


@pytest.mark.parametrize(
    "spot,strike,t_years,iv",
    [(0, 90, 30 / 365, 0.3), (100, 0, 30 / 365, 0.3), (100, 90, 0, 0.3), (100, 90, 30 / 365, 0)],
)
def test_bs_put_delta_invalid_inputs_return_nan(spot, strike, t_years, iv):
    assert math.isnan(ps.bs_put_delta(spot, strike, t_years, 0.04, iv))


# ---------------------------------------------------------------------------
# get_spot_and_volume
# ---------------------------------------------------------------------------
def test_get_spot_and_volume_fast_info_with_avg_volume():
    tk = FakeTicker(fast_info={"last_price": 150.0, "ten_day_average_volume": 2_000_000})
    spot, vol = ps.get_spot_and_volume(tk)
    assert spot == 150.0
    assert vol == 2_000_000
    assert tk.history_calls == 0  # never needed the fallback


def test_get_spot_and_volume_fast_info_falls_back_to_history_for_volume():
    hist = pd.DataFrame({"Volume": [100, 200, 300]})
    tk = FakeTicker(fast_info={"last_price": 50.0}, history_df=hist)
    spot, vol = ps.get_spot_and_volume(tk)
    assert spot == 50.0
    assert vol == 200.0


def test_get_spot_and_volume_fast_info_zero_avg_volume_and_empty_history():
    tk = FakeTicker(fast_info={"last_price": 50.0}, history_df=pd.DataFrame())
    spot, vol = ps.get_spot_and_volume(tk)
    assert spot == 50.0
    assert vol is None


def test_get_spot_and_volume_fast_info_nonpositive_price_falls_through_to_history():
    hist = pd.DataFrame({"Close": [10.0, 11.0], "Volume": [1000, 2000]})
    tk = FakeTicker(fast_info={"last_price": 0.0}, history_df=hist)
    spot, vol = ps.get_spot_and_volume(tk)
    assert spot == 11.0
    assert vol == 1500.0


def test_get_spot_and_volume_fast_info_raises_uses_history():
    hist = pd.DataFrame({"Close": [20.0, 22.0], "Volume": [10, 20]})
    tk = FakeTicker(fast_info_raises=True, history_df=hist)
    spot, vol = ps.get_spot_and_volume(tk)
    assert spot == 22.0
    assert vol == 15.0


def test_get_spot_and_volume_fast_info_raises_and_history_empty():
    tk = FakeTicker(fast_info_raises=True, history_df=pd.DataFrame())
    spot, vol = ps.get_spot_and_volume(tk)
    assert (spot, vol) == (None, None)


def test_get_spot_and_volume_everything_fails():
    tk = FakeTicker(fast_info_raises=True, history_raises=True)
    spot, vol = ps.get_spot_and_volume(tk)
    assert (spot, vol) == (None, None)


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
# scan_ticker
# ---------------------------------------------------------------------------
@pytest.fixture
def loose_config(monkeypatch):
    """Wide-open filters so scan_ticker tests can isolate one condition at a time."""
    monkeypatch.setattr(C, "MAX_STOCK_PRICE", None)
    monkeypatch.setattr(C, "MIN_AVG_STOCK_VOLUME", 0)
    monkeypatch.setattr(C, "SKIP_EARNINGS", False)
    monkeypatch.setattr(C, "DTE_MIN", 1)
    monkeypatch.setattr(C, "DTE_MAX", 60)
    monkeypatch.setattr(C, "MIN_BID", 0.10)
    monkeypatch.setattr(C, "MIN_OPEN_INTEREST", 10)
    monkeypatch.setattr(C, "MAX_SPREAD_PCT", 0.5)
    monkeypatch.setattr(C, "DELTA_MIN", 0.0)
    monkeypatch.setattr(C, "DELTA_MAX", 1.0)
    monkeypatch.setattr(C, "MIN_ANNUALIZED_RETURN", 0.0)
    monkeypatch.setattr(C, "RISK_FREE_RATE", 0.04)
    monkeypatch.setattr(C, "MAX_COLLATERAL", None)
    monkeypatch.setattr(C, "EXCLUDE_TICKERS", [])
    monkeypatch.setattr(ps, "get_realized_vol", lambda tk: 0.25)
    return C


def _install_ticker(monkeypatch, fake_tk):
    monkeypatch.setattr(ps.yf, "Ticker", lambda symbol: fake_tk)


def test_scan_ticker_no_spot_reports_failure(monkeypatch, loose_config):
    """Check that unavailable data and excluded stocks have distinct outcomes."""
    tk = FakeTicker(fast_info_raises=True, history_raises=True)
    _install_ticker(monkeypatch, tk)
    with pytest.raises(ps.ScanDataError, match="stock price unavailable"):
        ps.scan_ticker("AAA", date.today())


def test_scan_ticker_price_too_high_skips(monkeypatch, loose_config):
    """Check that unavailable data and excluded stocks have distinct outcomes."""
    monkeypatch.setattr(C, "MAX_STOCK_PRICE", 50)
    tk = FakeTicker(fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000})
    _install_ticker(monkeypatch, tk)
    with pytest.raises(ps.ScanSkipped, match="stock price exceeds limit"):
        ps.scan_ticker("AAA", date.today())


def test_scan_ticker_low_volume_skips(monkeypatch, loose_config):
    """Check that unavailable data and excluded stocks have distinct outcomes."""
    monkeypatch.setattr(C, "MIN_AVG_STOCK_VOLUME", 1_000_000)
    tk = FakeTicker(fast_info={"last_price": 100.0, "ten_day_average_volume": 500})
    _install_ticker(monkeypatch, tk)
    with pytest.raises(ps.ScanSkipped, match="average stock volume below limit"):
        ps.scan_ticker("AAA", date.today())


def test_scan_ticker_no_option_chain_reports_failure(monkeypatch, loose_config):
    """Check that unavailable data and excluded stocks have distinct outcomes."""
    tk = FakeTicker(fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000}, options_raises=True)
    _install_ticker(monkeypatch, tk)
    with pytest.raises(ps.ScanDataError, match="option expirations unavailable"):
        ps.scan_ticker("AAA", date.today())


def test_scan_ticker_dte_out_of_window_skips(monkeypatch, loose_config):
    monkeypatch.setattr(C, "DTE_MIN", 21)
    monkeypatch.setattr(C, "DTE_MAX", 45)
    today = date.today()
    exp_str = (today + timedelta(days=5)).isoformat()
    tk = FakeTicker(
        fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000},
        options=(exp_str,),
        option_chains={exp_str: Chain(pd.DataFrame([make_put_row()]))},
    )
    _install_ticker(monkeypatch, tk)
    assert ps.scan_ticker("AAA", today) == []


def test_scan_ticker_earnings_within_window_skips_that_expiration(monkeypatch, loose_config):
    monkeypatch.setattr(C, "SKIP_EARNINGS", True)
    today = date.today()
    exp_str = (today + timedelta(days=30)).isoformat()
    earnings_idx = pd.to_datetime([today + timedelta(days=10)])
    tk = FakeTicker(
        fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000},
        earnings_df=pd.DataFrame({"x": [1]}, index=earnings_idx),
        options=(exp_str,),
        option_chains={exp_str: Chain(pd.DataFrame([make_put_row()]))},
    )
    _install_ticker(monkeypatch, tk)
    assert ps.scan_ticker("AAA", today) == []


def test_scan_ticker_chain_fetch_failure_is_reported(monkeypatch, loose_config):
    """Check that unavailable data and excluded stocks have distinct outcomes."""
    today = date.today()
    exp_str = (today + timedelta(days=30)).isoformat()

    class RaisingChainTicker(FakeTicker):
        def option_chain(self, exp_str):
            raise RuntimeError("chain fetch failed")

    tk = RaisingChainTicker(fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000}, options=(exp_str,))
    _install_ticker(monkeypatch, tk)
    with pytest.raises(ps.ScanDataError, match="option chain unavailable"):
        ps.scan_ticker("AAA", today)


def test_scan_ticker_empty_puts_reports_failure(monkeypatch, loose_config):
    """Check that unavailable data and excluded stocks have distinct outcomes."""
    today = date.today()
    exp_str = (today + timedelta(days=30)).isoformat()
    tk = FakeTicker(
        fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000},
        options=(exp_str,),
        option_chains={exp_str: Chain(pd.DataFrame())},
    )
    _install_ticker(monkeypatch, tk)
    with pytest.raises(ps.ScanDataError, match="put chain empty"):
        ps.scan_ticker("AAA", today)


def _scan_with_row(monkeypatch, row, today=None):
    today = today or date.today()
    exp_str = (today + timedelta(days=30)).isoformat()
    tk = FakeTicker(
        fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000},
        options=(exp_str,),
        option_chains={exp_str: Chain(pd.DataFrame([row]))},
    )
    _install_ticker(monkeypatch, tk)
    return ps.scan_ticker("AAA", today)


def test_scan_ticker_accepts_a_qualifying_put(monkeypatch, loose_config):
    out = _scan_with_row(monkeypatch, make_put_row())
    assert len(out) == 1
    c = out[0]
    assert c.ticker == "AAA"
    assert c.strike == 90.0
    assert c.premium_per_contract == 100.0
    assert c.collateral == 9000.0
    assert c.breakeven == 89.0
    assert c.cushion_pct == 0.10
    assert c.earnings_date is None


@pytest.mark.parametrize(
    "row",
    [
        make_put_row(strike=100.0),          # not OTM
        make_put_row(bid=0.05),              # below MIN_BID
        make_put_row(ask=0.0),               # no ask
        make_put_row(ask=0.5, bid=1.0),      # ask < bid
        make_put_row(openInterest=1),        # too little open interest
        make_put_row(ask=5.0, bid=1.0),      # spread too wide
        make_put_row(impliedVolatility=0.0),  # delta becomes nan
    ],
)
def test_scan_ticker_rejects_rows_that_fail_a_filter(monkeypatch, loose_config, row):
    assert _scan_with_row(monkeypatch, row) == []


def test_scan_ticker_rejects_delta_outside_band(monkeypatch, loose_config):
    monkeypatch.setattr(C, "DELTA_MIN", 0.90)
    monkeypatch.setattr(C, "DELTA_MAX", 0.99)
    assert _scan_with_row(monkeypatch, make_put_row()) == []


def test_scan_ticker_rejects_low_annualized_return(monkeypatch, loose_config):
    monkeypatch.setattr(C, "MIN_ANNUALIZED_RETURN", 10.0)
    assert _scan_with_row(monkeypatch, make_put_row()) == []


def test_scan_ticker_handles_missing_values_in_row(monkeypatch, loose_config):
    row = make_put_row(bid=float("nan"), ask=float("nan"), impliedVolatility=float("nan"),
                        openInterest=float("nan"), volume=float("nan"))
    # all-NaN numeric fields default to 0/0.0, which then fails the MIN_BID filter.
    assert _scan_with_row(monkeypatch, row) == []


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------
@pytest.fixture
def score_config(monkeypatch):
    """Fixed weights and bands so score arithmetic can be checked by hand."""
    for name, value in [("W_RICHNESS", 0.4), ("W_YIELD", 0.2), ("W_SAFETY", 0.3), ("W_CUSHION", 0.1),
                        ("DELTA_MIN", 0.15), ("DELTA_MAX", 0.25), ("IV_HV_FULL_CREDIT", 1.5)]:
        monkeypatch.setattr(C, name, value)


def test_score_ranks_best_first_and_clamps_caps(score_config):
    """Combine richness, yield, safety and cushion, clamping each part to its cap."""
    low = make_candidate(ticker="LOW", iv_hv_ratio=1.25, annualized_return=0.10, delta=-0.25, cushion_pct=0.05)
    high = make_candidate(ticker="HIGH", iv_hv_ratio=3.0, annualized_return=1.0, delta=-0.10, cushion_pct=0.50)

    ranked = ps.score([low, high])
    assert [c.ticker for c in ranked] == ["HIGH", "LOW"]
    # HIGH is past every cap, so each part clamps to 1.0.
    assert ranked[0].score == 1.0
    assert ranked[1].score == round(0.4 * 0.5 + 0.2 * (0.10 / 0.60) + 0.3 * 0.0 + 0.1 * (0.05 / 0.20), 4)


def test_score_prefers_rich_premium_over_raw_volatility(score_config):
    """Rank a stock with unusually high option prices above one that is simply always volatile."""
    always_wild = make_candidate(ticker="WILD", iv=0.90, iv_hv_ratio=0.95, annualized_return=0.40)
    unusually_rich = make_candidate(ticker="RICH", iv=0.40, iv_hv_ratio=1.40, annualized_return=0.20)
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

    def fake_scan(symbol, today):
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
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today: [])

    assert ps.main() == 0
    assert pd.read_csv(next(main_env.glob("all_candidates_*.csv"))).empty
    assert len(list(main_env.glob("puts_*.md"))) == 1


def test_main_notification_failures_return_error(monkeypatch, main_env):
    """Report failed notifications through the exit status."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.setenv("SMS_TO", "+15551234567")
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today: [])

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ps, "send_email", boom)
    assert ps.main() == 1


@pytest.mark.parametrize("volume", [None, 0, float("nan"), float("inf")])
def test_scan_rejects_unknown_required_volume(monkeypatch, loose_config, volume):
    """Exclude stocks whose required trading volume cannot be verified."""
    monkeypatch.setattr(C, "MIN_AVG_STOCK_VOLUME", 1_000_000)
    monkeypatch.setattr(ps, "get_spot_and_volume", lambda tk: (100, volume))
    _install_ticker(monkeypatch, FakeTicker())
    with pytest.raises(ps.ScanDataError, match="volume unavailable"):
        ps.scan_ticker("AAA", date.today())


def test_scan_rejects_unknown_earnings(monkeypatch, loose_config):
    """Exclude stocks when the enabled earnings check cannot be completed."""
    monkeypatch.setattr(C, "SKIP_EARNINGS", True)
    tk = FakeTicker(fast_info={"last_price": 100, "ten_day_average_volume": 2_000_000})
    _install_ticker(monkeypatch, tk)
    with pytest.raises(ps.ScanDataError, match="earnings date unavailable"):
        ps.scan_ticker("AAA", date.today())


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
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today: [make_candidate(ticker=symbol)])
    assert ps.main() == 0
    csv = next(main_env.glob("all_candidates_*.csv"))
    assert len(pd.read_csv(csv)) == 2
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today: [])
    assert ps.main() == 0
    assert pd.read_csv(csv).empty
    assert "No contracts met" in next(main_env.glob("puts_*.md")).read_text()


def test_total_outage_is_not_a_successful_empty_scan(monkeypatch, main_env, capsys):
    """Label a total data outage as incomplete in reports and texts."""
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setenv("SMS_TO", "to@example.com")
    sent = []
    monkeypatch.setattr(ps, "send_email", lambda *args: sent.append(args))

    def fail(symbol, today):
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

    def skip(symbol, today):
        """Represent a stock excluded by the price limit."""
        raise ps.ScanSkipped("stock price exceeds limit")

    monkeypatch.setattr(ps, "scan_ticker", skip)
    assert ps.main() == 0
    report = capsys.readouterr().out
    assert "Stocks skipped before contract evaluation: 2" in report
    assert "INCOMPLETE" not in report


def test_no_listed_expirations_is_a_skip(monkeypatch, loose_config):
    """Treat stocks with no listed options as intentional exclusions."""
    _install_ticker(monkeypatch, FakeTicker(fast_info={"last_price": 100}))
    with pytest.raises(ps.ScanSkipped, match="no listed option expirations"):
        ps.scan_ticker("AAA", date.today())


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
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today: [make_candidate(ticker=symbol, dte=29)])
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
# realized volatility and portfolio limits
# ---------------------------------------------------------------------------
def _closes(n, daily_move=0.01):
    """Build a price series that alternates up and down by a fixed percentage."""
    prices, p = [], 100.0
    for i in range(n):
        p *= 1 + (daily_move if i % 2 else -daily_move)
        prices.append(p)
    return pd.DataFrame({"Close": prices})


def test_realized_vol_annualizes_daily_moves(monkeypatch):
    """Turn a year of daily price moves into an annual volatility figure."""
    monkeypatch.setattr(C, "HV_MIN_DAYS", 120)
    hv = ps.get_realized_vol(FakeTicker(history_df=_closes(252)))
    assert hv == pytest.approx(0.01 * math.sqrt(252), rel=0.02)


def test_realized_vol_short_history_is_a_skip(monkeypatch):
    """Skip recently listed stocks instead of reporting a data failure."""
    monkeypatch.setattr(C, "HV_MIN_DAYS", 120)
    with pytest.raises(ps.ScanSkipped, match="too little price history"):
        ps.get_realized_vol(FakeTicker(history_df=_closes(50)))


@pytest.mark.parametrize("tk", [
    FakeTicker(history_df=_closes(252, daily_move=0)),  # flat price, zero volatility
    FakeTicker(history_raises=True),
    FakeTicker(history_df=pd.DataFrame()),            # no Close column
])
def test_realized_vol_unavailable(monkeypatch, tk):
    """Report no volatility when the price history is missing, short, or flat."""
    monkeypatch.setattr(C, "HV_MIN_DAYS", 120)
    assert ps.get_realized_vol(tk) is None


def test_scan_records_iv_to_realized_vol_ratio(monkeypatch, loose_config):
    """Store each contract's implied vol relative to the stock's realized vol."""
    c = _scan_with_row(monkeypatch, make_put_row(impliedVolatility=0.30))[0]
    assert (c.hv, c.iv_hv_ratio) == (0.25, 1.2)


def test_scan_without_realized_vol_reports_failure(monkeypatch, loose_config):
    """Treat missing volatility history as a data failure, not a silent pass."""
    monkeypatch.setattr(ps, "get_realized_vol", lambda tk: None)
    with pytest.raises(ps.ScanDataError, match="realized volatility unavailable"):
        _scan_with_row(monkeypatch, make_put_row())


def test_scan_skips_excluded_tickers_before_fetching(monkeypatch, loose_config):
    """Skip configured names without requesting any market data."""
    monkeypatch.setattr(C, "EXCLUDE_TICKERS", ["AAA"])
    monkeypatch.setattr(ps.yf, "Ticker", lambda symbol: pytest.fail("should not fetch"))
    with pytest.raises(ps.ScanSkipped, match="excluded in config"):
        ps.scan_ticker("AAA", date.today())


def test_scan_drops_contracts_above_collateral_limit(monkeypatch, loose_config):
    """Leave out puts that would tie up more cash than the configured limit."""
    monkeypatch.setattr(C, "MAX_COLLATERAL", 8_000)
    assert _scan_with_row(monkeypatch, make_put_row(strike=90.0)) == []
    monkeypatch.setattr(C, "MAX_COLLATERAL", 9_000)
    assert len(_scan_with_row(monkeypatch, make_put_row(strike=90.0))) == 1


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
    md = ps.to_markdown([make_candidate(iv=0.3, hv=0.25, iv_hv_ratio=1.2)], 1, 1, datetime(2026, 9, 30))
    assert "collateral ≤ $30,000" in md
    assert "≤1 Semiconductors" in md
    assert "IV 30% vs 25% realized (1.20x)" in md
    assert "| 1.20 |" in md


def test_previous_scan_loads_files_written_before_richness_existed(tmp_path):
    """Keep comparing against older reports that lack the new volatility columns."""
    legacy = {k: v for k, v in asdict(make_candidate(ticker="OLD")).items() if k not in ps.OPTIONAL_FIELDS}
    pd.DataFrame([dict(legacy, report_group="top")]).to_csv(tmp_path / "all_candidates_2026-09-10.csv", index=False)
    previous = ps.load_previous_scan(tmp_path, date(2026, 9, 11))
    assert [c.ticker for c in previous.displayed] == ["OLD"]
    assert math.isnan(previous.candidates[0].iv_hv_ratio)
