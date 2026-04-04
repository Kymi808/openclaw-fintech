#!/usr/bin/env python3
"""
Test the yfinance-compatible adapter.
Run: PYTHONPATH=. python skills/market_data/test_adapter.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from dotenv import load_dotenv
load_dotenv("gateway/.env")

# Minimal config object matching CS system's cfg.data interface
class FakeDataConfig:
    lookback_years = 1


def main():
    # Clear caches so we test fresh fetches
    for f in ["data/prices_5.csv", "data/volumes_5.csv", "data/cross_asset.csv", "data/sentiment.json"]:
        if os.path.exists(f):
            os.remove(f)

    from skills.market_data.adapter import (
        fetch_price_data, fetch_cross_asset_data, fetch_news_sentiment,
    )

    cfg = FakeDataConfig()

    print("=" * 60)
    print("1. PRICE DATA (5 stocks, 1 year)")
    print("=" * 60)
    tickers = ["AAPL", "TSLA", "MSFT", "NVDA", "SPY"]
    prices, volumes = fetch_price_data(tickers, cfg, end_date="2026-04-01")
    print(f"  prices shape:  {prices.shape}")
    print(f"  volumes shape: {volumes.shape}")
    print(f"  columns: {list(prices.columns)}")
    print(f"  date range: {prices.index[0]} to {prices.index[-1]}")
    print(f"  AAPL latest: ${prices['AAPL'].iloc[-1]:,.2f}")
    print(f"  dtypes: {prices.dtypes.unique()}")
    print()

    print("=" * 60)
    print("2. CROSS-ASSET DATA")
    print("=" * 60)
    ca_tickers = ["^VIX", "^TNX", "^IRX", "TLT", "HYG", "LQD", "UUP", "GLD", "USO", "^GSPC", "SPY", "IWM", "QQQ"]
    ca = fetch_cross_asset_data(ca_tickers, "2025-04-01", "2026-04-01")
    print(f"  shape: {ca.shape}")
    print(f"  columns: {list(ca.columns)}")
    print(f"  date range: {ca.index[0]} to {ca.index[-1]}")
    print()

    print("=" * 60)
    print("3. NEWS SENTIMENT")
    print("=" * 60)
    sentiment = fetch_news_sentiment(["AAPL", "TSLA", "NVDA"])
    for ticker, s in sentiment.items():
        print(f"  {ticker}: avg={s['avg_sentiment']:.3f}, articles={s['n_articles']}, "
              f"pos_ratio={s['positive_ratio']:.2f}, neg_ratio={s['negative_ratio']:.2f}")
    print()

    print("=" * 60)
    print("COMPATIBILITY CHECK")
    print("=" * 60)
    # Verify format matches what CS system expects
    assert isinstance(prices.index, type(volumes.index)), "Index types match"
    assert list(prices.columns) == list(volumes.columns), "Columns match between prices/volumes"
    assert prices.dtypes.apply(lambda x: x == 'float64').all(), "All prices are float64"
    assert "^VIX" in ca.columns, "Cross-asset has ^VIX column"
    assert "avg_sentiment" in sentiment.get("AAPL", {}), "Sentiment has expected keys"
    print("  All compatibility checks PASSED")
    print()


if __name__ == "__main__":
    main()
