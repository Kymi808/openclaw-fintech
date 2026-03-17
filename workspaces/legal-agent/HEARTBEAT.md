# Legal Agent — Heartbeat Checklist

## SEC Filing Check (every 4 hours)

1. **Query SEC EDGAR** for new filings from tracked entities
2. **Filter** by monitored filing types
3. **Summarize** each new filing
4. **Assess relevance** to user's portfolio/business
5. **Alert** immediately for 8-K filings; batch others for daily digest
6. **Log** all checked filings to audit trail

## Contract Renewal Check (daily at 8 AM)

1. **Scan tracked contracts** for upcoming deadlines
2. **Alert** if any contract expires within 30 days
3. **Urgent alert** if any contract expires within 7 days
4. **Generate cancellation letter draft** if user previously requested

## GDPR Scan (weekly, Monday 2 AM)

1. **Scan configured URLs** for compliance issues
2. **Compare** to previous scan results — flag new issues and resolved issues
3. **Generate report** with severity-ranked findings
4. **Send** to Slack compliance channel
