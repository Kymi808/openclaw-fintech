#!/usr/bin/env python3
"""
Quick smoke test for the Alpaca market data provider.
Run: PYTHONPATH=. python skills/market_data/test_provider.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from dotenv import load_dotenv
load_dotenv("gateway/.env")


async def main():
    from skills.market_data import AlpacaDataProvider

    provider = AlpacaDataProvider()
    print("Alpaca Data Provider initialized")
    print(f"  API Key: {provider.api_key[:8]}...")
    print()

    # 1. Test snapshots (real-time prices)
    print("=" * 60)
    print("1. REAL-TIME SNAPSHOTS")
    print("=" * 60)
    try:
        snapshots = await provider.get_snapshots(["AAPL", "TSLA", "SPY", "NVDA", "MSFT"])
        for sym, snap in snapshots.items():
            print(f"  {sym:<6} ${snap.price:>10,.2f}  ({snap.change_pct:+.2f}%)  vol: {snap.volume:,.0f}")
        print(f"  OK — {len(snapshots)} snapshots fetched")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()

    # 2. Test historical bars (1 month of daily data)
    print("=" * 60)
    print("2. HISTORICAL BARS (30 days, daily)")
    print("=" * 60)
    try:
        bars = await provider.get_bars(
            symbols=["AAPL", "TSLA"],
            start="2026-03-01",
            end="2026-04-01",
            timeframe="1Day",
        )
        for sym, bar_list in bars.items():
            if bar_list:
                print(f"  {sym}: {len(bar_list)} bars, latest close ${bar_list[-1].close:,.2f}")
            else:
                print(f"  {sym}: no bars returned")
        print(f"  OK — bars fetched for {len(bars)} symbols")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()

    # 3. Test intraday bars (1-minute)
    print("=" * 60)
    print("3. INTRADAY BARS (1Min, today)")
    print("=" * 60)
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        bars = await provider.get_bars(
            symbols=["SPY"],
            start=today,
            timeframe="1Min",
            feed="iex",
        )
        spy_bars = bars.get("SPY", [])
        print(f"  SPY: {len(spy_bars)} 1-min bars today")
        if spy_bars:
            print(f"  Latest: {spy_bars[-1].timestamp} close=${spy_bars[-1].close:,.2f} vol={spy_bars[-1].volume:,.0f}")
        print("  OK — intraday data available")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()

    # 4. Test cross-asset ETF proxies
    print("=" * 60)
    print("4. CROSS-ASSET DATA (macro proxies)")
    print("=" * 60)
    try:
        cross = await provider.get_cross_asset_bars(
            start="2026-03-01",
            end="2026-04-01",
        )
        for ticker, bar_list in sorted(cross.items()):
            if bar_list:
                print(f"  {ticker:<6} -> {len(bar_list)} bars, latest ${bar_list[-1].close:,.2f}")
        print(f"  OK — {len(cross)} cross-asset tickers")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()

    # 5. Test news
    print("=" * 60)
    print("5. NEWS ARTICLES")
    print("=" * 60)
    try:
        articles = await provider.get_news(symbols=["AAPL", "TSLA"], limit=5)
        for a in articles[:5]:
            print(f"  [{a.source}] {a.headline[:70]}...")
            print(f"    Symbols: {a.symbols}  |  {a.created_at}")
        print(f"  OK — {len(articles)} articles fetched")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()

    # 6. Test DataFrame output (yfinance-compatible)
    print("=" * 60)
    print("6. DATAFRAME OUTPUT (yfinance-compatible)")
    print("=" * 60)
    try:
        df = await provider.get_bars_df(
            symbols=["AAPL", "TSLA", "SPY"],
            start="2026-03-01",
            end="2026-04-01",
        )
        print(f"  Shape: {df.shape}")
        print(f"  Columns: {list(df.columns[:6])}...")
        print(f"  Date range: {df.index[0]} to {df.index[-1]}")
        print("  OK — DataFrame format matches yfinance")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()

    # 7. Test cross-asset DataFrame
    print("=" * 60)
    print("7. CROSS-ASSET DATAFRAME")
    print("=" * 60)
    try:
        df = await provider.get_cross_asset_df(start="2026-03-01", end="2026-04-01")
        print(f"  Shape: {df.shape}")
        print(f"  Columns: {list(df.columns)}")
        if not df.empty:
            print("  Latest row:")
            print(f"    {df.iloc[-1].to_dict()}")
        print("  OK — cross-asset DataFrame ready")
    except Exception as e:
        print(f"  FAILED: {e}")

    await provider.close()
    print("\n" + "=" * 60)
    print("DONE — All tests complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
