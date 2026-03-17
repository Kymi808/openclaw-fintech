"""
OpenClaw skill handlers for the DeFi Agent.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

from skills.shared import (
    get_logger, audit_log, approval_engine,
    ALLOWED_CHAINS, ALLOWED_DEFI_PROTOCOLS, DEFI_LIMITS,
)
from .web3_client import Web3Client, TokenBalance

logger = get_logger("defi.handlers")

STATE_FILE = Path("./workspaces/defi-agent/state.json")


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": [], "swap_history": [], "governance_votes": []}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def get_wallet_balances(chains: list[str] = None) -> dict:
    """Fetch wallet balances across all configured chains."""
    chains = chains or ALLOWED_CHAINS
    client = Web3Client()
    all_balances: list[dict] = []
    total_value = 0.0

    for chain in chains:
        try:
            balances = await client.get_all_balances(chain)
            for b in balances:
                entry = {
                    "chain": b.chain,
                    "token": b.token,
                    "balance": round(b.balance, 6),
                    "value_usd": b.value_usd,
                }
                all_balances.append(entry)
                if b.value_usd:
                    total_value += b.value_usd
        except Exception as e:
            logger.error(f"Failed to fetch balances on {chain}: {e}")

    await client.close()

    audit_log("defi-agent", "balances_fetched", {
        "chains": chains,
        "token_count": len(all_balances),
    })

    return {
        "balances": all_balances,
        "total_value_usd": round(total_value, 2),
        "chains_queried": chains,
    }


async def propose_swap(
    chain: str,
    token_in: str,
    token_out: str,
    amount_in: float,
) -> dict:
    """Get a swap quote and create approval request if needed."""

    # Validate chain
    if chain not in ALLOWED_CHAINS:
        return {"error": f"Chain {chain} not allowed. Allowed: {ALLOWED_CHAINS}"}

    # Validate amount
    if amount_in > DEFI_LIMITS["max_single_swap"]:
        return {"error": f"Amount ${amount_in} exceeds max swap ${DEFI_LIMITS['max_single_swap']}"}

    client = Web3Client()

    try:
        # Check gas first
        gas = await client.get_gas_price(chain)
        if gas.estimated_swap_cost_usd > DEFI_LIMITS["max_gas_usd"]:
            await client.close()
            return {
                "status": "gas_too_high",
                "gas_usd": gas.estimated_swap_cost_usd,
                "max_gas": DEFI_LIMITS["max_gas_usd"],
                "message": (
                    f"⛽ Gas too high: ${gas.estimated_swap_cost_usd:.2f} "
                    f"(limit: ${DEFI_LIMITS['max_gas_usd']}). "
                    f"Will retry when gas drops."
                ),
            }

        # Get quote
        quote = await client.get_swap_quote(chain, token_in, token_out, amount_in)
        await client.close()

        # Format message
        msg = (
            f"🔄 Swap Proposal\n"
            f"Swap: {amount_in} {token_in} → ~{quote.amount_out:.4f} {token_out}\n"
            f"Route: {quote.route}\n"
            f"Price impact: {quote.price_impact_pct:.2f}%\n"
            f"Gas estimate: ~${quote.gas_estimate_usd:.2f}\n"
            f"Slippage tolerance: {DEFI_LIMITS['slippage_tolerance'] * 100}%\n"
        )

        # Auto-approve small swaps
        if amount_in <= 100:
            return {
                "status": "auto_approved",
                "quote": {
                    "token_in": token_in,
                    "token_out": token_out,
                    "amount_in": amount_in,
                    "amount_out": quote.amount_out,
                    "gas_usd": quote.gas_estimate_usd,
                },
                "message": msg + "\n✅ Auto-approved (under $100).",
            }

        # Request approval
        req_id = approval_engine.create_request(
            agent="defi-agent",
            action="swap",
            description=f"Swap {amount_in} {token_in} → {token_out} on {chain}",
            amount=amount_in,
            details={
                "chain": chain,
                "token_in": token_in,
                "token_out": token_out,
                "amount_out": quote.amount_out,
            },
        )

        return {
            "status": "awaiting_approval",
            "request_id": req_id,
            "quote": {
                "token_in": token_in,
                "token_out": token_out,
                "amount_in": amount_in,
                "amount_out": quote.amount_out,
                "gas_usd": quote.gas_estimate_usd,
            },
            "message": msg + f"\n⏳ Awaiting approval. Reply 'approve {req_id}'.",
        }

    except Exception as e:
        logger.error(f"Swap quote failed: {e}")
        await client.close()
        return {"error": f"Quote failed: {e}"}


async def execute_swap(request_id: str) -> dict:
    """Execute a previously approved swap."""
    # In production, this would:
    # 1. Build the swap transaction
    # 2. Sign it (via hardware wallet / MetaMask Snap)
    # 3. Submit to the network
    # 4. Wait for confirmation
    # 5. Log the result

    audit_log("defi-agent", "swap_executed", {"request_id": request_id})
    return {
        "status": "executed",
        "request_id": request_id,
        "message": "Swap submitted. Waiting for on-chain confirmation.",
    }


async def get_defi_positions() -> dict:
    """List all DeFi positions across protocols."""
    state = _load_state()

    # In production, this would query each protocol's contracts:
    # - Uniswap: NFT position manager for LP positions
    # - Aave: getUserAccountData for lending positions
    # - Lido: stETH balance for staking

    return {
        "positions": state.get("positions", []),
        "total_defi_value": sum(
            p.get("value_usd", 0) for p in state.get("positions", [])
        ),
    }


async def check_governance(protocols: list[str] = None) -> list[dict]:
    """Check for active governance proposals via Snapshot.org GraphQL API."""
    from .governance import check_all_governance
    import os

    protocols = protocols or ["uniswap", "aave", "compound"]
    voter_address = os.getenv("WALLET_ADDRESS")

    try:
        results = await check_all_governance(
            protocols=protocols,
            voter_address=voter_address,
        )
        return results
    except Exception as e:
        logger.error(f"Governance check failed: {e}")
        return []


async def get_gas_prices(chains: list[str] = None) -> list[dict]:
    """Fetch current gas prices across chains."""
    chains = chains or ALLOWED_CHAINS
    client = Web3Client()
    results = []

    for chain in chains:
        try:
            gas = await client.get_gas_price(chain)
            results.append({
                "chain": chain,
                "slow_gwei": round(gas.slow_gwei, 1),
                "standard_gwei": round(gas.standard_gwei, 1),
                "fast_gwei": round(gas.fast_gwei, 1),
                "swap_cost_usd": round(gas.estimated_swap_cost_usd, 2),
            })
        except Exception as e:
            logger.error(f"Gas price fetch failed for {chain}: {e}")

    await client.close()
    return results


async def heartbeat() -> str:
    """15-minute position check and risk monitoring."""
    logger.info("DeFi agent heartbeat starting")

    messages = []

    # 1. Check wallet balances
    balances = await get_wallet_balances()

    # 2. Check gas prices
    gas_prices = await get_gas_prices()
    for g in gas_prices:
        if g["swap_cost_usd"] > DEFI_LIMITS["max_gas_usd"]:
            messages.append(
                f"⛽ High gas on {g['chain']}: ${g['swap_cost_usd']:.2f} "
                f"({g['standard_gwei']:.0f} gwei)"
            )

    # 3. Check DeFi positions for risk signals
    state = _load_state()
    for pos in state.get("positions", []):
        # Health factor check (lending)
        hf = pos.get("health_factor")
        if hf and hf < 1.5:
            messages.append(
                f"⚠️ Low health factor on {pos['protocol']}: {hf:.2f} "
                f"(liquidation risk!)"
            )

        # Impermanent loss check (LP)
        il = pos.get("impermanent_loss_pct")
        if il and il > 5.0:
            messages.append(
                f"⚠️ High IL on {pos['protocol']} LP: {il:.1f}%"
            )

    # 4. Check governance
    proposals = await check_governance()
    for p in proposals:
        messages.append(
            f"🗳️ Active vote: {p['protocol']} — {p['title']} "
            f"(ends {p['voting_ends']})"
        )

    audit_log("defi-agent", "heartbeat", {
        "balances_count": len(balances.get("balances", [])),
        "alerts": len(messages),
    })

    if messages:
        return "\n\n".join(messages)
    return "Heartbeat: DeFi positions stable, gas normal."
