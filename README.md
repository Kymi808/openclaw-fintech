# OpenClaw Fintech — Multi-Agent Bot Team

A production-grade multi-agent fintech system built on [OpenClaw](https://github.com/openclaw/openclaw). Six specialized AI agents work as a team to handle crypto trading, portfolio management, DeFi operations, expense tracking, and legal compliance — all orchestrated through a single gateway accessible via Telegram, WhatsApp, Slack, and 20+ messaging platforms.

## Architecture

```
                         OpenClaw Gateway
                     ws://localhost:18789
                              │
              ┌───────────────┼───────────────┐
              │               │               │
         WhatsApp         Telegram          Slack
         (retail)         (alerts)       (internal)
              │               │               │
              └───────────────┼───────────────┘
                              │
                    ┌─────────┴─────────┐
                    │   Router Agent     │
                    │  (triage & route)  │
                    └─────────┬─────────┘
                              │
        ┌──────────┬──────────┼──────────┬──────────┐
        │          │          │          │          │
   ┌────┴────┐┌───┴────┐┌────┴───┐┌────┴────┐┌───┴─────┐
   │ Trading ││Portfolio││  DeFi  ││ Finance ││  Legal  │
   │  Agent  ││ Agent   ││ Agent  ││  Agent  ││  Agent  │
   └─────────┘└────────┘└────────┘└─────────┘└─────────┘
```

## Agents

### Router Agent
Triages every incoming message and routes to the correct specialist. Never executes financial actions directly — pure routing logic with anti-abuse protection.

### Trading Agent
- Real-time price monitoring across Binance and Coinbase
- Momentum-based trade signal generation
- Cross-exchange arbitrage detection (spread scanning, profit calculation net of fees)
- Configurable risk limits: max trade size, daily volume caps, position limits
- Human approval workflow for trades above threshold
- Full decision logging for every trade (executed or rejected)

**Heartbeat:** Every 5 minutes — fetches prices, checks stop-losses, scans arbitrage, evaluates signals.

### Portfolio Agent
- Multi-exchange balance aggregation (Binance, Coinbase, on-chain wallets)
- Real-time allocation tracking vs configurable target percentages
- Drift detection with automatic rebalance proposals
- Daily/weekly/monthly performance reporting with risk metrics
- Always requires human approval before executing rebalance trades

**Heartbeat:** Daily at 6 AM — full portfolio snapshot, drift check, report generation.

### DeFi Agent
- Multi-chain wallet monitoring (Ethereum, Polygon, Arbitrum, Base)
- Token swap quotes via 1inch aggregator
- Uniswap V3 transaction building (ERC-20 approvals with exact amounts, never unlimited)
- Governance proposal tracking via Snapshot.org GraphQL API
- Voting power lookup across 6 protocols (Uniswap, Aave, Compound, Curve, Lido, SushiSwap)
- Gas price monitoring with configurable max gas limits
- Liquidity position health monitoring (impermanent loss, health factor alerts)

**Heartbeat:** Every 15 minutes — balance check, position risk monitoring, gas tracking.

### Finance Agent
- Receipt OCR via Ollama vision models (llava) — extracts merchant, amount, date, payment method, line items
- Confidence scoring on OCR results
- Auto-categorization with learning (remembers your merchant → category corrections)
- Bank transaction sync via Plaid API (token exchange, transaction fetching, receipt matching)
- Monthly budget tracking with 80%/90% threshold alerts
- Tax document organization and quarterly summaries
- Proactive tax deadline reminders

**Heartbeat:** Weekly Monday 7 AM (expense report), daily 9 AM (budget alerts).

### Legal & Compliance Agent
- Contract analysis using local LLM only (Ollama) — documents never leave your machine
- Extracts parties, terms, obligations, risk flags, key dates from contracts
- SEC EDGAR integration — monitors filings (10-K, 10-Q, 8-K, S-1, DEF 14A, 13F) for tracked entities
- GDPR compliance scanner — checks cookie consent, privacy policy, trackers, security headers, form encryption
- Legal research via CourtListener API with verified court citations
- LLM synthesis of research with real citations clearly separated from unverified ones
- Contract renewal tracking with 30-day and 7-day alerts

**Heartbeat:** SEC filings every 4 hours, contract renewals daily 8 AM, GDPR scans weekly Monday 2 AM.

## Infrastructure

### Database
SQLite with WAL mode for concurrent write safety. Replaces JSON file storage with queryable, transactional persistence. All tables include proper indexes for query performance.

### Encryption at Rest
Fernet symmetric encryption (AES-128-CBC with HMAC-SHA256) for sensitive fields:
- Payment methods in expense records
- Contract summaries (confidential documents)
- Trade metadata

Requires `DATA_ENCRYPTION_KEY` in environment. Supports key rotation.

### Resilience
- **Retry with exponential backoff** — configurable attempts, delay, jitter, and retryable exception types on all external API calls
- **Circuit breakers** — per-exchange (Binance, Coinbase, Alchemy) with configurable failure threshold, recovery timeout, and half-open testing
- **Rate limiting** — token bucket limiter (20 trades/min, 60 API calls/min, 10 SEC calls/min)
- **Timeout management** — async timeout wrapper for all external calls

### Access Control (RBAC)
Five roles with granular permissions across 20+ actions:

| Role | Can Trade | Can View | Can Approve | Audit Log | Manage |
|------|-----------|----------|-------------|-----------|--------|
| Admin | Yes | Yes | Yes | Yes | Yes |
| Trader | Yes | Yes | Yes | No | No |
| Viewer | No | Yes | No | No | No |
| Compliance | No | Yes | No | Yes | No |
| Operator | No | Yes | No | Yes | Agents |

Agent-level restrictions allow limiting users to specific agents (e.g., a trader can only access trading-agent and portfolio-agent).

### Session Mapping
Bridges messaging platform identities to RBAC users:
- Telegram user IDs, WhatsApp phone numbers, Slack user IDs → RBAC users
- Pairing code flow for new unknown senders
- Cross-platform isolation (same phone number on Telegram vs WhatsApp → different sessions)

### Dead Letter Queue
Persistent queue for failed financial operations:
- Tracks partial execution (arbitrage buy succeeded but sell failed)
- Retry counting with configurable max retries
- Auto-escalation for entries pending too long
- Resolve/abandon/escalate workflow for manual intervention

### Startup Reconciliation
On boot, automatically:
- Detects trades stuck in PENDING status (may have filled on exchange while down)
- Expires stale approval requests and routes to DLQ
- Checks DLQ health and auto-escalates old entries
- Enforces data retention policies

### Monitoring
- **Prometheus metrics** — 30+ fintech-specific counters, gauges, and histograms exposed on `:9090/metrics`
- **Grafana dashboards** — pre-provisioned datasource, ready for dashboard import
- **9 alerting rules** — circuit breaker trips, volume limits, portfolio drift, gas prices, GDPR issues, error rates, missed heartbeats
- **Health checks** — checks Ollama, Binance, Coinbase, database; reports latency and circuit breaker state
- **Audit logging** — every financial action logged with timestamp, agent, action, and details to SQLite

### Human Approval Workflow
Configurable thresholds for when actions require human approval:
- Trades over $200 → approval required
- All portfolio rebalances → approval required
- DeFi swaps over $100 → approval required
- Governance votes → always notify-only (human decides)

Approval requests are sent via the user's messaging channel with approve/deny commands.

## Project Structure

```
openclaw-fintech/
├── gateway/
│   ├── config.yaml          # Gateway config: channels, routing, cron, guardrails
│   └── .env.example         # API keys template
├── workspaces/
│   ├── router/              # Router agent identity and rules
│   ├── trading-agent/       # SOUL.md, AGENTS.md, HEARTBEAT.md, WORKING.md
│   ├── portfolio-agent/
│   ├── defi-agent/
│   ├── finance-agent/
│   └── legal-agent/
├── skills/
│   ├── shared/              # Core infrastructure
│   │   ├── approval.py      # Human-in-the-loop approval workflow
│   │   ├── config.py        # Shared configuration and utilities
│   │   ├── database.py      # SQLite database layer with encryption
│   │   ├── dead_letter.py   # Dead letter queue for failed operations
│   │   ├── encryption.py    # Fernet encryption at rest
│   │   ├── health.py        # Health checks and liveness probes
│   │   ├── metrics.py       # Prometheus metrics collection
│   │   ├── rbac.py          # Role-based access control
│   │   ├── resilience.py    # Retry, circuit breaker, rate limiter
│   │   ├── session_mapper.py # Platform identity → RBAC mapping
│   │   └── startup.py       # Startup state reconciliation
│   ├── trading/
│   │   ├── exchange_client.py # Binance + Coinbase API clients
│   │   ├── strategy.py       # Momentum signals, arbitrage detection
│   │   ├── handlers.py        # Skill handlers (heartbeat, execute, etc.)
│   │   └── skill.yaml         # OpenClaw skill definition
│   ├── portfolio/
│   │   ├── handlers.py        # Rebalancing engine, performance reports
│   │   └── skill.yaml
│   ├── defi/
│   │   ├── web3_client.py     # Multi-chain RPC client
│   │   ├── transaction_builder.py # EVM transaction construction
│   │   ├── governance.py      # Snapshot.org GraphQL integration
│   │   ├── handlers.py
│   │   └── skill.yaml
│   ├── finance/
│   │   ├── receipt_ocr.py     # Ollama vision receipt extraction
│   │   ├── plaid_client.py    # Plaid bank sync
│   │   ├── handlers.py
│   │   └── skill.yaml
│   └── legal/
│       ├── courtlistener.py   # CourtListener API for case law
│       ├── handlers.py        # Contract analysis, SEC, GDPR
│       └── skill.yaml
├── tests/                     # 111 tests
├── docker/
│   ├── docker-compose.yaml    # Gateway + Ollama + Prometheus + Grafana
│   ├── prometheus.yml         # Scrape config
│   ├── alerts.yml             # Alerting rules
│   └── grafana/               # Dashboard provisioning
├── scripts/
│   ├── setup.sh               # One-command deployment
│   └── add_sec_entity.sh      # Add SEC-tracked companies
├── survey.md                  # OpenClaw fintech survey report
├── requirements.txt
└── pyproject.toml
```

## Quick Start

### Prerequisites
- Docker and Docker Compose
- API keys for at least one exchange (Binance or Coinbase)
- A messaging platform bot token (Telegram recommended for testing)

### Setup

```bash
# 1. Clone
git clone https://github.com/Kymi808/openclaw-fintech.git
cd openclaw-fintech

# 2. Configure
cp gateway/.env.example gateway/.env
# Edit gateway/.env with your API keys

# 3. Deploy
./scripts/setup.sh
```

This starts:
- OpenClaw Gateway on `ws://localhost:18789`
- Web UI on `http://localhost:3000`
- Ollama (local LLM) on `http://localhost:11434`
- Prometheus on `http://localhost:9091`
- Grafana on `http://localhost:3001`
- Log viewer on `http://localhost:8080`

### First Message

Send a message to your bot on Telegram:

```
"What's the price of BTC?"
```

The router will triage this to the trading agent, which will fetch prices from Binance and Coinbase and respond with a formatted market update.

### Add SEC Monitoring

```bash
# Track Apple (CIK: 320193)
./scripts/add_sec_entity.sh 320193 "Apple Inc."

# Track Tesla (CIK: 1318605)
./scripts/add_sec_entity.sh 1318605 "Tesla Inc."
```

## Configuration

### Environment Variables

See `gateway/.env.example` for all required and optional variables. Key ones:

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | LLM provider for agent reasoning |
| `TELEGRAM_BOT_TOKEN` | Yes* | At least one messaging channel required |
| `BINANCE_API_KEY` | For trading | Binance spot trading API key |
| `COINBASE_API_KEY` | For trading | Coinbase Advanced Trade API key |
| `ALCHEMY_API_KEY` | For DeFi | Ethereum/L2 RPC provider |
| `PLAID_CLIENT_ID` | For banking | Plaid bank sync |
| `DATA_ENCRYPTION_KEY` | Yes | Encryption key for sensitive data at rest |
| `SEC_EDGAR_USER_AGENT` | For SEC | Required by SEC EDGAR API |

### Risk Limits

Edit `skills/shared/config.py` or each agent's `AGENTS.md`:

```python
DEFAULT_LIMITS = {
    "max_single_trade": 100.0,    # Max $100 per trade
    "max_daily_volume": 500.0,    # Max $500/day total
    "max_open_positions": 5,      # Max 5 open positions
    "approval_threshold": 200.0,  # Require approval above $200
    "stop_loss_pct": 5.0,         # 5% stop-loss on all positions
}
```

### Portfolio Targets

Create `workspaces/portfolio-agent/config.json`:

```json
{
  "targets": {
    "BTC": 0.40,
    "ETH": 0.30,
    "SOL": 0.10,
    "USDT": 0.15,
    "OTHER": 0.05
  },
  "drift_threshold": 0.05
}
```

## Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Run all 111 tests
PYTHONPATH=. pytest tests/ -v

# Run specific test modules
PYTHONPATH=. pytest tests/test_strategy.py -v     # Trading strategy
PYTHONPATH=. pytest tests/test_encryption.py -v   # Encryption
PYTHONPATH=. pytest tests/test_resilience.py -v   # Retry/circuit breakers
PYTHONPATH=. pytest tests/test_database.py -v     # Database layer
PYTHONPATH=. pytest tests/test_rbac.py -v         # Access control
```

### Test Coverage

| Module | Tests | Covers |
|--------|-------|--------|
| Encryption | 8 | Roundtrip, key rotation, ephemeral keys, unicode, large content |
| Database | 11 | CRUD, encrypted fields, monthly aggregation, retention, concurrent writes |
| Approval | 8 | Create, approve, deny, auto-approve logic, timeout, formatting |
| RBAC | 10 | All 5 roles, agent-level restrictions, inactive users, permission enforcement |
| Resilience | 10 | Retry backoff, circuit breaker states, rate limiting, timeout |
| Strategy | 14 | Risk limits, arbitrage detection, momentum signals, formatting |
| Metrics | 8 | Counters, gauges, histograms, Prometheus export |
| GDPR | 2 | Missing consent detection, clean site validation |
| Receipt OCR | 9 | JSON parsing, amount/date normalization, confidence scoring, error handling |
| Session Mapper | 10 | Registration, pairing flow, cross-platform isolation, inactive users |
| Dead Letter | 8 | Enqueue, resolve, abandon, escalate, retry counting, filtering |
| Plaid | 7 | Category mapping, configuration check |

## Security Considerations

- **Local-first**: Contract analysis uses Ollama — confidential documents never leave your machine
- **Exact token approvals**: DeFi agent never approves unlimited ERC-20 allowances
- **Encrypted at rest**: Payment methods, contract summaries, and trade metadata are encrypted in the database
- **No secrets in logs**: `mask_sensitive()` applied to card numbers and account IDs
- **Circuit breakers**: Prevent cascading failures from taking down the whole system
- **RBAC enforcement**: Every action checked against role permissions before execution
- **Pairing codes**: Unknown messaging senders cannot interact without admin-approved pairing
- **Audit trail**: Every financial action logged with full context

### What This Does NOT Provide
- SOC 2 / PCI-DSS certification (requires external audit)
- Hardware wallet integration for DeFi signing (requires physical infrastructure)
- Penetration testing (requires external security firm)
- Legal compliance review (requires qualified counsel)
- Financial advice (the agents summarize and execute — they do not advise)

## License

MIT

## Disclaimer

This software is provided as-is under the MIT license. It is **not financial advice**. Autonomous trading bots can lose money. Always:
- Start with small amounts and manual approval for everything
- Test thoroughly in sandbox/testnet environments before using real funds
- Consult qualified legal and financial professionals
- Understand that you bear full responsibility for any financial losses
