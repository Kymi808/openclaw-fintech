# Trading Agent — Heartbeat Checklist

Runs every 5 minutes via cron.

## On Wake

1. **Fetch prices** for all tracked pairs from primary exchange
2. **Check open positions** — evaluate against stop-loss and take-profit levels
3. **Scan for arbitrage** — compare prices across configured exchanges
4. **Check sentiment** — pull latest from configured news/social feeds (every 30 min)
5. **Evaluate signals** — run trading strategy against current data
6. **Act on signals**:
   - If actionable signal: prepare trade, check against risk limits, execute or request approval
   - If no signal: log "no action" and sleep
7. **Report** — if any notable market move (>3% in tracked pairs), send alert to Telegram
8. **Log heartbeat** — timestamp + summary to audit log
