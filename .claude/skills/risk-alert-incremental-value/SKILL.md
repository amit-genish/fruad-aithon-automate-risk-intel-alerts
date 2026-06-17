---
name: risk-alert-incremental-value
description: >
  For risk alerts that passed the 10% precision threshold, calculate their
  incremental value over the existing payment decision ecosystem using a single
  Snowflake query per alert. Produces metrics (bad_rate, fraud_TPV, manual_review_load)
  across two timeframes and applies the incremental value threshold to identify alerts
  that should be flagged to Fraud Analytics.

  USE THIS SKILL as the second step of the Risk Intel AIthon E2E pipeline, after
  risk-alert-precision. Also invoke when asked to calculate incremental value,
  simulate alert impact, or check which alerts should be escalated to Fraud Analytics.
---

# Risk Alert Incremental Value Skill

Reads `precision.csv` and `alert_queries.csv` (outputs of `risk-alert-precision`),
runs one Snowflake query per qualifying alert per timeframe, and writes
`incremental_value.csv` for alerts that pass the escalation threshold.

## Inputs

| File | Source |
|------|--------|
| `precision.csv` | Output of risk-alert-precision |
| `alert_queries.csv` | Output of risk-alert-precision (columns: alert_name, redash_url, sql_text) |

## Output

`incremental_value.csv` — columns:
```
alert_name, timeframe, manual_review_load, fraud_count, bad_rate_pct,
fraud_tpv, monthly_manual_review_load, monthly_fraud_tpv,
passes_threshold
```

## Escalation threshold

```
passes = (bad_rate_pct >= 8)
       OR (bad_rate_pct >= 6 AND monthly_fraud_tpv >= 14000)
AND monthly_manual_review_load < 600
```

## Two timeframes

| Label | Definition |
|-------|-----------|
| `last_2w` | Last 14 days (now-14d → now) |
| `mature_90_30` | 90 to 30 days ago (now-90d → now-30d) |

---

## Step 1 — Filter qualifying alerts

Read `precision.csv`. Keep only alerts with `precision_pct >= 10` in at least one
time window. These are the alerts to process.

```python
import pandas as pd
precision = pd.read_csv('precision.csv')
qualifying = precision[precision['precision_pct'] >= 10]['alert'].unique().tolist()
print(f"{len(qualifying)} alerts above precision threshold")
```

## Step 2 — For each qualifying alert: modify SQL and write assumptions

Read `alert_queries.csv`, get the `sql_text` for each alert.

For each alert's SQL, identify and replace date/time filter clauses to match each
target timeframe. This is the one reasoning step — do it carefully:

**Write to `assumptions_{alert_name}.txt` before running any query:**
```
Alert: <name>
Original date filters found:
  - Line N: <original clause>  →  Column: <col_name>  Interpretation: <what it filters>
Timeframe modifications:
  last_2w:
    - <clause>  replaced with  <new_clause>
  mature_90_30:
    - <clause>  replaced with  <new_clause>
Columns NOT modified (and why):
  - <col>: not a timeframe filter, represents <X>
Confidence: high / medium / low
If low → stop and ask for guidance rather than proceeding with a wrong query.
```

**Known schema fixes to apply when modifying alert SQL:**
- `FVTRN_MELIO.RISKENGINEDECISIONS` → `FIVETRAN_CDC.FVTRN_MELIO.RISKENGINEDECISIONS` (add full prefix if missing)

**Modification rules:**
- Replace date filters that scope to "recent" payments with the target timeframe bounds
- If a column represents payment creation/scheduling date → replace
- If a column represents a review or label date → do NOT replace (not the payment scope)
- If the SQL has multiple date filters for the same concept, replace all of them
- Use `DATEADD` or explicit date arithmetic consistent with the existing SQL style
- For `last_2w`: `created_at >= DATEADD('day', -14, CURRENT_DATE())`
- For `mature_90_30`: `created_at BETWEEN DATEADD('day', -90, CURRENT_DATE()) AND DATEADD('day', -30, CURRENT_DATE())`

After writing assumptions, verify them (re-read the SQL and the assumptions file)
before proceeding. If you have low confidence in the modification, stop and ask.

## Step 3 — Build and run the full query

For each (alert × timeframe), fill in the query template from
`<skill_dir>/references/query_template.sql`:

1. Replace `{MODIFIED_ALERT_SQL}` with the timeframe-adjusted alert SQL
2. Replace `{RUN_DATE}` with `CURRENT_TIMESTAMP()`

**Run each query in a sub-agent** to avoid large Snowflake results overflowing context.
The sub-agent receives the filled-in SQL and writes the result (one row: manual_review_load,
fraud_count, bad_rate_pct, fraud_tpv) to `results_{alert_name}_{timeframe}.json`.

Sub-agent prompt template:
```
Run this Snowflake query and save the result as JSON to {output_path}.
The result should be a single row with columns: manual_review_load, fraud_count,
bad_rate_pct, fraud_tpv.

SQL:
{filled_query}
```

## Step 4 — Collect results and apply threshold

Read all `results_*.json` files. Annualise counts to monthly:

```python
# For last_2w (14 days): monthly = count * (30/14)
# For mature_90_30 (60-day window): monthly = count * (30/60)

timeframe_days = {'last_2w': 14, 'mature_90_30': 60}

for row in results:
    days = timeframe_days[row['timeframe']]
    row['monthly_manual_review_load'] = round(row['manual_review_load'] * 30 / days)
    row['monthly_fraud_tpv'] = round(row['fraud_tpv'] * 30 / days, 2)
    row['passes_threshold'] = (
        (row['bad_rate_pct'] >= 8 or
         (row['bad_rate_pct'] >= 6 and row['monthly_fraud_tpv'] >= 14000))
        and row['monthly_manual_review_load'] < 600
    )
```

Save all rows (including non-passing, for audit) to `incremental_value.csv`.

Print qualifying alerts:
```
Alert: <name>
  last_2w:       bad_rate=X%  fraud_tpv=$Y/mo  load=Z/mo  PASSES=True/False
  mature_90_30:  bad_rate=X%  fraud_tpv=$Y/mo  load=Z/mo  PASSES=True/False
```

## Step 5 — Output

Copy `incremental_value.csv` and all `assumptions_*.txt` files to the outputs folder.
The assumptions files are the audit trail for the SQL modifications.

Alerts where `passes_threshold = True` for at least one timeframe are inputs for
`risk-alert-slack-notifier`.
