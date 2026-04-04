"""
Secrets management abstraction.

Locally: reads from .env file (via python-dotenv)
Production: reads from environment variables (set by Docker, AWS Secrets Manager,
Kubernetes secrets, etc.)

This module provides a single get_secret() function that works in both contexts.
Never hardcode secrets. Never log secrets. Never commit secrets.
"""
import os
from typing import Optional
from .config import get_logger

logger = get_logger("secrets")

# Required secrets for the trading system
REQUIRED_SECRETS = [
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
]

# Optional secrets (system works without them, with reduced functionality)
OPTIONAL_SECRETS = [
    "ANTHROPIC_API_KEY",      # for LLM explanations
    "BINANCE_API_KEY",        # for crypto prices
    "BINANCE_API_SECRET",
    "COINBASE_API_KEY",       # for crypto prices
    "COINBASE_API_SECRET",
    "ALERT_WEBHOOK_URL",      # for Slack/Discord alerts
    "FMP_API_KEY",            # for Financial Modeling Prep fundamentals
]

# Placeholders that indicate a secret is not configured
PLACEHOLDER_VALUES = {"", "xxxxx", "sk-xxxxx", "test-key", "test-secret", "your-key-here"}


def get_secret(name: str, required: bool = False) -> Optional[str]:
    """
    Get a secret value from the environment.

    Args:
        name: environment variable name
        required: if True, raises ValueError when not found

    Returns:
        The secret value, or None if not set and not required
    """
    value = os.getenv(name, "")

    if value in PLACEHOLDER_VALUES:
        if required:
            raise ValueError(
                f"Required secret '{name}' is not configured. "
                f"Set it in gateway/.env or as an environment variable."
            )
        return None

    return value if value else None


def validate_secrets() -> dict:
    """
    Validate all required and optional secrets.

    Returns dict with status for each secret:
    {"ALPACA_API_KEY": "configured", "BINANCE_API_KEY": "missing", ...}
    """
    results = {}

    for name in REQUIRED_SECRETS:
        val = get_secret(name)
        if val:
            results[name] = "configured"
        else:
            results[name] = "MISSING (required)"
            logger.error(f"Required secret {name} is not configured")

    for name in OPTIONAL_SECRETS:
        val = get_secret(name)
        results[name] = "configured" if val else "not set (optional)"

    return results


def mask_secret(value: str, visible: int = 4) -> str:
    """Mask a secret for safe logging: 'sk-ant-abc123...' → 'sk-a...3...'"""
    if not value or len(value) <= visible * 2:
        return "***"
    return value[:visible] + "..." + value[-visible:]
