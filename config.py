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
MAX_PER_TICKER = 1          # best contract per underlying
OUTPUT_DIR = "reports"      # markdown files land here (relative to script)

# ---------------------------------------------------------------------------
# Scoring weights (must sum to 1). See README for the formula.
# ---------------------------------------------------------------------------
W_YIELD = 0.45     # annualized return on collateral
W_SAFETY = 0.35    # probability of expiring OTM (1 - |delta|)
W_CUSHION = 0.20   # % distance from spot to strike
