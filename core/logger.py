"""
Append-only daily CSV trade logger.
"""
import csv
import os
from datetime import datetime, timezone
import config


COLUMNS = ["timestamp", "strategy", "symbol", "action", "qty", "price", "rule_triggered", "order_id"]


def _log_path() -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(config.LOG_DIR, f"trades_{date_str}.csv")


def log_trade(
    strategy: str,
    symbol: str,
    action: str,
    qty: int | float,
    price: float,
    rule_triggered: str,
    order_id: str,
):
    path = _log_path()
    write_header = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "strategy": strategy,
            "symbol": symbol,
            "action": action,
            "qty": qty,
            "price": round(price, 4),
            "rule_triggered": rule_triggered,
            "order_id": order_id,
        })
