---
name: risk-alert-e2e
description: >
  End-to-end Risk Intel Alert automation pipeline. Runs the full flow:
  precision calculation → incremental value analysis → Slack notifications.
  Scheduled to run Mon–Sat early morning TLV time. Chains risk-alert-precision,
  risk-alert-incremental-value, and risk-alert-slack-notifier in sequence,
  passing files between steps via a shared run directory.

  USE THIS SKILL when asked to run the full risk alert pipeline, trigger the
  daily E2E flow, or set up the automated daily risk alert run. Also the entry
  point when the scheduler fires the daily task.
---

# Risk Alert E2E Skill

Orchestrates the three pipeline skills in sequence. Each step writes outputs
to a shared run directory; the next step reads from it.

## Run directory

All files for a single run are stored under:
```
/tmp/risk_intel_runs/{YYYYMMDD}/
```

## Full pipeline

```
risk-alert-precision
  ↓ precision.csv, alert_queries.csv
risk-alert-incremental-value
  ↓ incremental_value.csv
risk-alert-slack-notifier
```

---

## Execution

### Step 1 — Set up run directory

```python
from datetime import date
run_date = date.today().strftime('%Y%m%d')
run_dir = f'/tmp/risk_intel_runs/{run_date}'
import os; os.makedirs(run_dir, exist_ok=True)
print(f"Run directory: {run_dir}")
```

### Step 2 — Run risk-alert-precision

Invoke the `risk-alert-precision` skill. Pass `run_dir` as the output directory
so all files land there.

Expected outputs in `run_dir`:
- `precision.csv`
- `alert_queries.csv`

If precision step fails or produces 0 rows: log error, post a failure notice to
`#risk_fraud_squad`, and stop.

### Step 3 — Run risk-alert-incremental-value

Invoke `risk-alert-incremental-value`. Read inputs from `run_dir`, write outputs
to `run_dir`.

Expected outputs:
- `incremental_value.csv`
- `assumptions_{alert}.txt` files (one per alert processed)

Check: if no alerts pass the threshold, proceed to Step 4 (notifier handles
the "nothing to report" case gracefully).

### Step 4 — Run risk-alert-slack-notifier

Invoke `risk-alert-slack-notifier`. Read all inputs from `run_dir`.

### Step 5 — Run summary

Print to stdout:
```
✅ Risk Intel E2E run complete
Run date: {date}
Precision step: {N} alerts evaluated, {M} above threshold
Incremental value step: {K} alerts passed escalation threshold
Slack: {K} threads sent to #risk_fraud_squad
Run directory: {run_dir}
```

---

## Scheduling

This pipeline runs **Mon–Sat at 05:00 TLV time (02:00 UTC)**.

To set up the schedule, invoke the `schedule` skill with:
```
Prompt: "Run the risk-alert-e2e skill"
Schedule: 0 2 * * 1-6
```

The TLV timezone offset (UTC+3 in summer, UTC+2 in winter) means the safe
UTC equivalent is 02:00 — always before 05:00 TLV regardless of DST.

---

## Error handling

| Failure point | Action |
|--------------|--------|
| Precision step fails | Post to #risk_fraud_squad: "⚠️ Risk Intel daily run failed at precision step. Manual check required." Stop. |
| Incremental value step fails for one alert | Log, continue with others, include failures in run summary |
| Slack send fails | Log, continue, report at end |
| Zero alerts in sheet | Post: "📋 Risk Intel run: no payments in scope for today's window." |

---

## Post-MVP: result storage

Once the storage deliverable is ready, add a Step 6 that copies `run_dir`
contents to a persistent location (e.g., S3/GCS bucket or Snowflake stage)
keyed by run date.
