import pytest

from skills.market_data import adapter


def test_refuses_synthetic_data_in_paper_mode(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "paper")

    with pytest.raises(RuntimeError, match="Refusing to generate synthetic market data"):
        adapter._refuse_synthetic_data("Alpaca price")


def test_allows_synthetic_data_without_trading_env(monkeypatch):
    monkeypatch.delenv("TRADING_ENV", raising=False)

    adapter._refuse_synthetic_data("Alpaca price")
