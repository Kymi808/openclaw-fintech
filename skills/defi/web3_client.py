"""
Web3 client for interacting with EVM chains.
Handles wallet queries, token balances, and DeFi protocol interactions.
"""
from dataclasses import dataclass
from typing import Optional
import httpx

from skills.shared import (
    get_logger, require_env, audit_log,
    ALLOWED_CHAINS, ALLOWED_DEFI_PROTOCOLS, DEFI_LIMITS,
)

logger = get_logger("web3_client")

# Chain RPC endpoints (via Alchemy/Infura)
CHAIN_RPC = {
    "ethereum": "https://eth-mainnet.g.alchemy.com/v2/{api_key}",
    "polygon": "https://polygon-mainnet.g.alchemy.com/v2/{api_key}",
    "arbitrum": "https://arb-mainnet.g.alchemy.com/v2/{api_key}",
    "base": "https://base-mainnet.g.alchemy.com/v2/{api_key}",
}

# Common token addresses (Ethereum mainnet)
TOKEN_ADDRESSES = {
    "ethereum": {
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
        "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
        "stETH": "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84",
    },
}

# ERC-20 balanceOf function selector
BALANCE_OF_SELECTOR = "0x70a08231"

# Uniswap V3 Router
UNISWAP_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"


@dataclass
class TokenBalance:
    chain: str
    token: str
    address: str
    balance: float
    value_usd: Optional[float] = None


@dataclass
class SwapQuote:
    chain: str
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float
    price_impact_pct: float
    gas_estimate_usd: float
    route: str  # e.g., "Uniswap V3 ETH/USDC 0.05%"


@dataclass
class DefiPosition:
    protocol: str
    chain: str
    position_type: str  # "lp", "lending", "staking"
    tokens: list[str]
    value_usd: float
    apy_pct: float
    health_factor: Optional[float] = None  # for lending
    impermanent_loss_pct: Optional[float] = None  # for LP


@dataclass
class GasPrice:
    chain: str
    slow_gwei: float
    standard_gwei: float
    fast_gwei: float
    estimated_swap_cost_usd: float


class Web3Client:
    """EVM chain interaction client."""

    def __init__(self):
        self.api_key = require_env("ALCHEMY_API_KEY")
        self.wallet = require_env("WALLET_ADDRESS")
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _get_rpc_url(self, chain: str) -> str:
        if chain not in CHAIN_RPC:
            raise ValueError(f"Unsupported chain: {chain}")
        return CHAIN_RPC[chain].format(api_key=self.api_key)

    async def _rpc_call(self, chain: str, method: str, params: list) -> dict:
        """Make a JSON-RPC call to the chain."""
        if chain not in self._clients:
            self._clients[chain] = httpx.AsyncClient(timeout=15.0)

        url = self._get_rpc_url(chain)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        resp = await self._clients[chain].post(url, json=payload)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise Exception(f"RPC error: {result['error']}")
        return result.get("result")

    async def get_eth_balance(self, chain: str) -> float:
        """Get native token balance (ETH/MATIC/etc.)."""
        result = await self._rpc_call(chain, "eth_getBalance", [self.wallet, "latest"])
        # Convert from wei (hex) to ETH
        wei = int(result, 16)
        return wei / 1e18

    async def get_token_balance(self, chain: str, token_address: str) -> float:
        """Get ERC-20 token balance."""
        # Encode balanceOf(address) call
        padded_address = self.wallet[2:].lower().zfill(64)
        data = BALANCE_OF_SELECTOR + padded_address

        result = await self._rpc_call(chain, "eth_call", [
            {"to": token_address, "data": data},
            "latest",
        ])
        raw_balance = int(result, 16)
        # Most tokens use 18 decimals; USDC/USDT use 6
        if token_address in [
            TOKEN_ADDRESSES.get("ethereum", {}).get("USDC"),
            TOKEN_ADDRESSES.get("ethereum", {}).get("USDT"),
        ]:
            return raw_balance / 1e6
        return raw_balance / 1e18

    async def get_all_balances(self, chain: str) -> list[TokenBalance]:
        """Get all tracked token balances on a chain."""
        balances = []

        # Native token
        eth_balance = await self.get_eth_balance(chain)
        if eth_balance > 0:
            native_name = "ETH" if chain in ("ethereum", "arbitrum", "base") else "MATIC"
            balances.append(TokenBalance(
                chain=chain, token=native_name,
                address="native", balance=eth_balance,
            ))

        # ERC-20 tokens
        tokens = TOKEN_ADDRESSES.get(chain, {})
        for token_name, token_addr in tokens.items():
            try:
                balance = await self.get_token_balance(chain, token_addr)
                if balance > 0:
                    balances.append(TokenBalance(
                        chain=chain, token=token_name,
                        address=token_addr, balance=balance,
                    ))
            except Exception as e:
                logger.error(f"Failed to fetch {token_name} balance on {chain}: {e}")

        return balances

    async def get_gas_price(self, chain: str) -> GasPrice:
        """Fetch current gas prices."""
        result = await self._rpc_call(chain, "eth_gasPrice", [])
        gas_wei = int(result, 16)
        gas_gwei = gas_wei / 1e9

        # Estimate swap cost (~150k gas for a Uniswap swap)
        swap_gas = 150_000
        swap_cost_eth = (gas_wei * swap_gas) / 1e18
        # Rough ETH price for USD estimate (would fetch real price in production)
        eth_price = 3000  # placeholder

        return GasPrice(
            chain=chain,
            slow_gwei=gas_gwei * 0.8,
            standard_gwei=gas_gwei,
            fast_gwei=gas_gwei * 1.3,
            estimated_swap_cost_usd=swap_cost_eth * eth_price,
        )

    async def get_swap_quote(
        self, chain: str, token_in: str, token_out: str, amount_in: float,
    ) -> SwapQuote:
        """
        Get a swap quote from Uniswap V3 quoter.
        In production, this would call the Quoter contract or 1inch API.
        """
        # For now, use 1inch API for quotes (more reliable off-chain)
        tokens = TOKEN_ADDRESSES.get(chain, {})
        token_in_addr = tokens.get(token_in)
        token_out_addr = tokens.get(token_out)

        if not token_in_addr or not token_out_addr:
            raise ValueError(f"Unknown token: {token_in} or {token_out} on {chain}")

        # 1inch quote API
        if chain not in self._clients:
            self._clients[chain] = httpx.AsyncClient(timeout=15.0)

        chain_id = {"ethereum": 1, "polygon": 137, "arbitrum": 42161, "base": 8453}
        cid = chain_id.get(chain, 1)

        # Convert amount to wei
        decimals = 6 if token_in in ("USDC", "USDT") else 18
        amount_wei = int(amount_in * (10 ** decimals))

        resp = await self._clients[chain].get(
            f"https://api.1inch.dev/swap/v6.0/{cid}/quote",
            params={
                "src": token_in_addr,
                "dst": token_out_addr,
                "amount": str(amount_wei),
            },
            headers={"Accept": "application/json"},
        )

        if resp.status_code == 200:
            data = resp.json()
            out_decimals = 6 if token_out in ("USDC", "USDT") else 18
            amount_out = int(data["dstAmount"]) / (10 ** out_decimals)
            gas = await self.get_gas_price(chain)

            return SwapQuote(
                chain=chain,
                token_in=token_in,
                token_out=token_out,
                amount_in=amount_in,
                amount_out=amount_out,
                price_impact_pct=0.0,  # 1inch doesn't always return this
                gas_estimate_usd=gas.estimated_swap_cost_usd,
                route=f"1inch aggregator on {chain}",
            )

        # Fallback: estimate based on known prices
        gas = await self.get_gas_price(chain)
        return SwapQuote(
            chain=chain,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out=0.0,
            price_impact_pct=0.0,
            gas_estimate_usd=gas.estimated_swap_cost_usd,
            route="quote_unavailable — check manually",
        )

    async def close(self):
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
