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

## Data Flow (Detailed)

Five stages, each cross-referenced to the actual file paths. Read this when debugging, wiring a new agent, or tracing why a particular prediction ended up as a particular order.

### Stage A — Scheduler loop (30-second tick)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  skills/orchestrator/scheduler.py :: scheduler_loop()                        │
│                                                                              │
│  while running:                                                              │
│      now = datetime.now(ET)                                                  │
│      for task in SCHEDULE:                                                   │
│          if now within 2-min window AND not already fired today:             │
│              await run_scheduled_task(task)                                  │
│      sleep(30)                                                               │
└──────────────────┬───────────────────────────────────────────────────────────┘
                   │
     ┌─────────────┼─────────────┬─────────────┬─────────────┬────────────┐
     ▼             ▼             ▼             ▼             ▼            ▼
  06:30         07:00         09:32         10:00       10:15-15:30   15:45
  check_model   pre_market    execute_      daily_      intraday_     eod_
  _updates      _briefing     pending_      cycle       cycle(s)      close
  (git pull)    (intel only)  at_open       (FULL       (every 15m,   (liquidate
                              (replay)      PIPELINE)   5m in power   intraday)
                                                        hour)
```

### Stage B — `daily_cycle` pipeline

```
skills/orchestrator/pipeline.py :: run_daily_cycle()

 ┌───────────────────────────────────────────────────────────────────────┐
 │  (0) Rate-limit check (MIN_CYCLE_INTERVAL=300s), checkpoint.create()  │
 └───────────────────────────────────────┬───────────────────────────────┘
                                         ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  (1) LOAD PREDICTIONS  →  _load_real_predictions()                    │
 │      tries models in priority order: lightgbm, crossmamba, tst        │
 │      ──►  bridge.generate_predictions("lightgbm")   ── see stage C    │
 │      ──►  predictions: dict[ticker -> score]   (20 entries)           │
 │      _cache_predictions() writes data/cached_predictions.json         │
 └───────────────────────────────────────┬───────────────────────────────┘
                                         ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  (2) INTEL  →  skills/intel/handlers.py :: gather_briefing()          │
 │      macro news + sector news + company news (parallel) →             │
 │      SEC EDGAR filings → FRED economic data →                         │
 │      HMM regime probabilities → VIX, breadth → briefing dict          │
 │      checkpoint.update(INTEL_DONE)                                    │
 └───────────────────────────────────────┬───────────────────────────────┘
                                         ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  (3) ANALYSTS  →  analyst/handlers.py :: form_all_theses()            │
 │      5 personalities run IN PARALLEL (asyncio.gather)                 │
 │      INPUT per analyst: predictions, briefing, portfolio_state        │
 │      OUTPUT per analyst: thesis{conviction, recommended_params,       │
 │                                 risk_flags, reasoning}                │
 │      checkpoint.update(ANALYSTS_DONE)                                 │
 └───────────────────────────────────────┬───────────────────────────────┘
                                         ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  (4) PMs + CIO  →  pm/handlers.py :: resolve()                        │
 │      3 PMs (aggressive/balanced/conservative) each weight the 5       │
 │      analyst theses differently → propose portfolio params            │
 │      CIO applies safety-override hierarchy → picks 1 of 3             │
 │      OUTPUT: decision{decision_id, final_params, resolution,          │
 │                       requires_approval}                              │
 │      checkpoint.update(PM_DONE)                                       │
 └───────────────────────────────────────┬───────────────────────────────┘
                                         ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │  (5) EXECUTION  →  execution/handlers.py :: execute_daily()           │
 │      Session check → place orders OR queue  ── see stage E            │
 │      checkpoint.mark_complete()                                       │
 └───────────────────────────────────────────────────────────────────────┘
```

### Stage C — LightGBM inference (the model layer)

```
skills/signals/bridge.py :: generate_predictions("lightgbm")

 (1) get_signal_generator("lightgbm")
       │
       ├─► _patch_data_loader_for_alpaca()                [monkey-patches]
       │     data_loader.fetch_price_data = alpaca_fetch  (skills/market_data/adapter.py)
       │     data_loader.fetch_cross_asset_data = alpaca_cross
       │     sentiment_features.fetch_news_sentiment = enhanced (LLM-augmented)
       │     data_loader.fetch_fundamental_data = FMP_bulk (if FMP_API_KEY)
       │     data_loader.fetch_earnings_dates = FMP_earnings
       │
       ├─► _load_pickle_compat(models/latest_lightgbm_model.pkl)
       │     returns: {models: [lgb_model_1, lgb_model_2, lgb_model_3],  ← ensemble
       │              feature_names: ["fund_value_composite", ...],      ← 70 features
       │              feature_importance: pd.Series,
       │              config: Config}
       │
       └─► gen.initialize_risk()  (FactorRiskModel with regime detector)

 (2) gen.generate_signals()   →  CS/signal_generator.py :: generate_signals()

     ┌──────────────────────────────────────────────────────────────┐
     │ a. UNIVERSE                                                   │
     │    tickers = get_universe(cfg)  ← sp500_pit (PIT-correct)    │
     │    list(dict.fromkeys(tickers))  ← dedupe (fix from 46ec2b6) │
     │    → 491 unique tickers                                       │
     ├──────────────────────────────────────────────────────────────┤
     │ b. PRICES                                                     │
     │    fetch_price_data(tickers)  ← patched Alpaca adapter        │
     │    → prices: DataFrame[date × ticker], 8 years of daily bars │
     │    filter_universe_by_liquidity  → ~210 liquid tickers        │
     ├──────────────────────────────────────────────────────────────┤
     │ c. SECTORS                                                    │
     │    load_sector_map(tickers)  ← Wikipedia + cache              │
     │    → {AAPL: "Information Technology", ...}                    │
     ├──────────────────────────────────────────────────────────────┤
     │ d. FUNDAMENTALS (Point-in-time via FMP)                       │
     │    fetch_bulk_fundamentals(tickers, FMP_KEY)                  │
     │    → {ticker: [{filingDate, PE, PB, ROE, ...}, ...]}          │
     │    fetch_earnings_dates → next earnings per ticker            │
     ├──────────────────────────────────────────────────────────────┤
     │ e. FEATURES (build_fundamental_features + pv + cross-asset)   │
     │    70 selected features per ticker per date                   │
     │    Examples: fund_cs_rank_earnings_yield, pv_cs_mom_126d,     │
     │              quality_shareholder_yield, pv_cs_vol_5d          │
     │    → X_latest: DataFrame[ticker × 70 features]                │
     ├──────────────────────────────────────────────────────────────┤
     │ f. DATA QUALITY GATE                                          │
     │    if any feature >50% NaN → warn                             │
     │    if overall >30% NaN → RuntimeError (refuse to predict)     │
     ├──────────────────────────────────────────────────────────────┤
     │ g. PREDICT  →  model.py :: EnsembleRanker.predict(X_latest)   │
     │    preds = mean over 3 LightGBM ensemble models               │
     │    → pd.Series[ticker -> score], ~210 entries                 │
     ├──────────────────────────────────────────────────────────────┤
     │ h. RISK MODEL                                                 │
     │    FactorRiskModel.estimate(prices, fundamentals)             │
     │    update_regime(prices) → HMM bull/side/bear probs           │
     ├──────────────────────────────────────────────────────────────┤
     │ i. PORTFOLIO CONSTRUCTION                                     │
     │    PortfolioConstructor.construct_portfolio(                  │
     │      predictions, date, prev_weights, vol_estimates           │
     │    )                                                          │
     │    - rank preds cross-sectionally                             │
     │    - filter short universe (CD excluded — Run 17)             │
     │      mcap>$5B, EY>0, vol<0.30, sector not in blacklist        │
     │    - hysteresis against prev_weights (no thrash)              │
     │    - DD circuit breaker (-3% threshold → 0.50 floor)          │
     │    - sector cap, vol scaling, dust filter                     │
     │    → target_weights: pd.Series[ticker -> weight]              │
     └──────────────────────────────────────────────────────────────┘

 (3) target_weights.to_dict() → returns to bridge.py → returns to pipeline.py
     predictions dict[ticker -> weight ∈ [-0.03, +0.03]]
     20 entries (10 long, 10 short after max_positions_long/short filter
                 overridden by pm_params from the agent layer)
```

### Stage D — Agent debate (Intel → 5 Analysts → 3 PMs → CIO)

```
                    ┌─────────────────────────────────────┐
                    │   predictions (from stage C)        │
                    │   + briefing (from stage B)         │
                    └──────┬──────────────────────────────┘
                           │
               ┌───────────┼───────────┬───────────┬───────────┐
               │           │           │           │           │
          asyncio.gather — 5 analyst personalities in parallel
               │           │           │           │           │
               ▼           ▼           ▼           ▼           ▼
         ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌─────────┐
         │Momentum │ │  Value  │ │  Macro  │ │Sentiment │ │  Risk   │
         │w=0.35   │ │w=0.20   │ │w=0.30   │ │w=0.40    │ │w=0.30   │
         │dispersn │ │credit   │ │VIX      │ │sentiment │ │drawdown │
         │+0.30    │ │+0.20    │ │+0.25    │ │+0.20     │ │+0.30    │
         │breadth  │ │dd       │ │credit   │ │breadth   │ │VIX      │
         └────┬────┘ └────┬────┘ └────┬────┘ └─────┬────┘ └────┬────┘
              │           │           │            │           │
        analyst/scoring.py :: personality_conviction(6 base signals × weights)
              │           │           │            │           │
              ▼           ▼           ▼            ▼           ▼
    thesis{conviction ∈ [0,1], recommended_params{n_long, n_short, lev},
           risk_flags, reasoning}
              │           │           │            │           │
              └───────────┴───────────┼────────────┴───────────┘
                                      │
                           5 theses flow into:
                                      │
                                      ▼
      pm/handlers.py :: resolve()  →  3 PMs each compute proposal
                                      │
          ┌───────────────────┬───────┴──────┬────────────────┐
          ▼                   ▼              ▼                │
     ┌─────────┐         ┌─────────┐   ┌──────────────┐       │
     │Aggrssv  │         │Balanced │   │Conservative  │       │
     │mom 0.30 │         │eq 0.20  │   │risk 0.35     │       │
     │sent 0.30│         │(each)   │   │macro 0.25    │       │
     │×1.2 bias│         │×1.0     │   │×0.8 bias     │       │
     └────┬────┘         └────┬────┘   └──────┬───────┘       │
          │                   │               │               │
     pm_proposal:         pm_proposal:    pm_proposal:        │
     {n_long, n_short,    {...}           {...}               │
      leverage, vol_tgt,                                      │
      turnover_cap}                                           │
          │                   │               │               │
          └───────────────────┼───────────────┘               │
                              ▼                               │
                   pm/resolution.py :: CIO selects            │
                   hierarchy:                                 │
                   1. Safety: VIX>35 or DD>10% → FORCE cons   │
                   2. HMM: if conf>60% → regime-matched PM    │
                   3. VIX/breadth heuristic                   │
                   4. Default balanced                        │
                              │                               │
                              ▼                               │
            decision {decision_id: "PMD-0000XX",              │
                      final_params: {...},                    │
                      resolution: {selected_pm, rationale}}   │
                              │                               │
                              └───────────────────────────────┘
                                          │
                                          ▼
                            passed to execute_daily()  — stage E
```

### Stage E — Execution path (queue OR place orders)

```
skills/execution/handlers.py :: execute_daily(decision, predictions)

            ┌─────────────────────────────────────────────┐
            │  session = get_session()   (session.py)     │
            └────────────────┬────────────────────────────┘
                             │
           ┌─────────────────┴─────────────────┐
           │ CLOSED or AFTER_HOURS             │ OPEN / CLOSING / PRE_MARKET
           ▼                                   ▼
 ┌──────────────────────┐          ┌───────────────────────────────┐
 │ _save_pending_       │          │ build target portfolio:       │
 │ execution()          │          │   equity = account_equity     │
 │                      │          │   target_gross = eq × lev     │
 │ writes:              │          │   top N / bottom M from preds │
 │  workspaces/         │          │   equal-weight within leg     │
 │  execution-agent/    │          │                               │
 │  pending_execution.  │          │ diff vs current_positions     │
 │  json                │          │ dust filter (<$500 or <1% eq) │
 │                      │          │ turnover cap (scale if over)  │
 │ payload:             │          │                               │
 │  {saved_at,          │          │ for each trade:               │
 │   decision,          │          │   VWAP-split if large         │
 │   predictions}       │          │   _place_order(Alpaca):       │
 │                      │          │     POST /v2/orders           │
 │ return {status:      │          │     type=market, TIF=day      │
 │  queued_for_open}    │          │                               │
 │                      │          │ update state.json:            │
 │                      │          │   daily_turnover_used         │
 │                      │          │   overnight_positions         │
 │                      │          │   pdt_day_trade_count         │
 │                      │          │                               │
 │                      │          │ audit_log('daily_executed')   │
 │                      │          │ return ExecutionReport        │
 └──────────┬───────────┘          └──────────────┬────────────────┘
            │                                     │
   ──────── sits on disk ─────                    │
   until next 09:32 ET                            │
            │                                     │
 ┌──────────▼──────────────────────────────┐      │
 │ 09:32 ET: scheduler fires               │      │
 │ execute_pending_at_open():              │      │
 │   _load_pending_execution()             │      │
 │   if age > 72h: discard + clear         │      │
 │   else if market not open yet: bail     │      │
 │   else:                                 │      │
 │     execute_daily(decision, predictions)│──────┘
 │     clear pending file on success       │
 └─────────────────────────────────────────┘
```

### Data shapes at each boundary

| Hop | Shape | Example |
|---|---|---|
| Bridge → Pipeline | `dict[ticker → weight]` | `{"AAPL": 0.008, "CRM": -0.006, ...}` (20 entries) |
| Pipeline → Analysts | `predictions: dict`, `briefing: dict`, `portfolio_state: dict` | briefing={vix: 28.6, breadth: 0.62, hmm_bull: 0.35, ...} |
| Analyst → PM | 5× `thesis` | `{"momentum": {conviction: 0.68, recommended_params: {...}}, ...}` |
| PM → CIO → Exec | `decision` | `{decision_id: "PMD-000003", final_params: {n_long: 6, n_short: 4, max_gross_leverage: 0.77, ...}}` |
| Exec → Alpaca | `order` | `{symbol: "META", notional: 85000.0, side: "buy", type: "market", TIF: "day"}` |
| Exec → disk (closed market) | `pending_execution.json` | `{saved_at: ISO, decision: {...}, predictions: {...}}` |

### State files that persist across scheduler restarts

```
openclaw-fintech/
├── workspaces/
│   ├── execution-agent/
│   │   ├── state.json              ← account equity, positions, PDT
│   │   └── pending_execution.json  ← the queue (closed-market decisions)
│   └── orchestrator/checkpoints/
│       └── daily-YYYYMMDD-HHMMSS.json  ← every pipeline run's step-by-step state
├── data/
│   ├── fintech.db                  ← SQLite: P&L, approvals, encrypted trade meta
│   └── cached_predictions.json     ← last daily predictions (for intraday reuse)
└── logs/
    ├── audit.jsonl                 ← every agent decision, every order
    └── app_YYYY-MM-DD.log
```

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
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp gateway/.env.example gateway/.env
python cli.py
```

API keys and settings in `gateway/.env`:

```
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # paper trading endpoint
TRADING_ENV=paper             # refuses synthetic market data in scheduler
ANTHROPIC_API_KEY=...        # LLM sentiment + research reports
FMP_API_KEY=...              # point-in-time fundamentals ($29/mo)
FRED_API_KEY=...             # economic data (free)
ALERT_WEBHOOK_URL=...        # Slack/Discord alerts
DATA_ENCRYPTION_KEY=...      # Fernet key for at-rest encryption (see below)

# Optional:
PRIMARY_MODEL=lightgbm       # overrides model priority (default: lightgbm)
CS_SYSTEM_PATH=/abs/path     # required path to CS_Multi_Model_Trading_System for real predictions
ALLOW_DUMMY_PREDICTIONS=1    # local smoke tests only; never set in paper/live deployments
```

**Generating a `DATA_ENCRYPTION_KEY`** (run once, then paste into `.env`):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Without this key, the encryption manager falls back to a process-local ephemeral key on every start, meaning data written today cannot be decrypted tomorrow. The key must be stable across restarts. `.env` is loaded automatically at `skills.orchestrator` package import time so it's visible before any database initialization.

The trading pipeline fails closed if the external CS model repo or trained model artifacts are not available. Set `CS_SYSTEM_PATH` to the model repository before running the scheduler. `ALLOW_DUMMY_PREDICTIONS=1` exists only for local smoke tests and should never be used in paper-trading or production deployments.

Docker deployment with the external model repo mounted read-only:
```bash
CS_SYSTEM_PATH_HOST=/absolute/path/to/CS_Multi_Model_Trading_System \
  docker compose -f docker-compose.yml -f docker-compose.models.yml \
  up -d --build trading-scheduler
```

See `HANDOFF.md` for the release checklist, supported surface, runtime-state policy, and security
handoff notes.

## Testing

```bash
python -m ruff check .
python -m pytest
python -m compileall cli.py gateway_bot.py skills tests
docker compose config --services
docker compose -f docker/docker-compose.yaml config --services
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
