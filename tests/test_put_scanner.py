"""Unit tests for put_scanner.py. All network calls (yfinance, SMTP) are mocked."""
import math
import smtplib
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
    return C


def _install_ticker(monkeypatch, fake_tk):
    monkeypatch.setattr(ps.yf, "Ticker", lambda symbol: fake_tk)


def test_scan_ticker_no_spot_returns_empty(monkeypatch, loose_config):
    tk = FakeTicker(fast_info_raises=True, history_raises=True)
    _install_ticker(monkeypatch, tk)
    assert ps.scan_ticker("AAA", date.today()) == []


def test_scan_ticker_price_too_high_skips(monkeypatch, loose_config):
    monkeypatch.setattr(C, "MAX_STOCK_PRICE", 50)
    tk = FakeTicker(fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000})
    _install_ticker(monkeypatch, tk)
    assert ps.scan_ticker("AAA", date.today()) == []


def test_scan_ticker_low_volume_skips(monkeypatch, loose_config):
    monkeypatch.setattr(C, "MIN_AVG_STOCK_VOLUME", 1_000_000)
    tk = FakeTicker(fast_info={"last_price": 100.0, "ten_day_average_volume": 500})
    _install_ticker(monkeypatch, tk)
    assert ps.scan_ticker("AAA", date.today()) == []


def test_scan_ticker_no_option_chain_skips(monkeypatch, loose_config):
    tk = FakeTicker(fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000}, options_raises=True)
    _install_ticker(monkeypatch, tk)
    assert ps.scan_ticker("AAA", date.today()) == []


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


def test_scan_ticker_chain_fetch_failure_is_skipped(monkeypatch, loose_config):
    today = date.today()
    exp_str = (today + timedelta(days=30)).isoformat()

    class RaisingChainTicker(FakeTicker):
        def option_chain(self, exp_str):
            raise RuntimeError("chain fetch failed")

    tk = RaisingChainTicker(fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000}, options=(exp_str,))
    _install_ticker(monkeypatch, tk)
    assert ps.scan_ticker("AAA", today) == []


def test_scan_ticker_empty_puts_is_skipped(monkeypatch, loose_config):
    today = date.today()
    exp_str = (today + timedelta(days=30)).isoformat()
    tk = FakeTicker(
        fast_info={"last_price": 100.0, "ten_day_average_volume": 5_000_000},
        options=(exp_str,),
        option_chains={exp_str: Chain(pd.DataFrame())},
    )
    _install_ticker(monkeypatch, tk)
    assert ps.scan_ticker("AAA", today) == []


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
def test_score_ranks_best_first_and_clamps_caps(monkeypatch):
    monkeypatch.setattr(C, "W_YIELD", 0.5)
    monkeypatch.setattr(C, "W_SAFETY", 0.3)
    monkeypatch.setattr(C, "W_CUSHION", 0.2)

    low = make_candidate(ticker="LOW", annualized_return=0.10, prob_otm=0.5, cushion_pct=0.05)
    high = make_candidate(ticker="HIGH", annualized_return=1.0, prob_otm=1.0, cushion_pct=0.50)

    ranked = ps.score([low, high])
    assert [c.ticker for c in ranked] == ["HIGH", "LOW"]
    # HIGH's return/cushion are both above their caps, so they clamp to 1.0.
    assert ranked[0].score == round(0.5 * 1.0 + 0.3 * 1.0 + 0.2 * 1.0, 4)
    assert ranked[1].score == round(0.5 * (0.10 / 0.60) + 0.3 * 0.5 + 0.2 * (0.05 / 0.20), 4)


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
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    called = {"smtp": False}

    class BoomSMTP:
        def __init__(self, *a, **k):
            called["smtp"] = True

    monkeypatch.setattr(smtplib, "SMTP", BoomSMTP)
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

    assert ps.main() == 0
    out = capsys.readouterr().out
    assert "AAA" in out

    md_files = list(main_env.glob("puts_*.md"))
    csv_files = list(main_env.glob("all_candidates_*.csv"))
    assert len(md_files) == 1
    assert len(csv_files) == 1
    assert len(sent) == 2  # one email, one "sms" (sent via send_email too)


def test_main_full_run_no_candidates_skips_csv(monkeypatch, main_env):
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today: [])

    assert ps.main() == 0
    assert list(main_env.glob("all_candidates_*.csv")) == []
    assert len(list(main_env.glob("puts_*.md"))) == 1


def test_main_notification_failures_are_swallowed(monkeypatch, main_env):
    monkeypatch.setattr(ps.sys, "argv", ["put_scanner.py"])
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.setenv("SMS_TO", "+15551234567")
    monkeypatch.setattr(ps, "scan_ticker", lambda symbol, today: [])

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ps, "send_email", boom)
    assert ps.main() == 0  # errors are logged, not raised
