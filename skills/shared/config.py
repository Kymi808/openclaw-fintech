"""
Shared configuration and utilities for all fintech skills.
"""
import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

# Logging
LOG_DIR = Path(os.getenv("AUDIT_LOG_PATH", "./logs")).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"fintech.{name}")


def audit_log(agent: str, action: str, details: dict) -> None:
    """Append a structured audit entry to the JSONL audit log."""
    log_path = LOG_DIR / "audit.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "action": action,
        **details,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def require_env(key: str) -> str:
    """Get a required environment variable or raise."""
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Missing required environment variable: {key}")
    return val


def mask_sensitive(value: str, visible: int = 4) -> str:
    """Mask a sensitive string, showing only the last `visible` characters."""
    if len(value) <= visible:
        return "***"
    return "***" + value[-visible:]


# Trade limits (defaults — agents can override)
DEFAULT_LIMITS = {
    "max_single_trade": 100.0,
    "max_daily_volume": 500.0,
    "max_open_positions": 5,
    "approval_threshold": 200.0,
    "stop_loss_pct": 5.0,
}

# DeFi limits
DEFI_LIMITS = {
    "max_single_swap": 500.0,
    "max_daily_volume": 2000.0,
    "max_gas_usd": 20.0,
    "slippage_tolerance": 0.005,  # 0.5%
    "slippage_max": 0.01,  # 1%
}

# Allowed exchanges and protocols
ALLOWED_EXCHANGES = ["binance", "coinbase"]
ALLOWED_DEFI_PROTOCOLS = [
    "uniswap_v3", "sushiswap", "1inch",
    "aave_v3", "lido", "compound_v3", "curve",
]
ALLOWED_CHAINS = ["ethereum", "polygon", "arbitrum", "base"]

# Allowed trading pairs
ALLOWED_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
