"""Tests for Plaid category mapping."""
import pytest
from skills.finance.plaid_client import PlaidClient


class TestPlaidCategoryMapping:
    def setup_method(self):
        self.client = PlaidClient()

    def test_food_category(self):
        assert self.client.map_plaid_category(["Food and Drink"]) == "Food & Dining"
        assert self.client.map_plaid_category(["Food and Drink", "Restaurants"]) == "Food & Dining"
        assert self.client.map_plaid_category(["Shops", "Coffee Shop"]) == "Food & Dining"

    def test_transport_category(self):
        assert self.client.map_plaid_category(["Travel"]) == "Transport"
        assert self.client.map_plaid_category(["Transportation"]) == "Transport"
        assert self.client.map_plaid_category(["Shops", "Taxi"]) == "Transport"

    def test_software_category(self):
        assert self.client.map_plaid_category(["Service"]) == "Software/SaaS"
        assert self.client.map_plaid_category(["Shops", "Subscription"]) == "Software/SaaS"

    def test_entertainment_category(self):
        assert self.client.map_plaid_category(["Recreation"]) == "Entertainment"

    def test_health_category(self):
        assert self.client.map_plaid_category(["Healthcare"]) == "Health"

    def test_unknown_falls_to_other(self):
        assert self.client.map_plaid_category(["Unknown Category"]) == "Other"
        assert self.client.map_plaid_category([]) == "Other"

    def test_is_not_configured(self):
        client = PlaidClient()
        client.client_id = ""
        client.secret = ""
        assert client.is_configured is False
