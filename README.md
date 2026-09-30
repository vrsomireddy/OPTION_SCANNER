# Daily Cash-Secured Put Scanner (Nasdaq-100 Tech)

Suggestions only. No order placement. Reads implied-volatility history from
Interactive Brokers over a read-only API connection (see [IBKR setup](#ibkr-setup)).

## What it does

Each run:
1. Pulls fresh quotes and option chains from Yahoo Finance for the tickers in `config.py`,
   skipping any listed in `EXCLUDE_TICKERS` (e.g. stocks you already hold).
2. Drops underlyings with thin volume (`MIN_AVG_STOCK_VOLUME`, 1M shares/day) or
   priced above `MAX_STOCK_PRICE` — that price cap is currently `None`, i.e. disabled.
3. Looks at every expiration 21–45 DTE, skipping any that spans the next earnings date.
   Stocks with unknown earnings dates or missing required average volume are excluded
   and recorded as data failures.
4. For each OTM put: computes Black-Scholes delta from the quoted implied vol,
   keeps |Δ| 0.15–0.25, OI ≥ 500, bid ≥ $0.20, bid-ask ≤ 10%, collateral ≤ `MAX_COLLATERAL`
   ($30,000), and **annualized return on collateral ≥ 15%** where `annualized = bid / strike × 365 / DTE`.
5. For each stock that still has candidates, asks IBKR for a year of daily implied
   volatility and records the **IV percentile**: the share of past-year days on which the
   stock's IV was lower than today. Stocks with under `IV_MIN_DAYS` (120) of IV history are skipped.
6. Scores, ranks, keeps the best contract per ticker, and outputs the top 5, with at most
   `MAX_PER_GROUP` names from each `SECTOR_GROUPS` group (default: 1 semiconductor).
7. Adds up to 5 alternative names, in score order, excluding all top-pick tickers. The
   sector-group limit counts top picks and alternatives together.
8. Compares recommendations with the latest usable earlier daily report.

### Scoring (deterministic)

```
score = 0.40 × IV percentile                          # premium rich vs the stock's own past year
      + 0.20 × min(annualized, 60%)/60%                # yield
      + 0.30 × (DELTA_MAX − |delta|)/(DELTA_MAX − DELTA_MIN)   # safer end of the delta band
      + 0.10 × min(cushion, 20%)/20%                   # % distance spot → strike
```
Raw yield rises with volatility, so on its own it favors names that are always volatile.
The IV percentile instead rewards selling when options are expensive *for that stock*.
The safety term is rescaled to the delta band so it actually separates candidates.
Weights live in `config.py`. Same inputs → same output, always.

### Output
- Terminal + `reports/puts_YYYY-MM-DD.md` (top 5 table + trade descriptions)
- `reports/all_candidates_YYYY-MM-DD.csv` (everything that passed filters, ranked)
- Optional email and/or SMS (see below)

Reports distinguish completed stocks, stocks skipped before contract evaluation,
and data failures. Any data failure marks the report and notifications incomplete
and returns exit status 1; requested notification failures also return 1. A failed
chain excludes that stock from the run. Successful empty scans return 0.
Same-day reruns replace both reports; empty results produce a CSV with headers only.

### Daily changes and alternatives

The full report labels each displayed put as a new ticker, a changed contract,
or a repeat contract relative to the previous report's displayed names and contracts.
It shows bid and score changes only when the exact ticker, expiration, and strike
exist in the previous candidate CSV. A changed contract can still have a comparison
if it was previously among the qualifying candidates. Missing comparisons are explicit.
These labels describe report membership, not positions you hold or trades you made.

The comparison date is shown explicitly. Same-day and future files are ignored;
unreadable files are logged and the next earlier usable file is tried. An empty
prior CSV is a valid baseline. Older CSVs without selection metadata use the current
top-pick limits to reconstruct their selections, so changed limits can affect those
legacy labels. New CSVs record the actual `report_group`, `comparison_date`, and
`change_note`. Prior reports may be incomplete, and score changes can also reflect
configuration changes.

Identical spot, bid, and ask values for the same contract trigger a **potentially
stale** flag. When annualized return rises with a lower DTE and the same quote,
the report explains that the quoted premium has not improved. This is a heuristic:
unchanged prices do not prove staleness, changed prices do not prove freshness,
and quote timestamps are unavailable. Flags do not change ranking or eligibility.

Email includes the full comparisons and alternatives. Text messages include changes
for top picks and the alternative names; details for alternatives are in the full
report. The longer text may span multiple messages depending on delivery service.

`reports/`, `logs/`, `venv/`, and `.env` are gitignored.

## Setup (Mac)

```bash
cd ~/projects/option_scanner
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env      # edit if you want email/SMS; otherwise leave as is
./venv/bin/python put_scanner.py   # test run, ~3 min for the full universe
```

## IBKR setup

The scan needs IB Gateway (or TWS) running and logged in when it starts.

1. In Gateway: Configure → Settings → API → Settings: enable **ActiveX and Socket Clients**,
   tick **Read-Only API**, and note the socket port (Gateway live 4001, paper 4002; TWS 7496/7497).
   Set `IBKR_PORT` in `config.py` to match.
2. Market data: historical implied volatility needs the stock and options (OPRA) data
   subscriptions on the account.
3. Gateway restarts daily and asks for two-factor login about weekly. Set the daily auto-restart
   time outside 10:00 ET, and log in again when prompted, or the scan cannot reach it.

The connection is opened with `readonly=True` and does not download positions, orders, or
balances. The project contains no order code. If Gateway is unreachable, every stock with
candidates is reported as a data failure and the run is marked INCOMPLETE, so the scanner never
ranks picks without IV history.

## Email / SMS

Fill in `.env`:
- **Email:** Gmail App Password (myaccount.google.com/apppasswords, requires 2-step verification) → `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.
- **Text:** set `SMS_TO` to your carrier's email-to-SMS address, e.g. `4045551234@vtext.com` (Verizon), `@txt.att.net` (AT&T), `@tmomail.net` (T-Mobile). A short one-line-per-pick message is sent.

Unset variables simply disable that channel.

Gmail displays the App Password as four space-separated groups. **Strip the spaces**
before pasting into `.env` — `run.sh` sources the file as shell, so an unquoted space
makes the second group look like a command and the password silently ends up empty.

Verify credentials without waiting for a full scan:

```bash
bash -c 'set -a && source .env && set +a && ./venv/bin/python put_scanner.py --test-email'
```

It sends a one-line test message and exits. On a bad login it prints the Gmail error
and returns 1 instead of a traceback. Missing SMTP credentials also return 1.

## Scheduling on Mac

### Option A — cron (currently installed)
```bash
crontab -l   # to inspect
crontab -e   # to change
# 10:00 local, Mon–Fri. This machine is on Eastern time, so that is 10:00 ET.
0 10 * * 1-5 /bin/bash /Users/vsomireddy/projects/option_scanner/run.sh
```
Each run appends to `logs/run_YYYY-MM-DD.log`. Cron does not fire while the Mac is
asleep, and a missed job is not made up — a closed laptop at 10:00 means no report.
Give cron Full Disk Access if macOS blocks it: System Settings → Privacy & Security → Full Disk Access → add `/usr/sbin/cron`.

### Option B — launchd (runs missed jobs on wake)
The bundled `com.venkat.putscanner.plist` still has `YOUR_USER` placeholders and is not
installed. To switch to it, remove the cron line first so the scan does not run twice:
```bash
crontab -e   # remove only the scanner entry; preserve other jobs
sed -i '' "s#/Users/YOUR_USER/put-scanner#$HOME/projects/option_scanner#g" com.venkat.putscanner.plist
cp com.venkat.putscanner.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.venkat.putscanner.plist
launchctl start com.venkat.putscanner     # manual test
```
Keep the Mac awake at 10:00: `sudo pmset repeat wakeorpoweron MTWRF 09:55:00`.

## Running in the cloud instead

The script has no Mac dependencies.

- **AWS**: EC2 t3.micro + the same cron line, or package as a Lambda (Python 3.12, ~3 min runtime, EventBridge rule `cron(0 14 ? * MON-FRI *)` = 10:00 ET during EDT; use SES for email). The `.env` values go in Lambda environment variables or Secrets Manager.
- **Azure**: Azure Functions timer trigger `0 0 14 * * 1-5`, or a Container App Job on a schedule. Secrets in Key Vault.

## Tuning

All knobs are in `config.py`. Common tweaks:
- `MAX_COLLATERAL` caps cash per contract (default $30,000; `None` removes it).
  `MAX_STOCK_PRICE` can additionally drop whole stocks above a price.
- `EXCLUDE_TICKERS` lists names never to suggest. Keep it in step with what you hold.
- Add groups to `SECTOR_GROUPS` (e.g. AI cloud: CRWV, NBIS) or raise `MAX_PER_GROUP`.
- Lower `MIN_ANNUALIZED_RETURN` in low-IV markets or you may get an empty list.
- Raise `MAX_SPREAD_PCT` above 0.10 if wide-market names are being filtered out.
- `MAX_PER_TICKER = 2` to see two expirations per name.
- `ALTERNATIVE_N = 5` adds five distinct names after the top picks; set it to `0`
  to disable alternatives. Fewer are shown when too few additional names qualify.

## Caveats
- Yahoo data is ~15 min delayed and occasionally stale/missing; treat bids as approximate and confirm on IBKR before entering an order.
- IV percentile comes from IBKR's daily stock-level implied volatility; per-contract IV and delta
  still come from Yahoo.
- Delta is computed from Yahoo's IV, not read from the exchange, so it can differ slightly from your broker's Greeks.
- The scan runs on market holidays too and will email a report built from the previous session's stale quotes. Check the date before acting on a holiday-morning email.
- Not financial advice.
