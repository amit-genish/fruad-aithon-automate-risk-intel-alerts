---
name: risk-alert-e2e
description: >
  End-to-end Risk Intel Alert automation pipeline. Runs the full flow:
  (optional) precision calculation → incremental value analysis → rule conversion →
  Slack notifications. Scheduled to run Mon–Sat early morning TLV time.
  By default skips the precision stage and goes straight to incremental value
  using all unique alert names from the last ~30 days of the spreadsheet.
  Chains risk-alert-incremental-value, risk-alert-to-rules, and
  risk-alert-slack-notifier in sequence (plus optionally risk-alert-precision),
  passing files between steps via a shared run directory.

  USE THIS SKILL when asked to run the full risk alert pipeline, trigger the
  daily E2E flow, or set up the automated daily risk alert run. Also the entry
  point when the scheduler fires the daily task.
---

# Risk Alert E2E Skill

Orchestrates the pipeline skills in sequence. Each step writes outputs
to a shared run directory; the next step reads from it.

## Run directory

All files for a single run are stored under:
```
/tmp/risk_intel_runs/{YYYYMMDD}/
```

## Pipeline (skip mode — default)

```
risk-alert-precision  [PRECISION_MODE=skip]
  ↓ alert_queries.csv  (no precision.csv)
risk-alert-incremental-value  (all alerts qualify — no precision filter)
  ↓ incremental_value.csv
risk-alert-to-rules
  ↓ rule_candidate_*.json, rule_conversion_report.md
risk-alert-slack-notifier
```

## Pipeline (full mode)

```
risk-alert-precision  [PRECISION_MODE=full]
  ↓ precision.csv + alert_queries.csv
risk-alert-incremental-value  (filters alerts with precision_pct >= 10)
  ↓ incremental_value.csv
risk-alert-to-rules
  ↓ rule_candidate_*.json, rule_conversion_report.md
risk-alert-slack-notifier
```

---

## Pre-flight: check prerequisites

Before running any pipeline step, verify that all required tools and dependencies
are installed and up to date. Run the following checks:

### 1. Locate the skill directory

The plugin installs skills under:
```
~/.claude/plugins/marketplaces/aithon/risk-alert/skills/
```

Set `SKILL_ROOT` to that path, and `PRECISION_DIR` / `IV_DIR` to the
`risk-alert-precision` and `risk-alert-incremental-value` subdirectories.

```bash
SKILL_ROOT="$HOME/.claude/plugins/marketplaces/aithon/risk-alert/skills"
PRECISION_DIR="$SKILL_ROOT/risk-alert-precision"
IV_DIR="$SKILL_ROOT/risk-alert-incremental-value"
```

### 2. Python / uv (for precision skill)

```bash
# Check uv is installed
which uv || { echo "ERROR: uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }

# Sync dependencies (installs if missing, upgrades if stale)
uv sync --project "$PRECISION_DIR" -q
echo "Python deps: OK"
```

### 3. Node.js / npm (for precision + incremental value Snowflake scripts)

```bash
# Check Node ≥ 18
node --version || { echo "ERROR: node not found. Install via nvm or brew."; exit 1; }

# Install npm deps for precision skill
(cd "$PRECISION_DIR" && npm ci --silent) && echo "precision npm deps: OK"

# Install npm deps for incremental-value skill
(cd "$IV_DIR" && npm ci --silent) && echo "incremental-value npm deps: OK"
```

### 4. Snowflake environment variable

```bash
# SNOWFLAKE_PAT must be set
[[ -n "$SNOWFLAKE_PAT" ]] || { echo "ERROR: SNOWFLAKE_PAT not set."; exit 1; }
echo "Snowflake env: OK"
```

If any prerequisite check fails: stop and report clearly which tool is missing
and how to install it. Do not proceed to the pipeline steps.

---

## Ask: Precision mode?

After prerequisites pass, ask the user (or check the invocation context):

> **Run the precision stage, or skip it and go straight to incremental value?**
>
> - `skip` *(default)* — download the spreadsheet, extract all unique alert names from the
>   last ~30 days, fetch their Redash SQL, and proceed directly to incremental value.
>   All alerts are treated as qualifying (no precision threshold applied).
> - `full` — run the complete `risk-alert-precision` skill first; only alerts with
>   `precision_pct >= 10` proceed to incremental value.

If invoked by the scheduler (non-interactive), default to `skip`.
If invoked interactively and no preference is given, default to `skip` without asking.

Store the answer as `PRECISION_MODE` (`skip` or `full`).

---

## Ask: Slack or report only?

After the precision mode is set, ask the user (or check the invocation context):

> **Send Slack messages to #risk_fraud_squad, or write a local report only?**
>
> - `slack` — full Slack thread per alert + rule candidate replies (production mode)
> - `report` — write all results to `{run_dir}/report.md` only, no Slack messages sent

If invoked by the scheduler (non-interactive), default to `slack`.
If invoked interactively and no preference is given, ask before proceeding.

Store the answer as `OUTPUT_MODE` (`slack` or `report`) and pass it to Step 5.

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

Invoke the `risk-alert-precision` skill with `PRECISION_MODE` set to the value
chosen above. Pass `run_dir` as the output directory.

| Mode | Expected outputs |
|------|-----------------|
| `skip` | `alert_queries.csv` only |
| `full` | `alert_queries.csv` + `precision.csv` |

If the step fails or produces 0 alerts: log error, post a failure notice to
`#risk_fraud_squad` (if `OUTPUT_MODE=slack`), and stop.

### Step 3 — Run risk-alert-incremental-value

Invoke `risk-alert-incremental-value`. Read inputs from `run_dir`, write outputs
to `run_dir`.

**Important — precision filter behaviour:**
- If `precision.csv` exists in `run_dir` (`PRECISION_MODE=full`): the IV skill applies
  its normal filter (keep alerts with `precision_pct >= 10`).
- If `precision.csv` is absent (`PRECISION_MODE=skip`): skip Step 1 of the IV skill
  entirely and treat **all** alerts in `alert_queries.csv` as qualifying.

Expected outputs:
- `incremental_value.csv`
- `assumptions_{alert}.txt` files (one per alert processed)

Check: if no alerts pass the threshold, skip Steps 4–5 and proceed directly to
Step 6 (notifier handles the "nothing to report" case gracefully).

### Step 4 — Run risk-alert-to-rules

Invoke `risk-alert-to-rules` on the current `run_dir`.

Expected outputs in `run_dir`:
- `rule_candidate_{alert_name}.json` — one file per qualifying alert
- `rule_conversion_report.md` — summary of mapped vs. unmapped conditions and
  recommended activation order

If the rule conversion step fails (e.g., Chalk feature store unavailable), log
the error but do **not** stop the pipeline — proceed to Step 5. Include a
failure note in the final output.

### Step 5 — Deliver results

**If `OUTPUT_MODE=slack`:** Invoke `risk-alert-slack-notifier`. Read all inputs
from `run_dir`. The notifier handles rule content in-thread: for each qualifying
alert it will check for `rule_candidate_{alert_name}.json` and the corresponding
section in `rule_conversion_report.md`, then post them as additional replies.

**If `OUTPUT_MODE=report`:** Write `{run_dir}/report.md` with:
- Run metadata (date, counts)
- Per-alert section: precision table, incremental value table, SQL, rule candidate JSON
- Rule conversion notes
Print the path to the report at the end. Do NOT send any Slack messages.

### Step 6 — Run summary

Print to stdout:
```
✅ Risk Intel E2E run complete
Run date: {date}
Precision stage: {skipped | N alerts evaluated, M above threshold}
Incremental value step: {N} alerts processed, {K} passed escalation threshold
Rule conversion: {J} rule candidates written, {R} fully mappable
Output: {K} threads sent to #risk_fraud_squad  |  Report: {run_dir}/report.md
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

When scheduled, `OUTPUT_MODE` defaults to `slack`.

---

## Error handling

| Failure point | Action |
|--------------|--------|
| Prerequisite missing | Print install instructions, stop |
| Precision step fails (`PRECISION_MODE=full`) | Post to #risk_fraud_squad (slack mode) or write to report (report mode): "⚠️ Risk Intel daily run failed at precision step. Manual check required." Stop. |
| Spreadsheet download fails (`PRECISION_MODE=skip`) | Post/write: "⚠️ Risk Intel daily run failed: could not download alert spreadsheet." Stop. |
| Zero alert names extracted from spreadsheet | Post/write: "📋 Risk Intel run: no alerts found in last 30 days of spreadsheet." Stop. |
| Incremental value step fails for one alert | Log, continue with others, include failures in run summary |
| Rule conversion step fails entirely | Log error, continue to output step; output will skip rule content for all alerts |
| Rule conversion: Chalk feature store unavailable | Flag all conditions as unmapped, still write skeleton rule files |
| Slack send fails | Log, continue, report at end |
| Zero alerts pass IV threshold | Post/write: "📋 Risk Intel run: no alerts passed the escalation threshold today." |

---

## Post-MVP: result storage

Once the storage deliverable is ready, add a step that copies `run_dir`
contents to a persistent location (e.g., S3/GCS bucket or Snowflake stage)
keyed by run date.
