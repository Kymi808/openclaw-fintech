"""Tests for the session mapper (platform identity → RBAC)."""
import pytest
from skills.shared.session_mapper import SessionMapper
from skills.shared.rbac import Role


@pytest.fixture
def mapper():
    m = SessionMapper()
    m._mappings = {}
    m._pending_pairings = {}
    return m


class TestSessionMapper:
    def test_register_and_lookup(self, mapper):
        mapper.register("telegram", "12345", "kyle", "Kyle Z", Role.ADMIN)

        assert mapper.is_known("telegram", "12345")
        assert mapper.lookup("telegram", "12345") == "kyle"

    def test_unknown_sender(self, mapper):
        assert not mapper.is_known("telegram", "99999")
        assert mapper.lookup("telegram", "99999") is None

    def test_get_user(self, mapper):
        mapper.register("whatsapp", "+1234567890", "trader1", "Test Trader", Role.TRADER)

        user = mapper.get_user("whatsapp", "+1234567890")
        assert user is not None
        assert user.role == Role.TRADER

    def test_unregister(self, mapper):
        mapper.register("slack", "U123", "ops1", "Ops Person", Role.OPERATOR)
        assert mapper.is_known("slack", "U123")

        mapper.unregister("slack", "U123")
        assert not mapper.is_known("slack", "U123")

    def test_pairing_flow(self, mapper):
        # Unknown sender gets a pairing code
        code = mapper.generate_pairing_code("telegram", "newuser")
        assert len(code) == 6

        # Wrong code fails
        result = mapper.verify_pairing("telegram", "newuser", "WRONG1", "newuser1")
        assert result is None

        # Correct code succeeds
        user = mapper.verify_pairing(
            "telegram", "newuser", code,
            rbac_user_id="newuser1", name="New User", role=Role.VIEWER,
        )
        assert user is not None
        assert user.role == Role.VIEWER
        assert mapper.is_known("telegram", "newuser")

    def test_handle_incoming_known(self, mapper):
        mapper.register("telegram", "12345", "kyle", "Kyle", Role.ADMIN)

        result = mapper.handle_incoming_message("telegram", "12345")
        assert result["authorized"] is True
        assert result["user_id"] == "kyle"
        assert result["role"] == "admin"

    def test_handle_incoming_unknown(self, mapper):
        result = mapper.handle_incoming_message("telegram", "unknown99")
        assert result["authorized"] is False
        assert "pairing_code" in result
        assert len(result["pairing_code"]) == 6

    def test_handle_incoming_inactive(self, mapper):
        user = mapper.register("telegram", "inactive", "inactive1", "Inactive", Role.VIEWER)
        user.is_active = False

        result = mapper.handle_incoming_message("telegram", "inactive")
        assert result["authorized"] is False
        assert "deactivated" in result["reason"].lower()

    def test_cross_platform_isolation(self, mapper):
        mapper.register("telegram", "12345", "kyle_tg", "Kyle TG", Role.ADMIN)
        mapper.register("whatsapp", "12345", "kyle_wa", "Kyle WA", Role.TRADER)

        assert mapper.lookup("telegram", "12345") == "kyle_tg"
        assert mapper.lookup("whatsapp", "12345") == "kyle_wa"

    def test_list_all_mappings(self, mapper):
        mapper.register("telegram", "111", "user1", "User 1", Role.ADMIN)
        mapper.register("slack", "222", "user2", "User 2", Role.VIEWER)

        mappings = mapper.get_all_mappings()
        assert len(mappings) == 2
        platforms = {m["platform"] for m in mappings}
        assert platforms == {"telegram", "slack"}
