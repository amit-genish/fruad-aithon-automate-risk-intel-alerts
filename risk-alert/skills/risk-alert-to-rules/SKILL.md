---
name: risk-alert-to-rules
description: Converts Redash SQL alert queries into candidate json-rules-engine rule definitions using Chalk feature names. Use this skill after /risk-alert-incremental-value has run — it reads incremental_value.csv to find alerts passing the threshold, fetches their SQL from alert_queries.csv, maps SQL conditions to Chalk features, and outputs rule JSON candidates ready for strategy-builder review. Trigger when asked to: convert alerts to rules, generate rule JSON from alert SQL, translate alerts to chalk features, create strategy rules from alert analysis, or after incremental value analysis.
---

# Risk Alert → Rule Converter

Reads the incremental value output, finds qualifying alerts, maps their SQL WHERE conditions to Chalk features, and outputs candidate rule JSON for the strategy-builder.

## Workflow

### Step 1: Find alerts passing the threshold

Read `incremental_value.csv` from the run directory (default: `/tmp/risk_intel/`).
Filter rows where `passes_threshold` is `True`.
Group by `alert_name` — an alert qualifies if it passes in either `last_2w` or `mature_90_30` timeframe.

### Step 2: Fetch SQL for qualifying alerts

Read `alert_queries.csv` from the same directory.
For each qualifying alert, find its row by `alert_name` and extract `sql_text`.

### Step 3: Analyze and map conditions

For each qualifying alert, work through the SQL WHERE clause systematically:

1. **Identify the table alias → Chalk namespace** using the mapping table in `references/sql-to-feature-mapping.md`
2. **Map each condition** to a Chalk feature + operator + value
3. **Preserve logical structure**: SQL `AND` → `all` group; SQL `OR` → `any` group; SQL `NOT` → `not: true` on the condition
4. **Attempt to find every condition before declaring it unmapped** — see rule below

**Rule: grep both fact sources before giving up.** A condition is only "unmapped" after you have searched both the Chalk feature store (`feature-fetcher-item-datum.ts`) and the OrchestrationItemDatum enum (`orchestration-item-datum-types.ts`) with at least two keyword variants, and found nothing relevant. "Not in the mapping table" ≠ "unmapped."

See `references/sql-to-feature-mapping.md` for clone/pull instructions, grep patterns, the superset relationship between the two files, and confirmed active facts.

Only move a condition to `unmapped_conditions` after both searches return nothing relevant.

### Step 4: Generate rule JSON candidates

For each qualifying alert, output a **JSONC file** named `rule_candidate_{snake_case_alert_name}.jsonc` to the run directory. JSONC (JSON with comments) is used so each `fact` value can be annotated with its full enum key — this makes the file ready to paste into `strategy-builder-api` TypeScript rules.

Use the **engine DB format** (consumed directly by json-rules-engine — see `references/rule-format.md`):

```jsonc
{
  "name": "<Alert Name> — candidate",
  "priority": 10,
  "conditions": {
    "all": [
      {
        "fact": "payment.melio_db__raw__amount", // FeatureFetcherItemDatum.PaymentMelioDbRawAmount
        "path": "$.value",
        "operator": "greaterThan",
        "value": 1500
      },
      {
        "fact": "ato-v3-score", // AtoV3ItemDatum.AtoV3Score
        "path": "$.value",
        "operator": "greaterThan",
        "value": 20
      }
    ]
  },
  "event": {
    "type": "decision",
    "params": {
      "decision": "pending",
      "mos": [],
      "subcategory": "fraud",
      "reason": "<alert name>"
    }
  },
  "_conversion_notes": {
    "source_alert": "<alert_name>",
    "redash_url": "<url from alert_queries.csv>",
    "alert_stats": {
      "bad_rate_pct": "<from incremental_value.csv>",
      "monthly_fraud_tpv": "<from incremental_value.csv>"
    },
    "unmapped_conditions": ["<SQL conditions with no fact equivalent>"],
    "analyst_notes": ["<anything requiring human review>"]
  }
}
```

Key rules:
- `path` is always `"$.value"` for every fact type — both FeatureFetcherItemDatum (Chalk) and OrchestrationItemDatum use `{ value: X }` at runtime
- Every `fact` string must have an inline comment with its full enum key (e.g., `// AtoV3ItemDatum.AtoV3Score` or `// FeatureFetcherItemDatum.PaymentMelioDbRawAmount`)
- `decision` defaults to `"pending"` — conservative, triggers manual review
- Leave `mos`, `subcategory`, `riskDecisionCodeId`, `labelIds`, `limitations` for the analyst
- `_conversion_notes` is non-engine metadata and won't affect rule execution
- For BETWEEN: produce two conditions (`greaterThanInclusive` + `lessThanInclusive`) in the same `all` group
- Routing numbers: keep as strings in the `in` array even if SQL uses integers

### Step 5: Write a summary report

Write `rule_conversion_report.md` to the run directory with:
- List of alerts processed
- Per-alert: conditions mapped vs. unmapped
- Missing Chalk features (fields used in SQL that don't exist in the feature store)
- Recommended next steps (e.g., "Add `payment.melio_db__raw__partner_name` to Chalk before finalizing this rule")

---

## Important: Fields NOT in the Chalk feature store

Several common SQL fields from alert queries **have no Chalk feature equivalent** as of 2025-06. These cannot be used directly as rule conditions and must be noted:

| SQL field | Status | Recommendation |
|---|---|---|
| `p.riskstatus` | Not in Chalk | Usually a query filter, not needed in rule logic (engine sets risk status) |
| `p.status` | Not in Chalk | Exclude from rule; engine controls payment status |
| `p.partnername <> 'paypal'` / `<> 'fiserv_us-bank'` | **Skip intentionally** | Strategies always filter out paypal and fiserv at the engine level — these conditions are redundant and should be omitted from the rule. Mention in `_conversion_notes` that they were intentionally excluded for this reason. |
| `p.isfinanced` | Not in Chalk | Flag as missing — needs new feature |
| `dwa.aba` (domestic wire ABA) | Not verified in Chalk | Check `delivery_method.*` or flag as missing |
| `red.*` (RISKENGINEDECISIONS) | Not in Chalk | Skip — engine decisions aren't used as rule inputs |
| `rp.org_ode_decision` | Use ODE label features | See `organization.*` ODE label features |

---

## Reference files

- `references/sql-to-feature-mapping.md` — full SQL column → Chalk feature mapping table + operator mapping
- `references/rule-format.md` — rule JSON format examples with nested conditions, operators, and special value types
