"""Tests for role-based access control."""
import pytest
from skills.shared.rbac import Role, Action


class TestRBAC:
    def test_admin_has_all_permissions(self, access_control):
        # Default admin exists
        for action in Action:
            assert access_control.check_permission("admin", action) is True

    def test_trader_permissions(self, access_control):
        access_control.add_user("trader1", "Test Trader", Role.TRADER)

        assert access_control.check_permission("trader1", Action.EXECUTE_TRADE) is True
        assert access_control.check_permission("trader1", Action.APPROVE_TRADE) is True
        assert access_control.check_permission("trader1", Action.VIEW_PORTFOLIO) is True
        assert access_control.check_permission("trader1", Action.MANAGE_USERS) is False
        assert access_control.check_permission("trader1", Action.VIEW_AUDIT_LOG) is False

    def test_viewer_readonly(self, access_control):
        access_control.add_user("viewer1", "Test Viewer", Role.VIEWER)

        assert access_control.check_permission("viewer1", Action.VIEW_POSITIONS) is True
        assert access_control.check_permission("viewer1", Action.VIEW_PORTFOLIO) is True
        assert access_control.check_permission("viewer1", Action.VIEW_SEC_FILINGS) is True
        assert access_control.check_permission("viewer1", Action.EXECUTE_TRADE) is False
        assert access_control.check_permission("viewer1", Action.APPROVE_TRADE) is False
        assert access_control.check_permission("viewer1", Action.EXECUTE_SWAP) is False

    def test_compliance_role(self, access_control):
        access_control.add_user("compliance1", "Compliance Officer", Role.COMPLIANCE)

        assert access_control.check_permission("compliance1", Action.ANALYZE_CONTRACT) is True
        assert access_control.check_permission("compliance1", Action.VIEW_SEC_FILINGS) is True
        assert access_control.check_permission("compliance1", Action.VIEW_AUDIT_LOG) is True
        assert access_control.check_permission("compliance1", Action.RUN_COMPLIANCE_SCAN) is True
        assert access_control.check_permission("compliance1", Action.EXECUTE_TRADE) is False
        assert access_control.check_permission("compliance1", Action.MANAGE_AGENTS) is False

    def test_unknown_user_denied(self, access_control):
        assert access_control.check_permission("nobody", Action.VIEW_POSITIONS) is False

    def test_inactive_user_denied(self, access_control):
        user = access_control.add_user("inactive1", "Inactive User", Role.ADMIN)
        user.is_active = False
        assert access_control.check_permission("inactive1", Action.VIEW_POSITIONS) is False

    def test_agent_level_restriction(self, access_control):
        access_control.add_user(
            "restricted1", "Restricted Trader", Role.TRADER,
            allowed_agents={"trading-agent", "portfolio-agent"},
        )

        assert access_control.check_permission(
            "restricted1", Action.EXECUTE_TRADE, agent="trading-agent"
        ) is True
        assert access_control.check_permission(
            "restricted1", Action.EXECUTE_TRADE, agent="defi-agent"
        ) is False

    def test_require_permission_raises(self, access_control):
        access_control.add_user("viewer2", "Viewer", Role.VIEWER)

        with pytest.raises(PermissionError, match="lacks permission"):
            access_control.require_permission("viewer2", Action.EXECUTE_TRADE)

    def test_add_and_remove_user(self, access_control):
        access_control.add_user("temp1", "Temp User", Role.VIEWER)
        assert access_control.get_user("temp1") is not None

        access_control.remove_user("temp1")
        assert access_control.get_user("temp1") is None

    def test_list_users(self, access_control):
        access_control.add_user("u1", "User 1", Role.TRADER)
        access_control.add_user("u2", "User 2", Role.VIEWER)

        users = access_control.list_users()
        assert len(users) >= 3  # admin + u1 + u2
        user_ids = {u["user_id"] for u in users}
        assert "u1" in user_ids
        assert "u2" in user_ids
