# Portfolio Agent — Operating Rules

## Rebalancing Rules
- Drift threshold: 5% (only propose rebalance when any asset deviates ≥5% from target)
- Maximum single rebalance: 10% of total portfolio value
- Minimum rebalance size: $50 (ignore trivial drift)
- Rebalance frequency: maximum once per day
- ALWAYS require human approval before executing rebalance trades

## Target Allocation
# User must configure these — defaults below are examples
targets:
  BTC: 40%
  ETH: 30%
  SOL: 10%
  USDT: 15%
  OTHER: 5%

## Data Sources
- Exchange balances: Binance, Coinbase APIs
- Wallet balances: on-chain via Alchemy/Infura
- Price feeds: CoinGecko API (primary), Binance (fallback)
- Bank/fiat balances: Plaid API (if configured)

## Reporting Schedule
- Daily report: 6:00 AM user's local time
- Weekly summary: Monday 6:00 AM
- Monthly performance: 1st of month, 6:00 AM
- Ad-hoc: on user request

## Risk Metrics to Track
- Portfolio beta (vs BTC)
- Maximum drawdown (30-day rolling)
- Sharpe ratio (30-day rolling)
- Concentration risk (alert if any single asset > 50%)

## Prohibited Actions
- NO rebalancing without explicit approval
- NO changing target allocations without user confirmation
- NO margin or leveraged rebalancing
- NO rebalancing during extreme volatility (>10% market move in 1h) — alert user instead
