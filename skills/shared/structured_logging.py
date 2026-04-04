"""
Structured JSON logging for production.

Replaces basic text logging with JSON-formatted log entries that can be
ingested by ELK, CloudWatch, Datadog, or any log aggregation system.

Usage:
    from skills.shared.structured_logging import setup_structured_logging
    setup_structured_logging()  # call once at startup

Log entries include:
- timestamp (ISO 8601)
- level
- logger name
- message
- Extra fields (symbol, order_id, etc.)
"""
import json
import logging
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields if present
        for key in ("symbol", "order_id", "run_id", "agent", "action",
                     "decision_id", "cycle", "step", "error"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        # Add exception info
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
            }

        return json.dumps(log_entry)


def setup_structured_logging(
    level: int = logging.INFO,
    json_output: bool = True,
    log_file: str = None,
):
    """
    Configure structured logging for the entire application.

    Args:
        level: logging level (INFO, WARNING, etc.)
        json_output: if True, use JSON format; if False, use human-readable
        log_file: optional file path to also write logs to
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Clear existing handlers
    root.handlers.clear()

    if json_output:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler (optional)
    if log_file:
        import os
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(JSONFormatter())  # always JSON for files
        root.addHandler(file_handler)

    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
