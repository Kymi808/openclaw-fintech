from .config import (
    get_logger, audit_log, require_env, mask_sensitive,
    DEFAULT_LIMITS, DEFI_LIMITS,
    ALLOWED_EXCHANGES, ALLOWED_DEFI_PROTOCOLS, ALLOWED_CHAINS, ALLOWED_PAIRS,
)
from .encryption import EncryptionManager
from .approval import approval_engine, ApprovalStatus
from .resilience import (
    retry, RetryExhausted,
    CircuitBreaker, CircuitOpenError, CircuitState,
    RateLimiter, with_timeout,
    binance_circuit, coinbase_circuit, alchemy_circuit,
    exchange_limiter, api_limiter, sec_limiter,
)
from .rbac import access_control, Role, Action
from .health import health_checker
from .metrics import metrics, timed, start_metrics_server
from .session_mapper import session_mapper
from .dead_letter import dlq, FailureType


def get_db():
    """Lazy-load the database singleton to avoid polluting test imports."""
    from .database import db
    return db
