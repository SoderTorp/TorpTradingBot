import json
import os
from dotenv import load_dotenv

load_dotenv()

# Alpaca credentials
ALPACA_KEY = os.environ["KEY"]
ALPACA_SECRET = os.environ["SECRET"]
PAPER_TRADING = os.environ.get("PAPER_TRADING", "true").strip().lower() == "true"
# Note: the alpaca-py SDK selects the correct endpoint automatically based on
# PAPER_TRADING. paper=True → paper-api.alpaca.markets, paper=False → api.alpaca.markets

# Wheel Strategy
WHEEL_STOCK = os.environ.get("WHEEL_STOCK", "AAPL").strip()

# Budget
TOTAL_BUDGET = float(os.environ.get("TOTAL_BUDGET", "50000"))
TRADING_PCT = float(os.environ.get("TRADING_PCT", "0.10"))
RESERVE_PCT = float(os.environ.get("RESERVE_PCT", "0.10"))
WHEEL_ALLOC = float(os.environ.get("WHEEL_ALLOC", "0.50"))
COPY_ALLOC = float(os.environ.get("COPY_ALLOC", "0.50"))

TRADING_POOL = TOTAL_BUDGET * TRADING_PCT          # $5,000
WHEEL_BUDGET = TRADING_POOL * WHEEL_ALLOC          # $2,500
COPY_BUDGET = TRADING_POOL * COPY_ALLOC            # $2,500
RESERVE_FLOOR = TOTAL_BUDGET * RESERVE_PCT         # $5,000 — never go below

# Copy Trade Risk Controls
COPY_TRADE_MAX_POSITION_PCT = float(os.environ.get("COPY_TRADE_MAX_POSITION_PCT", "2"))
COPY_TRADE_MAX_ALLOCATION_PCT = float(os.environ.get("COPY_TRADE_MAX_ALLOCATION_PCT", "15"))
COPY_TRADE_MAX_CONCURRENT = int(os.environ.get("COPY_TRADE_MAX_CONCURRENT", "3"))
COPY_TRADE_DISCLOSURE_AGE_MAX = int(os.environ.get("COPY_TRADE_DISCLOSURE_AGE_MAX", "10"))
COPY_TRADE_STOP_LOSS_PCT = float(os.environ.get("COPY_TRADE_STOP_LOSS_PCT", "8"))
COPY_TRADE_TRAILING_TRIGGER_PCT = float(os.environ.get("COPY_TRADE_TRAILING_TRIGGER_PCT", "2"))
COPY_TRADE_TRAILING_DISTANCE_PCT = float(os.environ.get("COPY_TRADE_TRAILING_DISTANCE_PCT", "4"))
COPY_TRADE_KELLY_FRACTION = float(os.environ.get("COPY_TRADE_KELLY_FRACTION", "0.25"))
COPY_TRADE_LADDER_TARGETS = json.loads(os.environ.get("COPY_TRADE_LADDER_TARGETS", "[4, 8, 12]"))

# Ollama AI Advisor
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "60"))

# State and log dirs (inside container: /app; outside: relative path)
STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
