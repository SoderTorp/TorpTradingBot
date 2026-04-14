"""
Append-only daily CSV trade logger.
"""

import csv
import os
from datetime import datetime, timezone

LOG_DIR = os.environ.get("LOG_DIR", "logs")

COLUMNS = [
    "timestamp",
    "strategy",
    "market_id",
    "outcome",
    "side",
    "size_usdc",
    "price",
    "dry_run",
    "reason",
    "order_id",
]


def _log_path() -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"trades_{date_str}.csv")


class TradeLogger:
    """Append-only daily CSV logger for trade signals."""

    def log(
        self,
        strategy: str,
        market_id: str,
        outcome: str,
        side: str,
        size_usdc: float,
        price: float,
        dry_run: bool,
        reason: str,
        order_id: str | None,
    ) -> None:
        path = _log_path()
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "strategy": strategy,
                    "market_id": market_id,
                    "outcome": outcome,
                    "side": side,
                    "size_usdc": round(size_usdc, 2),
                    "price": round(price, 6),
                    "dry_run": dry_run,
                    "reason": reason,
                    "order_id": order_id or "",
                }
            )
