---
name: risk-alert-aggregation
description: >
  Analyze risk alert effectiveness at catching bad payments. Downloads the Risk Intel Alerts
  Google Sheet via the Google Drive connector, joins with Snowflake PROD.ANALYTICS.RISK_PAYMENTS
  (MO_LABEL), and exports a CSV with per-alert and per-alert-combination counts of total/good/bad
  payments and % bad, broken down by time windows 1d, 3d, 1w, 1m.
  
  USE THIS SKILL whenever Amit asks to: run the alert analysis, check alert effectiveness,
  process the Risk Intel Alerts table, aggregate alerts, see which alerts catch bad payments,
  or generate the weekly/daily alert report. Also use it if asked to refresh or re-run the
  analysis, even if phrased casually ("can you run the alerts thing again?").
---

# Risk Alert Aggregation Skill

Produces a deterministic CSV showing how effective each risk alert is at catching bad payments,
split across four time windows and broken down by individual alert codes and alert combinations.

## What "bad payment" means

| Source | Column | Bad when |
|--------|--------|----------|
| Risk Intel Alerts table | `Risk` | value == `"Bad Payment"` |
| Snowflake `PROD.ANALYTICS.RISK_PAYMENTS` | `MO_LABEL` | value in `{"SF", "ATO"}` |

**Worst case**: a payment is **bad** if EITHER source says so. Good = both say clean (or no SF record).

## Output structure

The output CSV has one row per (time_window × alert_type × alert) with columns:

| Column | Description |
|--------|-------------|
| `time_window` | `1d`, `3d`, `1w`, or `1m` |
| `alert_type` | `individual` (single alert, split by comma) or `joined` (full combination string) |
| `alert` | The alert code or combination |
| `total_payments` | Payments that triggered this alert in the window |
| `good_payments` | Payments classified good |
| `bad_payments` | Payments classified bad |
| `pct_bad` | `bad / total × 100` |

---

## Running the analysis

### Step 1 — Download the Google Sheet as CSV

The sheet ID is `1BVjaJlIGpSWhH1IJBOkwAr7xWqMB8IdbtkkWFyRkoQU`.

Use the Google Drive connector tool `download_file_content`:
```
fileId: "1BVjaJlIGpSWhH1IJBOkwAr7xWqMB8IdbtkkWFyRkoQU"
exportMimeType: "text/csv"
```

The result is a JSON object with a `content` field containing the base64-encoded CSV.
Decode it and save to `/tmp/risk_intel_alerts.csv`:

```python
import base64, json
result = <tool output>
csv_bytes = base64.b64decode(result["content"])
with open("/tmp/risk_intel_alerts.csv", "wb") as f:
    f.write(csv_bytes)
```

> If the Google Drive connector is unavailable, ask Amit to export the sheet manually
> (File → Download → CSV) and provide the local path.

### Step 2 — Confirm column names (first run only)

```bash
pip install pandas --break-system-packages -q
python <skill_dir>/scripts/analyze_alerts.py /tmp/risk_intel_alerts.csv --print-columns
```

This prints all column names and a sample row. Verify that the defaults match:
- Payment ID column: `"Payment ID"`
- Risk status column: `"Risk"`
- Alerts column: `"Alert Codes"` ← **most likely to differ — check this one**
- Created-at column: `"Created At"`

If the alerts column is named differently (e.g., `"Alert"`, `"Alerts"`, `"Alert Category"`),
pass `--alerts-col "Actual Column Name"` in subsequent steps.

### Step 3 — Get Snowflake credentials

Ask Amit for, or look for in context:
- Snowflake account identifier (e.g., `xero.us-east-1.snowflakecomputing.com` → use `xero.us-east-1`)
- Username and password
- Role (optional, leave blank for default)

Prefer environment variables to avoid secrets in shell history:
```bash
export SF_ACCOUNT="..."
export SF_USER="..."
export SF_PASSWORD="..."
```

### Step 4 — Run the analysis

```bash
pip install pandas snowflake-connector-python --break-system-packages -q

SCRIPT="<skill_dir>/scripts/analyze_alerts.py"
OUTPUT="/tmp/alert_aggregation_$(date +%Y%m%d).csv"

python "$SCRIPT" /tmp/risk_intel_alerts.csv \
  --alerts-col "Alert Codes" \
  --output "$OUTPUT" \
  --sf-account "$SF_ACCOUNT" \
  --sf-user "$SF_USER" \
  --sf-password "$SF_PASSWORD"
```

Add `--sf-role <role>` if needed. Add `--skip-snowflake` to run on main table data only
(e.g., for a quick sanity check when Snowflake is unavailable).

The script prints a summary table to stdout while running.

### Step 5 — Present results

Copy the output CSV to the outputs folder and present it with `present_files`.
Also paste the printed summary table into chat so Amit can see the headline numbers
without opening the file.

---

## Scheduling (daily run)

After confirming the analysis works, offer to schedule it:

> "Want me to schedule this to run automatically every morning?"

Use the `schedule` skill to create a daily task that runs steps 1–5 above.
Save the output to a dated file in Amit's outputs folder each time.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Column not found` error | Run `--print-columns` to see actual names; adjust `--alerts-col` etc. |
| Snowflake auth failure | Check account identifier format — it should NOT include `.snowflakecomputing.com` |
| `Bad Payment` not found in Risk column | Run `--print-columns` and inspect the sample row; the exact string may differ |
| All payments show as good | Confirm Snowflake PAYMENT_ID type matches the alerts table (both strings or both ints) |
| Timestamps parse as NaT | The Created At column may have a non-standard format; check `--print-columns` sample |

To change what counts as "bad", edit the top of `scripts/analyze_alerts.py`:
```python
BAD_RISK_VALUES = {"Bad Payment"}   # ← add/remove values here
BAD_MO_LABELS   = {"SF", "ATO"}    # ← add/remove values here
```
