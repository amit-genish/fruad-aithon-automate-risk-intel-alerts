---
name: risk-alert-slack-notifier
description: >
  Posts Risk Intel Alert recommendations to the #risk_fraud_squad Slack channel.
  For each alert that passed the incremental value threshold, sends a formatted
  thread with precision metrics, incremental value metrics across both timeframes,
  the raw alert SQL, and (when available) the rule candidate JSON and conversion
  notes as additional thread replies.

  USE THIS SKILL as the final step of the Risk Intel AIthon E2E pipeline, after
  risk-alert-to-rules. Also invoke when asked to send risk alert notifications,
  post to risk_fraud_squad, or notify Fraud Analytics of qualifying alerts.
---

# Risk Alert Slack Notifier Skill

Reads `incremental_value.csv`, `alert_queries.csv`, and (optionally)
`rule_candidate_*.json` + `rule_conversion_report.md`, then posts one Slack
thread per qualifying alert to `#risk_fraud_squad`.

## Inputs

| File | Source | Required |
|------|--------|----------|
| `incremental_value.csv` | Output of risk-alert-incremental-value | ✅ |
| `alert_queries.csv` | Output of risk-alert-precision | ✅ |
| `precision.csv` | Output of risk-alert-precision (for precision per timeframe) | ✅ |
| `rule_candidate_{alert_name}.json` | Output of risk-alert-to-rules (one per qualifying alert) | Optional |
| `rule_conversion_report.md` | Output of risk-alert-to-rules | Optional |

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

### Second reply — Rule candidate (if available)

Check whether `rule_candidate_{snake_case_alert_name}.json` exists in `run_dir`.
The snake_case filename mirrors the naming used by risk-alert-to-rules (e.g.,
`rule_candidate_ato_v3_alert.json` for alert "ATO v3 Alert").

If the file exists, post a third reply in the same thread:

```
📐 *Rule candidate JSON:*
```json
{full contents of rule_candidate_{alert_name}.json}
```
```

Then extract the per-alert section from `rule_conversion_report.md` for this
alert (the section headed `### {N}. {alert_name}`) and post it as a fourth reply:

```
📝 *Conversion notes:*
{per-alert section text from rule_conversion_report.md, formatted as-is}
```

If neither file exists (risk-alert-to-rules was skipped or failed), do not post
the rule replies — continue to the next alert silently.

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
