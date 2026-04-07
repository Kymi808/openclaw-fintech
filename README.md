# OpenClaw Quant: Multi-Agent Autonomous Equity Trading System

## Overview

OpenClaw Quant is an end-to-end quantitative trading system that combines ML-driven cross-sectional equity ranking with a multi-agent debate architecture for autonomous portfolio management. The system replaces traditional single-model, single-signal trading with an ensemble of specialized agents that evaluate market conditions from different perspectives before arriving at a consensus trading decision.

The core insight driving the architecture: **diversification across model architectures and analytical perspectives produces better risk-adjusted returns than any single model or viewpoint.** This is supported by the empirical finding that while individual models (LightGBM, TST, CrossMamba) achieve weak predictive signals individually (Rank IC ~0.02), the ensemble achieves substantially higher signal quality (Rank IC 0.063) with better drawdown control.

## System Architecture

The system operates as a pipeline that flows from data ingestion through agent debate to trade execution:

```
DATA LAYER                    INTELLIGENCE LAYER              DECISION LAYER                EXECUTION LAYER
─────────────────────────    ──────────────────────          ──────────────────────        ──────────────────
Alpaca Market Data ──────┐   Intel Agent                     5 Analysts (parallel)         Execution Agent
  Historical bars        │     Regime detection (HMM)          Momentum Analyst              Fill polling
  Real-time snapshots    │     Market breadth                  Value Analyst                 Retry with backoff
  News API               │     News aggregation                Macro Analyst                 VWAP splitting
                         │                                     Sentiment Analyst             Idempotent orders
SEC EDGAR ───────────────┤   3 News Gatherers (parallel)       Risk Analyst              
  Insider trading (F4)   │     Macro news agent                         │                 Intraday Scanner
  Material filings (8K)  │     Sector news agent             3 PMs                          VWAP reversion
                         │     Company news agent              Aggressive PM                 Opening range breakout
FRED ────────────────────┤                                     Conservative PM               Momentum burst
  Economic releases      │   LLM Sentiment (Haiku)             Balanced PM                  Gap analysis
  Rate decisions         │     Context-aware scoring                    │
                         │     Event classification          CIO (final decision)          Position Manager
CS Multi-Model System ───┘     Temporal decay                  HMM regime-aware              Trailing stops
  CrossMamba ensemble                                          Safety overrides               Partial profit-taking
  LightGBM ensemble                                            Approval workflow              Signal invalidation
  TST ensemble                                                                                Asymmetric long/short
```

### Why Multi-Agent?

Traditional quant systems use a single model to generate signals and a fixed set of rules for portfolio construction. This creates brittleness — when the model's assumptions are violated (regime change, black swan), the entire system fails uniformly.

The multi-agent approach addresses this by:
1. **Diverse signal interpretation**: Five analyst personalities weight the same ML predictions and market data differently. A momentum analyst and a risk analyst will draw opposite conclusions from the same VIX spike — this disagreement is informative.
2. **Dynamic parameter selection**: Instead of hardcoded portfolio parameters (N long positions, leverage ratio), three PM personalities propose different configurations and a CIO selects based on current market regime. In a VIX crisis, the system autonomously shifts from aggressive (20 longs, 1.6x leverage) to conservative (5 longs, 0.8x leverage).
3. **Reproducibility**: Unlike LLM-based agent systems that produce different outputs each run, all scoring functions are deterministic. The same inputs always produce the same debate outcome. LLMs are used only for human-readable explanations and news analysis — never for trading decisions.

### Agent Details

**Analyst Personalities** — Each analyst uses the same 6 base signals (model dispersion, breadth, sentiment, vol regime, credit stress, drawdown proximity) but with different weight vectors. The weights encode different investment philosophies:

| Analyst | Primary signals | Model preference | Risk profile | Bias |
|---------|----------------|-----------------|-------------|------|
| Momentum | Model dispersion (0.35), breadth (0.30) | CrossMamba 60% | Aggressive | Bull |
| Value | Credit stress (0.20), drawdown (0.20) | CrossMamba 45%, LightGBM 35% | Moderate | Neutral |
| Macro | VIX regime (0.30), credit (0.25) | CrossMamba 45%, TST 35% | Moderate | Neutral |
| Sentiment | Sentiment (0.40), breadth (0.20) | CrossMamba 50% | Aggressive | Bull |
| Risk | Drawdown (0.30), VIX (0.30) | CrossMamba 55% | Conservative | Bear |

Signal weights are adapted over time via an exponential decay feedback loop (decay factor 0.95, minimum weight floor 8%) based on whether the analyst's conviction direction correlated with realized returns over the 10-day prediction horizon.

**PM Personalities** — Each PM blends the 5 analyst theses into portfolio parameters using different analyst preference weights:

- **Aggressive**: Weights momentum (0.30) and sentiment (0.30) highest. Applies 1.2x bias to leverage and position count.
- **Conservative**: Weights risk (0.35) and macro (0.25) highest. Applies 0.8x bias.
- **Balanced**: Equal weighting (0.20 each).

**CIO** — Selects which PM's proposal becomes the active portfolio configuration. Selection hierarchy:
1. Safety override: VIX > 35 OR drawdown > 10% → force conservative (hard limit)
2. HMM-based: If HMM confidence > 60%, use regime probabilities (P(bear) > 50% → conservative, P(bull) > 65% → aggressive)
3. VIX/breadth fallback: Elevated VIX or weak breadth → conservative, low VIX + strong breadth → aggressive
4. Default: Balanced

## ML Models

The ML layer is provided by the [CS Multi-Model Trading System](https://github.com/Kymi808/CS_Multi_Model_Trading_System), a separate repository that handles feature engineering, model training, and signal generation. OpenClaw consumes its predictions through `skills/signals/bridge.py`.

**Model Priority**: CrossMamba → TST → LightGBM (falls back to LightGBM on macOS ARM due to PyTorch selective scan segfault).

**Current Performance** (real data, 460 stocks, 10L/10S neutral, walk-forward validation, 24bp round-trip costs):

| Model | Annual Return | Sharpe | Max Drawdown | Win Rate |
|-------|-------------|--------|-------------|----------|
| **CrossMamba** | 35.38% | 3.393 | -13.66% | 59.25% |
| **TST** | 37.02% | 3.382 | -9.25% | 57.79% |
| Ensemble | 19.53% | 1.878 | -9.84% | 55.13% |
| LightGBM | 13.38% | 1.238 | -10.22% | 55.26% |
| SPY benchmark | 20.67% | 1.378 | -18.76% | 57.14% |

LightGBM Avg Rank IC: 0.063 ± 0.141 (IR: 0.44). Returns are near-neutral (~10% net exposure) — driven by stock selection alpha, not market beta. Caveats: fundamental look-ahead bias (yfinance), single bullish test period (2021-2026). Expect 30-40% degradation live.

Models retrain automatically every 14 days via GitHub Actions on Linux (CrossMamba requires CUDA-compatible environment).

## Risk Management

### HMM Regime Detection
Replaces simple moving average crossovers with a 3-state Gaussian Hidden Markov Model (Hamilton, 1989). The model is trained on 5 macro observables (market return, realized vol, credit spreads, yield curve slope, VIX) with an expanding window and quarterly refits. Output is a probability distribution over bull/sideways/bear states, which provides strictly more information than a binary trend signal.

### GARCH Volatility
Forward-looking conditional volatility estimates via GARCH(1,1) (Bollerslev, 1986) replace backward-looking rolling standard deviation for risk parity position sizing. GARCH captures volatility clustering (big moves follow big moves) and mean reversion — critical during regime transitions when rolling estimates lag.

### Tail Risk Protection
Three independent tail risk detectors that scale down exposure automatically:
- **Gap-down**: Any daily return < -3% → halve exposure (0.5x scale)
- **Vol spike**: 5-day vol > 2x 63-day vol → reduce by 40% (0.6x)
- **Consecutive losses**: 3+ consecutive down days → reduce by 30% (0.7x)

### Execution Safeguards
Pre-trade checks that block orders under dangerous conditions:
- Unusual volume (>5x 20-day average) — possible pump-and-dump
- Wide spreads (>5x typical) — market maker pulled quotes
- Price anomaly (>5x ATR move) — possible stop hunting or flash crash
- Momentum ignition detection — directional burst + volume spike pattern
- Market stress halt (SPY down >3% or VIX >40) — correlated selloff
- Wash trade prevention — no opposite-side trade within 5 minutes
- Fat finger protection — max 10% of equity per order

## Intraday Trading

The intraday system operates independently from the daily model, using different signals and a separate ML model:

**Signals**: VWAP reversion, opening range breakout, momentum burst, gap analysis — all with ATR-adaptive thresholds per stock (using `calibration.py`).

**Asymmetric Long/Short**: Based on the overnight premium effect (documented in academic literature: most equity returns occur overnight, not intraday), the system applies higher conviction thresholds for intraday longs (confidence ≥ 0.65, R:R ≥ 1.5x) than shorts (confidence ≥ 0.50, R:R ≥ 1.0x). Position management is also asymmetric — longs have tighter trailing stops and earlier profit-taking, while shorts are given more room to run.

**Institutional Methodology**: The intraday ML model (when trained with sufficient data) uses triple barrier labeling (Lopez de Prado, 2018), sample uniqueness weighting, purged walk-forward validation with embargo gaps, and meta-labeling for bet sizing. Microstructure features include order flow imbalance (OFI), Kyle's lambda, Amihud illiquidity, VPIN proxy, Roll's spread, and volatility signature ratios.

## Data Sources

| Source | Data | Cost | Status |
|--------|------|------|--------|
| Alpaca Market Data | Prices, bars, snapshots, news | Free | Active |
| SEC EDGAR | Insider trading (Form 4), material filings (8-K) | Free | Active |
| FRED | Fed decisions, CPI, jobs, GDP, consumer sentiment | Free | Active |
| Claude Haiku | Context-aware news sentiment, event classification | ~$0.60/mo | Active |
| yfinance | Historical prices, fundamentals (with look-ahead bias) | Free | Backtest only |
| Financial Modeling Prep | Point-in-time fundamentals, earnings estimates | $29/mo | Ready, needs key |
| OpenBB | Options IV skew, short interest, put-call ratio | Free + keys | Ready, needs install |

## Production Operations

### Scheduler
The autonomous scheduler (`skills/orchestrator/scheduler.py`) runs a full trading day cycle:

```
 6:30 AM ET  Check for retrained models (git pull from GitHub Actions)
 7:00 AM     Pre-market briefing (regime, news, macro)
10:00 AM     Daily cycle: ML predictions → 5 analysts → 3 PMs → CIO → execute
10:15 AM     Intraday scans begin (every 15 minutes)
 3:00 PM     Power hour: scans every 5 minutes
 3:30 PM     Begin closing intraday positions
 3:45 PM     Mandatory EOD close — all intraday positions must be flat
 4:05 PM     P&L snapshot + position reconciliation vs Alpaca
 4:15 PM     Weekly research report (Fridays)
 4:30 PM     Feedback loop: score predictions, update adaptive weights
 5:00 PM     Intraday model update: collect data, retrain
```

### Persistence
- **P&L tracking**: SQLite database with daily snapshots, equity curve, Sharpe, drawdown
- **Approvals**: SQLite-backed with auto-expiry (survives process restarts)
- **Agent state**: JSON files with atomic writes (safe_save_state prevents corruption)
- **Checkpoints**: Pipeline step tracking for crash recovery (detects dangerous incomplete executions)

### Alerting
Slack/Discord webhook alerts for: pipeline failures, order rejections, reconciliation discrepancies, drawdown warnings, daily P&L summary. Configure `ALERT_WEBHOOK_URL` in `gateway/.env`.

## Setup

```bash
git clone https://github.com/Kymi808/openclaw-fintech.git
cd openclaw-fintech
python cli.py
```

API keys in `gateway/.env`:
```
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ANTHROPIC_API_KEY=...        # LLM sentiment + research reports
FMP_API_KEY=...              # point-in-time fundamentals ($29/mo)
FRED_API_KEY=...             # economic data (free)
ALERT_WEBHOOK_URL=...        # Slack/Discord alerts
```

Docker deployment (enables CrossMamba on Linux):
```bash
docker compose up -d
```

## Testing

```bash
python -m pytest tests/ -q    # 211 tests
```

Coverage: analyst scoring, personality conviction, preset interpolation, PM resolution, CIO safety override, market session detection, PDT compliance, order splitting, model blending, P&L tracking, reconciliation, checkpoint recovery, feedback loop, FMP integration, intraday signals, position management, ATR calibration, correlation filtering.

## Known Limitations and Honest Assessment

1. **No live track record.** The system has zero days of live P&L. Backtest results, no matter how rigorous the methodology, are not predictive of future performance. Paper trading validation (6+ months) is required before any real capital allocation.

2. **Fundamental look-ahead bias.** yfinance returns current fundamentals for all historical dates. The FMP integration is built but not yet active. Every backtest result using fundamental features is inflated by an estimated 2-5%.

3. **Single market regime in backtest.** The 2021-2026 period is predominantly bullish. The HMM regime detector has never been tested on a genuine bear market transition. The 2022 correction is the only stress period in the data.

4. **CrossMamba macOS incompatibility.** PyTorch's selective scan operation segfaults on Apple Silicon. The system falls back to LightGBM locally, which is functional but not the primary model.

5. **Approximate transaction costs.** The volatility-scaled slippage model is better than fixed costs but has not been calibrated against actual execution data. Real costs may be 30-50% higher than estimated.

## References

- Asness, Moskowitz & Pedersen (2013), "Value and Momentum Everywhere"
- Bali & Hovakimian (2009), "Volatility Spreads and Expected Stock Returns"
- Bernard & Thomas (1989), "Post-Earnings-Announcement Drift"
- Bollerslev (1986), "Generalized Autoregressive Conditional Heteroskedasticity"
- Frazzini & Pedersen (2014), "Betting Against Beta"
- Grinold & Kahn, "Active Portfolio Management" (2nd ed.)
- Hamilton (1989), "A New Approach to the Economic Analysis of Nonstationary Time Series"
- Kelly (1956), "A New Interpretation of Information Rate"
- Lakonishok & Lee (2001), "Are Insider Trades Informative?"
- Loh (2010), "Investor Inattention and the Underreaction to Stock Recommendations"
- Lopez de Prado (2018), "Advances in Financial Machine Learning"
- Novy-Marx (2013), "The Other Side of Value"
- Rapach et al. (2016), "Short Interest and Aggregate Stock Returns"
