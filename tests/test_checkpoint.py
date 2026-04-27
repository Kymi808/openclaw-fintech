"""
Tests for pipeline checkpoint and crash recovery.
"""
import tempfile
from skills.orchestrator.checkpoint import (
    CheckpointManager, PipelineStep, generate_run_id,
)


class TestCheckpointManager:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = CheckpointManager(checkpoint_dir=self.tmpdir)

    def test_create_checkpoint(self):
        cp = self.mgr.create("run-001", "daily")
        assert cp.run_id == "run-001"
        assert cp.current_step == "started"
        assert cp.cycle_type == "daily"

    def test_update_checkpoint(self):
        self.mgr.create("run-001")
        cp = self.mgr.update("run-001", PipelineStep.INTEL_DONE, briefing={"vix": 20})
        assert cp.current_step == "intel_done"
        assert cp.briefing["vix"] == 20

    def test_load_checkpoint(self):
        self.mgr.create("run-001")
        self.mgr.update("run-001", PipelineStep.ANALYSTS_DONE)
        cp = self.mgr.load("run-001")
        assert cp.current_step == "analysts_done"

    def test_get_incomplete(self):
        self.mgr.create("run-001")
        self.mgr.update("run-001", PipelineStep.INTEL_DONE)

        self.mgr.create("run-002")
        self.mgr.update("run-002", PipelineStep.COMPLETED)

        incomplete = self.mgr.get_incomplete()
        assert len(incomplete) == 1
        assert incomplete[0].run_id == "run-001"

    def test_can_resume_safe_steps(self):
        self.mgr.create("run-001")
        self.mgr.update("run-001", PipelineStep.INTEL_DONE)
        cp = self.mgr.load("run-001")

        ok, reason = self.mgr.can_resume(cp)
        assert ok is True

    def test_cannot_resume_during_execution(self):
        self.mgr.create("run-001")
        self.mgr.update("run-001", PipelineStep.EXECUTION_STARTED)
        cp = self.mgr.load("run-001")

        ok, reason = self.mgr.can_resume(cp)
        assert ok is False
        assert "manual reconciliation" in reason.lower()

    def test_can_resume_after_pm(self):
        self.mgr.create("run-001")
        self.mgr.update("run-001", PipelineStep.PM_DONE, decision={"n_long": 10})
        cp = self.mgr.load("run-001")

        ok, reason = self.mgr.can_resume(cp)
        assert ok is True

    def test_mark_complete(self):
        self.mgr.create("run-001")
        self.mgr.mark_complete("run-001", {"orders": 5})
        cp = self.mgr.load("run-001")
        assert cp.current_step == "completed"

    def test_mark_failed(self):
        self.mgr.create("run-001")
        self.mgr.mark_failed("run-001", "Network error")
        cp = self.mgr.load("run-001")
        assert cp.current_step == "failed"
        assert cp.error == "Network error"

    def test_generate_run_id(self):
        rid = generate_run_id("daily")
        assert rid.startswith("daily-")
        assert len(rid) > 10

    def test_load_nonexistent(self):
        cp = self.mgr.load("nonexistent")
        assert cp is None
