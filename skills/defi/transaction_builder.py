"""
EVM transaction builder for DeFi operations.
Builds unsigned transactions for swaps, approvals, and LP operations.
Does NOT sign or broadcast — that requires a hardware wallet or MPC signer.
"""
import json
from dataclasses import dataclass
from typing import Optional
import httpx

from skills.shared import get_logger, audit_log, require_env, DEFI_LIMITS

logger = get_logger("tx_builder")

# Uniswap V3 SwapRouter ABI (relevant function signatures)
UNISWAP_ROUTER_V3 = "0xE592427A0AEce92De3Edee1F18E0157C05861564"

# ERC-20 approve function signature
ERC20_APPROVE_SIG = "0x095ea7b3"
# Uniswap exactInputSingle function signature
EXACT_INPUT_SINGLE_SIG = "0x414bf389"

# Chain IDs
CHAIN_IDS = {
    "ethereum": 1,
    "polygon": 137,
    "arbitrum": 42161,
    "base": 8453,
}

# Max uint256 for unlimited approval (which we do NOT use — security risk)
# We always approve exact amounts
MAX_APPROVAL_AMOUNT = None  # Intentionally None — force exact amounts


@dataclass
class UnsignedTransaction:
    """An unsigned EVM transaction ready for signing."""
    chain: str
    chain_id: int
    to: str
    data: str
    value: int  # in wei
    gas_limit: int
    max_fee_per_gas: int  # in wei
    max_priority_fee_per_gas: int  # in wei
    nonce: int
    description: str  # Human-readable description

    def to_dict(self) -> dict:
        return {
            "chain": self.chain,
            "chainId": hex(self.chain_id),
            "to": self.to,
            "data": self.data,
            "value": hex(self.value),
            "gas": hex(self.gas_limit),
            "maxFeePerGas": hex(self.max_fee_per_gas),
            "maxPriorityFeePerGas": hex(self.max_priority_fee_per_gas),
            "nonce": hex(self.nonce),
            "type": "0x2",  # EIP-1559
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class TransactionBuilder:
    """Builds unsigned EVM transactions for DeFi operations."""

    def __init__(self):
        self.api_key = require_env("ALCHEMY_API_KEY")
        self.wallet = require_env("WALLET_ADDRESS")

    async def _get_nonce(self, chain: str) -> int:
        """Get the next nonce for the wallet."""
        from .web3_client import Web3Client
        client = Web3Client()
        result = await client._rpc_call(
            chain, "eth_getTransactionCount", [self.wallet, "latest"]
        )
        await client.close()
        return int(result, 16)

    async def _get_gas_params(self, chain: str) -> tuple[int, int]:
        """Get current gas parameters (maxFeePerGas, maxPriorityFeePerGas)."""
        from .web3_client import Web3Client
        client = Web3Client()

        # Get base fee
        block = await client._rpc_call(chain, "eth_getBlockByNumber", ["latest", False])
        base_fee = int(block.get("baseFeePerGas", "0x0"), 16)

        # Get priority fee
        try:
            priority = await client._rpc_call(chain, "eth_maxPriorityFeePerGas", [])
            priority_fee = int(priority, 16)
        except Exception:
            priority_fee = 1_500_000_000  # 1.5 gwei default

        await client.close()

        # maxFeePerGas = 2 * baseFee + priorityFee (safe margin)
        max_fee = 2 * base_fee + priority_fee

        return max_fee, priority_fee

    def _encode_address(self, address: str) -> str:
        """Pad an address to 32 bytes."""
        return address[2:].lower().zfill(64)

    def _encode_uint256(self, value: int) -> str:
        """Encode a uint256 value to 32 bytes."""
        return hex(value)[2:].zfill(64)

    async def build_erc20_approval(
        self,
        chain: str,
        token_address: str,
        spender: str,
        amount: int,  # in token's smallest unit
    ) -> UnsignedTransaction:
        """
        Build an ERC-20 approval transaction.
        NEVER approves unlimited amounts — always exact.
        """
        # approve(address spender, uint256 amount)
        data = (
            ERC20_APPROVE_SIG
            + self._encode_address(spender)
            + self._encode_uint256(amount)
        )

        nonce = await self._get_nonce(chain)
        max_fee, priority_fee = await self._get_gas_params(chain)

        return UnsignedTransaction(
            chain=chain,
            chain_id=CHAIN_IDS.get(chain, 1),
            to=token_address,
            data=data,
            value=0,
            gas_limit=60_000,  # Approvals are ~46k gas
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=priority_fee,
            nonce=nonce,
            description=f"Approve {spender} to spend {amount} tokens on {token_address}",
        )

    async def build_uniswap_swap(
        self,
        chain: str,
        token_in: str,
        token_out: str,
        amount_in: int,  # in token's smallest unit
        amount_out_min: int,  # minimum acceptable output
        fee_tier: int = 3000,  # 0.3% pool
        deadline_seconds: int = 300,  # 5 minute deadline
    ) -> UnsignedTransaction:
        """
        Build a Uniswap V3 exactInputSingle swap transaction.
        """
        import time

        deadline = int(time.time()) + deadline_seconds

        # exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))
        # Encode the struct parameters
        params_data = (
            self._encode_address(token_in)
            + self._encode_address(token_out)
            + self._encode_uint256(fee_tier)
            + self._encode_address(self.wallet)  # recipient
            + self._encode_uint256(deadline)
            + self._encode_uint256(amount_in)
            + self._encode_uint256(amount_out_min)
            + self._encode_uint256(0)  # sqrtPriceLimitX96 = 0 (no limit)
        )

        data = EXACT_INPUT_SINGLE_SIG + params_data

        nonce = await self._get_nonce(chain)
        max_fee, priority_fee = await self._get_gas_params(chain)

        # Check gas cost against limits
        gas_limit = 200_000  # Typical Uniswap swap
        gas_cost_wei = gas_limit * max_fee
        gas_cost_eth = gas_cost_wei / 1e18

        # Rough USD estimate
        eth_price_usd = 3000  # Would fetch real price in production
        gas_cost_usd = gas_cost_eth * eth_price_usd

        if gas_cost_usd > DEFI_LIMITS["max_gas_usd"]:
            raise ValueError(
                f"Gas cost ${gas_cost_usd:.2f} exceeds limit "
                f"${DEFI_LIMITS['max_gas_usd']}. Try again when gas is lower."
            )

        tx = UnsignedTransaction(
            chain=chain,
            chain_id=CHAIN_IDS.get(chain, 1),
            to=UNISWAP_ROUTER_V3,
            data=data,
            value=0,  # Non-zero only for ETH→token swaps
            gas_limit=gas_limit,
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=priority_fee,
            nonce=nonce,
            description=(
                f"Swap {amount_in} of {token_in} → {token_out} "
                f"(min output: {amount_out_min}, fee: {fee_tier/10000}%)"
            ),
        )

        audit_log("defi-agent", "tx_built", {
            "type": "uniswap_swap",
            "chain": chain,
            "token_in": token_in,
            "token_out": token_out,
            "amount_in": amount_in,
            "gas_cost_usd": round(gas_cost_usd, 2),
        })

        return tx

    async def build_swap_with_approval(
        self,
        chain: str,
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        fee_tier: int = 3000,
    ) -> list[UnsignedTransaction]:
        """
        Build both approval + swap transactions.
        Returns a list of transactions to be signed and sent in order.
        """
        transactions = []

        # Step 1: Approve the router to spend our tokens
        approval_tx = await self.build_erc20_approval(
            chain=chain,
            token_address=token_in,
            spender=UNISWAP_ROUTER_V3,
            amount=amount_in,  # Exact amount, not unlimited
        )
        transactions.append(approval_tx)

        # Step 2: Execute the swap
        swap_tx = await self.build_uniswap_swap(
            chain=chain,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out_min=amount_out_min,
            fee_tier=fee_tier,
        )
        # Increment nonce for the second transaction
        swap_tx.nonce = approval_tx.nonce + 1
        transactions.append(swap_tx)

        return transactions


# Singleton
tx_builder = TransactionBuilder()
