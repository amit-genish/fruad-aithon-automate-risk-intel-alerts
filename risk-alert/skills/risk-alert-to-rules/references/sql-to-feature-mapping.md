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

## Two sources for rule facts

Strategy rules can use facts from **two independent sources**. Always check both before declaring a signal unmapped:

### 1. Chalk feature store (`feature-fetcher-item-datum.ts`)

Clone/pull before grepping — do not rely on a stale local copy:

```bash
CHALK="<chalk-feature-store-root>/packages/chalk-typed/src/feature-fetcher-item-datum.ts"

# Always try at least two keyword variants derived from the SQL column name
# e.g. for dwa.aba try "aba" and "domestic_wire"
grep -i "<keyword1>" "$CHALK"
grep -i "<keyword2>" "$CHALK"
```

Features can be on any entity type (`payment`, `organization`, `delivery_method`, `vendor`, `funding_source`, `payment_action`, etc.) — grep by keyword across the whole file, not by namespace. The fact must semantically match the SQL condition, whatever entity it lives on.

The enum value (right side of `=`) is the exact string to use as the `fact`.

### 2. OrchestrationItemDatum enum (`risk-orchestration`)

Orchestration item results (scores produced by executors like ATO v3, AML, etc.) are also available as rule facts. These are **not** Chalk features — they come from a separate pipeline.

Clone/pull `risk-orchestration` before grepping — the enum evolves as new executors are added:

```bash
ORCH="<risk-orchestration-root>/src/shared/types/orchestration-item-datum/orchestration-item-datum-types.ts"

# Try at least two keyword variants — same rule as for Chalk
grep -i "<keyword1>" "$ORCH"
grep -i "<keyword2>" "$ORCH"
```

The enum value (right side of `=`) is the exact string to use as the `fact`. Example:
```typescript
export enum AtoV3ItemDatum {
  AtoV3Score = 'ato-v3-score',   // ← fact: "ato-v3-score"
}
```

**When to look here:** any SQL condition that reads from `PRODUCTION_RISK_ORCHESTRATION_ITEM_EXECUTION_RESULTS` or references an executor key (e.g., `atoV3Executor`, `amlExecutor`).

**Important:** `OrchestrationItemDatum` is a union type that **includes** `FeatureFetcherItemDatum` (i.e., all Chalk feature names are also valid OrchestrationItemDatum keys). Grepping the orchestration file will therefore surface Chalk features too. The distinction matters for the rule engine: Chalk features use `path: "$.value"` because the runtime wraps them as `{ value: X }`. To verify how a datum key behaves in live rules — especially whether it uses `path: "$.value"` or not — query `prod.analytics.risk_strategy_decision_features` for recent decisions that include the datum key and inspect its structure in the result JSON.

**Confirmed active OrchestrationItemDatum facts in live strategies** (as of 2026-09):
| Datum key | Enum | Used in strategies |
|---|---|---|
| `ato-v3-score` | `AtoV3ItemDatum.AtoV3Score` | ap-fraud, policy, compliance, full |
