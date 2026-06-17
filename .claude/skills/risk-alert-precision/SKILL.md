---
name: risk-alert-precision
description: >
  Calculate precision of Risk Intel Alerts at catching fraud payments.
  Downloads the Risk Intel Alerts Google Sheet as XLSX, extracts Redash SQL
  hyperlinks from the alert cells, fetches alert SQL queries from Redash,
  joins with Snowflake is_fraud logic, and outputs precision per alert per
  sliding time window (1d, 3d, 1w, 2w, 1m).

  USE THIS SKILL whenever asked to: calculate alert precision, run the risk
  intel alerts analysis, scrape the alerts sheet, check which alerts are catching
  fraud, or produce the daily/weekly precision report. Also the first step of the
  full E2E pipeline.
---

# Risk Alert Precision Skill

Produces `precision.csv` and `alert_queries.csv` as inputs for the incremental
value skill and the E2E pipeline.

## Output files

| File | Contents |
|------|----------|
| `precision.csv` | (alert × time_window): total_payments, fraud_payments, precision_pct |
| `alert_queries.csv` | alert name, Redash URL, SQL text |

**Precision** = fraud_payments / total_reviewed_payments × 100

**Sheet column layout** (the two `Risk Status` columns are at different positions):
```
[0]  Payment ID           [7]  Alerts (hyperlinks → Redash)
[1]  Amount               [8]  Added At          ← scope filter
[2]  Payment Status       [9]  Analyst
[3]  Risk Status (infra)  [10] Reviewed           ← Yes/No/empty
[4]  Created At (Payment) [11] Risk Status (enum) ← fraud signal
[5]  Org ID               [12] Fraud type
[6]  Alert Count          [13] Fraud Ring
                          [14] Notes
                          [15] Date reviewed
```

**Scope**: Only rows where `Added At >= today - 30 days` are processed.
Pass `--scope-days 90` for the post-MVP 3-month window.

**Fraud tagging** (worst case — a payment is fraud if EITHER is true):
- Snowflake `is_fraud` = true (5-condition CASE, see Step 3)
- Sheet `Risk Status` (col 11, enum) = `"Bad Payment"`

**Exclusions** applied before counting:
- Payments not yet reviewed (`Reviewed` col 10 is null/empty)
- Payments with `Reviewed = "No"` (legacy auto-added rows from alerts logic changes)

---

## Step 1 — Download the Google Sheet as XLSX

Use the Google Drive connector:
```
Tool: download_file_content
fileId: "1BVjaJlIGpSWhH1IJBOkwAr7xWqMB8IdbtkkWFyRkoQU"
exportMimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
```

The result is a JSON `{content: <base64>, ...}`. Decode and save:
```python
import base64
xlsx_bytes = base64.b64decode(result["content"])
with open("/tmp/risk_intel/alerts.xlsx", "wb") as f:
    f.write(xlsx_bytes)
```

## Step 2 — Parse XLSX → sheet data + hyperlinks

```bash
pip install openpyxl pandas --break-system-packages -q

# First run: verify column detection
python <skill_dir>/scripts/parse_sheet.py /tmp/risk_intel/alerts.xlsx --print-columns

# Full parse — last 30 days (MVP scope)
python <skill_dir>/scripts/parse_sheet.py /tmp/risk_intel/alerts.xlsx \
    --out-dir /tmp/risk_intel/ --scope-days 30

# Post-MVP: extend to 90 days
# python <skill_dir>/scripts/parse_sheet.py /tmp/risk_intel/alerts.xlsx \
#     --out-dir /tmp/risk_intel/ --scope-days 90
```

Outputs: `sheet_data.csv` and `alert_links.json`.

If column detection fails (warnings printed), look at the --print-columns output and
adjust the pattern lists in `parse_sheet.py` → `find_col()` calls accordingly.

## Steps 3 & 4 — Run in parallel

Steps 3 and 4 are independent — kick them off at the same time and wait for both before continuing to Step 5.

---

## Step 3 — Fetch Redash SQL for each alert

`parse_sheet.py` produces:
- `alert_links.json` — `{ alert_name: url }` (best-effort; single-alert cells are reliable, multi-alert cells are a fallback)
- `unique_alerts.json` — sorted list of all unique alert names in scope

**Why two files**: XLSX cells can only carry one hyperlink each. A cell with
"Alert_A, Alert_B" gives one URL. We resolve this by preferring cells where
the alert appears alone (unambiguous). Any alert that only ever appears in
multi-alert cells may get the wrong URL. `unique_alerts.json` ensures we never
miss an alert even if its URL is uncertain.

**For each alert in `unique_alerts.json`:**
1. Check `alert_links.json` for a URL
2. Use the Redash plugin to open the URL and fetch the query SQL
3. If no URL is found (alert printed as "NO URL found" in parse output), search
   Redash by the alert name directly using the plugin's search capability
4. Store results as `alert_queries.csv`: `alert_name, redash_url, sql_text`

Any alert for which SQL cannot be retrieved (URL broken, query deleted) should
be logged with `sql_text = NULL` and excluded from downstream processing.

## Step 4 — Snowflake is_fraud query

Run this query directly via the Snowflake MCP. If the result set is large enough
to overflow context, immediately write the raw result to
`/tmp/risk_intel/snowflake_fraud.csv` before doing anything else with it.

```sql
WITH payment_ids AS (
  -- Inline the payment IDs from sheet_data.csv
  -- (Claude should construct this from the CSV, e.g. using a VALUES list
  --  or by reading from a stage if > 5000 IDs)
  SELECT COLUMN1::VARCHAR AS payment_id
  FROM VALUES {payment_id_values_list}
),
payment_fraud_tags AS (
  SELECT DISTINCT et.entity_id AS payment_id
  FROM fivetran_cdc.mongo_tagging_tagging.entitytags et
  JOIN fivetran_cdc.mongo_tagging_tagging.tags t ON et.tag = t._id
  WHERE et.entity = 'payment'
    AND et.deleted_at IS NULL
    AND t.deleted_at IS NULL
    AND lower(t.name) IN (
        'reverse-phishing', 'unilateral-vendor-fraud', 'ato',
        'fraudulent-payment', 'eto', 'fraudulent-dm'
    )
),
base AS (
  SELECT
    rp.payment_id,
    rp.last_risk_decision_subcategory,
    rp.last_risk_decision_code_description,
    rp.false_decline_potential,
    rp.last_engine_reason,
    ap.money_flow,
    od.risk_ode_decision,
    od.risk_ode_decision_labels::varchar AS risk_ode_decision_labels,
    rpl.mo_label,
    loss_attr.highlevelclaim,
    loss_attr.reason,
    pft.payment_id AS fraud_tag_payment_id
  FROM payment_ids pid
  JOIN PROD.ANALYTICS.RISK_PAYMENTS rp ON rp.payment_id = pid.payment_id
  LEFT JOIN prod.analytics.analytics_payments_history ap
    ON ap.payment_id = rp.payment_id AND ap.data_interval_end = (SELECT MAX(data_interval_end) FROM prod.analytics.analytics_payments_history)
  LEFT JOIN prod.analytics.payments_parent_vendor_history p
    ON p.id = rp.payment_id AND p.data_interval_end = (SELECT MAX(data_interval_end) FROM prod.analytics.payments_parent_vendor_history)
  LEFT JOIN prod.analytics.organization_dim_history od
    ON od.organization_id = p.organizationid AND od.data_interval_end = (SELECT MAX(data_interval_end) FROM prod.analytics.organization_dim_history)
  LEFT JOIN prod.analytics.risk_payments_labeling_history rpl
    ON rpl.payment_id = rp.payment_id AND rpl.data_interval_end = (SELECT MAX(data_interval_end) FROM prod.analytics.risk_payments_labeling_history)
  LEFT JOIN FIVETRAN_CDC.FVTRN_MELIO.LOSSES loss_attr
    ON loss_attr.paymentid = rp.payment_id
  LEFT JOIN payment_fraud_tags pft ON pft.payment_id = rp.payment_id
)
SELECT
  payment_id,
  CASE
    WHEN (
      (last_risk_decision_subcategory = 'fraud'
       OR last_risk_decision_code_description = 'problematic vendor')
      AND (false_decline_potential = false
           OR false_decline_potential IS NULL
           OR money_flow = 'ar'
           OR last_engine_reason IN ('declineKybMoneyInReject','declineVendorOrgRejectFraud'))
    )
    OR (risk_ode_decision = 'reject' AND risk_ode_decision_labels ILIKE '%stolen data%')
    OR mo_label IN ('FRAUD OTHER','VENDOR-SIDE FRAUD','ATO','SF','3RD PARTY ATO')
    OR (lower(highlevelclaim) = 'fraud'
        AND (lower(reason) <> 'friendly fraud' OR reason IS NULL))
    OR fraud_tag_payment_id IS NOT NULL
    THEN true ELSE false
  END AS is_fraud
FROM base
```

Save the result as `snowflake_fraud.csv` (`payment_id`, `is_fraud`).

**Payment ID list size**: construct the `payment_ids` CTE as a VALUES list directly
in the query. If the list is very large (> ~5000 IDs) and the MCP call becomes
unwieldy, write the IDs to a file and use a Snowflake stage or temp table instead:
```sql
CREATE TEMPORARY TABLE tmp_payment_ids (payment_id VARCHAR);
INSERT INTO tmp_payment_ids VALUES (...);
-- then replace the payment_ids CTE with: SELECT payment_id FROM tmp_payment_ids
```

---

## Step 5 — Calculate precision

```bash
python <skill_dir>/scripts/calculate_precision.py \
    --sheet    /tmp/risk_intel/sheet_data.csv \
    --sf-fraud /tmp/risk_intel/snowflake_fraud.csv \
    --output   /tmp/risk_intel/precision.csv
```

The script prints alerts above 10% precision threshold at the end — useful sanity check.

## Step 6 — Output

Copy `precision.csv` and `alert_queries.csv` to the outputs folder and present them.
Print the count of alerts above 10% precision in at least one window.

These two files are the inputs for `risk-alert-incremental-value`.
