"""
Configuration for the daily cash-secured put scanner.
Edit this file to tune the strategy. No trades are ever placed.
"""

# ---------------------------------------------------------------------------
# Universe: Nasdaq-100 technology-sector names (edit freely).
# ---------------------------------------------------------------------------
UNIVERSE = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALAB", "ALNY", "AMAT",
    "AMD", "AMGN", "AMZN", "APP", "ARM", "ASML", "AVGO", "AXON", "BKNG", "BKR",
    "CCEP", "CDNS", "CEG", "CMCSA", "COST", "CPRT", "CRWD", "CRWV", "CSCO", "CSX",
    "CTAS", "DASH", "DDOG", "DXCM", "EXC", "FANG", "FAST", "FER", "FTNT", "GEHC",
    "GILD", "GOOGL", "HON", "HONA", "IDXX", "INTC", "INTU", "ISRG", "KDP", "KHC",
    "KLAC", "LIN", "LITE", "LRCX", "MAR", "MCHP", "MDLZ", "MELI", "META", "MNST",
    "MPWR", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NFLX", "NVDA", "NXPI", "ODFL",
    "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM", "REGN",
    "RKLB", "ROP", "ROST", "SBUX", "SHOP", "SNDK", "SNPS", "SPCX", "STX", "TER",
    "TMUS", "TRI", "TSLA", "TTWO", "TXN", "VRTX", "WBD", "WDAY", "WDC",
    "WMT", "XEL",
]

# ---------------------------------------------------------------------------
# Strategy filters
# ---------------------------------------------------------------------------
DELTA_MIN = 0.15            # absolute put delta, lower bound
DELTA_MAX = 0.25            # absolute put delta, upper bound
DTE_MIN = 21                # days to expiration
DTE_MAX = 45
MIN_ANNUALIZED_RETURN = 0.15   # 15% annualized on cash collateral (strike x 100)
MAX_STOCK_PRICE = None     # No max price
SKIP_EARNINGS = True        # drop expirations that span an earnings date
RISK_FREE_RATE = 0.04       # used in Black-Scholes delta

# ---------------------------------------------------------------------------
# Premium richness: where today's implied vol sits in the stock's past year.
# Read from IBKR (IB Gateway or TWS must be running with the API enabled).
# Gateway live port 4001, paper 4002; TWS live 7496, paper 7497.
# ---------------------------------------------------------------------------
IBKR_HOST = "127.0.0.1"
IBKR_PORT = 4001
IBKR_CLIENT_ID = 17         # any id not used by another API client
IBKR_TIMEOUT = 10           # seconds to wait for the connection
IV_LOOKBACK = "1 Y"         # implied-volatility history window
IV_MIN_DAYS = 120           # fewer daily IV values than this = stock skipped

# ---------------------------------------------------------------------------
# Portfolio limits
# ---------------------------------------------------------------------------
MAX_COLLATERAL = 30_000     # max cash per contract (strike x 100); None = no limit
EXCLUDE_TICKERS = ["INTC", "MRVL", "MU"]   # never suggest (e.g. names already held)
MAX_PER_GROUP = 1           # max displayed names (top picks + alternatives) per group below
SECTOR_GROUPS = {
    "Semiconductors": [
        "ADI", "ALAB", "AMAT", "AMD", "ARM", "ASML", "AVGO", "INTC", "KLAC", "LRCX", "MCHP",
        "MPWR", "MRVL", "MU", "NVDA", "NXPI", "QCOM", "SNDK", "TER", "TXN",
    ],
}

# ---------------------------------------------------------------------------
# Liquidity filters
# ---------------------------------------------------------------------------
MIN_AVG_STOCK_VOLUME = 1_000_000   # 10-day average shares/day
MIN_OPEN_INTEREST = 500
MAX_SPREAD_PCT = 0.10              # (ask - bid) / mid
MIN_BID = 0.20                     # ignore sub-20c options

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
TOP_N = 5                   # suggestions per day
ALTERNATIVE_N = 5           # additional qualifying names outside the top picks
MAX_PER_TICKER = 1          # best contract per underlying
OUTPUT_DIR = "reports"      # markdown files land here (relative to script)

# ---------------------------------------------------------------------------
# Scoring weights (must sum to 1). See README for the formula.
# ---------------------------------------------------------------------------
W_RICHNESS = 0.40  # IV percentile: today's implied vol vs the stock's past year
W_YIELD = 0.20     # annualized return on collateral
W_SAFETY = 0.30    # lower |delta| within the DELTA_MIN..DELTA_MAX band
W_CUSHION = 0.10   # % distance from spot to strike
