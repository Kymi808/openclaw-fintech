"""
Shared test fixtures for the fintech agent test suite.
"""
import os
import tempfile
import pytest

# Set test encryption key before any imports
os.environ["DATA_ENCRYPTION_KEY"] = "test-encryption-key-not-for-production"
os.environ.setdefault("BINANCE_API_KEY", "test-key")
os.environ.setdefault("BINANCE_API_SECRET", "test-secret")
os.environ.setdefault("COINBASE_API_KEY", "test-key")
os.environ.setdefault("COINBASE_API_SECRET", "test-secret")
os.environ.setdefault("ALCHEMY_API_KEY", "test-key")
os.environ.setdefault("WALLET_ADDRESS", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD28")
os.environ.setdefault("SEC_EDGAR_USER_AGENT", "TestBot test@test.com")


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary database for testing."""
    import uuid
    from skills.shared.database import Database
    db_path = str(tmp_path / f"test_{uuid.uuid4().hex[:8]}.db")
    database = Database(db_path=db_path)
    yield database
    # Clean up thread-local connections
    if hasattr(Database._tls, "connections"):
        for key in list(Database._tls.connections.keys()):
            if db_path in key:
                Database._tls.connections[key].close()
                del Database._tls.connections[key]


@pytest.fixture
def encryption_manager():
    """Create an encryption manager with a test key."""
    from skills.shared.encryption import EncryptionManager
    return EncryptionManager(key="test-key-12345")


@pytest.fixture
def approval_engine():
    """Fresh approval engine for each test."""
    from skills.shared.approval import ApprovalEngine
    return ApprovalEngine()


@pytest.fixture
def access_control():
    """Fresh RBAC for each test."""
    from skills.shared.rbac import AccessControl
    return AccessControl()
