# Weekly Progress Log

## Week of March 24 - March 30, 2026

### What I Did

- **Switched LLM backend from Ollama to Anthropic Claude API**
  - Receipt OCR now uses Claude Sonnet 4 vision (was llava on Ollama)
  - Legal contract analysis and legal research now use Claude Sonnet 4 (was llama3.1:70b on Ollama)
  - Health checks now verify Anthropic API connectivity instead of Ollama
  - Gateway config updated to use Anthropic as primary provider
  - All 111 existing tests still pass after migration

- **Set up demo CLI** (`demo.py`)
  - Interactive terminal demo that exercises all 5 agents without needing Telegram
  - Suitable for screenshare walkthroughs

- **Project overview**
  - Multi-agent fintech bot with 5 specialist agents: Trading, Portfolio, DeFi, Finance, Legal
  - Pattern-based router dispatches messages to the right agent
  - Alpaca paper trading (no real money), Binance/Coinbase price feeds
  - Approval workflow for trades > $200, all rebalances
  - SEC EDGAR monitoring, GDPR scanning, contract analysis
  - Full resilience: circuit breakers, retry with backoff, rate limiting
  - 111 tests covering strategy, database, encryption, RBAC, approvals, resilience, metrics

### Blockers / Open Questions

- Need Telegram bot token to test full end-to-end flow
- Exchange API keys (Binance, Coinbase, Alpaca) needed for live price feeds
- SEC EDGAR User-Agent should be set to a real company email

### Next Steps

- [ ] Test live trading flow with Alpaca paper trading
- [ ] Wire up portfolio agent with real exchange balances
- [ ] Add more test coverage for agent handlers
- [ ] Set up monitoring dashboard (Grafana)

---

*Use this file to document progress each week. Add a new section header for each week.*
