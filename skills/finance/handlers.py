"""
OpenClaw skill handlers for the Finance Agent.
Expense tracking, receipt processing, tax documents, bookkeeping.
"""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx

from skills.shared import get_logger, audit_log, require_env, mask_sensitive

logger = get_logger("finance.handlers")

DATA_DIR = Path("./workspaces/finance-agent/data")
EXPENSES_FILE = DATA_DIR / "expenses.json"
CATEGORIES_FILE = DATA_DIR / "categories.json"
BUDGETS_FILE = DATA_DIR / "budgets.json"

# Default budget categories
DEFAULT_BUDGETS = {
    "Food & Dining": 500.0,
    "Transport": 200.0,
    "Software/SaaS": 300.0,
    "Office": 100.0,
    "Entertainment": 150.0,
    "Health": 200.0,
    "Utilities": 150.0,
    "Other": 200.0,
}

# Merchant → category mapping (learned over time)
DEFAULT_MERCHANT_MAP = {
    "starbucks": "Food & Dining",
    "uber": "Transport",
    "lyft": "Transport",
    "amazon": "Office",
    "github": "Software/SaaS",
    "vercel": "Software/SaaS",
    "anthropic": "Software/SaaS",
    "openai": "Software/SaaS",
    "netflix": "Entertainment",
    "spotify": "Entertainment",
}


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_expenses() -> list[dict]:
    if EXPENSES_FILE.exists():
        return json.loads(EXPENSES_FILE.read_text())
    return []


def _save_expenses(expenses: list[dict]):
    _ensure_data_dir()
    EXPENSES_FILE.write_text(json.dumps(expenses, indent=2))


def _load_budgets() -> dict:
    if BUDGETS_FILE.exists():
        return json.loads(BUDGETS_FILE.read_text())
    return DEFAULT_BUDGETS


def _load_merchant_map() -> dict:
    if CATEGORIES_FILE.exists():
        return json.loads(CATEGORIES_FILE.read_text())
    return DEFAULT_MERCHANT_MAP


def _save_merchant_map(mapping: dict):
    _ensure_data_dir()
    CATEGORIES_FILE.write_text(json.dumps(mapping, indent=2))


def _auto_categorize(merchant: str) -> str:
    """Auto-categorize based on merchant name."""
    merchant_map = _load_merchant_map()
    merchant_lower = merchant.lower().strip()
    for key, category in merchant_map.items():
        if key in merchant_lower:
            return category
    return "Other"


async def process_receipt(image_path: str) -> dict:
    """
    Extract expense data from a receipt image using Ollama vision model.
    Triggered when a user sends a photo via any messaging channel.
    """
    from .receipt_ocr import receipt_ocr

    logger.info(f"Processing receipt image: {image_path}")

    try:
        receipt_data = await receipt_ocr.extract(image_path)
        extracted = {
            "merchant": receipt_data.merchant,
            "amount": receipt_data.amount or 0.0,
            "date": receipt_data.date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "payment_method": receipt_data.payment_method or "unknown",
            "items": receipt_data.items,
            "raw_image_path": image_path,
            "confidence": receipt_data.confidence,
            "model_used": receipt_data.model_used,
        }
    except FileNotFoundError:
        return {"error": f"Image not found: {image_path}"}
    except RuntimeError as e:
        # Ollama not available — fall back to manual entry prompt
        return {
            "error": str(e),
            "message": (
                "Could not process receipt automatically.\n"
                "Please enter manually: merchant, amount, category\n"
                "Example: 'expense Starbucks 5.75 Food & Dining'"
            ),
        }
    except ValueError as e:
        return {"error": f"Could not parse receipt: {e}"}

    # Auto-categorize
    category = _auto_categorize(extracted["merchant"])

    # Save expense
    expense = {
        "id": f"EXP-{len(_load_expenses()) + 1:06d}",
        "merchant": extracted["merchant"],
        "amount": extracted["amount"],
        "category": category,
        "date": extracted["date"],
        "payment_method": mask_sensitive(extracted.get("payment_method", ""), 4),
        "source": "receipt_photo",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    expenses = _load_expenses()
    expenses.append(expense)
    _save_expenses(expenses)

    audit_log("finance-agent", "receipt_processed", {
        "expense_id": expense["id"],
        "merchant": expense["merchant"],
        "amount": expense["amount"],
        "category": category,
    })

    # Budget check
    budgets = _load_budgets()
    month_expenses = [
        e for e in expenses
        if e["category"] == category
        and e["date"].startswith(datetime.now(timezone.utc).strftime("%Y-%m"))
    ]
    month_total = sum(e["amount"] for e in month_expenses)
    budget_limit = budgets.get(category, 0)

    msg = (
        f"🧾 Receipt Captured\n"
        f"Merchant: {expense['merchant']}\n"
        f"Amount: ${expense['amount']:.2f}\n"
        f"Category: {category} (auto)\n"
        f"Date: {expense['date']}\n"
    )

    if budget_limit > 0:
        msg += f"\nMonthly spend in {category}: ${month_total:.2f} / ${budget_limit:.2f}"
        if month_total >= budget_limit * 0.8:
            msg += f"\n⚠️ Warning: {month_total/budget_limit*100:.0f}% of budget used"

    return {"expense": expense, "message": msg}


async def add_expense(
    merchant: str,
    amount: float,
    category: Optional[str] = None,
    date: Optional[str] = None,
) -> dict:
    """Manually add an expense entry."""
    if not category:
        category = _auto_categorize(merchant)
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    expense = {
        "id": f"EXP-{len(_load_expenses()) + 1:06d}",
        "merchant": merchant,
        "amount": amount,
        "category": category,
        "date": date,
        "source": "manual",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    expenses = _load_expenses()
    expenses.append(expense)
    _save_expenses(expenses)

    # Learn merchant → category mapping
    merchant_map = _load_merchant_map()
    merchant_key = merchant.lower().strip()
    if merchant_key not in merchant_map:
        merchant_map[merchant_key] = category
        _save_merchant_map(merchant_map)

    audit_log("finance-agent", "expense_added", {
        "expense_id": expense["id"],
        "amount": amount,
        "category": category,
    })

    return {"expense": expense, "message": f"✅ Added: ${amount:.2f} at {merchant} ({category})"}


async def get_expenses(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """Query expenses by date range and/or category."""
    expenses = _load_expenses()

    if start_date:
        expenses = [e for e in expenses if e["date"] >= start_date]
    if end_date:
        expenses = [e for e in expenses if e["date"] <= end_date]
    if category:
        expenses = [e for e in expenses if e["category"].lower() == category.lower()]

    total = sum(e["amount"] for e in expenses)

    return {
        "expenses": expenses,
        "count": len(expenses),
        "total": round(total, 2),
        "filters": {"start_date": start_date, "end_date": end_date, "category": category},
    }


async def budget_status() -> str:
    """Check current month's budget utilization."""
    expenses = _load_expenses()
    budgets = _load_budgets()
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")

    month_expenses = [e for e in expenses if e["date"].startswith(current_month)]

    # Aggregate by category
    by_category: dict[str, float] = {}
    for e in month_expenses:
        by_category[e["category"]] = by_category.get(e["category"], 0) + e["amount"]

    total_spent = sum(by_category.values())
    total_budget = sum(budgets.values())

    month_name = datetime.now(timezone.utc).strftime("%B %Y")
    lines = [
        f"📊 Budget Status — {month_name}",
        f"| Category        | Spent     | Budget  | Status |",
        f"|-----------------|-----------|---------|--------|",
    ]

    for cat, budget in sorted(budgets.items()):
        spent = by_category.get(cat, 0)
        pct = (spent / budget * 100) if budget > 0 else 0
        status = "✅" if pct < 80 else ("⚠️" if pct < 100 else "🔴")
        lines.append(
            f"| {cat:<15} | ${spent:>7.2f} | ${budget:>5.0f}  | {status}     |"
        )

    lines.append(f"")
    lines.append(f"Total: ${total_spent:.2f} / ${total_budget:.2f}")

    # Tax-deductible estimate
    deductible_categories = {"Software/SaaS", "Office", "Utilities"}
    deductible = sum(
        by_category.get(cat, 0) for cat in deductible_categories
    )
    lines.append(f"Tax-deductible: ${deductible:.2f}")

    return "\n".join(lines)


async def tax_summary(year: Optional[int] = None) -> str:
    """Generate a tax document summary for the given year."""
    if not year:
        year = datetime.now(timezone.utc).year

    expenses = _load_expenses()
    year_str = str(year)
    year_expenses = [e for e in expenses if e["date"].startswith(year_str)]

    # Aggregate by category
    by_category: dict[str, float] = {}
    for e in year_expenses:
        by_category[e["category"]] = by_category.get(e["category"], 0) + e["amount"]

    total = sum(by_category.values())
    deductible_categories = {"Software/SaaS", "Office", "Utilities", "Health"}
    deductible = sum(by_category.get(cat, 0) for cat in deductible_categories)

    lines = [
        f"📋 Tax Summary — {year}",
        f"Total expenses: ${total:,.2f}",
        f"Tax-deductible: ${deductible:,.2f}",
        f"",
        f"Breakdown:",
    ]
    for cat, amount in sorted(by_category.items(), key=lambda x: -x[1]):
        is_deductible = "✅" if cat in deductible_categories else ""
        lines.append(f"  {cat}: ${amount:,.2f} {is_deductible}")

    lines.append(f"\n⚖️ This is an estimate. Consult a tax professional.")

    return "\n".join(lines)


async def bank_transactions(
    access_token: str = None,
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """
    Fetch recent bank transactions via Plaid and auto-categorize.
    Matches transactions against existing receipts and flags unmatched ones.
    """
    from .plaid_client import plaid_client

    if not plaid_client.is_configured:
        return {
            "transactions": [],
            "message": "Plaid not configured. Set PLAID_CLIENT_ID and PLAID_SECRET in .env",
        }

    if not access_token:
        return {
            "transactions": [],
            "message": "No bank account linked. Use Plaid Link to connect your bank account.",
        }

    logger.info("Fetching bank transactions via Plaid")

    if not start_date:
        start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        plaid_txns = await plaid_client.get_transactions(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as e:
        logger.error(f"Plaid transaction fetch failed: {e}")
        return {"error": f"Failed to fetch transactions: {e}"}

    # Load existing expenses for matching
    expenses = _load_expenses()
    existing_merchants_dates = {
        (e["merchant"].lower(), e["date"], e["amount"])
        for e in expenses
    }

    new_expenses = []
    matched = 0
    unmatched_txns = []

    for txn in plaid_txns:
        if txn.pending:
            continue  # Skip pending transactions

        # Plaid amounts: positive = money out (debit), negative = money in (credit)
        if txn.amount <= 0:
            continue  # Skip credits/refunds for expense tracking

        merchant = txn.merchant_name or txn.name
        our_category = plaid_client.map_plaid_category(txn.category)

        # Check if we already have this expense (receipt match)
        match_key = (merchant.lower(), txn.date, txn.amount)
        if match_key in existing_merchants_dates:
            matched += 1
            continue

        # New expense — auto-categorize and add
        category = _auto_categorize(merchant) or our_category
        expense_id = f"EXP-{len(expenses) + len(new_expenses) + 1:06d}"

        expense = {
            "id": expense_id,
            "merchant": merchant,
            "amount": txn.amount,
            "category": category,
            "date": txn.date,
            "payment_method": mask_sensitive(txn.account_id, 4),
            "source": "bank_sync",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "plaid_transaction_id": txn.transaction_id,
        }
        new_expenses.append(expense)

        # Check if this should be flagged for review
        if txn.amount > 500:
            unmatched_txns.append({
                "transaction": expense,
                "reason": "Amount over $500 — needs manual review",
            })

    # Save new expenses
    if new_expenses:
        expenses.extend(new_expenses)
        _save_expenses(expenses)

        # Learn merchant categories
        merchant_map = _load_merchant_map()
        for exp in new_expenses:
            key = exp["merchant"].lower().strip()
            if key not in merchant_map:
                merchant_map[key] = exp["category"]
        _save_merchant_map(merchant_map)

    audit_log("finance-agent", "bank_transactions_synced", {
        "source": "plaid",
        "period": f"{start_date} to {end_date}",
        "total_fetched": len(plaid_txns),
        "new_expenses": len(new_expenses),
        "matched_receipts": matched,
        "flagged": len(unmatched_txns),
    })

    return {
        "transactions": new_expenses,
        "new_count": len(new_expenses),
        "matched_receipts": matched,
        "flagged_for_review": unmatched_txns,
        "message": (
            f"Synced {len(plaid_txns)} transactions ({start_date} to {end_date}).\n"
            f"New expenses: {len(new_expenses)}\n"
            f"Matched to receipts: {matched}\n"
            f"Flagged for review: {len(unmatched_txns)}"
        ),
    }


async def budget_alerts() -> str:
    """
    Proactive budget alert check (cron: daily at 9 AM).
    Alerts when any category hits 80% or total hits 90%.
    """
    expenses = _load_expenses()
    budgets = _load_budgets()
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    month_expenses = [e for e in expenses if e["date"].startswith(current_month)]

    by_category: dict[str, float] = {}
    for e in month_expenses:
        by_category[e["category"]] = by_category.get(e["category"], 0) + e["amount"]

    total_spent = sum(by_category.values())
    total_budget = sum(budgets.values())
    alerts = []

    # Category-level alerts
    for cat, budget in budgets.items():
        spent = by_category.get(cat, 0)
        if budget <= 0:
            continue
        pct = spent / budget * 100
        if pct >= 100:
            alerts.append(f"🔴 OVER BUDGET: {cat} — ${spent:.2f} / ${budget:.2f} ({pct:.0f}%)")
        elif pct >= 80:
            alerts.append(f"⚠️ Approaching limit: {cat} — ${spent:.2f} / ${budget:.2f} ({pct:.0f}%)")

    # Total budget alert
    if total_budget > 0:
        total_pct = total_spent / total_budget * 100
        if total_pct >= 100:
            alerts.append(f"🔴 TOTAL OVER BUDGET: ${total_spent:.2f} / ${total_budget:.2f}")
        elif total_pct >= 90:
            alerts.append(f"⚠️ Total spend at {total_pct:.0f}%: ${total_spent:.2f} / ${total_budget:.2f}")

    # Upcoming tax deadlines
    now = datetime.now(timezone.utc)
    tax_deadlines = [
        (datetime(now.year, 1, 15), "Q4 estimated tax"),
        (datetime(now.year, 4, 15), "Annual tax filing / Q1 estimated tax"),
        (datetime(now.year, 6, 15), "Q2 estimated tax"),
        (datetime(now.year, 9, 15), "Q3 estimated tax"),
    ]
    for deadline, desc in tax_deadlines:
        days_until = (deadline - now.replace(tzinfo=None)).days
        if 0 < days_until <= 30:
            alerts.append(f"🗓️ Tax deadline in {days_until} days: {desc} ({deadline.strftime('%B %d')})")

    if not alerts:
        return ""  # No alerts — don't send anything

    audit_log("finance-agent", "budget_alerts", {
        "alert_count": len(alerts),
        "total_spent": total_spent,
        "total_budget": total_budget,
    })

    return "💰 Budget Alerts\n\n" + "\n".join(alerts)


async def weekly_report() -> str:
    """Generate weekly expense mini-report (cron: Monday 7 AM)."""
    logger.info("Finance agent weekly report")

    # Last 7 days
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    result = await get_expenses(start_date=start_str, end_date=end_str)
    budget = await budget_status()

    msg = (
        f"📅 Weekly Expense Report ({start_str} to {end_str})\n"
        f"Transactions: {result['count']}\n"
        f"Total spent: ${result['total']:.2f}\n\n"
        f"{budget}"
    )

    audit_log("finance-agent", "weekly_report", {
        "period": f"{start_str} to {end_str}",
        "transaction_count": result["count"],
        "total": result["total"],
    })

    return msg
