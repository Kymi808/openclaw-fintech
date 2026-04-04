"""
Pipeline checkpointing for crash recovery.

Each pipeline run creates a checkpoint file that records:
- Run ID (unique per run)
- Current step (intel, analysts, pm, execution)
- Intermediate results (briefing, theses, decision)
- Timestamp of each step completion

On restart, the pipeline checks for incomplete runs and can:
- Resume from the last completed step
- Detect if execution was partially completed (to avoid double-trading)
- Clean up stale runs older than a threshold

This prevents the most dangerous crash scenario:
"PM decision made, 5 of 10 orders executed, process died, restart re-runs
the entire pipeline and tries to execute 10 new orders → now 15 positions."
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from skills.shared import get_logger

logger = get_logger("orchestrator.checkpoint")

CHECKPOINT_DIR = Path("./workspaces/orchestrator/checkpoints")
STALE_THRESHOLD_HOURS = 4  # checkpoints older than this are cleaned up


class PipelineStep(Enum):
    STARTED = "started"
    INTEL_DONE = "intel_done"
    ANALYSTS_DONE = "analysts_done"
    PM_DONE = "pm_done"
    APPROVED = "approved"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_DONE = "execution_done"
    COMPLETED = "completed"
    FAILED = "failed"


# Steps that are safe to re-run (idempotent / read-only)
SAFE_TO_RERUN = {PipelineStep.STARTED, PipelineStep.INTEL_DONE, PipelineStep.ANALYSTS_DONE}

# Steps where re-running is DANGEROUS (would cause double-trading)
DANGEROUS_TO_RERUN = {PipelineStep.EXECUTION_STARTED}


@dataclass
class Checkpoint:
    run_id: str
    cycle_type: str  # "daily" or "intraday"
    current_step: str
    started_at: str
    updated_at: str
    briefing: dict = field(default_factory=dict)
    analyst_theses: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    execution_result: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class CheckpointManager:
    """Manages pipeline checkpoints for crash recovery."""

    def __init__(self, checkpoint_dir: str = None):
        self.dir = Path(checkpoint_dir) if checkpoint_dir else CHECKPOINT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def create(self, run_id: str, cycle_type: str = "daily") -> Checkpoint:
        """Create a new checkpoint for a pipeline run."""
        now = datetime.now(timezone.utc).isoformat()
        cp = Checkpoint(
            run_id=run_id,
            cycle_type=cycle_type,
            current_step=PipelineStep.STARTED.value,
            started_at=now,
            updated_at=now,
        )
        self._save(cp)
        return cp

    def update(self, run_id: str, step: PipelineStep, **data) -> Checkpoint:
        """Update checkpoint with completed step and intermediate data."""
        cp = self.load(run_id)
        if not cp:
            raise ValueError(f"Checkpoint {run_id} not found")

        cp.current_step = step.value
        cp.updated_at = datetime.now(timezone.utc).isoformat()

        for key, val in data.items():
            if hasattr(cp, key):
                setattr(cp, key, val)

        self._save(cp)
        return cp

    def load(self, run_id: str) -> Optional[Checkpoint]:
        """Load a checkpoint."""
        path = self._path(run_id)
        if not path.exists():
            return None
        return Checkpoint.from_dict(json.loads(path.read_text()))

    def get_incomplete(self) -> list[Checkpoint]:
        """Find incomplete pipeline runs (not completed or failed)."""
        incomplete = []
        for f in self.dir.glob("*.json"):
            try:
                cp = Checkpoint.from_dict(json.loads(f.read_text()))
                if cp.current_step not in (PipelineStep.COMPLETED.value, PipelineStep.FAILED.value):
                    incomplete.append(cp)
            except Exception:
                continue
        return incomplete

    def can_resume(self, cp: Checkpoint) -> tuple[bool, str]:
        """
        Check if an incomplete run can safely be resumed.

        Returns (can_resume, reason).
        """
        step = PipelineStep(cp.current_step)

        # If execution started but didn't finish, it's DANGEROUS
        if step in DANGEROUS_TO_RERUN:
            return False, (
                f"Run {cp.run_id} was in {step.value} when it crashed. "
                f"Orders may have been partially executed. "
                f"Manual reconciliation required before resuming."
            )

        # Safe steps can be re-run
        if step in SAFE_TO_RERUN:
            return True, f"Can resume from {step.value} (safe to re-run)"

        # PM done but not yet approved/executed — safe to resume from PM
        if step == PipelineStep.PM_DONE:
            return True, "PM decision exists, can proceed to approval/execution"

        if step == PipelineStep.APPROVED:
            return True, "Approved but not yet executed, can proceed to execution"

        return False, f"Unknown step: {step.value}"

    def mark_complete(self, run_id: str, result: dict = None):
        """Mark a run as successfully completed."""
        self.update(run_id, PipelineStep.COMPLETED, execution_result=result or {})

    def mark_failed(self, run_id: str, error: str):
        """Mark a run as failed."""
        self.update(run_id, PipelineStep.FAILED, error=error)

    def cleanup_stale(self, max_age_hours: int = None):
        """Remove checkpoints older than threshold."""
        max_age = max_age_hours or STALE_THRESHOLD_HOURS
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)

        for f in self.dir.glob("*.json"):
            try:
                cp = Checkpoint.from_dict(json.loads(f.read_text()))
                updated = datetime.fromisoformat(cp.updated_at)
                if updated < cutoff and cp.current_step in (
                    PipelineStep.COMPLETED.value, PipelineStep.FAILED.value
                ):
                    f.unlink()
                    logger.debug(f"Cleaned up checkpoint {cp.run_id}")
            except Exception:
                continue

    def _save(self, cp: Checkpoint):
        import numpy as np

        def _default(obj):
            if isinstance(obj, (np.bool_, np.integer)):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

        self._path(cp.run_id).write_text(json.dumps(cp.to_dict(), indent=2, default=_default))


def generate_run_id(cycle_type: str = "daily") -> str:
    """Generate a unique run ID based on date and cycle type."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M%S")
    return f"{cycle_type}-{date_str}-{time_str}"
