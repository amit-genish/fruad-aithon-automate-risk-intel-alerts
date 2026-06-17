---
name: risk-alert-slack-notifier
description: >
  Posts Risk Intel Alert recommendations to the #risk_fraud_squad Slack channel.
  For each alert that passed the incremental value threshold, sends a formatted
  thread with precision metrics, incremental value metrics across both timeframes,
  and the raw alert SQL as a follow-up reply.

  USE THIS SKILL as the final step of the Risk Intel AIthon E2E pipeline, after
  risk-alert-incremental-value. Also invoke when asked to send risk alert
  notifications, post to risk_fraud_squad, or notify Fraud Analytics of qualifying alerts.
---

# Risk Alert Slack Notifier Skill

Reads `incremental_value.csv` and `alert_queries.csv`, posts one Slack thread
per qualifying alert to `#risk_fraud_squad`.

## Inputs

| File | Source |
|------|--------|
| `incremental_value.csv` | Output of risk-alert-incremental-value |
| `alert_queries.csv` | Output of risk-alert-precision |
| `precision.csv` | Output of risk-alert-precision (for precision per timeframe) |

---

## Step 1 — Identify qualifying alerts

Read `incremental_value.csv`. Keep rows where `passes_threshold = True`.
Group by `alert_name` — an alert may pass in one or both timeframes.

If no alerts qualify, post a single summary message to the channel and exit:
```
🔍 Risk Intel daily run complete — no alerts met the escalation threshold today.
Run date: {date}
```

## Step 2 — Resolve Slack user/channel IDs

Use the Slack MCP to resolve:
- Channel: search for `risk_fraud_squad` → get channel ID
- User group: search for `fraud-data-analysts` → get user group handle

## Step 3 — Build and send one thread per alert

For each qualifying alert:

### Main message

```
🚨 *Risk Alert Recommendation* — {alert_name}
<!subteam^{fraud_data_analysts_id}> please review

*Precision* (alerts that passed ≥10% threshold):
{precision_table}

*Incremental value over ecosystem*:
{incremental_value_table}

_Run date: {run_date}_
```

**Precision table** — include only timeframes where precision_pct >= 10:
```
| Time window | Total payments | Fraud payments | Precision |
|-------------|---------------|----------------|-----------|
| 1d          | 45            | 8              | 17.8%     |
| 1w          | 312           | 38             | 12.2%     |
```

**Incremental value table** — always show both timeframes:
```
| Timeframe          | Monthly load | Bad rate | Fraud TPV/mo |
|--------------------|-------------|----------|-------------|
| Last 2 weeks       | 180         | 14.2%    | $42,500     |
| Mature (90→30 days)| 95          | 9.1%     | $18,200     |
✅ Passes threshold: Last 2 weeks
```

Mark with ✅ the timeframe(s) that passed the threshold.
Mark with ❌ those that didn't.

Use Slack MCP tool `slack_send_message`:
```
channel: #risk_fraud_squad  (use resolved channel ID)
text: <formatted main message>
```
Save the returned `ts` (message timestamp) — needed for the reply.

### First reply — Alert SQL

Post the alert's SQL as a thread reply:
```
📋 *Alert logic (Redash SQL):*
```sql
{sql_text from alert_queries.csv}
```
```

Use `slack_send_message` with `thread_ts` = the ts from the main message.

## Step 4 — Summary

After all threads are sent, print to stdout:
```
Sent {N} alert recommendations to #risk_fraud_squad
Alerts: {comma-separated alert names}
```

## Error handling

If the Slack MCP fails for a specific alert, log the error and continue with
the remaining alerts. Do not abort the full run for a single send failure.
Collect all failures and report them at the end.
