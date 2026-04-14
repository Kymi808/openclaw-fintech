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

**Model Priority**: LightGBM (primary) → CrossMamba → TST. LightGBM is the currently deployed production model (CS commit `46ec2b6` — Run 17). CrossMamba/TST remain available as fallbacks on Linux. Override with the `PRIMARY_MODEL` env var.

The primary model was switched from CrossMamba to LightGBM after an empirical audit (see CS repo `audit_v3`) found that the previously reported high-Sharpe results were driven by (1) fundamental look-ahead bias in yfinance, (2) a favorable backtest window, and (3) an aggressive 10L/10S configuration that would not survive realistic TC assumptions. The honest, audited numbers are substantially lower but reflect behavior you can actually deploy.

**Current production performance** — LightGBM Run 17, 100L/30S, 10-day horizon, 5-year walk-forward OOS (2021-03 to 2026-03), 14bp round-trip TC, PIT fundamentals via FMP (no look-ahead), Consumer Discretionary excluded from short universe:

| Metric | Value |
|---|---|
| Sharpe Ratio | **0.627** |
| Max Drawdown | **-9.55%** |
| Total Return (5y) | +19.83% |
| Annual Return | +3.70% |
| Annual Volatility | 5.90% |
| Avg Rank IC | 0.020 |
| IC Info Ratio | 0.234 |
| Avg Net Exposure | 16.5% |
| Avg Gross Exposure | 57.6% |
| Win Rate (daily) | 53.1% |
| 2022 Return | -6.49% |
| 2024 Return | +3.12% |
| 2025 Return | +13.66% |

SPY benchmark over same window: ann 12.33%, Sharpe 0.725, MDD -24.50%. **The strategy underperforms long SPY in absolute return but with half the drawdown and a different return stream — its value is as a low-correlation alpha sleeve, not a market replacement.** Live paper-trading degradation of 20-40% is expected.

Historical per-model numbers in older commits (CrossMamba Sharpe 3.4, etc.) came from a 10L/10S concentrated neutral config with yfinance fundamentals and a different TC model — they are **not reproducible under the current methodology** and should be treated as superseded.

Models retrain automatically every 14 days via GitHub Actions on Linux (CrossMamba requires CUDA). LightGBM retrains locally via `retrain.py` in the CS repo.

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
 9:32 AM     Execute any decision queued while market was closed (replay)
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

**Run as:**

```bash
PYTHONPATH=. python -m skills.orchestrator.scheduler
```

### Queueing decisions made while the market is closed

`execute_daily` saves the current decision + predictions to `workspaces/execution-agent/pending_execution.json` whenever it's called outside `PRE_MARKET`, `OPEN`, or `CLOSING` sessions. The 09:32 ET `execute_pending_at_open` task reads this file, verifies the decision is less than 72 hours old, and replays it against the live market. This lets you trigger a full pipeline run on weekends / overnight / holidays and have it execute at the next open automatically.

Only one pending decision can exist at a time — running the pipeline twice while closed overwrites the older entry so you don't stack up stale signals.

### Operations — inspecting state

All read-only; safe to run any time:

```bash
# What's queued right now?
cat workspaces/execution-agent/pending_execution.json | python -m json.tool | head -30

# One-line status with age
python -c "
import json, os
from datetime import datetime, timezone
p = 'workspaces/execution-agent/pending_execution.json'
if not os.path.exists(p):
    print('(no pending decision)')
else:
    d = json.load(open(p))
    saved = datetime.fromisoformat(d['saved_at'])
    age_h = (datetime.now(timezone.utc) - saved).total_seconds() / 3600
    print(f\"decision_id={d['decision']['decision_id']}  n_preds={len(d['predictions'])}  age={age_h:.1f}h\")
"

# All past pipeline runs (every daily/intraday cycle)
ls -lt workspaces/orchestrator/checkpoints/

# Latest run's full detail
ls -t workspaces/orchestrator/checkpoints/ | head -1 | \
  xargs -I{} python -m json.tool workspaces/orchestrator/checkpoints/{}

# Current execution-agent state (account equity, open positions, PDT count)
cat workspaces/execution-agent/state.json 2>/dev/null | python -m json.tool

# Tail the live audit log (every agent decision + order)
tail -f logs/audit.jsonl

# Recent daily-cycle completions
grep daily_cycle_complete logs/audit.jsonl | tail -5
```

### Manually triggering pipelines (useful for testing & weekend decisions)

```bash
# Run one daily cycle right now (queues if market is closed)
PYTHONPATH=. python -c "
import asyncio
from skills.orchestrator.pipeline import run_daily_cycle
print(asyncio.run(run_daily_cycle()))
"

# Force a pending-queue replay (no-op if nothing queued, or if market is closed)
PYTHONPATH=. python -c "
import asyncio
from skills.execution.handlers import execute_pending_at_open
print(asyncio.run(execute_pending_at_open()))
"
```

### Persistence
- **P&L tracking**: SQLite database with daily snapshots, equity curve, Sharpe, drawdown (`data/fintech.db`)
- **Approvals**: SQLite-backed with auto-expiry (survives process restarts)
- **Agent state**: JSON files with atomic writes (`safe_save_state` prevents corruption) under `workspaces/`
- **Checkpoints**: Pipeline step tracking for crash recovery at `workspaces/orchestrator/checkpoints/` (detects incomplete executions on next start)
- **Pending queue**: `workspaces/execution-agent/pending_execution.json` — decisions made while market was closed
- **Encrypted fields**: trade metadata, payment references, and P&L summaries in `fintech.db` are Fernet-encrypted at rest (requires `DATA_ENCRYPTION_KEY` to decrypt across restarts — see Setup)

### Alerting
Slack/Discord webhook alerts for: pipeline failures, order rejections, reconciliation discrepancies, drawdown warnings, daily P&L summary. Configure `ALERT_WEBHOOK_URL` in `gateway/.env`.

## Setup

```bash
git clone https://github.com/Kymi808/openclaw-fintech.git
cd openclaw-fintech
python cli.py
```

API keys and settings in `gateway/.env`:

```
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # paper trading endpoint
ANTHROPIC_API_KEY=...        # LLM sentiment + research reports
FMP_API_KEY=...              # point-in-time fundamentals ($29/mo)
FRED_API_KEY=...             # economic data (free)
ALERT_WEBHOOK_URL=...        # Slack/Discord alerts
DATA_ENCRYPTION_KEY=...      # Fernet key for at-rest encryption (see below)

# Optional:
PRIMARY_MODEL=lightgbm       # overrides model priority (default: lightgbm)
CS_SYSTEM_PATH=/abs/path     # CS repo location (default: /Users/kylezeng/CS_Multi_Model_Trading_System)
```

**Generating a `DATA_ENCRYPTION_KEY`** (run once, then paste into `.env`):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Without this key, the encryption manager falls back to a process-local ephemeral key on every start, meaning data written today cannot be decrypted tomorrow. The key must be stable across restarts. `.env` is loaded automatically at `skills.orchestrator` package import time so it's visible before any database initialization.

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

1. **No live track record.** The system has zero days of live P&L. Backtest results — even the honest Run 17 numbers above — are not predictive of live performance. Paper trading validation (6+ months) is required before any real capital allocation.

2. **Fundamental look-ahead bias — now mitigated.** FMP is the primary fundamentals source (point-in-time by `filingDate`). yfinance remains as a fallback only. Prior backtest results with yfinance-only fundamentals were inflated by an estimated 2-5% and are superseded.

3. **Single market regime in backtest.** The 2021-2026 period is predominantly bullish. The HMM regime detector has never been tested on a genuine bear market transition. Jan 2022 (-5.1% month) is the only real stress period in the sample and contributed most of the 2022 annual loss (-6.49%).

4. **Model decay 2022-2023.** IC by year: 2022 -0.006, 2023 -0.003, 2024 +0.033, 2025 +0.045. The cross-sectional signal is strongly regime-dependent and produces essentially zero edge at VIX > 25. This is a feature-quality limitation, not a parameter-tuning one — future improvement requires new features, not portfolio tweaks.

5. **CrossMamba macOS incompatibility.** PyTorch's selective scan operation segfaults on Apple Silicon. On Mac the scheduler uses LightGBM only. On Linux production boxes CrossMamba/TST are available as fallbacks but LightGBM (Run 17) is the current primary because the honest audit supports it.

6. **Short leg is a hedge, not alpha.** Standalone short Sharpe over the 5-year sample is -0.22. It contributes +5 to +14% cumulative in the worst 5-10% of long-PnL days, which is why the 0.45L / 0.25S split is kept — removing shorts breaches the <10% MDD target even though it raises absolute return.

7. **Approximate transaction costs.** Using 14 bp round-trip (7 bp/side). Volatility-scaled slippage model has not been calibrated against actual Alpaca fills. Real costs may be 30-50% higher; paper trading will calibrate this.

8. **Consumer Discretionary short exclusion.** Empirically justified (Run 15b diagnostics: CD shorts mean PnL -1.01%, t=-3.20, losing 4/6 years) but is a hardcoded sector blacklist. If the regime shifts (e.g., luxury retailers blow up), the filter may become inappropriate. Revisit in feedback loop.

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
