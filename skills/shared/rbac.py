"""
Role-Based Access Control (RBAC) for the fintech agent team.
Controls who can do what across the system.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .config import get_logger, audit_log

logger = get_logger("rbac")


class Role(Enum):
    ADMIN = "admin"          # Full access to all agents and actions
    TRADER = "trader"        # Can approve trades, view portfolio
    VIEWER = "viewer"        # Read-only access to reports and summaries
    COMPLIANCE = "compliance"  # Legal agent access, audit log access
    OPERATOR = "operator"    # Can manage agents but not execute trades


class Action(Enum):
    # Trading
    EXECUTE_TRADE = "execute_trade"
    APPROVE_TRADE = "approve_trade"
    VIEW_POSITIONS = "view_positions"
    VIEW_MARKET_DATA = "view_market_data"

    # Portfolio
    VIEW_PORTFOLIO = "view_portfolio"
    APPROVE_REBALANCE = "approve_rebalance"
    MODIFY_TARGETS = "modify_targets"

    # DeFi
    EXECUTE_SWAP = "execute_swap"
    APPROVE_SWAP = "approve_swap"
    VIEW_DEFI = "view_defi"
    VOTE_GOVERNANCE = "vote_governance"

    # Finance
    ADD_EXPENSE = "add_expense"
    VIEW_EXPENSES = "view_expenses"
    VIEW_TAX = "view_tax"
    SYNC_BANK = "sync_bank"

    # Legal
    ANALYZE_CONTRACT = "analyze_contract"
    VIEW_SEC_FILINGS = "view_sec_filings"
    RUN_COMPLIANCE_SCAN = "run_compliance_scan"
    VIEW_COMPLIANCE = "view_compliance"

    # System
    VIEW_AUDIT_LOG = "view_audit_log"
    MANAGE_AGENTS = "manage_agents"
    MANAGE_USERS = "manage_users"
    CONFIGURE_LIMITS = "configure_limits"


# Permission matrix: which roles can perform which actions
PERMISSIONS: dict[Role, set[Action]] = {
    Role.ADMIN: set(Action),  # All actions

    Role.TRADER: {
        Action.EXECUTE_TRADE, Action.APPROVE_TRADE, Action.VIEW_POSITIONS,
        Action.VIEW_MARKET_DATA, Action.VIEW_PORTFOLIO, Action.APPROVE_REBALANCE,
        Action.EXECUTE_SWAP, Action.APPROVE_SWAP, Action.VIEW_DEFI,
        Action.VOTE_GOVERNANCE, Action.ADD_EXPENSE, Action.VIEW_EXPENSES,
    },

    Role.VIEWER: {
        Action.VIEW_POSITIONS, Action.VIEW_MARKET_DATA, Action.VIEW_PORTFOLIO,
        Action.VIEW_DEFI, Action.VIEW_EXPENSES, Action.VIEW_TAX,
        Action.VIEW_SEC_FILINGS, Action.VIEW_COMPLIANCE,
    },

    Role.COMPLIANCE: {
        Action.VIEW_POSITIONS, Action.VIEW_PORTFOLIO, Action.VIEW_DEFI,
        Action.VIEW_EXPENSES, Action.VIEW_TAX,
        Action.ANALYZE_CONTRACT, Action.VIEW_SEC_FILINGS,
        Action.RUN_COMPLIANCE_SCAN, Action.VIEW_COMPLIANCE,
        Action.VIEW_AUDIT_LOG,
    },

    Role.OPERATOR: {
        Action.VIEW_POSITIONS, Action.VIEW_MARKET_DATA, Action.VIEW_PORTFOLIO,
        Action.VIEW_DEFI, Action.VIEW_EXPENSES,
        Action.VIEW_AUDIT_LOG, Action.MANAGE_AGENTS,
    },
}


@dataclass
class User:
    user_id: str  # phone number, email, or platform ID
    name: str
    role: Role
    allowed_agents: set[str] = field(default_factory=lambda: {"all"})
    is_active: bool = True


class AccessControl:
    """RBAC enforcement engine."""

    def __init__(self):
        self._users: dict[str, User] = {}
        self._load_defaults()

    def _load_defaults(self):
        """Load default admin user. In production, load from config/DB."""
        # Default admin — must be overridden in production
        self._users["admin"] = User(
            user_id="admin",
            name="System Admin",
            role=Role.ADMIN,
        )

    def add_user(self, user_id: str, name: str, role: Role,
                 allowed_agents: set[str] = None) -> User:
        user = User(
            user_id=user_id,
            name=name,
            role=role,
            allowed_agents=allowed_agents or {"all"},
        )
        self._users[user_id] = user
        audit_log("system", "user_added", {
            "user_id": user_id, "role": role.value,
        })
        return user

    def remove_user(self, user_id: str) -> bool:
        if user_id in self._users:
            del self._users[user_id]
            audit_log("system", "user_removed", {"user_id": user_id})
            return True
        return False

    def get_user(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)

    def check_permission(self, user_id: str, action: Action,
                         agent: str = None) -> bool:
        """Check if a user has permission to perform an action."""
        user = self._users.get(user_id)
        if not user:
            logger.warning(f"Unknown user {user_id} attempted {action.value}")
            return False

        if not user.is_active:
            logger.warning(f"Inactive user {user_id} attempted {action.value}")
            return False

        # Check role permissions
        allowed_actions = PERMISSIONS.get(user.role, set())
        if action not in allowed_actions:
            audit_log("rbac", "permission_denied", {
                "user_id": user_id, "role": user.role.value,
                "action": action.value, "agent": agent,
            })
            logger.warning(
                f"Permission denied: {user_id} ({user.role.value}) → {action.value}"
            )
            return False

        # Check agent-level access
        if agent and "all" not in user.allowed_agents:
            if agent not in user.allowed_agents:
                audit_log("rbac", "agent_access_denied", {
                    "user_id": user_id, "agent": agent,
                })
                return False

        return True

    def require_permission(self, user_id: str, action: Action,
                           agent: str = None) -> None:
        """Check permission and raise if denied."""
        if not self.check_permission(user_id, action, agent):
            raise PermissionError(
                f"User '{user_id}' lacks permission for '{action.value}'"
                + (f" on agent '{agent}'" if agent else "")
            )

    def list_users(self) -> list[dict]:
        return [
            {
                "user_id": u.user_id,
                "name": u.name,
                "role": u.role.value,
                "is_active": u.is_active,
                "agents": list(u.allowed_agents),
            }
            for u in self._users.values()
        ]


# Singleton
access_control = AccessControl()
