# Portfolio Agent — Heartbeat Checklist

Runs daily at 6:00 AM.

## On Wake

1. **Fetch all balances** — exchanges, wallets, bank accounts
2. **Fetch current prices** for all held assets
3. **Calculate portfolio value** and allocation percentages
4. **Compare to targets** — compute drift for each asset
5. **Check risk metrics** — drawdown, concentration, beta
6. **Decision**:
   - If any drift ≥ 5%: generate rebalance proposal, send to user for approval
   - If no drift: send daily summary report
   - If extreme volatility detected: send alert, skip rebalance
7. **Generate report** — format and send via configured channel
8. **Log heartbeat** — timestamp + portfolio snapshot to audit log
