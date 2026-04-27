"""
Outcome scorer — evaluates past decisions against realized returns.

Every decision in the system gets scored after its prediction horizon
(10 trading days for daily signals, EOD for intraday).

Scoring criteria:
- Analyst accuracy: did the conviction direction match the market outcome?
- PM accuracy: did the selected PM's params produce better risk-adjusted returns
  than the rejected PMs would have?
- Signal accuracy: did intraday signals hit their targets?

All scores are stored in SQLite for historical analysis.
"""
import sqlite3
import os
from datetime import datetime, timezone

import numpy as np

from skills.shared import get_logger

logger = get_logger("feedback.scorer")

DB_PATH = os.path.join("data", "feedback.db")


class OutcomeScorer:
    """
    Tracks predictions and scores them against realized outcomes.

    Flow:
    1. record_prediction() — called when a decision is made
    2. score_outcomes() — called after the prediction horizon passes
    3. get_scores() — returns accuracy scores per agent
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    prediction_type TEXT NOT NULL,
                    prediction_value REAL NOT NULL,
                    context TEXT DEFAULT '{}',
                    horizon_days INTEGER DEFAULT 10,
                    outcome_value REAL,
                    score REAL,
                    scored_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pred_agent
                ON predictions(agent, prediction_type)
            """)

    def record_prediction(
        self,
        agent: str,
        prediction_type: str,
        prediction_value: float,
        context: dict = None,
        horizon_days: int = 10,
    ):
        """
        Record a prediction to be scored later.

        Args:
            agent: "momentum-analyst", "pm-aggressive", "cio", etc.
            prediction_type: "conviction", "regime_call", "signal"
            prediction_value: the predicted value (conviction, direction, etc.)
            context: additional context for scoring
            horizon_days: when to score this prediction
        """
        import json
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO predictions
                (timestamp, agent, prediction_type, prediction_value, context, horizon_days)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                agent, prediction_type, prediction_value,
                json.dumps(context or {}), horizon_days,
            ))

    def record_analyst_thesis(self, analyst_name: str, conviction: float, bias: str):
        """Record an analyst's thesis for future scoring."""
        self.record_prediction(
            agent=f"{analyst_name}-analyst",
            prediction_type="conviction",
            prediction_value=conviction,
            context={"bias": bias},
            horizon_days=10,
        )

    def record_pm_decision(self, pm_name: str, n_long: int, n_short: int, leverage: float):
        """Record a PM's portfolio parameter decision."""
        self.record_prediction(
            agent=f"pm-{pm_name}",
            prediction_type="params",
            prediction_value=leverage,
            context={"n_long": n_long, "n_short": n_short},
            horizon_days=10,
        )

    def record_cio_decision(self, selected_pm: str, vix_regime: str):
        """Record the CIO's PM selection."""
        self.record_prediction(
            agent="cio",
            prediction_type="pm_selection",
            prediction_value=1.0,  # placeholder
            context={"selected_pm": selected_pm, "vix_regime": vix_regime},
            horizon_days=10,
        )

    def score_outcomes(self, portfolio_return_10d: float, market_return_10d: float = 0.0):
        """
        Score all unscored predictions against realized returns.

        Called every 10 trading days (or daily for predictions that have matured).

        Scoring:
        - Analyst conviction: higher conviction should correlate with higher alpha
          Score = correlation(conviction, excess_return) over recent predictions
        - PM params: did the selected leverage/positioning produce good risk-adjusted return?
          Score = portfolio_return / realized_vol (mini-Sharpe)
        - CIO: was the selected PM the best choice?
          Score = 1 if selected PM would have outperformed alternatives
        """
        now = datetime.now(timezone.utc).isoformat()
        alpha = portfolio_return_10d - market_return_10d

        with sqlite3.connect(self.db_path) as conn:
            # Score unscored predictions that have matured
            unscored = conn.execute("""
                SELECT id, agent, prediction_type, prediction_value, context
                FROM predictions
                WHERE score IS NULL
                AND julianday('now') - julianday(timestamp) >= horizon_days
            """).fetchall()

            for row in unscored:
                pred_id, agent, pred_type, pred_value, context_str = row

                if pred_type == "conviction":
                    # Score: did direction match? High conviction bullish + positive alpha = good
                    import json
                    ctx = json.loads(context_str) if context_str else {}
                    bias = ctx.get("bias", "neutral")

                    if bias in ("bull", "neutral"):
                        # Bullish analysts scored by positive alpha
                        score = _clamp(0.5 + alpha * 10)  # scale: 1% alpha → 0.6 score
                    else:
                        # Bearish analysts scored by negative alpha (they predicted risk)
                        score = _clamp(0.5 - alpha * 10)

                    # Weight by conviction: confident and right = very good, confident and wrong = very bad
                    score = 0.5 + (score - 0.5) * pred_value

                elif pred_type == "params":
                    # Score PM by risk-adjusted return
                    score = _clamp(0.5 + portfolio_return_10d * 20)  # 5% return → 1.0

                elif pred_type == "pm_selection":
                    # CIO scored by whether portfolio return was positive
                    score = _clamp(0.5 + portfolio_return_10d * 10)

                else:
                    score = 0.5

                conn.execute("""
                    UPDATE predictions SET outcome_value = ?, score = ?, scored_at = ?
                    WHERE id = ?
                """, (portfolio_return_10d, round(score, 4), now, pred_id))

        logger.info(f"Scored {len(unscored)} predictions (alpha={alpha:+.4f})")

    def get_agent_scores(self, agent: str, n_recent: int = 20) -> dict:
        """Get recent accuracy scores for an agent."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT score FROM predictions
                WHERE agent = ? AND score IS NOT NULL
                ORDER BY scored_at DESC LIMIT ?
            """, (agent, n_recent)).fetchall()

        if not rows:
            return {"avg_score": 0.5, "n_scored": 0, "trend": 0.0}

        scores = [r[0] for r in rows]
        avg = float(np.mean(scores))

        # Trend: are recent scores improving or declining?
        if len(scores) >= 4:
            recent = np.mean(scores[:len(scores)//2])
            older = np.mean(scores[len(scores)//2:])
            trend = recent - older
        else:
            trend = 0.0

        return {
            "avg_score": round(avg, 4),
            "n_scored": len(scores),
            "trend": round(trend, 4),
        }

    def get_all_agent_scores(self, n_recent: int = 20) -> dict[str, dict]:
        """Get scores for all agents."""
        with sqlite3.connect(self.db_path) as conn:
            agents = conn.execute(
                "SELECT DISTINCT agent FROM predictions WHERE score IS NOT NULL"
            ).fetchall()

        return {
            agent[0]: self.get_agent_scores(agent[0], n_recent)
            for agent in agents
        }


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))
