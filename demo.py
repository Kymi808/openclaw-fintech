"""
Quick demo script — runs the trading and portfolio skills directly.
No gateway needed. Just: python demo.py
"""
import asyncio
import os
from dotenv import load_dotenv

# Load .env from gateway folder
load_dotenv("gateway/.env")


async def demo_alpaca_prices():
    """Fetch live stock prices from Alpaca paper trading."""
    from skills.trading.exchange_client import AlpacaClient

    print("\n" + "=" * 60)
    print("  ALPACA PAPER TRADING — LIVE STOCK PRICES")
    print("=" * 60)

    client = AlpacaClient()
    pairs = ["AAPL/USD", "TSLA/USD", "MSFT/USD", "NVDA/USD", "SPY/USD"]

    for pair in pairs:
        try:
            ticker = await client.get_ticker(pair)
            arrow = "+" if ticker.change_24h_pct >= 0 else ""
            print(
                f"  {ticker.pair:<10} ${ticker.price:>10,.2f}  "
                f"{arrow}{ticker.change_24h_pct:.2f}%  "
                f"vol: {ticker.volume_24h:,.0f}"
            )
        except Exception as e:
            print(f"  {pair:<10} ERROR: {e}")

    await client.close()


async def demo_alpaca_account():
    """Show Alpaca paper trading account status."""
    from skills.trading.exchange_client import AlpacaClient

    print("\n" + "=" * 60)
    print("  ALPACA PAPER ACCOUNT")
    print("=" * 60)

    client = AlpacaClient()
    cash = await client.get_balance("USD")
    print(f"  Cash balance: ${cash:,.2f}")

    # Check if we hold any positions
    for symbol in ["AAPL", "TSLA", "MSFT", "NVDA", "SPY"]:
        qty = await client.get_balance(symbol)
        if qty > 0:
            print(f"  {symbol}: {qty} shares")

    await client.close()


async def demo_paper_trade():
    """Place a small paper trade on Alpaca."""
    from skills.trading.exchange_client import AlpacaClient

    print("\n" + "=" * 60)
    print("  PAPER TRADE DEMO")
    print("=" * 60)

    client = AlpacaClient()

    # Buy 1 share of AAPL
    print("  Placing order: BUY 1 share of AAPL (paper)...")
    try:
        result = await client.place_order("AAPL/USD", "BUY", 1.0)
        print(f"  Order ID:  {result.order_id}")
        print(f"  Status:    {result.status}")
        print(f"  Side:      {result.side}")
        print(f"  Amount:    {result.amount}")
        if result.price:
            print(f"  Price:     ${result.price:,.2f}")
        print(f"  Fee:       ${result.fee:.2f} (commission-free)")
    except Exception as e:
        print(f"  Trade failed: {e}")

    await client.close()


async def demo_ollama():
    """Test that Ollama is running and responsive."""
    import httpx

    print("\n" + "=" * 60)
    print("  OLLAMA STATUS")
    print("=" * 60)

    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.get("http://localhost:11434/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            if models:
                for m in models:
                    size_gb = m.get("size", 0) / 1e9
                    print(f"  Model: {m['name']:<25} ({size_gb:.1f} GB)")
            else:
                print("  Ollama running but no models pulled yet.")
                print("  Run: ollama pull llama3.1:8b && ollama pull llava:7b")
    except Exception as e:
        print(f"  Ollama not reachable: {e}")
        print("  Make sure 'ollama serve' is running.")


async def main():
    print("\n  OpenClaw Fintech — Demo")
    print("  All trades are PAPER (no real money)\n")

    await demo_ollama()
    await demo_alpaca_prices()
    await demo_alpaca_account()

    # Ask before placing a trade
    answer = input("\n  Place a paper trade (BUY 1 AAPL)? [y/N] ")
    if answer.strip().lower() == "y":
        await demo_paper_trade()

    print("\n  Demo complete.\n")


if __name__ == "__main__":
    asyncio.run(main())
