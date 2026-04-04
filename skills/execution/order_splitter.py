"""
VWAP order splitting for large positions.

Splits orders > $10k notional into child orders executed over time
to reduce market impact.
"""
import asyncio
from dataclasses import dataclass

from skills.shared import get_logger

logger = get_logger("execution.order_splitter")

# Split threshold
VWAP_THRESHOLD_USD = 10_000.0

# Number of child orders
N_SLICES = 5

# Delay between slices (seconds)
SLICE_DELAY = 120  # 2 minutes between slices = 10 min total


@dataclass
class OrderSlice:
    symbol: str
    side: str
    notional: float
    slice_index: int
    total_slices: int


def should_split(notional: float) -> bool:
    """Check if an order should be VWAP-split."""
    return abs(notional) >= VWAP_THRESHOLD_USD


def create_slices(symbol: str, side: str, notional: float) -> list[OrderSlice]:
    """
    Split a large order into equal-sized child orders.

    Returns list of OrderSlice objects to be executed with delays.
    """
    if not should_split(notional):
        return [OrderSlice(symbol, side, notional, 0, 1)]

    slice_size = notional / N_SLICES
    return [
        OrderSlice(
            symbol=symbol,
            side=side,
            notional=round(slice_size, 2),
            slice_index=i,
            total_slices=N_SLICES,
        )
        for i in range(N_SLICES)
    ]


async def execute_slices(
    slices: list[OrderSlice],
    execute_fn,
) -> list[dict]:
    """
    Execute order slices with delays between them.

    Args:
        slices: list of OrderSlice to execute
        execute_fn: async callable(symbol, side, notional) -> dict
    """
    results = []
    for s in slices:
        try:
            logger.info(
                f"VWAP slice {s.slice_index + 1}/{s.total_slices}: "
                f"{s.side} ${s.notional:,.2f} of {s.symbol}"
            )
            result = await execute_fn(s.symbol, s.side, s.notional)
            results.append(result)
        except Exception as e:
            logger.error(f"Slice {s.slice_index + 1} failed: {e}")
            results.append({"error": str(e), "slice": s.slice_index})

        # Delay between slices (skip delay after last slice)
        if s.slice_index < s.total_slices - 1:
            await asyncio.sleep(SLICE_DELAY)

    return results
