"""
Production scheduler for daily and intraday trading cycles.

Schedule (all times ET):
  06:00  Pre-market briefing (intel only, no trades)
  09:35  Daily cycle (full debate + trade)
  10:00  Intraday cycle (every 30 min)
  ...
  15:30  Last intraday cycle
  15:45  EOD mandatory close of intraday positions
  16:15  Daily P&L snapshot + reconciliation

Run as: PYTHONPATH=. python -m skills.orchestrator.scheduler
"""
import asyncio
import signal
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

from skills.shared import get_logger

logger = get_logger("orchestrator.scheduler")

ET = ZoneInfo("America/New_York")

# Schedule definitions (hour, minute) in ET
# Modeled after institutional equity trading desk operations
SCHEDULE = {
    # Pre-market: check for retrained models from GitHub Actions
    "check_model_updates": time(6, 30),
    # Pre-market: scan overnight news, gaps, macro events
    "pre_market_briefing": time(7, 0),
    # Opening: wait for opening range to form (9:30-10:00 is noise)
    "daily_cycle": time(10, 0),
    # Intraday: active scanning window
    "intraday_start": time(10, 15),
    "intraday_end": time(15, 30),
    # Power hour: increased scanning frequency (3:00-3:30)
    "power_hour_start": time(15, 0),
    # EOD: staged close — begin at 3:30, aggressive by 3:45, no new trades by 3:55
    "eod_begin_close": time(15, 30),
    "eod_close": time(15, 45),
    # Post-close: reconcile, P&L, prepare next-day catalyst list
    "daily_pnl": time(16, 5),
    "weekly_report": time(16, 15),  # Fridays only
    "feedback_loop": time(16, 30),
    "intraday_model_update": time(17, 0),
}

# Scanning frequency (minutes)
INTRADAY_INTERVAL_MINUTES = 15       # normal: scan every 15 min
POWER_HOUR_INTERVAL_MINUTES = 5      # power hour (3:00-3:30): every 5 min


async def run_scheduled_task(task_name: str):
    """Execute a scheduled task by name."""
    try:
        if task_name == "pre_market_briefing":
            from skills.intel.handlers import pre_market_briefing
            result = await pre_market_briefing()
            logger.info(f"Pre-market briefing:\n{result}")

        elif task_name == "daily_cycle":
            from skills.orchestrator.pipeline import run_daily_cycle
            result = await run_daily_cycle()
            logger.info(f"Daily cycle: {result.get('status')}")

        elif task_name == "intraday_cycle":
            from skills.orchestrator.pipeline import run_intraday_cycle
            result = await run_intraday_cycle()
            logger.info(f"Intraday cycle: {result.get('status')}")

        elif task_name == "eod_close":
            from skills.execution.handlers import close_intraday_positions
            result = await close_intraday_positions()
            logger.info(f"EOD close: {result.get('status')}")

        elif task_name == "daily_pnl":
            from skills.pnl.reconciliation import reconcile_positions, format_reconciliation_report
            from skills.pnl.tracker import get_pnl_tracker
            report = await reconcile_positions()
            logger.info(format_reconciliation_report(report))
            logger.info("Daily P&L snapshot recorded")

        elif task_name == "weekly_report":
            from skills.research.report import generate_weekly_report
            report = await generate_weekly_report()
            logger.info(f"Weekly report generated ({len(report)} chars)")

        elif task_name == "feedback_loop":
            from skills.feedback.loop import run_feedback_loop
            result = await run_feedback_loop()
            logger.info(f"Feedback loop: {result.get('n_agents_scored', 0)} agents scored")

        elif task_name == "intraday_model_update":
            from skills.intraday.model.predictor import get_intraday_predictor
            predictor = get_intraday_predictor()
            n_samples = await predictor.collect_training_data()
            logger.info(f"Collected {n_samples} intraday training samples")
            summary = predictor.train()
            logger.info(f"Intraday model: {summary.get('status')}")

        elif task_name == "check_model_updates":
            # Pull latest CrossMamba/LightGBM models from GitHub
            # (GitHub Actions retrains every 14 days and pushes new .pkl files)
            import subprocess
            cs_path = os.environ.get(
                "CS_SYSTEM_PATH",
                os.path.join(os.path.dirname(__file__), "..", "..", "..",
                             "CS_Multi_Model_Trading_System"),
            )
            if os.path.exists(os.path.join(cs_path, ".git")):
                result = subprocess.run(
                    ["git", "pull", "--ff-only"],
                    cwd=cs_path, capture_output=True, text=True, timeout=30,
                )
                if "Already up to date" not in result.stdout:
                    logger.info(f"Model update pulled: {result.stdout.strip()}")
                    # Clear cached generators so new models are loaded
                    from skills.signals.bridge import _generators
                    _generators.clear()
                    logger.info("Model cache cleared — new models will load on next cycle")
                else:
                    logger.debug("No model updates available")

        else:
            logger.warning(f"Unknown task: {task_name}")

    except Exception as e:
        logger.error(f"Scheduled task {task_name} failed: {e}")
        from skills.shared.alerting import alert_pipeline_failure
        await alert_pipeline_failure(task_name, "scheduler", str(e))


async def scheduler_loop():
    """
    Main scheduler loop. Checks every 30 seconds if a task should run.

    Uses a simple "last run" tracking to avoid double-execution.
    """
    last_run: dict[str, str] = {}  # task -> last run date+time
    logger.info("Scheduler started")

    running = True

    def _stop(sig, frame):
        nonlocal running
        logger.info(f"Scheduler stopping (signal {sig})")
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while running:
        now = datetime.now(ET)
        today = now.date().isoformat()
        current_time = now.time()

        # Skip weekends
        if now.weekday() >= 5:
            await asyncio.sleep(60)
            continue

        # Check each scheduled task
        for task_name, scheduled_time in SCHEDULE.items():
            key = f"{task_name}:{today}"

            # Skip if already run today
            if key in last_run:
                continue

            # Check if it's time (within 2 minutes of scheduled time)
            scheduled_minutes = scheduled_time.hour * 60 + scheduled_time.minute
            current_minutes = current_time.hour * 60 + current_time.minute
            if 0 <= current_minutes - scheduled_minutes < 2:
                logger.info(f"Running scheduled task: {task_name}")
                await run_scheduled_task(task_name)
                last_run[key] = now.isoformat()

        # Intraday scanning: 15-min intervals normal, 5-min during power hour
        if SCHEDULE["intraday_start"] <= current_time <= SCHEDULE["intraday_end"]:
            # Power hour (3:00-3:30): scan every 5 min
            if SCHEDULE["power_hour_start"] <= current_time <= SCHEDULE["eod_begin_close"]:
                interval = POWER_HOUR_INTERVAL_MINUTES
                label = "power_hour"
            else:
                interval = INTRADAY_INTERVAL_MINUTES
                label = "intraday"

            intraday_key = f"{label}:{today}:{current_time.hour}:{current_time.minute // interval}"
            if intraday_key not in last_run:
                if current_time.minute % interval < 2:
                    logger.info(f"Running {label} scan (every {interval}min)")
                    await run_scheduled_task("intraday_cycle")
                    last_run[intraday_key] = now.isoformat()

        # Weekly report: Fridays only at 4:15 PM
        if now.weekday() == 4:  # Friday
            weekly_key = f"weekly_report:{today}"
            if weekly_key not in last_run:
                if SCHEDULE["weekly_report"] <= current_time < time(current_time.hour, current_time.minute + 2):
                    logger.info("Running weekly research report (Friday)")
                    await run_scheduled_task("weekly_report")
                    last_run[weekly_key] = now.isoformat()

        await asyncio.sleep(30)

    logger.info("Scheduler stopped")


def main():
    """Entry point for the scheduler."""
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    from dotenv import load_dotenv
    load_dotenv("gateway/.env")

    logger.info("Starting OpenClaw trading scheduler")
    logger.info(f"Schedule: {SCHEDULE}")

    asyncio.run(scheduler_loop())


if __name__ == "__main__":
    main()
