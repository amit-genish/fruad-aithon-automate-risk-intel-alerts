# SQL → Chalk Feature Mapping Reference

## Table Alias → Chalk Namespace

| SQL alias | Snowflake table | Chalk namespace |
|---|---|---|
| `p` | PAYMENTS | `payment.*` |
| `o` / `org` | ORGANIZATIONS | `organization.*` |
| `v` | VENDORS | `vendor.*` |
| `dm` | DELIVERYMETHODS | `delivery_method.*` |
| `ba` | BANKACCOUNTS | `delivery_method.*` (bank account fields live on the DM namespace) |
| `dwa` | DOMESTICWIREACCOUNTS | `delivery_method.*` (wire-specific DM fields) |
| `fs` | FUNDINGSOURCES | `funding_source.*` |
| `u` | USERS | `user.*` |
| `red` | RISKENGINEDECISIONS | ❌ Not available as Chalk features |
| `rp` | RISK_PAYMENTS | Partial — use `organization.*` ODE label features |
| `la` / `od` | ORGANIZATIONDECISIONS | Use `organization.*` ODE label/decision features |

---

## Column → Chalk Feature Mapping

### Payment fields (`p.*`)

| SQL column | Chalk feature | Notes |
|---|---|---|
| `p.amount` | `payment.melio_db__raw__amount` | Cast string literals to number: `'12000'` → `12000` |
| `p.createdat` | `payment.melio_db__raw__created_at` | Use `isDateWithinRange` for relative dates |
| `p.createorigin` | `payment.melio_db__raw__create_origin` | e.g., `'user-signup'`, `'ar-invoice'`, `'request'` |
| `p.isfinanced` | ❌ Not in Chalk | Flag as missing |
| `p.riskstatus` | ❌ Not in Chalk | Skip — engine sets this, not a valid rule input |
| `p.status` | ❌ Not in Chalk | Skip |
| `p.partnername <> 'paypal'` / `<> 'fiserv_us-bank'` | **Skip intentionally** | Strategies always filter out paypal and fiserv at the engine level — these conditions are redundant. Omit from rule; note in `_conversion_notes` that they were intentionally skipped because the strategy engine enforces these exclusions globally. |
| `p.scheduleddate` | `payment.melio_db__raw__scheduled_date` | |
| `p.moneyDirection` | `payment.melio_db__raw__money_direction` | |

**Derived payment delivery type features** (use instead of joining on `dm.deliverytype` when checking payment-level type):
- `payment.derived__is__delivery_type_ach`
- `payment.derived__is__delivery_type_check`
- `payment.derived__is__delivery_type_wire`
- `payment.derived__is__delivery_type_card`

### Organization fields (`o.*`)

| SQL column | Chalk feature | Notes |
|---|---|---|
| `o.createdat` | `organization.melio_db__raw__created_at` (via `organization.derived__num__days_since_created_at`) | Use date features |
| `o.createorigin` | `organization.melio_db__raw__create_origin` | |
| `o.taxidtype` | `organization.melio_db__raw__tax_id_type` | Values: `'SSN'`, `'EIN'` |
| `o.companyname` | `organization.melio_db__raw__company_name` (check exact name) | |
| ODE decision (via `od.decision`) | `organization.derived__is__ode_label_*` | e.g., `organization.derived__is__ode_label_ato`, `...fraud`, `...abuse` |

### Delivery method fields (`dm.*` / `ba.*`)

| SQL column | Chalk feature | Notes |
|---|---|---|
| `dm.deliverytype` | `delivery_method.melio_db__raw__delivery_type` | Values: `'ach'`, `'check'`, `'domestic_wire'` |
| `dm.createdat` | `delivery_method.melio_db__raw__created_at` | Use `isDateWithinRange` for relative dates |
| `dm.isverified` | `delivery_method.melio_db__raw__is_verified` (verify exact name) | |
| `ba.routingnumber` / `dm.routingnumber` | `delivery_method.melio_db__normalized_routing_number` | Use `in` operator for list checks; keep as strings |
| `dwa.aba` (domestic wire ABA) | `delivery_method.melio_db__raw__domestic_wire_account_aba` | Use `equal` or `in` operator |

**Routing number risk features** (prefer over raw routing number when checking for risky routing numbers):
- `normalized_routing_number.snowflake__tag__routing_number_risk_category` — risk tag for the routing number
- `normalized_routing_number.snowflake__rate__bad_organization_rate_with_payments_last_6_months` — bad org rate

### Vendor fields (`v.*`)

| SQL column | Chalk feature | Notes |
|---|---|---|
| `v.contactemail` | `vendor.melio_db__raw__contact_email` (verify exact name) | Use `isNotNull` / `isNull` |

### IP / Whitepages / risk score fields

These come from `red.modelResult` JSON in SQL but are available directly as Chalk features:

| SQL modelResult field | Chalk feature |
|---|---|
| `features.payorWpClientIpNetworkScore` | `payment.wp__num__payor_client_ip_network_score` |
| `features.payorWpClientIpIdentityScore` | `payment.wp__num__payor_client_ip_identity_score` |
| `features.payorWpClientIpIsProxy` | `payment.wp__num__payor_registration_ip_is_proxy` |

---

## Operator Mapping

| SQL | Rule operator |
|---|---|
| `=` | `equal` |
| `<>` / `!=` | `notEqual` |
| `>` | `greaterThan` |
| `>=` | `greaterThanInclusive` |
| `<` | `lessThan` |
| `<=` | `lessThanInclusive` |
| `IN (...)` | `in` with array value |
| `NOT IN (...)` | `notIn` with array value |
| `IS NULL` | `isNull` with `value: null` |
| `IS NOT NULL` | `isNotNull` with `value: null` |
| `BETWEEN a AND b` | two conditions: `greaterThanInclusive a` AND `lessThanInclusive b` |
| `LIKE 'X%'` | `regExpMatches` with `{ "pattern": "^X", "flags": "i" }` |
| `>= dateadd(day, -N, current_date())` | `isDateWithinRange` with `{ "daysOffset": -N, "inclusive": true }` |

---

## Logical Structure

| SQL | Rule structure |
|---|---|
| `A AND B AND C` | `{ "all": [A, B, C] }` |
| `A OR B OR C` | `{ "any": [A, B, C] }` |
| `A AND (B OR C)` | `{ "all": [A, { "any": [B, C] }] }` |
| `NOT (A AND B)` | top-level group with `"not": true` on the `all` subgroup |
| `(A OR B) AND (C OR D)` | `{ "all": [{ "any": [A, B] }, { "any": [C, D] }] }` |

---

## Hardcoded date issue

If the SQL has an absolute date like `dm.createdat > '2025-12-01'`, this cannot be expressed as a rule condition without converting to a relative offset. Options:

1. **Skip**: If the date filter was a temporary alert constraint, exclude it from the rule
2. **Convert**: Calculate how many days ago that date was from today and use `isDateWithinRange` — but note that this will drift over time
3. **Flag**: Document in `_conversion_notes.analyst_notes` for human decision

---

## Three sources for rule facts

`OrchestrationItemDatum` (defined in `risk-orchestration`) is a **superset union** of all fact sources. Always check all three before declaring a signal unmapped:

| Source | Repo | File | When to search |
|---|---|---|---|
| 1. FeatureFetcher (Chalk) | `chalk-feature-store` | `packages/chalk-typed/src/feature-fetcher-item-datum.ts` | Payment/entity features computed by Chalk |
| 2. RALF | `payment-approval` | `packages/risk-analyzer-features/src/features-datum.ts` | Risk-Analyzer model scores (e.g., `atoV2ModelScore`) |
| 3. OrchestrationItemDatum | `risk-orchestration` | `src/shared/types/orchestration-item-datum/orchestration-item-datum-types.ts` | Executor-specific scores (e.g., `ato-v3-score`); also a superset of 1 and 2 |

Clone/pull each repo before grepping — these enums evolve frequently. Always try at least two keyword variants:

```bash
FF="<chalk-feature-store-root>/packages/chalk-typed/src/feature-fetcher-item-datum.ts"
RALF="<payment-approval-root>/packages/risk-analyzer-features/src/features-datum.ts"
ORCH="<risk-orchestration-root>/src/shared/types/orchestration-item-datum/orchestration-item-datum-types.ts"

grep -i "<keyword1>" "$FF" "$RALF" "$ORCH"
grep -i "<keyword2>" "$FF" "$RALF" "$ORCH"
```

Features can be on any entity type — grep by keyword, not by namespace.

**`path: "$.value"` applies to ALL facts from all three sources.** Every datum result is wrapped as `{ value: X, error?: ... }` at runtime (`OrchestrationItemDatumResult`). Confirmed: `blockExtremeATOV2ModelScore.ts` in `strategy-builder-api` uses `path: '$.value'` on a RALF (`RiskAnalyzerFeaturesItemDatum`) fact.

**MRCA enum rule:** In the `.jsonc` comment, always use the **Most Common Ancestor** enum — `OrchestrationItemDatum` — since it is the superset of all three sources. Never write the sub-enum:
```jsonc
// ✅ correct
"fact": "ato-v3-score" // OrchestrationItemDatum.AtoV3Score
"fact": "atoV2ModelScore" // OrchestrationItemDatum.RiskAnalyzerFeature_atoV2ModelScore
"fact": "payment.melio_db__raw__amount" // OrchestrationItemDatum.PaymentMelioDbRawAmount

// ❌ wrong — sub-enum is too specific
"fact": "ato-v3-score" // AtoV3ItemDatum.AtoV3Score
```

**`path: "$.value"` is always present** — no exceptions across all three sources.

**Confirmed live strategy facts** (as of 2026-09, verified via `fivetran_cdc.decision_engine_decision.strategyconfigurations`):
| Fact string | Source | Enum key |
|---|---|---|
| `atoV2ModelScore` | RALF | `OrchestrationItemDatum.RiskAnalyzerFeature_atoV2ModelScore` |
