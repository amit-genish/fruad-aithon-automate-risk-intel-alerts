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

**Write to `assumptions_{alert_name}.txt` before finalising any SQL:**
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

### SQL fixes required before adding to modified_queries.json

**1. Qualify all unresolved table references with `FIVETRAN_CDC.` prefix.**
Any bare `FVTRN_MELIO.*`, `DECISION_ENGINE_DECISION.*`, or `MONGO_TAGGING_TAGGING.*`
reference will fail. Prefix everything:
```sql
-- Before:  FROM FVTRN_MELIO.PAYMENTS p
-- After:   FROM FIVETRAN_CDC.FVTRN_MELIO.PAYMENTS p
```

**2. The subquery must expose a column named `payment_id`.**
The template joins `alert_payments.payment_id`. Redash SQLs often select `p.id`
without an alias — add `AS payment_id`:
```sql
-- Before:  SELECT p.id, p.organizationid, ...
-- After:   SELECT p.id AS payment_id, p.organizationid, ...
```

**3. Remove any trailing ORDER BY** — the script strips it automatically, but
double-check that no ORDER BY in a subquery is accidentally removed.

**4. CTE-based alert SQLs require `full_sql`, not `modified_sql`.**

If the alert's SQL starts with `WITH` (i.e., it defines its own CTEs), it **cannot** be
placed inside the template's `alert_payments AS (...)` CTE — Snowflake does not allow
nested `WITH` blocks. Use `full_sql` instead of `modified_sql` in the JSON entry.

To build `full_sql`, merge the alert's CTEs into the template's WITH block:

1. **Lift** all CTE definitions from the alert's `WITH` clause to the top of the outer `WITH`
2. **Replace** `alert_payments AS ( {MODIFIED_ALERT_SQL} )` with just the final `SELECT`
   from the alert's SQL (the SELECT that references those CTEs), minus its `ORDER BY`
3. **Continue** with the template's own CTEs (`first_decisions`, `incremental_payments`,
   `payment_with_fraud`) and final SELECT unchanged

Example — alert SQL with CTEs:
```sql
-- Alert SQL (CTE-based):
WITH
todays_payments AS (SELECT pmt.id AS payment_id, ... WHERE pmt.CREATEDAT::date = current_timestamp::date),
ato_v3_scores AS (SELECT ... WHERE _FIVETRAN_SYNCED between ... and current_timestamp),
...
SELECT max(tp.payment_id) as payment_id, ... FROM todays_payments tp JOIN ato_v3_scores ... WHERE ...
GROUP BY tp.ORGANIZATIONID
ORDER BY ...
```

Becomes `full_sql`:
```sql
WITH
-- Alert's own CTEs at top level (date filters modified for timeframe):
todays_payments AS (SELECT pmt.id AS payment_id, ... WHERE pmt.CREATEDAT >= DATEADD('day',-14,CURRENT_DATE())),
ato_v3_scores AS (SELECT ... WHERE _FIVETRAN_SYNCED between DATEADD('day',-14,CURRENT_DATE()) and current_timestamp),
...,
-- Final SELECT from alert becomes alert_payments (no ORDER BY):
alert_payments AS (
    SELECT max(tp.payment_id) as payment_id, ... FROM todays_payments tp JOIN ato_v3_scores ... WHERE ...
    GROUP BY tp.ORGANIZATIONID
),
-- Template CTEs continue unchanged:
first_decisions AS (...),
incremental_payments AS (...),
payment_with_fraud AS (...)
SELECT COUNT(*) AS manual_review_load, ... FROM payment_with_fraud
```

Use `"full_sql"` as the key in modified_queries.json (instead of `"modified_sql"`) — the
runner uses `full_sql` directly, bypassing the template wrapper.

### Alerts with known issues — mark as error, skip

- **Fraud Ring**: fails with `invalid identifier 'PROXY'` — Redash alias not in raw tables.
- **RISKENGINEDECISIONS full-scan queries** (MM Fraud Ring, New Email Domains, Explosive
  Growth, Dormant Account): may be very slow over large windows; mark error if they time out.

### Write modified_queries.json

After writing all assumptions files, output `{run_dir}/modified_queries.json`.

Use `"modified_sql"` for simple SQLs (no top-level `WITH`); use `"full_sql"` for CTE-based
SQLs (see fix #4 above). The runner checks `full_sql` first, falls back to `modified_sql`.

```json
[
  {
    "alert_name": "Bad Vendors CC FR",
    "timeframe": "last_2w",
    "modified_sql": "SELECT p.id AS payment_id, ... WHERE dm.createdat >= DATEADD('day',-14,CURRENT_DATE()) ..."
  },
  {
    "alert_name": "Bad Vendors CC FR",
    "timeframe": "mature_90_30",
    "modified_sql": "SELECT p.id AS payment_id, ... WHERE dm.createdat BETWEEN DATEADD('day',-90,CURRENT_DATE()) AND DATEADD('day',-30,CURRENT_DATE()) ..."
  },
  {
    "alert_name": "ATO v3 Alert",
    "timeframe": "last_2w",
    "full_sql": "WITH\ntodays_payments AS (...),\nato_v3_scores AS (...),\n...,\nalert_payments AS (\n  SELECT max(tp.payment_id) as payment_id, ... GROUP BY ...\n),\nfirst_decisions AS (...),\n...\nSELECT COUNT(*) AS manual_review_load, ... FROM payment_with_fraud"
  },
  ...
]
```

Include one entry per (alert × timeframe). Alerts skipped due to known issues should be
omitted from the JSON (their result files will be absent; Step 4 treats them as zeros).

## Step 3 — Run all queries via the TypeScript script

```bash
# First time in a new shell: install deps
cd <skill_dir> && npm install

# Run (opens a browser SSO window on first use per session)
SNOWFLAKE_USER=<your-email> \
  npx tsx <skill_dir>/scripts/run_queries.ts \
    /tmp/risk_intel/modified_queries.json \
    /tmp/risk_intel/
```

`SNOWFLAKE_ACCOUNT` is read from the environment (set in ~/.zshrc).
Optional env vars: `SNOWFLAKE_AUTHENTICATOR` (default: `EXTERNALBROWSER`),
`SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE`.

The script runs queries sequentially, wrapping each SQL in `query_template.sql`.
Each writes `result_{alert_name}_{timeframe}.json` to the output dir.
Errors are caught per-query and written as zeros + error message — the script never aborts early.

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
