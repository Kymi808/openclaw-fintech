# DeFi Agent — Heartbeat Checklist

Runs every 15 minutes via cron.

## On Wake

1. **Check wallet balances** across all configured chains
2. **Check LP positions** — current value, fees earned, impermanent loss
3. **Check lending positions** — health factor, liquidation distance, APY
4. **Check staking positions** — rewards accrued, APY changes
5. **Check gas prices** — if pending transactions are queued, evaluate timing
6. **Check governance** — any new proposals on tracked protocols?
7. **Risk checks**:
   - Protocol TVL change > 20%? → ALERT
   - Impermanent loss > 5%? → ALERT
   - Lending health factor < 1.5? → ALERT
   - APY drop > 50%? → ALERT
8. **Log heartbeat** — positions snapshot to audit log
9. **Report** — only send to user if alerts triggered or on scheduled summary
