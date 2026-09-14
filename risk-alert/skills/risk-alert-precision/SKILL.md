---
name: risk-alert-precision
description: >
  Download the Risk Intel Alerts Google Sheet, fetch Redash SQL for each alert,
  and optionally calculate precision against Snowflake is_fraud data.

  Supports two modes:
  - skip (default in E2E pipeline): download sheet + fetch Redash SQL only →
    outputs alert_queries.csv. No precision calculation.
  - full: additionally joins with Snowflake is_fraud and outputs precision per
    alert per sliding time window (1d, 3d, 1w, 2w, 1m) → outputs both
    precision.csv and alert_queries.csv.

  USE THIS SKILL whenever asked to: calculate alert precision, run the risk
  intel alerts analysis, scrape the alerts sheet, check which alerts are catching
  fraud, or produce the daily/weekly precision report. Also the first step of the
  full E2E pipeline.
---

# Risk Alert Precision Skill

Supports two modes controlled by `PRECISION_MODE`:

| Mode | Steps | Outputs |
|------|-------|---------|
| `skip` (default in E2E) | 1 → 2 → 3 | `alert_queries.csv` only |
| `full` | 1 → 2 → 3 → 4 → 5 | `alert_queries.csv` + `precision.csv` |

## Mode: skip

Use when you only need the alert list and their SQL (e.g. E2E pipeline skipping
precision stage). Stops after Step 3 — no Snowflake query, no precision.csv.

## Mode: full

Runs all steps including Snowflake is_fraud fetch and precision calculation.
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
# Use uv (preferred). A pyproject.toml exists at <skill_dir>/pyproject.toml — uv sync installs deps.
uv sync --project <skill_dir> -q

# First run: verify column detection
uv run --project <skill_dir> python <skill_dir>/scripts/parse_sheet.py /tmp/risk_intel/alerts.xlsx --print-columns

# Full parse — last 30 days (MVP scope)
uv run --project <skill_dir> python <skill_dir>/scripts/parse_sheet.py /tmp/risk_intel/alerts.xlsx \
    --out-dir /tmp/risk_intel/ --scope-days 30

# Post-MVP: extend to 90 days
# uv run --project <skill_dir> python <skill_dir>/scripts/parse_sheet.py /tmp/risk_intel/alerts.xlsx \
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

**`sql_text` MUST be the verbatim SQL returned by the Redash plugin — never
summarize, compress, or manually transcribe it.** Truncated or rewritten SQL breaks
the incremental value step in hard-to-diagnose ways (missing ORDER BY in QUALIFY,
dropped GROUP BY, wrong aliases).

Any alert for which SQL cannot be retrieved (URL broken, query deleted) should
be logged with `sql_text = Fail to fetch` and excluded from downstream processing.

## Step 4 — Snowflake is_fraud query

`PROD.ANALYTICS.RISK_PAYMENTS` has pre-computed `IS_FRAUD` and `IS_BAD` columns —
use them directly. Run via the TypeScript script (no MCP, no batching):

```bash
# First time in a new shell: install deps
cd <skill_dir> && npm install

# Run (opens a browser SSO window on first use per session)
SNOWFLAKE_USER=<your-email> \
  npx tsx <skill_dir>/scripts/fetch_fraud.ts \
    /tmp/risk_intel/sheet_data.csv \
    /tmp/risk_intel/snowflake_fraud.csv
```

`SNOWFLAKE_ACCOUNT` is read from the environment (set in ~/.zshrc).
Optional env vars: `SNOWFLAKE_AUTHENTICATOR` (default: `EXTERNALBROWSER`),
`SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE`.

The script issues a single VALUES-CTE query for all IDs — no batching needed.
Any payment_id not in RISK_PAYMENTS is written with `is_fraud=false`.
Output: `snowflake_fraud.csv` with columns `payment_id, is_fraud, is_bad`.

---

## Step 5 — Calculate precision *(full mode only)*

```bash
python <skill_dir>/scripts/calculate_precision.py \
    --sheet    /tmp/risk_intel/sheet_data.csv \
    --sf-fraud /tmp/risk_intel/snowflake_fraud.csv \
    --output   /tmp/risk_intel/precision.csv
```

The script prints alerts above 10% precision threshold at the end — useful sanity check.

## Step 6 — Output

**If `PRECISION_MODE=skip`:** copy `alert_queries.csv` to the outputs folder.
No `precision.csv` is written — this is intentional. The incremental value skill
will treat all alerts as qualifying when `precision.csv` is absent.

**If `PRECISION_MODE=full`:** copy both `precision.csv` and `alert_queries.csv`
to the outputs folder. Print the count of alerts above 10% precision in at least
one window.

Both files (when present) are inputs for `risk-alert-incremental-value`.
