"""
Non-blocking Ollama client for local LLM inference.

All public methods return a dict with at least:
  {"source": "llm" | "fallback", "text": str}

The bot never blocks on AI calls — if Ollama is unreachable or slow, the
fallback response is returned immediately and the bot continues normally.
"""

from __future__ import annotations

import json
import logging

import requests

log = logging.getLogger(__name__)

_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_MODEL = "llama3"
_TIMEOUT = 30  # seconds
_TEMPERATURE = 0.1  # low temperature for consistent, factual output


class OllamaClient:
    def __init__(self, config: dict):
        ollama_cfg = config.get("ollama", {})
        self.host = ollama_cfg.get("host", _DEFAULT_HOST).rstrip("/")
        self.model = ollama_cfg.get("model", _DEFAULT_MODEL)
        self.url = f"{self.host}/api/generate"

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def analyze_market_sentiment(self, market_title: str, current_price: float) -> dict:
        """
        Given a market title and current price, return an opinion on whether
        the price seems mispriced relative to the title's content.
        """
        prompt = (
            f"You are a prediction market analyst. A market asks: '{market_title}'\n"
            f"The current YES price is {current_price:.2f} (0=impossible, 1=certain).\n"
            "In 1-2 sentences: does this price seem fair, overpriced, or underpriced? "
            "Reply concisely. No disclaimers."
        )
        return self._generate(prompt)

    def summarize_wallet_score(self, wallet: str, score_breakdown: dict) -> dict:
        """Return a human-readable summary of why a wallet scored well or poorly."""
        lines = "\n".join(
            f"  {k}: {v:.2f}" for k, v in score_breakdown.items()
        )
        prompt = (
            f"A Polymarket wallet {wallet[:10]}… has the following score breakdown:\n"
            f"{lines}\n"
            "In 1-2 sentences, explain why this wallet is or isn't worth copying. "
            "Be specific about the strongest and weakest dimensions."
        )
        return self._generate(prompt)

    def generate_trade_rationale(self, trade: dict) -> dict:
        """Generate a human-readable reason for a copied trade to include in the log."""
        prompt = (
            f"A copy-trade bot is about to {'BUY' if trade.get('side','BUY')=='BUY' else 'SELL'} "
            f"{trade.get('size_usdc', 0):.2f} USDC on market {trade.get('market_id', 'unknown')} "
            f"outcome '{trade.get('outcome', 'YES')}' at price {trade.get('price', 0):.4f}, "
            f"copying wallet {str(trade.get('wallet', ''))[:10]}…\n"
            "Write one sentence explaining the trade rationale for an audit log. "
            "Be concise and factual."
        )
        return self._generate(prompt)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _generate(self, prompt: str) -> dict:
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": _TEMPERATURE},
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "").strip()
            return {"source": "llm", "text": text}
        except requests.ConnectionError:
            log.debug("Ollama not reachable at %s", self.host)
        except requests.Timeout:
            log.debug("Ollama timed out after %ds", _TIMEOUT)
        except (requests.RequestException, json.JSONDecodeError, KeyError) as exc:
            log.debug("Ollama error: %s", exc)
        return {"source": "fallback", "text": "Ollama unavailable"}
