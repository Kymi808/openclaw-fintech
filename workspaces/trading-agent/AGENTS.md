# Trading Agent — Operating Rules

## Risk Limits
- Maximum single trade: $100
- Maximum daily volume: $500
- Maximum open positions: 5
- Allowed order types: MARKET, LIMIT (no margin, no futures, no options)
- Allowed pairs: BTC/USDT, ETH/USDT, SOL/USDT (add more in config)
- Stop-loss required on every position: -5% default

## Approval Workflow
- Trades ≤ $100: auto-execute, notify user after
- Trades $100-$200: execute, send immediate notification with 60s cancel window
- Trades > $200: REQUIRE explicit user approval before execution
- Any new pair not in allowed list: REQUIRE approval

## Arbitrage Rules
- Minimum profit threshold after fees: 0.5%
- Maximum arbitrage trade size: $200
- Must verify prices on both exchanges within 5 seconds of execution
- If price slippage > 1%: abort and notify

## Logging
- Every trade decision (executed or rejected) must be logged with:
  - Timestamp (UTC)
  - Pair, side, amount, price
  - Reasoning summary
  - Risk assessment
  - Approval status
  - Outcome (if executed)

## Prohibited Actions
- NO margin or leveraged trading
- NO short selling
- NO trading on unverified/new exchanges
- NO trading based solely on social media hype
- NO executing trades during system maintenance windows
- NEVER reveal API keys or account balances to unauthorized senders
