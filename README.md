# 🤖 TorpTradingBot

A lightweight, AI-assisted trading bot running in a minimal Python Docker container.  
Combines a **Wheel Options Strategy** with **AI-powered insights** from a local Ollama LLM, and a **Politician Copy Trade** strategy that mirrors disclosed trades from historically successful elected officials.

---

## Strategies

### 🎡 Wheel Strategy
An options income strategy that systematically sells Cash-Secured Puts (CSPs) and Covered Calls (CCs) on selected tickers to generate consistent premium income.

- Sells Cash-Secured Puts on target tickers at chosen strike/expiry
- If assigned, transitions to selling Covered Calls against the acquired shares
- Repeats the cycle to continuously collect premium
- AI insights from a local **Ollama LLM** assist with ticker selection, strike evaluation, and market sentiment analysis

### 🏛️ Politician Copy Trade
Mirrors publicly disclosed stock trades from elected officials with strong historical track records, leveraging mandatory disclosure laws (e.g. STOCK Act in the US).

- Monitors official trade disclosures for target politicians
- Automatically mirrors qualifying trades based on configurable filters
- Focuses on entries; position sizing is configurable

---

## Tech Stack

| Component | Details |
|-----------|---------|
| Runtime | Python (minimal Docker container) |
| Server | OS with Docker engine |
| AI Model | Ollama (local, `http://localhost:11434`) |
| Trading Platform | Alpaca account and API keys |
| Scheduling | Cron jobs |
| License | MIT |

---

## Project Structure

```
tradebot/
├── strategies/
│   ├── wheel.py          # Wheel strategy logic
│   └── politician.py     # Politician copy trade logic
├── ai/
│   └── ollama_client.py  # Local LLM integration
├── scheduler/
│   └── cron_jobs.sh      # Cron job definitions
├── config.yaml           # Configuration file
├── main.py               # Entry point
├── Dockerfile
└── requirements.txt
```

---

## Getting Started

### Prerequisites
- Docker
- Ubuntu server
- [Ollama](https://ollama.com) running locally on port `11434`
- Alpaca brokerage API credentials

### Installation

```bash
git clone https://github.com/SoderTorp/TorpTradingBot.git
cd TorpTradingBot
```

Configure your settings: <---- Update
```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your API keys and preferences
```

Build and run the Docker container:
```bash
docker build -t TorpTradingBot .
docker run -d --network host TorpTradingBot <--- Update
```

### Scheduling

Cron jobs are defined in `scheduler/cron_jobs.sh`. Register them with: <---- Update

---

## Configuration

Key options in `config.yaml`: <---- Update

```yaml
ollama:
  host: http://localhost:11434
  model: llama3  # or any locally available model

wheel:
  tickers: ["AAPL", "TSLA", "MSFT"]
  min_premium: 0.50
  max_delta: 0.30

politician_copy:
  politicians: []  # List of officials to track
  min_trade_value: 15000
  delay_days: 1    # Days after disclosure to mirror
```

---

## 🧑🏽‍💻 User Interface

Web interface displaying dashboard at http://localhost:6060 

---

## ⚠️ Disclaimer

**This project is for educational and informational purposes only. It does not constitute financial advice, investment advice, trading advice, or any other type of advice. Nothing in this repository should be interpreted as a recommendation to buy, sell, or hold any financial instrument.**

Trading options and equities involves significant risk of loss and is not suitable for all investors. Past performance — including that of any politicians, strategies, or AI models referenced — is not indicative of future results. You are solely responsible for your own trading decisions and any financial outcomes that result from using this software.

The authors and contributors of this project accept no liability for any financial losses, damages, or other consequences arising from the use or misuse of this software. **Use at your own risk.**

Always consult a licensed financial advisor before making investment decisions.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
