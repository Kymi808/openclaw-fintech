"""
Production order management: fill confirmation, partial fills, retry, idempotency.

Addresses:
1. Fire-and-forget orders → poll for fill confirmation
2. Partial fills → track filled vs remaining qty
3. Transient failures → retry with backoff (network, rate limits)
4. Crash recovery → idempotent order IDs prevent double-trading
5. Fill price validation → alert if fill deviates from expected price
"""
import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from skills.shared import get_logger, audit_log
from skills.pnl import get_pnl_tracker

logger = get_logger("execution.order_manager")

# Fill polling config
FILL_POLL_INTERVAL = 2.0     # seconds between polls
FILL_POLL_MAX_WAIT = 60.0    # max seconds to wait for fill
FILL_PRICE_DEVIATION = 0.02  # alert if fill > 2% from expected

# Retry config
MAX_RETRIES = 3
RETRY_BACKOFF = [1.0, 3.0, 10.0]  # seconds between retries


@dataclass
class OrderStatus:
    """Detailed order status after submission."""
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    status: str           # "new", "partially_filled", "filled", "canceled", "rejected"
    requested_notional: float
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    filled_notional: float = 0.0
    remaining_qty: float = 0.0
    fees: float = 0.0
    fill_deviation_pct: float = 0.0  # % deviation from expected price
    attempts: int = 1
    error: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def is_complete(self) -> bool:
        return self.status in ("filled", "canceled", "rejected")

    @property
    def is_success(self) -> bool:
        return self.status == "filled"

    def to_dict(self) -> dict:
        return {**self.__dict__, "is_complete": self.is_complete, "is_success": self.is_success}


def _get_alpaca_client() -> tuple[str, dict]:
    """Get Alpaca base URL and auth headers."""
    api_key = os.getenv("ALPACA_API_KEY", "")
    api_secret = os.getenv("ALPACA_API_SECRET", "")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    return base_url, headers


async def submit_order(
    symbol: str,
    side: str,
    notional: float,
    expected_price: float = 0.0,
    strategy: str = "daily",
    idempotency_key: str = None,
) -> OrderStatus:
    """
    Submit an order with full production safeguards.

    1. Generate idempotent client_order_id (prevents double-trading on retry)
    2. Submit to Alpaca
    3. Poll for fill confirmation
    4. Validate fill price
    5. Record in P&L tracker
    6. Retry on transient failures

    Args:
        symbol: stock symbol
        side: "buy" or "sell"
        notional: dollar amount
        expected_price: expected fill price (for deviation check)
        strategy: "daily" or "intraday" (for P&L attribution)
        idempotency_key: unique key to prevent duplicate orders on retry
    """
    client_order_id = idempotency_key or f"{strategy}-{symbol}-{side}-{uuid.uuid4().hex[:8]}"
    base_url, headers = _get_alpaca_client()

    order_status = OrderStatus(
        order_id="",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        status="new",
        requested_notional=notional,
    )

    for attempt in range(MAX_RETRIES):
        order_status.attempts = attempt + 1
        try:
            async with httpx.AsyncClient(
                base_url=base_url, headers=headers, timeout=15.0,
            ) as client:
                # Submit order
                body = {
                    "symbol": symbol,
                    "notional": str(abs(notional)),
                    "side": side.lower(),
                    "type": "market",
                    "time_in_force": "day",
                    "client_order_id": client_order_id,
                }

                resp = await client.post("/v2/orders", json=body)

                # Handle duplicate order (idempotency)
                if resp.status_code == 422:
                    error_body = resp.json()
                    if "already been taken" in str(error_body).lower():
                        logger.info(f"Duplicate order detected for {client_order_id}, fetching status")
                        return await _poll_order_by_client_id(client, client_order_id, order_status, expected_price)
                    order_status.status = "rejected"
                    order_status.error = str(error_body)
                    return order_status

                resp.raise_for_status()
                data = resp.json()
                order_status.order_id = data.get("id", "")

                # Poll for fill
                filled_status = await _poll_for_fill(client, order_status.order_id, order_status, expected_price)

                # Record in P&L tracker
                if filled_status.is_success:
                    tracker = get_pnl_tracker()
                    tracker.record_trade(
                        symbol=symbol,
                        side=side,
                        qty=filled_status.filled_qty,
                        price=filled_status.filled_avg_price,
                        fees=filled_status.fees,
                        strategy=strategy,
                        order_id=filled_status.order_id,
                    )

                return filled_status

        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503):
                # Retryable
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning(f"Order {symbol} attempt {attempt + 1} failed (HTTP {e.response.status_code}), retrying in {wait}s")
                await asyncio.sleep(wait)
                continue
            else:
                order_status.status = "rejected"
                order_status.error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
                return order_status

        except (httpx.ConnectError, httpx.ReadTimeout) as e:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            logger.warning(f"Order {symbol} attempt {attempt + 1} network error, retrying in {wait}s: {e}")
            await asyncio.sleep(wait)
            continue

        except Exception as e:
            order_status.status = "rejected"
            order_status.error = str(e)
            logger.error(f"Order {symbol} unexpected error: {e}")
            return order_status

    # All retries exhausted
    order_status.status = "rejected"
    order_status.error = f"All {MAX_RETRIES} attempts failed"
    audit_log("execution-agent", "order_failed", {
        "symbol": symbol, "side": side, "notional": notional,
        "attempts": MAX_RETRIES, "error": order_status.error,
    })
    return order_status


async def _poll_for_fill(
    client: httpx.AsyncClient,
    order_id: str,
    order_status: OrderStatus,
    expected_price: float,
) -> OrderStatus:
    """Poll Alpaca for order fill confirmation."""
    elapsed = 0.0
    while elapsed < FILL_POLL_MAX_WAIT:
        await asyncio.sleep(FILL_POLL_INTERVAL)
        elapsed += FILL_POLL_INTERVAL

        try:
            resp = await client.get(f"/v2/orders/{order_id}")
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status", "")
            order_status.status = status
            order_status.filled_qty = float(data.get("filled_qty", 0) or 0)
            order_status.filled_avg_price = float(data.get("filled_avg_price", 0) or 0)
            order_status.filled_notional = order_status.filled_qty * order_status.filled_avg_price

            if status == "filled":
                # Validate fill price
                if expected_price > 0 and order_status.filled_avg_price > 0:
                    deviation = abs(order_status.filled_avg_price - expected_price) / expected_price
                    order_status.fill_deviation_pct = round(deviation, 4)
                    if deviation > FILL_PRICE_DEVIATION:
                        logger.warning(
                            f"Fill price deviation: {order_status.symbol} filled at "
                            f"${order_status.filled_avg_price:.2f} vs expected "
                            f"${expected_price:.2f} ({deviation:.2%} deviation)"
                        )

                logger.info(
                    f"Order filled: {order_status.symbol} {order_status.side} "
                    f"qty={order_status.filled_qty:.4f} @ ${order_status.filled_avg_price:.2f}"
                )
                return order_status

            if status in ("canceled", "rejected", "expired"):
                order_status.error = f"Order {status}: {data.get('reject_reason', '')}"
                return order_status

            if status == "partially_filled":
                logger.info(
                    f"Partial fill: {order_status.symbol} {order_status.filled_qty:.4f} "
                    f"of requested ${order_status.requested_notional:.2f}"
                )

        except Exception as e:
            logger.warning(f"Poll error for {order_id}: {e}")

    # Timeout — order may still fill
    if order_status.filled_qty > 0:
        order_status.status = "partially_filled"
        order_status.remaining_qty = max(0, order_status.requested_notional / max(order_status.filled_avg_price, 1) - order_status.filled_qty)
    else:
        order_status.status = "timeout"
        order_status.error = f"Fill not confirmed within {FILL_POLL_MAX_WAIT}s"

    return order_status


async def _poll_order_by_client_id(
    client: httpx.AsyncClient,
    client_order_id: str,
    order_status: OrderStatus,
    expected_price: float,
) -> OrderStatus:
    """Look up an existing order by client_order_id (for idempotency)."""
    try:
        resp = await client.get(
            "/v2/orders",
            params={"status": "all", "limit": 10, "nested": "true"},
        )
        resp.raise_for_status()
        for order in resp.json():
            if order.get("client_order_id") == client_order_id:
                order_status.order_id = order.get("id", "")
                order_status.status = order.get("status", "")
                order_status.filled_qty = float(order.get("filled_qty", 0) or 0)
                order_status.filled_avg_price = float(order.get("filled_avg_price", 0) or 0)
                return order_status
    except Exception as e:
        logger.error(f"Failed to lookup order {client_order_id}: {e}")

    order_status.status = "unknown"
    return order_status


async def cancel_order(order_id: str) -> bool:
    """Cancel an open order."""
    base_url, headers = _get_alpaca_client()
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10.0) as client:
            resp = await client.delete(f"/v2/orders/{order_id}")
            return resp.status_code in (200, 204)
    except Exception as e:
        logger.error(f"Failed to cancel order {order_id}: {e}")
        return False
