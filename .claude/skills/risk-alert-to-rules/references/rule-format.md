# Rule JSON Format Reference

Rules use the **engine DB format** consumed by json-rules-engine directly.

## Basic structure

```json
{
  "name": "Rule name",
  "priority": 10,
  "conditions": {
    "all": [ ...conditions ]
  },
  "event": {
    "type": "decision",
    "params": {
      "decision": "pending",
      "mos": [],
      "subcategory": "fraud",
      "reason": "Human-readable reason"
    }
  }
}
```

## Leaf condition (single fact check)

```json
{
  "fact": "payment.melio_db__raw__amount",
  "path": "$.value",
  "operator": "greaterThanInclusive",
  "value": 5000
}
```

- `path` is always `"$.value"` for Chalk features
- Omit `path` only for special synthetic facts

## Condition group: AND (all must match)

```json
{
  "all": [
    { "fact": "...", "path": "$.value", "operator": "equal", "value": "ach" },
    { "fact": "...", "path": "$.value", "operator": "greaterThan", "value": 1000 }
  ]
}
```

## Condition group: OR (any must match)

```json
{
  "any": [
    { "fact": "payment.melio_db__raw__amount", "path": "$.value", "operator": "greaterThan", "value": 1500 },
    { "fact": "payment.melio_db__raw__create_origin", "path": "$.value", "operator": "in", "value": ["ar-invoice", "request"] }
  ]
}
```

## Nested groups: AND containing an OR

```json
{
  "all": [
    {
      "fact": "delivery_method.melio_db__raw__created_at",
      "path": "$.value",
      "operator": "isDateWithinRange",
      "value": { "daysOffset": -10, "inclusive": true }
    },
    {
      "any": [
        {
          "fact": "delivery_method.melio_db__normalized_routing_number",
          "path": "$.value",
          "operator": "in",
          "value": ["236070545", "124303162", "031101334", "073972181"]
        },
        {
          "fact": "payment.derived__is__delivery_type_wire",
          "path": "$.value",
          "operator": "equal",
          "value": true
        }
      ]
    },
    {
      "fact": "delivery_method.melio_db__raw__delivery_type",
      "path": "$.value",
      "operator": "notEqual",
      "value": "check"
    }
  ]
}
```

## Operators

**Comparison**: `equal`, `notEqual`, `lessThan`, `lessThanInclusive`, `greaterThan`, `greaterThanInclusive`

**Array**: `in`, `notIn`, `contains`, `doesNotContain`, `intersects`, `containsArray`

**Null checks**: `isNull`, `isNotNull`, `isNullOrEmpty` (value is always `null`)

**Date**: `isDateWithinRange` — value is `{ "daysOffset": -N, "inclusive": true/false }` (negative = past)

**Regex**: `regExpMatches` — value is `{ "pattern": "string", "flags": "i" }`

**Length**: `lengthGreaterThan` — value is a number

## Decision values

Payment rules: `"approved"`, `"pending"`, `"declined"`
Organization rules: `"block"`, `"blockCompliance"`, `"blockRisk"`, `"accept"`, `"reject"`

Default for new candidate rules: `"pending"` (conservative — sends to manual review)

## MO values

`"SF"`, `"ATO"`, `"3ATO"`, `"Policy"`, `"Exposure"`, `"Compliance"`, `"Specific Payor"`, `"Vendor-Side Fraud"`, `"Sanctions"`, `"SBB"`, `"Abuse"`

## BETWEEN example (amount range)

SQL: `p.amount BETWEEN 9000 AND 10000`

Rule:
```json
{
  "all": [
    {
      "fact": "payment.melio_db__raw__amount",
      "path": "$.value",
      "operator": "greaterThanInclusive",
      "value": 9000
    },
    {
      "fact": "payment.melio_db__raw__amount",
      "path": "$.value",
      "operator": "lessThanInclusive",
      "value": 10000
    }
  ]
}
```

## Full example: DM routings alert converted to rule

Corresponding SQL WHERE:
```sql
WHERE (p.amount > 1500 OR p.createorigin IN ('ar-invoice', 'request', 'ar-link'))
  AND dm.createdat >= dateadd(day, -10, current_date())
  AND ((ba.routingnumber IN (236070545, 124303162, 031101334, 073972181, 121145307) OR p.isfinanced = 'true') OR dwa.aba = 121145307)
  AND dm.deliverytype <> 'check'
  AND p.partnername <> 'paypal'
  AND p.partnername <> 'fiserv_us-bank'
```

Rule JSON candidate:
```json
{
  "name": "DM routings 026073150 236070545 124303162 031101334 — candidate",
  "priority": 10,
  "conditions": {
    "all": [
      {
        "any": [
          {
            "fact": "payment.melio_db__raw__amount",
            "path": "$.value",
            "operator": "greaterThan",
            "value": 1500
          },
          {
            "fact": "payment.melio_db__raw__create_origin",
            "path": "$.value",
            "operator": "in",
            "value": ["ar-invoice", "request", "ar-link"]
          }
        ]
      },
      {
        "fact": "delivery_method.melio_db__raw__created_at",
        "path": "$.value",
        "operator": "isDateWithinRange",
        "value": { "daysOffset": -10, "inclusive": true }
      },
      {
        "any": [
          {
            "fact": "delivery_method.melio_db__normalized_routing_number",
            "path": "$.value",
            "operator": "in",
            "value": ["236070545", "124303162", "031101334", "073972181", "121145307"]
          }
        ]
      },
      {
        "fact": "delivery_method.melio_db__raw__delivery_type",
        "path": "$.value",
        "operator": "notEqual",
        "value": "check"
      }
    ]
  },
  "event": {
    "type": "decision",
    "params": {
      "decision": "pending",
      "mos": [],
      "subcategory": "fraud",
      "reason": "DM routing number associated with fraud"
    }
  },
  "_conversion_notes": {
    "source_alert": "DM routings 026073150 236070545 124303162 031101334",
    "unmapped_conditions": [
      "p.isfinanced = 'true' — no Chalk feature for is_financed",
      "dwa.aba = 121145307 — domestic wire ABA not verified in Chalk",
      "p.partnername <> 'paypal' — no Chalk feature for partner_name",
      "p.partnername <> 'fiserv_us-bank' — no Chalk feature for partner_name"
    ],
    "analyst_notes": [
      "Consider adding payment.melio_db__raw__partner_name to Chalk to support partner exclusions",
      "The isfinanced condition may be important — verify with risk analyst whether financed payments need special handling",
      "Routing number 026073150 from the alert name is NOT in the IN list in the SQL — verify with alert owner"
    ]
  }
}
```
