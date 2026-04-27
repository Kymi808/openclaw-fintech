"""
Safe JSON state file operations for all agents.

Handles:
- Missing files (returns default)
- Corrupt JSON (returns default, logs warning)
- Atomic writes (write to tmp, then rename — no partial writes)
- Backup on load failure
"""
import json
import shutil
from pathlib import Path

from .config import get_logger

logger = get_logger("state")


def _json_default(obj):
    """Handle numpy types and other non-JSON-serializable objects."""
    import numpy as np
    if isinstance(obj, (np.bool_, np.integer)):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def safe_load_state(path: Path, default: dict = None) -> dict:
    """
    Safely load JSON state from a file.

    Returns default dict if:
    - File doesn't exist
    - File is empty
    - File contains invalid JSON
    - File contains non-dict JSON
    """
    default = default if default is not None else {}

    if not path.exists():
        return dict(default)

    try:
        text = path.read_text().strip()
        if not text:
            logger.warning(f"Empty state file: {path}")
            return dict(default)

        data = json.loads(text)
        if not isinstance(data, dict):
            logger.warning(f"State file is not a dict: {path} (got {type(data).__name__})")
            return dict(default)

        return data

    except json.JSONDecodeError as e:
        logger.error(f"Corrupt state file: {path} — {e}")
        # Backup corrupt file for debugging
        backup = path.with_suffix(".corrupt")
        try:
            shutil.copy2(path, backup)
            logger.info(f"Backed up corrupt state to {backup}")
        except Exception:
            pass
        return dict(default)

    except Exception as e:
        logger.error(f"Failed to load state from {path}: {e}")
        return dict(default)


def safe_save_state(path: Path, state: dict) -> bool:
    """
    Atomically save JSON state to a file.

    Writes to a temp file first, then renames. This prevents
    partial writes from corrupting the state if the process crashes.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, indent=2, default=_json_default))
        tmp_path.rename(path)
        return True
    except Exception as e:
        logger.error(f"Failed to save state to {path}: {e}")
        return False
