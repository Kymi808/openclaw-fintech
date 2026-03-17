"""
Exchange API client for Binance and Coinbase.
Handles price fetching, order execution, and account queries.
Includes retry logic, circuit breakers, rate limiting, and metrics.
"""
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Optional
import httpx

from skills.shared import (
    get_logger, require_env, audit_log, mask_sensitive,
    retry, RetryExhausted,
    binance_circuit, coinbase_circuit,
    exchange_limiter, api_limiter,
    metrics, timed,
)

logger = get_logger("exchange_client")


@dataclass
class Ticker:
    pair: str
    price: float
    volume_24h: float
    change_24h_pct: float
    exchange: str
    timestamp: float


@dataclass
class OrderResult:
    order_id: str
    pair: str
    side: str  # BUY or SELL
    amount: float
    price: float
    total: float
    fee: float
    status: str  # FILLED, PARTIAL, REJECTED
    exchange: str


class BinanceClient:
    """Binance spot trading client with resilience patterns."""

    BASE_URL = "https://api.binance.com"

    def __init__(self):
        self.api_key = require_env("BINANCE_API_KEY")
        self.api_secret = require_env("BINANCE_API_SECRET")
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"X-MBX-APIKEY": self.api_key},
            timeout=10.0,
        )

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    @binance_circuit
    @retry(max_attempts=3, base_delay=1.0, retryable_exceptions=(httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout))
    @timed("api_latency_seconds", "trades_total")
    async def get_ticker(self, pair: str) -> Ticker:
        """Get current price for a trading pair (e.g., BTCUSDT)."""
        await api_limiter.acquire()
        symbol = pair.replace("/", "")
        resp = await self._client.get(
            "/api/v3/ticker/24hr", params={"symbol": symbol}
        )
        resp.raise_for_status()
        data = resp.json()
        return Ticker(
            pair=pair,
            price=float(data["lastPrice"]),
            volume_24h=float(data["volume"]),
            change_24h_pct=float(data["priceChangePercent"]),
            exchange="binance",
            timestamp=time.time(),
        )

    async def get_all_tickers(self, pairs: list[str]) -> list[Ticker]:
        """Get tickers for multiple pairs."""
        results = []
        for pair in pairs:
            try:
                ticker = await self.get_ticker(pair)
                results.append(ticker)
            except RetryExhausted as e:
                logger.error(f"All retries exhausted for {pair} on Binance: {e.last_exception}")
                metrics.counter("errors_total").inc(label="binance_ticker")
            except Exception as e:
                logger.error(f"Failed to fetch {pair} from Binance: {e}")
                metrics.counter("errors_total").inc(label="binance_ticker")
        return results

    @binance_circuit
    @retry(max_attempts=2, base_delay=0.5, retryable_exceptions=(httpx.HTTPStatusError, httpx.ConnectError))
    async def get_balance(self, asset: str) -> float:
        """Get account balance for an asset."""
        await api_limiter.acquire()
        params = self._sign({})
        resp = await self._client.get("/api/v3/account", params=params)
        resp.raise_for_status()
        for bal in resp.json()["balances"]:
            if bal["asset"] == asset:
                return float(bal["free"])
        return 0.0

    @binance_circuit
    @retry(max_attempts=2, base_delay=0.5, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
    @timed("trade_latency_seconds", "trades_executed")
    async def place_order(
        self, pair: str, side: str, amount: float,
        order_type: str = "MARKET", price: Optional[float] = None,
    ) -> OrderResult:
        """Place a spot order with rate limiting."""
        await exchange_limiter.acquire()

        symbol = pair.replace("/", "")
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type,
            "quantity": f"{amount:.8f}",
        }
        if order_type == "LIMIT" and price:
            params["price"] = f"{price:.2f}"
            params["timeInForce"] = "GTC"

        params = self._sign(params)
        resp = await self._client.post("/api/v3/order", params=params)
        resp.raise_for_status()
        data = resp.json()

        fill_price = float(data.get("fills", [{}])[0].get("price", 0))
        fill_qty = float(data.get("executedQty", 0))
        fee = sum(float(f.get("commission", 0)) for f in data.get("fills", []))

        result = OrderResult(
            order_id=str(data["orderId"]),
            pair=pair,
            side=side.upper(),
            amount=fill_qty,
            price=fill_price,
            total=fill_price * fill_qty,
            fee=fee,
            status=data["status"],
            exchange="binance",
        )

        audit_log("trading-agent", "order_placed", {
            "exchange": "binance",
            "order_id": result.order_id,
            "pair": pair,
            "side": side,
            "amount": amount,
            "price": fill_price,
            "status": result.status,
        })

        metrics.gauge("daily_volume_usd").set(
            metrics.gauge("daily_volume_usd").get() + result.total
        )

        return result

    async def close(self):
        await self._client.aclose()


class CoinbaseClient:
    """Coinbase Advanced Trade API client with resilience patterns."""

    BASE_URL = "https://api.coinbase.com"

    def __init__(self):
        self.api_key = require_env("COINBASE_API_KEY")
        self.api_secret = require_env("COINBASE_API_SECRET")
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=10.0,
        )

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            self.api_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "CB-ACCESS-KEY": self.api_key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    @coinbase_circuit
    @retry(max_attempts=3, base_delay=1.0, retryable_exceptions=(httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout))
    @timed("api_latency_seconds")
    async def get_ticker(self, pair: str) -> Ticker:
        """Get current price for a product (e.g., BTC-USD)."""
        await api_limiter.acquire()
        product_id = pair.replace("/", "-")
        path = f"/api/v3/brokerage/products/{product_id}"
        headers = self._auth_headers("GET", path)
        resp = await self._client.get(path, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return Ticker(
            pair=pair,
            price=float(data["price"]),
            volume_24h=float(data.get("volume_24h", 0)),
            change_24h_pct=float(data.get("price_percentage_change_24h", 0)),
            exchange="coinbase",
            timestamp=time.time(),
        )

    async def get_all_tickers(self, pairs: list[str]) -> list[Ticker]:
        results = []
        for pair in pairs:
            try:
                ticker = await self.get_ticker(pair)
                results.append(ticker)
            except RetryExhausted as e:
                logger.error(f"All retries exhausted for {pair} on Coinbase: {e.last_exception}")
                metrics.counter("errors_total").inc(label="coinbase_ticker")
            except Exception as e:
                logger.error(f"Failed to fetch {pair} from Coinbase: {e}")
                metrics.counter("errors_total").inc(label="coinbase_ticker")
        return results

    @coinbase_circuit
    @retry(max_attempts=2, base_delay=0.5, retryable_exceptions=(httpx.HTTPStatusError, httpx.ConnectError))
    async def get_balance(self, currency: str) -> float:
        """Get account balance for a currency."""
        await api_limiter.acquire()
        path = "/api/v3/brokerage/accounts"
        headers = self._auth_headers("GET", path)
        resp = await self._client.get(path, headers=headers)
        resp.raise_for_status()
        for acct in resp.json().get("accounts", []):
            if acct["currency"] == currency:
                return float(acct["available_balance"]["value"])
        return 0.0

    @coinbase_circuit
    @retry(max_attempts=2, base_delay=0.5, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
    @timed("trade_latency_seconds", "trades_executed")
    async def place_order(
        self, pair: str, side: str, amount: float,
        order_type: str = "MARKET",
    ) -> OrderResult:
        """Place an order via Coinbase Advanced Trade."""
        await exchange_limiter.acquire()
        import uuid
        import json

        product_id = pair.replace("/", "-")
        path = "/api/v3/brokerage/orders"
        body_dict = {
            "client_order_id": str(uuid.uuid4()),
            "product_id": product_id,
            "side": side.upper(),
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": f"{amount:.2f}",
                }
            },
        }
        body = json.dumps(body_dict)
        headers = self._auth_headers("POST", path, body)
        resp = await self._client.post(path, headers=headers, content=body)
        resp.raise_for_status()
        data = resp.json()

        result = OrderResult(
            order_id=data.get("order_id", "unknown"),
            pair=pair,
            side=side.upper(),
            amount=amount,
            price=0.0,  # Market order — fill price returned async
            total=amount,
            fee=0.0,
            status=data.get("status", "PENDING"),
            exchange="coinbase",
        )

        audit_log("trading-agent", "order_placed", {
            "exchange": "coinbase",
            "order_id": result.order_id,
            "pair": pair,
            "side": side,
            "amount": amount,
            "status": result.status,
        })

        return result

    async def close(self):
        await self._client.aclose()


def get_exchange_client(exchange: str) -> BinanceClient | CoinbaseClient:
    """Factory to get the right exchange client."""
    if exchange == "binance":
        return BinanceClient()
    elif exchange == "coinbase":
        return CoinbaseClient()
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")
