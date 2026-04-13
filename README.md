# 📈 TorpTradingBot

A lightweight, AI-assisted trading bot running in a Python Docker container.
Combines a **Wheel Options Strategy** with **AI-powered insights** from a local Ollama LLM, and a **Politician Copy Trade** strategy that mirrors disclosed trades from historically successful elected officials.

---

## Strategies

### 🎡 Wheel Strategy

An options income strategy that systematically sells Cash-Secured Puts (CSPs) and Covered Calls (CCs) on a single configured ticker to generate premium income.

- **PUT stage:** Sells a CSP ~10% OTM, 2–4 weeks out. Closes at 50% profit or on assignment.
- **CALL stage:** After assignment, sells a Covered Call ~10% above cost basis, 2–4 weeks out. Closes at 50% profit or if called away.
- Repeats the cycle to continuously collect premium.
- A local **Ollama LLM** acts as an advisory gate before each new position — it can flag concerns but never blocks trading if it's unreachable.
- Runs every **15 minutes** during market hours, with a daily summary at market close.

### 🏛️ Politician Copy Trade

Mirrors publicly disclosed stock trades from elected officials, leveraging mandatory STOCK Act disclosures scraped from [Capitol Trades](https://www.capitoltrades.com).

- Scans for new disclosures every **30 minutes** during market hours.
- Applies configurable risk gates: disclosure age, max concurrent positions, total allocation cap, and a 15% cash buffer.
- Position sizing uses a **Kelly criterion** fraction.
- Position monitoring runs every **5 minutes**: hard stop loss, trailing stop, and a profit ladder that sells 1/3 of the position at each configured target.

---

## Tech Stack

| Component | Details |
|-----------|---------|
| Language | Python 3.12 |
| Container | Docker (python:3.12-slim) |
| Scheduling | Cron (runs inside the container) |
| Brokerage | [Alpaca](https://alpaca.markets) (paper or live) |
| AI Advisor | [Ollama](https://ollama.com) (local, optional) |
| Web UI | Flask on port 6060 |
| Data source | alpaca-py, yfinance, Capitol Trades (scraped) |

---

## Project Structure

```
TorpTradingBot/
├── core/
│   ├── alpaca_client.py    # Alpaca API singleton wrappers (orders, prices, account)
│   ├── logger.py           # Append-only daily CSV trade logger (logs/)
│   └── market_hours.py     # Market open check; early-exit helper for cron scripts
├── strategies/
│   ├── wheel.py            # Wheel strategy (CSP → CC cycle)
│   ├── copy_trading.py     # Politician copy trade (scan + monitor modes)
│   ├── market_data.py      # yfinance market context for LLM prompts
│   └── ollama_advisor.py   # LLM advisory gates (4 decision points)
├── web/
│   ├── app.py              # Flask API + static file server
│   └── templates/
│       └── index.html      # Single-page dashboard (dark theme)
├── state/                  # Persistent JSON state (survives restarts)
│   ├── wheel.json
│   └── copy_trading.json
├── logs/                   # Daily trade CSVs + cron.log
├── config.py               # All config from environment variables
├── crontab                 # Cron schedule (installed at build time)
├── entrypoint.sh           # Starts cron + Flask, keeps container alive
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Getting Started

### Prerequisites

- Docker and Docker Compose
- [Alpaca](https://alpaca.markets) account and API keys (paper trading works fine)
- [Ollama](https://ollama.com) running locally (optional — bot continues without it)

### Installation

```bash
git clone https://github.com/SoderTorp/TorpTradingBot.git
cd TorpTradingBot
```

*Optional: Create your `.env` file:

```bash
cp .env.example .env
# Edit .env with your keys and preferences (or use the web UI after first boot)
```

These settings are also available in the Web UI Configuration tab.

Start the bot:

```bash
docker compose up -d
```

🧑🏽‍💻 The web UI will be available at `http://localhost:6060`.

---

## Configuration

All configuration is via environment variables (`.env` file). Every setting can also be changed live through the **CONFIGURATION** tab in the web UI — changes are written back to `.env` and take effect immediately without restarting the container.

### Alpaca

| Variable | Default | Description |
|----------|---------|-------------|
| `KEY` | — | Alpaca API key |
| `SECRET` | — | Alpaca API secret |
| `PAPER_TRADING` | `true` | `true` for paper account, `false` for live |

### Budget

| Variable | Default | Description |
|----------|---------|-------------|
| `TOTAL_BUDGET` | `50000` | Total account size ($) |
| `TRADING_PCT` | `0.10` | Fraction of budget available for active trading |
| `RESERVE_PCT` | `0.10` | Fraction of budget never touched (minimum cash buffer) |
| `WHEEL_ALLOC` | `0.50` | Fraction of trading pool allocated to the Wheel strategy |
| `COPY_ALLOC` | `0.50` | Fraction of trading pool allocated to Copy Trading |

### Wheel Strategy

| Variable | Default | Description |
|----------|---------|-------------|
| `WHEEL_STOCK` | `AAPL` | Ticker symbol for the Wheel strategy |

### Copy Trade Risk Controls

| Variable | Default | Description |
|----------|---------|-------------|
| `COPY_TRADE_MAX_POSITION_PCT` | `0.02` | Max % of total budget per copy trade position |
| `COPY_TRADE_MAX_ALLOCATION_PCT` | `0.15` | Max total % of budget across all copy trades |
| `COPY_TRADE_MAX_CONCURRENT` | `3` | Max number of open copy trade positions |
| `COPY_TRADE_DISCLOSURE_AGE_MAX` | `10` | Max days since disclosure to still mirror a trade |
| `COPY_TRADE_STOP_LOSS_PCT` | `0.08` | Hard stop loss threshold (8% loss closes position) |
| `COPY_TRADE_TRAILING_TRIGGER_PCT` | `0.02` | Gain % that activates the trailing stop |
| `COPY_TRADE_TRAILING_DISTANCE_PCT` | `0.04` | Trailing stop distance from peak price |
| `COPY_TRADE_KELLY_FRACTION` | `0.25` | Kelly criterion fraction for position sizing |
| `COPY_TRADE_LADDER_TARGETS` | `[4, 8, 12]` | Profit % targets; sells 1/3 of position at each level |

### Ollama AI Advisor

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3.2` | Model name to use |
| `OLLAMA_TIMEOUT` | `60` | Request timeout in seconds |

---

## Cron Schedule

All times are UTC. US market hours are roughly 13:30–20:00 UTC.

| Schedule | Command | Description |
|----------|---------|-------------|
| `*/15 13-20 * * 1-5` | `wheel.py` | Wheel strategy — check and act every 15 min |
| `*/30 13-20 * * 1-5` | `copy_trading.py` | Scan Capitol Trades for new disclosures |
| `*/5 13-20 * * 1-5` | `copy_trading.py --monitor` | Monitor open copy trade positions |
| `30 20 * * 1-5` | `wheel.py --summary` | Daily summary at ~4:30 PM ET |

---

## Web UI

The dashboard at `http://localhost:6060` has four tabs:

- **Dashboard** — Account summary (equity, cash, P&L), live strategy state, and today's trade log.
- **State** — Raw JSON state for both strategies.
- **Log** — Last 150 lines of `cron.log`.
- **Settings** — Live config editor. Grouped fields for Alpaca, Budget, Wheel, Copy Trade, and Ollama. Includes a connection test button.

---

## Disclaimer

**This project is for educational and informational purposes only. It does not constitute financial advice, investment advice, trading advice, or any other type of advice. Nothing in this repository should be interpreted as a recommendation to buy, sell, or hold any financial instrument.**

Trading options and equities involves significant risk of loss and is not suitable for all investors. Past performance — including that of any politicians, strategies, or AI models referenced — is not indicative of future results. You are solely responsible for your own trading decisions and any financial outcomes that result from using this software.

The authors and contributors of this project accept no liability for any financial losses, damages, or other consequences arising from the use or misuse of this software. **Use at your own risk.**

Always consult a licensed financial advisor before making investment decisions.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
