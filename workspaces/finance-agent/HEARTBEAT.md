# Finance Agent — Heartbeat Checklist

Triggered by: receipt photos, email notifications, and weekly cron (Monday 7 AM).

## On Receipt Photo

1. **Extract data** using vision model — merchant, amount, date, payment method
2. **Categorize** based on merchant name and learned patterns
3. **Check for duplicates** — same merchant + amount + date within 24h
4. **Store** receipt data and image reference
5. **Update budget tracking** — check against monthly limits
6. **Notify user** with extracted data and category
7. **Log** to audit trail

## On Weekly Cron (Monday 7 AM)

1. **Fetch bank transactions** from Plaid (last 7 days)
2. **Reconcile** transactions with captured receipts
3. **Flag unmatched** transactions for user review
4. **Categorize** new transactions
5. **Generate weekly mini-report**
6. **Check upcoming** tax deadlines (next 30 days)
7. **Send report** via configured channel
