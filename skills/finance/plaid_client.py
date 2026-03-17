"""
Plaid API client for bank account transaction syncing.
Handles token exchange, transaction fetching, and webhook processing.
"""
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx

from skills.shared import get_logger, audit_log, retry, mask_sensitive

logger = get_logger("plaid_client")

PLAID_ENVS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}


@dataclass
class PlaidTransaction:
    transaction_id: str
    account_id: str
    date: str
    name: str  # Merchant name
    amount: float  # Positive = debit, negative = credit
    category: list[str]  # Plaid's auto-categorization
    pending: bool
    payment_channel: str  # online, in store, other
    merchant_name: Optional[str] = None
    iso_currency_code: str = "USD"


class PlaidClient:
    """Plaid API client for bank transaction sync."""

    def __init__(self):
        self.client_id = os.getenv("PLAID_CLIENT_ID", "")
        self.secret = os.getenv("PLAID_SECRET", "")
        self.env = os.getenv("PLAID_ENV", "sandbox")
        self.base_url = PLAID_ENVS.get(self.env, PLAID_ENVS["sandbox"])
        self._access_tokens: dict[str, str] = {}  # account_name -> access_token

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.secret)

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def _auth_body(self) -> dict:
        return {
            "client_id": self.client_id,
            "secret": self.secret,
        }

    @retry(max_attempts=3, base_delay=1.0, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
    async def exchange_public_token(self, public_token: str) -> str:
        """Exchange a Plaid Link public token for an access token."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/item/public_token/exchange",
                json={
                    **self._auth_body(),
                    "public_token": public_token,
                },
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            access_token = data["access_token"]

            audit_log("finance-agent", "plaid_token_exchanged", {
                "item_id": data.get("item_id"),
            })

            return access_token

    @retry(max_attempts=3, base_delay=1.0, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
    async def get_transactions(
        self,
        access_token: str,
        start_date: str = None,
        end_date: str = None,
        count: int = 100,
    ) -> list[PlaidTransaction]:
        """Fetch transactions from a linked bank account."""
        if not start_date:
            start_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/transactions/get",
                json={
                    **self._auth_body(),
                    "access_token": access_token,
                    "start_date": start_date,
                    "end_date": end_date,
                    "options": {
                        "count": count,
                        "offset": 0,
                    },
                },
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        transactions = []
        for txn in data.get("transactions", []):
            transactions.append(PlaidTransaction(
                transaction_id=txn["transaction_id"],
                account_id=txn["account_id"],
                date=txn["date"],
                name=txn["name"],
                amount=txn["amount"],
                category=txn.get("category", []),
                pending=txn.get("pending", False),
                payment_channel=txn.get("payment_channel", "other"),
                merchant_name=txn.get("merchant_name"),
                iso_currency_code=txn.get("iso_currency_code", "USD"),
            ))

        audit_log("finance-agent", "plaid_transactions_fetched", {
            "count": len(transactions),
            "start_date": start_date,
            "end_date": end_date,
        })

        return transactions

    @retry(max_attempts=2, base_delay=1.0, retryable_exceptions=(httpx.ConnectError,))
    async def get_accounts(self, access_token: str) -> list[dict]:
        """Fetch linked bank accounts."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/accounts/get",
                json={
                    **self._auth_body(),
                    "access_token": access_token,
                },
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        accounts = []
        for acct in data.get("accounts", []):
            accounts.append({
                "account_id": acct["account_id"],
                "name": acct.get("name", ""),
                "type": acct.get("type", ""),
                "subtype": acct.get("subtype", ""),
                "balance_current": acct.get("balances", {}).get("current"),
                "balance_available": acct.get("balances", {}).get("available"),
                "currency": acct.get("balances", {}).get("iso_currency_code", "USD"),
                "mask": acct.get("mask", ""),  # Last 4 digits
            })

        return accounts

    def map_plaid_category(self, plaid_categories: list[str]) -> str:
        """Map Plaid's category taxonomy to our expense categories."""
        if not plaid_categories:
            return "Other"

        primary = plaid_categories[0].lower() if plaid_categories else ""

        # Check secondary category first for "shops" (which is ambiguous)
        if len(plaid_categories) > 1:
            secondary = plaid_categories[1].lower()
            if "restaurant" in secondary or "coffee" in secondary or "food" in secondary:
                return "Food & Dining"
            if "taxi" in secondary or "ride" in secondary or "gas" in secondary:
                return "Transport"
            if "software" in secondary or "subscription" in secondary:
                return "Software/SaaS"

        mapping = {
            "food and drink": "Food & Dining",
            "travel": "Transport",
            "transportation": "Transport",
            "shops": "Office",
            "service": "Software/SaaS",
            "recreation": "Entertainment",
            "healthcare": "Health",
            "payment": "Other",
            "transfer": "Other",
            "bank fees": "Other",
        }

        for key, category in mapping.items():
            if key in primary:
                return category

        return "Other"


# Singleton
plaid_client = PlaidClient()
