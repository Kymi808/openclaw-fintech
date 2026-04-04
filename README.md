# OpenClaw Quant — Multi-Agent Autonomous Trading System

A production-grade quantitative trading system that combines ML-driven stock ranking with a multi-agent debate architecture for portfolio decision-making. The system autonomously generates trading signals, debates portfolio construction parameters through 16 specialized agents, and executes trades on Alpaca.

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     DATA LAYER                                   │
│  Alpaca Market Data (bars, snapshots, news)                     │
│  SEC EDGAR (filings, insider trading)                           │
│  FRED (macro economic releases)                                  │
│  Claude Haiku (LLM sentiment analysis)                          │
│  Financial Modeling Prep (fundamentals, ready for API key)      │
└──────────────────────┬──────────────────────────────────────────┘
                       │
┌──────────────────────▼──────���───────────────────────────────────┐
│                 ML SIGNAL GENERATION                              │
│  CS_Multi_Model_Trading_System (separate repo)                  │
│  ├── CrossMamba (primary, Sharpe 2.36, trains on Linux)         │
│  ├─��� LightGBM (fallback, trains in seconds)                    │
│  └── TST (Time Series Transformer, optional)                   │
│  186 features → 50 selected → stock rankings                    │
└──────────────────────┬──���───────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────────┐
│              NEWS GATHERING (3 agents, parallel)                  │
│  ├── Macro News Agent (Fed, economic data, geopolitical)        │
│  ├── Sector News Agent (industry trends, sector rotation)       │
│  └── Company News Agent (earnings, insider, M&A, analyst)       │
│  + SEC EDGAR filings + FRED macro releases                      │
│  + Claude Haiku LLM analysis (~$0.60/month)                     │
└────────────────────���─┬──────────────────────────────────────────┘
                       │
┌──────────────────────▼──���───────────────────────────────────────┐
│              MARKET INTELLIGENCE (Intel Agent)                    │
│  Aggregates: regime detection + market breadth + news sentiment │
│  Produces: MarketBriefing consumed by all analysts              │
└──────────────────��───┬──────────────────���───────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────────┐
│              MULTI-AGENT DEBATE (5 analysts, parallel)           │
│  ├── Momentum Analyst (trend-following, weights breadth)        │
│  ├── Value Analyst (mean-reversion, weights credit stress)      │
│  ├���─ Macro Analyst (top-down, weights VIX regime)               │
│  ├── Sentiment Analyst (news-driven, weights LLM sentiment)     │
│  └── Risk Analyst (defensive, weights drawdown proximity)       │
│  Each outputs: conviction score + recommended portfolio params  │
│  Scoring is DETERMINISTIC (no LLM in decision loop)             │
└──────────────────────┬──────────────────────────────────────────┘
                       │
┌──────────────────────▼─��─────────────────────────���──────────────┐
│              PORTFOLIO MANAGERS (3 PMs propose)                   │
│  ├── Aggressive PM (weights momentum + sentiment analysts)      │
│  ├── Conservative PM (weights risk + macro analysts)            │
│  └── Balanced PM (equal weighting)                              │
│  Each blends all 5 analyst theses → proposes n_long, n_short,  │
│  leverage, vol target, sector constraints                       │
└──────────────────��───┬──────────────────────────────────────────┘
                       │
┌──────────────────────▼───────��──────────────────────────��───────┐
│              CIO (Chief Investment Officer)                       │
│  Selects which PM's proposal to use:                            │
│  VIX crisis → conservative. Low vol → aggressive.               │
│  Safety override: VIX > 35 or drawdown > 10% → force conserv.  │
│  First daily run always requires human approval.                │
└──────────────────────┬──────────────��───────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────────┐
│              EXECUTION AGENT                                     │
│  Daily: ML rankings → portfolio construction → Alpaca orders    │
│  Intraday: VWAP/ORB/momentum/gap signals → EOD close           │
│  Safeguards: manipulation detection, risk limits, fat finger    │
│  VWAP splitting, fill polling, retry, idempotent orders         │
└──────────────────���───┬──────────────────────────────────────────┘
                       │
┌──���───────────────────▼─────────────────────────────────────���────┐
│              MONITORING & LEARNING                                │
│  P&L Tracker (SQLite): daily returns, Sharpe, drawdown          │
│  Position Reconciliation: system state vs Alpaca                │
│  Adaptive Feedback: weights evolve based on realized returns    │
│  Alerting: Slack/Discord webhooks for failures                  │
│  Weekly Research Report: Claude-generated institutional report  │
└──��──────────────────────────────────────────────────────────────┘
```

## Agent Inventory (16 Total)

| Agent | Type | What It Does |
|-------|------|-------------|
| **Intel Agent** | Data gathering | Aggregates regime (VIX, yields, credit), breadth (sector rotation, advance/decline), news sentiment into a MarketBriefing |
| **Momentum Analyst** | Thesis formation | Trend-following bias. Weights model dispersion (0.35) and breadth (0.30). CrossMamba-weighted predictions (0.60). Aggressive risk profile |
| **Value Analyst** | Thesis formation | Mean-reversion bias. Weights credit stress (0.20) and drawdown proximity (0.20). Moderate risk profile |
| **Macro Analyst** | Thesis formation | Top-down bias. Weights VIX regime (0.30) and credit spread (0.25). Moderate risk profile |
| **Sentiment Analyst** | Thesis formation | News-driven. Weights sentiment signal (0.40). Aggressive risk profile |
| **Risk Analyst** | Thesis formation | Defensive bias. Weights drawdown (0.30) and VIX (0.30). Conservative risk profile |
| **Aggressive PM** | Portfolio construction | Trusts momentum (0.30) and sentiment (0.30). Scales leverage up 20%, positions up 20% |
| **Conservative PM** | Portfolio construction | Trusts risk (0.35) and macro (0.25). Scales leverage down 20%, positions down 20% |
| **Balanced PM** | Portfolio construction | Equal trust across all analysts. No scaling bias |
| **CIO** | Decision maker | Selects PM based on market conditions. Safety overrides for extreme VIX or drawdown |
| **Macro News Agent** | News gathering | Monitors Fed, economic data, geopolitical events via Alpaca news + FRED |
| **Sector News Agent** | News gathering | Monitors industry trends, sector rotation via Alpaca news |
| **Company News Agent** | News gathering | Monitors earnings, insider activity, M&A, analyst actions via Alpaca news + SEC EDGAR |
| **Execution Agent** | Trade execution | Session-aware (ET timezone), PDT compliance, VWAP order splitting, fill confirmation, retry logic |
| **Intraday Agent** | Intraday trading | VWAP reversion, opening range breakout, momentum burst, gap analysis. Asymmetric long/short (overnight premium) |
| **Research Agent** | Report generation | Weekly institutional-grade research report via Claude Sonnet |

## ML Models

### CS_Multi_Model_Trading_System (Separate Repo)

Three alpha models rank ~100 S&P 500 stocks by predicted 10-day forward return:

| Model | Architecture | Complexity | Sharpe | Max DD | Role |
|-------|-------------|-----------|--------|--------|------|
| **CrossMamba** | Selective state-space (Mamba) | O(n) linear | 2.36 | -9.2% | Primary — best risk-adjusted returns |
| **TST** | Time Series Transformer | O(n²) quadratic | 2.31 | -9.3% | Secondary |
| **LightGBM** | Gradient boosting ensemble | O(n log n) | 1.56 | -20.2% | Fallback (runs on any platform) |

**Feature pipeline:** 186 raw features → 50 selected via stability-based IC screening
- Price/volume: momentum, mean reversion, volatility, volume, technicals (100 features)
- Fundamentals: PE, ROE, margins, earnings, analyst targets (40 features)
- Cross-asset: VIX regime, yield curve, credit spreads, oil, gold, dollar (36 features)
- Sentiment: news sentiment from Alpaca + Claude Haiku LLM analysis (10 features)

**Training methodology (López de Prado):**
- Walk-forward validation with purge gap (10 days) and embargo (5 days)
- Triple barrier labeling (target/stop/timeout, not fixed-horizon returns)
- Sample uniqueness weighting (overlapping samples downweighted)
- Fractional differentiation (d=0.4, stationary while preserving memory)
- Retrained every 14 days via GitHub Actions on Linux

### Intraday Model

Separate LightGBM model predicting 1-hour forward returns:

- **Features:** VWAP distance, volume profile, order flow imbalance, Kyle's lambda, VPIN, Amihud illiquidity, Roll's spread, autocorrelation, SPY correlation (30+ features)
- **Labeling:** Triple barrier (volatility-adaptive bands)
- **Training:** Purged walk-forward with 60-bar embargo + sample uniqueness weights
- **Meta-labeling:** Secondary model predicts whether primary is correct → sizes bets
- **Retrains:** Daily on yesterday's intraday data

## Intraday Trading Signals

Four signal types, each with ATR-adaptive per-symbol thresholds:

| Signal | Setup | Hold Time | How It Works |
|--------|-------|-----------|-------------|
| **VWAP Reversion** | Price deviates >1.5 ATR from VWAP | ~90 min | Fades to VWAP. Institutional benchmark signal |
| **Opening Range Breakout** | Price breaks first-30-min high/low | ~3 hours | Momentum continuation after opening range forms |
| **Momentum Burst** | 1 ATR move in 5 bars + 2x avg volume | ~60 min | Captures institutional flow-driven moves |
| **Gap Analysis** | Fade small gaps (<0.5 ATR), continue large gaps (>1.5 ATR) | ~2 hours | ATR-relative, not fixed percentage |

**Asymmetric management (overnight premium effect):**
- Intraday longs need higher conviction (0.65 vs 0.50) and better R:R (1.5:1 vs 1.0:1)
- Longs have tighter trailing stops (lock profits fast — intraday drag)
- Shorts have wider trailing stops (let winners run — aligned with intraday weakness)
- Research basis: Cliff Asness, "The Overnight Return" — most equity returns happen overnight

## Position Management

After entry, each intraday position is actively managed:

**Trailing stops (asymmetric):**
- Longs: breakeven at 35%, lock 60% at 60%, lock 80% at target
- Shorts: breakeven at 50%, lock 40% at 75%, lock 65% at target

**Partial profit-taking:**
- Longs: 1/3 at 40% of target, 1/2 at 75% (aggressive — take profits fast)
- Shorts: 1/4 at 60% of target, 1/2 at target (patient — let it run)

**Signal invalidation:** VWAP shift >0.5%, max loss >2x initial risk, time decay

**Correlation filtering:** Max 2 signals per sector per signal type. 5 tech stocks triggering simultaneously = take best 2, not all 5.

## Risk Management

### Pre-Trade Risk Limits (Hard, Cannot Override)

| Limit | Value | Why |
|-------|-------|-----|
| Max gross exposure | 200% | Reg T margin requirement |
| Max net exposure | 80% | Prevents full directional bet |
| Max single position | 10% of equity | Diversification |
| Max daily loss | -3% | Halt all trading for the day |
| Max positions | 30 | Prevents overtrading |
| Min equity | $25,000 | PDT rule compliance |
| Max trades/day | 50 | Prevents churning |
| Max sector concentration | 30% | Sector diversification |

### Market Manipulation Safeguards

| Threat | Detection | Action |
|--------|-----------|--------|
| Pump & dump | Volume >5x 20-day average | Block new entries |
| Spoofing | Spread >5x normal | Block — market maker pulled quotes |
| Stop hunting | Price move >5x ATR | Block — anomalous move |
| Momentum ignition | Directional burst + volume spike | Block — designed to trigger algos then reverse |
| Flash crash | SPY down >3% intraday or VIX >40 | Halt ALL new positions |
| Wash trading | Buy + sell same stock within 5 min | Block — illegal under SEC rules |
| Fat finger | Order >10% of equity or >$100k | Block — obvious error |

### Risk Model (CS System)

- Multi-speed regime detector: fast (5/20d) + medium (20/50d) + slow (50/200d), weighted 0.2/0.3/0.5
- Tail risk protection: gap-down (>3% → halve exposure), vol spike (2x → cut 40%), 3+ consecutive down days (cut 30%)
- Barra-style factor neutralization with sector neutrality
- Volatility targeting with drawdown control
- Volatility-dependent transaction costs (1-3x base during high vol)

## Alpha Optimization

### Kelly Criterion Position Sizing
Positions sized by mathematically optimal fraction of capital. Quarter-Kelly (25%) captures 75% of growth rate with much lower variance. Higher conviction = larger position.

### Transaction Cost-Aware Filtering
Trades where expected return < 3x round-trip cost (72 bps) are skipped. Prevents churning — the #1 destroyer of quant fund returns.

### Alpha Decay Tracking
Signals decay exponentially. Momentum signals (fast decay) execute immediately. Fundamental signals (slow decay) can wait for better price.

### Dynamic Ensemble Weighting
Model weights shift toward whichever has highest recent Information Coefficient. Adapts to changing market regimes automatically.

### Market Impact Estimation (Almgren-Chriss)
Estimates how our order moves the price. Orders with >10 bps estimated impact use VWAP splitting. Saves 10-40 bps per trade on illiquid names.

## Adaptive Learning

The system evolves over time via exponential decay weighting:

1. **Every decision is recorded** — analyst theses, PM decisions, CIO selections
2. **After 10 trading days**, predictions are scored against realized returns
3. **Weights update**: agents that were right recently get more influence
4. **Safeguards**: 0.95 decay factor (slow adaptation), 8% minimum weight floor, 5 minimum samples before adapting

Example evolution:
```
Week 1:  All analysts start at equal weight (1.0x)
Week 3:  Momentum analyst was right → 1.05x. Risk analyst was wrong → 0.97x
Week 6:  Weights converge. Best analysts at 1.2x, worst at 0.8x
Market shifts: Risk analyst correctly predicted selloff → weight recovers
```

## Production Schedule

```
 6:30 AM ET  Check for retrained models (git pull from GitHub Actions)
 7:00 AM     Pre-market briefing (news, gaps, overnight events)
 9:30 AM     Market opens — NO TRADING (opening noise)
10:00 AM     Daily cycle: ML → 5 analysts → 3 PMs → CIO → execute
10:15 AM     First intraday scan
10:15-3:00   Intraday scans every 15 minutes
 3:00-3:30   POWER HOUR: scans every 5 minutes
 3:30        Begin closing intraday positions
 3:45        Aggressive close — all intraday must be flat
 4:00 PM     Market close
 4:05        P&L snapshot + position reconciliation
 4:15        Weekly research report (Fridays only)
 4:30        Feedback loop (score predictions, update agent weights)
 5:00        Intraday model retrain (on today's data)
```

GitHub Actions runs on the 1st and 15th of each month to retrain CrossMamba + LightGBM.

## Directory Structure

```
openclaw-fintech/
├── cli.py                          # Production CLI interface
├── Dockerfile                      # Docker deployment
├── docker-compose.yml              # Container orchestration
├── gateway/.env                    # API keys (not committed)
│
├── skills/
│   ├── analyst/                    # 5 analyst personalities
│   │   ├── handlers.py             # form_thesis(), form_all_theses()
│   │   ├── scoring.py              # Deterministic conviction scoring
│   │   ├── personalities.py        # Signal weights, model weights, risk profiles
│   │   └── presets.py              # Parameter interpolation by conviction
│   │
│   ├── pm/                         # 3 PM personalities + CIO
│   │   ├── handlers.py             # resolve(), apply_approved_params()
│   │   └── resolution.py           # Conviction blending, CIO selection, approval gates
│   │
│   ├── intel/                      # Market intelligence
│   │   ├── handlers.py             # gather_briefing(), pre_market_briefing()
│   │   └── regime.py               # VIX regime, breadth, cross-asset signals
│   │
│   ├── execution/                  # Trade execution
│   │   ├── handlers.py             # execute_daily(), execute_intraday(), close_intraday()
│   │   ├── session.py              # Market hours, PDT compliance, EOD detection
│   │   ├── order_manager.py        # Fill polling, retry, idempotency
│   │   ├── order_splitter.py       # VWAP splitting for large orders
│   │   ├── safeguards.py           # Manipulation detection (7 checks)
│   │   ├── risk_limits.py          # Hard pre-trade limits (8 checks)
│   │   └── alpha_optimization.py   # Kelly sizing, cost filtering, impact estimation
│   │
│   ├── intraday/                   # Intraday trading
│   │   ├── signals.py              # VWAP, ORB, momentum burst, gap analysis
│   │   ├── scanner.py              # ML-filtered scanning, asymmetric thresholds
│   │   ├── calibration.py          # ATR-adaptive thresholds, correlation filtering
│   │   ├── position_manager.py     # Trailing stops, partial profits, invalidation
│   │   └── model/                  # Intraday ML model
│   │       ├── features.py         # 30+ intraday features + microstructure
│   │       ├── microstructure.py   # OFI, Kyle's lambda, VPIN, Amihud, Roll spread
│   │       ├── labeling.py         # Triple barrier, sample uniqueness weights
│   │       ├── meta_labeling.py    # Secondary model for bet sizing
│   │       └── predictor.py        # LightGBM with purged walk-forward
│   │
│   ├── news/                       # News gathering (3 agents)
│   │   ├── gatherers.py            # Macro, sector, company news from Alpaca
│   │   ├── aggregator.py           # Combines all sources into NewsDigest
│   │   ├── edgar.py                # SEC EDGAR filings + insider trading
│   │   ├── fred.py                 # FRED economic data releases
│   │   ��── llm_sentiment.py        # Claude Haiku analysis (~$0.02/day)
│   │
│   ├── research/                   # Weekly reports
│   │   └── report.py               # Claude Sonnet institutional research report
│   │
│   ├── orchestrator/               # Pipeline coordination
│   │   ├── pipeline.py             # run_daily_cycle(), run_intraday_cycle()
│   │   ├── scheduler.py            # Institutional-grade cron schedule
│   │   └── checkpoint.py           # Crash recovery, prevents double-trading
│   │
│   ├── signals/                    # ML model bridge
│   │   └── bridge.py               # Loads CrossMamba/LightGBM, patches data pipeline
│   │
│   ├── market_data/                # Professional data provider
│   │   ├── provider.py             # Alpaca Data API v2 (bars, snapshots, news)
│   │   ├── adapter.py              # yfinance-compatible interface for CS system
│   │   ├── fmp.py                  # Financial Modeling Prep (ready for API key)
│   │   └── models.py               # Bar, NewsArticle, Snapshot dataclasses
│   │
│   ├── pnl/                        # Performance tracking
│   │   ├── tracker.py              # SQLite P&L: daily returns, Sharpe, drawdown
│   │   └── reconciliation.py       # Verify system state vs Alpaca positions
│   │
│   ├── feedback/                   # Adaptive learning
│   │   ├── scorer.py               # Score past predictions against outcomes
│   │   ├── adapter.py              # Exponential decay weight adjustment
│   │   └── loop.py                 # Daily learning cycle + retrain trigger
│   │
│   ├── shared/                     # Infrastructure (from OpenClaw)
│   │   ├── config.py               # Logging, audit trail, limits, allowed pairs
│   │   ├── approval.py             # SQLite-backed approval workflow
│   │   ├─��� resilience.py           # Retry, circuit breakers, rate limiting
│   │   ├── alerting.py             # Slack/Discord webhook alerts
│   │   ├── state.py                # Safe JSON I/O (atomic writes, corruption handling)
│   │   ├── secrets.py              # Secret management abstraction
│   │   ├── structured_logging.py   # JSON logging for ELK/CloudWatch
│   │   ├── encryption.py           # Data-at-rest encryption
│   │   ├── metrics.py              # Prometheus-compatible metrics
│   │   ├── health.py               # Health check endpoints
│   │   └── rbac.py                 # Role-based access control
│   │
│   ├── trading/                    # Exchange clients (from OpenClaw)
│   │   ├── exchange_client.py      # Alpaca, Binance, Coinbase clients
│   │   └── strategy.py             # Risk checks, momentum signals
���   │
│   └── legal/                      # Compliance (from OpenClaw)
│       └── handlers.py             # SEC EDGAR, GDPR scanning, contract analysis
│
├── tests/                          # 211 tests
│   ├── test_quant_agents.py        # Scoring, personalities, PM, CIO, session
│   ├── test_intraday.py            # Signals, ATR, correlation, position mgmt
│   ├── test_pnl.py                 # P&L tracking, reconciliation
│   ├── test_checkpoint.py          # Crash recovery
│   ├��─ test_feedback.py            # Adaptive learning
│   ├── test_fmp.py                 # FMP integration
│   ├── test_order_manager.py       # Fill polling, retry
│   └── ...                         # Approval, encryption, resilience, etc.
│
├── data/                           # Runtime data (gitignored)
│   ├── pnl.db                      # P&L tracking database
│   ├── approvals.db                # Approval workflow database
│   ├── feedback.db                 # Prediction scoring database
│   └── *.csv, *.json               # Cached market data
│
└── workspaces/                     # Agent state (gitignored)
    ├── pm-agent/state.json
    ├── execution-agent/state.json
    ├── intel-agent/state.json
    └── orchestrator/checkpoints/
```

## Running the System

### Interactive CLI
```bash
cd /Users/kylezeng/VSNX/openclaw-fintech
python cli.py
```

Commands:
- `run cycle` — full daily pipeline: ML → debate → trade
- `scan` — intraday signal scan
- `briefing` — market intelligence report
- `portfolio` — current positions + exposure
- `positions` — detailed Alpaca position list
- `pnl` — P&L report
- `reconcile` — verify positions vs Alpaca
- `news` — aggregated news from 5 sources
- `report` — weekly research report (Claude)
- `analysts` — all 5 analyst convictions
- `pm status` — PM parameters + last decision
- `feedback` — adaptive weight status
- `approve APR-XXXXX` / `deny APR-XXXXX` — approval workflow
- `secrets` — verify API key configuration
- `health` — system health check

### Autonomous Scheduler
```bash
python -m skills.orchestrator.scheduler
```

### Docker Deployment
```bash
docker compose up -d
```

## Configuration

### Required (gateway/.env)
```
ALPACA_API_KEY=your_key
ALPACA_API_SECRET=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

### Optional
```
ANTHROPIC_API_KEY=sk-ant-...    # LLM sentiment + research reports
FMP_API_KEY=your_key            # Financial Modeling Prep fundamentals
FRED_API_KEY=your_key           # FRED economic data (has free tier)
ALERT_WEBHOOK_URL=https://...   # Slack/Discord alerts
SEC_EDGAR_USER_AGENT=Company contact@company.com
```

## Cost

| Component | Cost |
|-----------|------|
| Alpaca (trading + IEX data) | Free |
| Claude Haiku (LLM sentiment) | ~$0.04/week |
| Claude Sonnet (weekly report) | ~$0.07/week |
| GitHub Actions (model retraining) | Free |
| SEC EDGAR, FRED | Free |
| **Total (Mac)** | **~$0.11/week** |
| + Linux server (CrossMamba) | +$1.50/week |
| + FMP fundamentals | +$7.25/week |

## Key Design Decisions

1. **Deterministic scoring, not LLM decisions.** Analyst convictions and PM resolutions use weighted signal functions, not LLM text generation. This makes the system reproducible, testable, and debuggable. Claude is used only for human-readable explanations and news sentiment analysis.

2. **CrossMamba as primary model.** Best Sharpe (2.36) and lowest drawdown (-9.2%) in backtests. Trains on GitHub Actions (Linux) because PyTorch's selective scan segfaults on macOS ARM. LightGBM is the local fallback.

3. **Asymmetric intraday trading.** Research shows most equity returns happen overnight (close→open), not intraday. Intraday longs face a structural headwind. The system requires higher conviction for long intraday trades and manages them more aggressively.

4. **Agent debate is structured, not conversational.** Five analysts score signals simultaneously using different weight vectors. Three PMs blend the scores. CIO selects based on conditions. This is an ensemble-of-opinions, not an LLM chat.

5. **Feedback loop with safeguards.** Weights adapt based on realized performance, but slowly (0.95 decay factor) with minimum floors (8%). This prevents overfitting to recent noise while still learning from experience.

6. **Pre-trade safeguards are hard limits.** Even if the PM approves aggressive parameters, manipulation detection and risk limits block dangerous trades. These cannot be overridden.

7. **Training and inference use the same data pipeline.** Both GitHub Actions (training) and the local bridge (inference) use the Alpaca data adapter. No train/test mismatch.
