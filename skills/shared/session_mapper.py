"""
Maps messaging platform user identities to RBAC users.
Bridges Telegram/WhatsApp/Slack user IDs to the access control system.
"""
import json
from pathlib import Path
from typing import Optional

from .config import get_logger, audit_log
from .rbac import access_control, Role, User

logger = get_logger("session_mapper")

SESSIONS_FILE = Path("./data/sessions.json")


class SessionMapper:
    """
    Maps platform-specific sender identities to RBAC users.

    Example mappings:
    - Telegram user_id "123456" → RBAC user "kyle" (Role.ADMIN)
    - WhatsApp phone "+1234567890" → RBAC user "kyle" (Role.TRADER)
    - Slack user "U0123ABC" → RBAC user "ops-team" (Role.OPERATOR)
    """

    def __init__(self):
        # platform:sender_id → rbac_user_id
        self._mappings: dict[str, str] = {}
        # platform:sender_id → pairing_code (for new user pairing)
        self._pending_pairings: dict[str, str] = {}
        self._load()

    def _load(self):
        """Load saved mappings from disk."""
        if SESSIONS_FILE.exists():
            try:
                data = json.loads(SESSIONS_FILE.read_text())
                self._mappings = data.get("mappings", {})
            except (json.JSONDecodeError, KeyError):
                pass

    def _save(self):
        """Persist mappings to disk."""
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(json.dumps({
            "mappings": self._mappings,
        }, indent=2))

    def _make_key(self, platform: str, sender_id: str) -> str:
        return f"{platform}:{sender_id}"

    def register(self, platform: str, sender_id: str, rbac_user_id: str,
                 name: str = "", role: Role = Role.VIEWER) -> User:
        """
        Register a mapping from a platform identity to an RBAC user.
        Creates the RBAC user if it doesn't exist.
        """
        key = self._make_key(platform, sender_id)

        # Create RBAC user if needed
        user = access_control.get_user(rbac_user_id)
        if not user:
            user = access_control.add_user(
                user_id=rbac_user_id,
                name=name or f"{platform}:{sender_id}",
                role=role,
            )

        self._mappings[key] = rbac_user_id
        self._save()

        audit_log("system", "session_registered", {
            "platform": platform,
            "sender_id": sender_id,
            "rbac_user": rbac_user_id,
            "role": role.value,
        })

        logger.info(f"Registered {key} → {rbac_user_id} ({role.value})")
        return user

    def lookup(self, platform: str, sender_id: str) -> Optional[str]:
        """Look up the RBAC user ID for a platform sender."""
        key = self._make_key(platform, sender_id)
        return self._mappings.get(key)

    def get_user(self, platform: str, sender_id: str) -> Optional[User]:
        """Get the full RBAC User object for a platform sender."""
        rbac_id = self.lookup(platform, sender_id)
        if rbac_id:
            return access_control.get_user(rbac_id)
        return None

    def unregister(self, platform: str, sender_id: str) -> bool:
        """Remove a platform identity mapping."""
        key = self._make_key(platform, sender_id)
        if key in self._mappings:
            del self._mappings[key]
            self._save()
            audit_log("system", "session_unregistered", {
                "platform": platform,
                "sender_id": sender_id,
            })
            return True
        return False

    def is_known(self, platform: str, sender_id: str) -> bool:
        """Check if a sender has been registered."""
        return self._make_key(platform, sender_id) in self._mappings

    def generate_pairing_code(self, platform: str, sender_id: str) -> str:
        """Generate a pairing code for a new unknown sender."""
        import secrets
        code = secrets.token_hex(3).upper()  # 6-char hex code
        key = self._make_key(platform, sender_id)
        self._pending_pairings[key] = code

        audit_log("system", "pairing_code_generated", {
            "platform": platform,
            "sender_id": sender_id,
        })
        return code

    def verify_pairing(self, platform: str, sender_id: str, code: str,
                       rbac_user_id: str, name: str = "",
                       role: Role = Role.VIEWER) -> Optional[User]:
        """Verify a pairing code and register the user if correct."""
        key = self._make_key(platform, sender_id)
        expected = self._pending_pairings.get(key)

        if not expected or expected != code.upper():
            audit_log("system", "pairing_failed", {
                "platform": platform,
                "sender_id": sender_id,
            })
            return None

        del self._pending_pairings[key]
        return self.register(platform, sender_id, rbac_user_id, name, role)

    def get_all_mappings(self) -> list[dict]:
        """List all registered mappings."""
        results = []
        for key, rbac_id in self._mappings.items():
            platform, sender_id = key.split(":", 1)
            user = access_control.get_user(rbac_id)
            results.append({
                "platform": platform,
                "sender_id": sender_id,
                "rbac_user": rbac_id,
                "role": user.role.value if user else "unknown",
                "active": user.is_active if user else False,
            })
        return results

    def handle_incoming_message(self, platform: str, sender_id: str) -> dict:
        """
        Process an incoming message sender identity.
        Returns the user context or a pairing prompt.

        This is called by the OpenClaw gateway on every incoming message.
        """
        user = self.get_user(platform, sender_id)

        if user:
            if not user.is_active:
                return {
                    "authorized": False,
                    "reason": "Account deactivated. Contact admin.",
                }
            return {
                "authorized": True,
                "user_id": user.user_id,
                "name": user.name,
                "role": user.role.value,
            }

        # Unknown sender — generate pairing code
        code = self.generate_pairing_code(platform, sender_id)
        return {
            "authorized": False,
            "reason": "unknown_sender",
            "pairing_code": code,
            "message": (
                f"Welcome! To use the fintech bot, you need to pair your account.\n"
                f"Your pairing code is: {code}\n"
                f"Ask your admin to register you with this code."
            ),
        }


# Singleton
session_mapper = SessionMapper()
