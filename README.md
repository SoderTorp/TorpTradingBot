# TorpTradingBot

A lightweight, AI-assisted copy trading bot for [Polymarket](https://polymarket.com) — a prediction market built on the Polygon blockchain.

The bot dynamically discovers and scores active trader wallets on-chain, then mirrors their trades in **dry-run mode by default**. A local Ollama LLM provides optional trade rationale and market sentiment commentary.

---

## How It Works

1. Strategy: **REGULAR COPY**

Instead of copying a static leaderboard (which rewards past performance, not current activity), the bot scores wallets dynamically across four dimensions over a rolling 30-day window:

| Dimension | Weight | Description |
|-----------|--------|-------------|
| Recency | 30% | Has this wallet traded in the last 7 days? |
| Win Rate | 35% | % of resolved positions that closed profitably |
| Entry Timing | 25% | Did they enter early (low price) on positions that moved in their favour? |
| Market Diversity | 10% | Number of distinct markets traded |

Wallets that score above the configured threshold are added to a watchlist. Every 15 minutes, the bot checks those wallets for new trades and logs (or places) matching orders.

2. Strategy: **SuspiciousActivityDetector**

Hunts for *new* wallets (< 30 days old) that are suspiciously profitable
on politically-sensitive prediction markets: oil, gold, defense, pharma,
trade policy, and financial policy markets.

The hypothesis: fresh accounts hitting high win rates on policy-impacted
markets shortly before announcements may have advance information.

| Dimension | Weight | Description |
|-----------|--------|-------------|
| New accounts | 35% | primary signal: brand-new account |
| Concentration | 30% | primary signal: all-in on political markets |
| Trade velocity | 20% | secondary: burst trading behaviour |
| Win rate & profitability | 10% | bonus: high win rate is suspicious but often unmeasurable for new accounts |
| Asset concentration | 5% | bonus: extreme profit rate |

---

## Tech Stack

| Component | Details |
|-----------|---------|
| Language | Python 3.12 |
| Container | Docker (python:3.12-slim) |
| Scheduling | Cron (runs inside the container) |
| Market | [Polymarket](https://polymarket.com) (Polygon, USDC) |
| AI | [Ollama](https://ollama.com) (local, optional) |
| Web UI | Flask on port 6060 |

---

## Project Structure

```
TorpTradingBot/
├── strategies/
│   ├── portfolio.py.         # VirtualPortfolio — dry-run simulated trading account
│   ├── suspicious_activity.py # Hunts for *new* wallets that are suspiciously profitable
│   └── polymarket_copy.py    # Wallet discovery, scoring, copy trade logic
├── ai/
│   └── ollama_client.py      # Local Ollama LLM integration
├── scheduler/
│   └── cron_jobs.sh          # Cron job definitions
├── core/
│   └── logger.py             # Append-only daily CSV trade logger
├── web/
│   ├── app.py                # Flask web UI
│   └── templates/
│       └── index.html        # Single-page dashboard
├── state/                    # Persistent JSON state (survives restarts)
│   ├── watchlist.json
│   └── open_positions.json
├── logs/                     # Daily trade CSVs + cron.log
├── config.yaml               # All bot configuration
├── main.py                   # Entry point (--task discover_wallets|copy_trades)
├── entrypoint.sh             # Starts cron + Flask, keeps container alive
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Getting Started

### Prerequisites

- Docker and Docker Compose
- [Ollama](https://ollama.com) running locally (optional — the bot works without it)
- A funded Polygon wallet + Polymarket API key (only needed for live trading)

### Run in dry-run mode (no credentials needed)

```bash
git clone https://github.com/SoderTorp/TorpTradingBot.git
cd TorpTradingBot
docker compose up -d
```

The web UI will be available at `http://localhost:6060`.

In dry-run mode the bot runs the full discovery and scoring pipeline, identifies trades it *would* place, and logs them with full detail — no orders are ever submitted.

### Enable live trading

1. Set `mode: live` in `config.yaml` (or via the Settings tab in the web UI)
2. Add your credentials to `.env`:

```env
POLYGON_PRIVATE_KEY=your_private_key
POLYMARKET_API_KEY=your_api_key
POLYMARKET_PROXY_WALLET=your_proxy_wallet_address
```

3. Restart the container: `docker compose restart`

---

## Configuration

All non-secret settings live in `config.yaml` and can be edited live through the **Settings** tab in the web UI. Secret credentials (wallet private key, API key) go in `.env` and are never written to `config.yaml`.

```yaml
mode: dry_run  # "dry_run" or "live"

ollama:
  host: http://localhost:11434
  model: llama3.2:1b

polymarket:
  min_score_threshold: 0.65
  max_wallets_tracked: 20
  watchlist_refresh_hours: 24
  min_bet_usdc: 20
  max_bet_usdc: 100
  min_market_volume: 10000
  max_days_to_resolution: 30
```

---

## Cron Schedule

| Schedule | Task | Description |
|----------|------|-------------|
| `0 6 * * *` | `discover_wallets` | Refresh wallet watchlist (daily at 06:00 UTC) |
| `*/15 * * * *` | `copy_trades` | Check tracked wallets for new trades (every 15 min) |

---

## Web UI

The dashboard at `http://localhost:6060` has four tabs:

- **Dashboard** — Scored watchlist + today's trade signals
- **State** — Raw JSON for watchlist and open positions
- **Log** — Last 150 lines of `cron.log`
- **Settings** — Live config editor with connection test button

---

## Disclaimer

This project is for educational and informational purposes only. It does not constitute financial advice. Trading involves significant risk of loss. Use at your own risk.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
