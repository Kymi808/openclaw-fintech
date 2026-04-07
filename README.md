# OpenClaw Quant — Multi-Agent Autonomous Trading System

Production-grade quantitative trading platform built on the OpenClaw framework. Combines ML-driven stock ranking with a multi-agent debate architecture for autonomous equity trading.

## Architecture

```
Market Data (Alpaca) → Intel Agent → 5 Analysts → 3 PMs → CIO → Execution → Alpaca
                           ↑              ↑                  ↑
                    News Gathering    ML Models         HMM Regime
                    (5 sources)    (CrossMamba/         Detection
                                   LightGBM)
```

### Agent Layer (16 instances)
- **5 Analyst Personalities**: Momentum, Value, Macro, Sentiment, Risk — each with different signal weights and model preferences
- **3 PM Personalities**: Aggressive, Conservative, Balanced — each blends analyst theses differently  
- **CIO**: Selects PM based on HMM regime probabilities. Safety override on VIX > 35 or drawdown > 10%
- **3 News Gatherers**: Macro, Sector, Company — plus SEC EDGAR + FRED + LLM sentiment (Claude Haiku)

### ML Models
- **CrossMamba** (primary): Selective state-space model, 2.36 Sharpe on synthetic backtest. Trains on GitHub Actions (Linux), can't run on macOS ARM
- **LightGBM** (fallback): Gradient boosting ensemble, runs everywhere. Used for local inference on Mac
- **TST**: Time Series Transformer, secondary ensemble member
- **Ensemble**: Combines all 3 — the real edge (individual models underperform, ensemble outperforms)
- **Intraday Model**: LightGBM on microstructure features, triple barrier labeling, meta-labeling for bet sizing

### Data Pipeline
- **Alpaca**: Real-time prices, historical bars, news API (primary)
- **yfinance**: Historical data for backtesting (fallback)
- **SEC EDGAR**: Insider trading (Form 4), material filings (8-K)
- **FRED**: Fed funds rate, CPI, jobs, GDP, consumer sentiment
- **FMP**: Point-in-time fundamentals (ready, needs API key)
- **OpenBB**: Options IV skew, short interest (ready, needs install)
- **Claude Haiku**: Context-aware news sentiment (~$0.60/month)

### Risk Management
- **HMM Regime Detection**: 3-state Gaussian model (Hamilton 1989) replaces MA crossovers
- **GARCH Volatility**: Forward-looking conditional vol for risk parity
- **Tail Risk Protection**: Gap-down detection, vol spike halving, consecutive loss reduction
- **Execution Safeguards**: Momentum ignition, wash trade, fat finger, spread monitoring
- **Pre-Trade Risk Limits**: Max exposure (200%), daily loss halt (-3%), sector concentration (30%)

### Institutional Techniques
- Triple barrier labeling (Lopez de Prado)
- Sample uniqueness weighting
- Fractional differentiation for stationarity
- Meta-labeling for bet sizing
- Asymmetric intraday long/short (overnight premium effect)
- Kelly criterion position sizing
- Transaction cost-aware filtering
- Almgren-Chriss market impact estimation

## Quick Start

```bash
# Interactive CLI
python cli.py

# Commands
run cycle    — full pipeline: ML → debate → approve → trade
portfolio    — positions + exposure
pnl          — P&L report
news         — aggregated news digest
report       — weekly research report (Claude-generated)
analysts     — 5 analyst theses
scan         — intraday signal scanner
```

## Autonomous Trading

```bash
# Start the scheduler (runs 24/7)
python -m skills.orchestrator.scheduler
```

Schedule (ET):
- 6:30 AM: Pull latest retrained models from GitHub
- 7:00 AM: Pre-market briefing
- 10:00 AM: Daily cycle (ML → debate → trade)
- 10:15 AM - 3:00 PM: Intraday scans every 15 min
- 3:00 PM - 3:30 PM: Power hour scans every 5 min
- 3:45 PM: Mandatory EOD close of intraday positions
- 4:05 PM: P&L snapshot + position reconciliation
- 4:30 PM: Feedback loop (score predictions, update weights)

## Model Retraining

Automated via GitHub Actions every 14 days:
```
GitHub Actions → Fetch Alpaca data → Train CrossMamba + LightGBM → Push .pkl → git pull
```

Manual: `python retrain.py --models crossmamba,lightgbm` (requires Linux for CrossMamba)

## Deployment

```bash
# Docker (production, enables CrossMamba on Linux)
docker compose up -d

# Or locally (LightGBM only on Mac)
python cli.py
```

## Configuration

Set API keys in `gateway/.env`:
```
ALPACA_API_KEY=your_key
ALPACA_API_SECRET=your_secret
ANTHROPIC_API_KEY=your_key          # for LLM sentiment + reports
FMP_API_KEY=your_key                # for point-in-time fundamentals ($29/mo)
ALERT_WEBHOOK_URL=your_slack_url    # for Slack/Discord alerts
FRED_API_KEY=your_key               # for economic data (free)
```

## Tests

```bash
python -m pytest tests/ -q
# 211 tests covering: scoring, personalities, PM resolution, CIO override,
# session detection, PDT compliance, order splitting, model blending,
# P&L tracking, reconciliation, checkpoint recovery, feedback loop
```

## Current Status

- **Paper trading**: $500k Alpaca account, ready for Monday
- **Backtest IC**: 0.063 Rank IC (LightGBM, 460 stocks, risk-adjusted targets)
- **Ensemble Sharpe**: Pending (Colab comparison running)
- **Live track record**: 0 days — starting Monday

## Known Limitations

1. CrossMamba segfaults on macOS ARM (trains on GitHub Actions Linux)
2. Fundamental data has look-ahead bias until FMP is activated
3. No bear market validation (backtest period is mostly bullish)
4. Intraday ML model needs 20+ days of training data
5. Feedback loop needs 5+ daily cycles before adapting weights
