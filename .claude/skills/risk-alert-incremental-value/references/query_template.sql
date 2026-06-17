-- ============================================================
-- Risk Alert Incremental Value Query Template
-- ============================================================
-- All metrics are calculated in a single Snowflake query per
-- (alert × timeframe). Claude fills in MODIFIED_ALERT_SQL and
-- the timeframe bounds before running.
--
-- Parameters to substitute before running:
--   {MODIFIED_ALERT_SQL}  — the alert's Redash SQL with date
--                           filters replaced for this timeframe
--   {RUN_DATE}            — CURRENT_TIMESTAMP() for daily runs
-- ============================================================

WITH

-- ── Step 1: Manual fraud tags (from risk intel / ops tagging) ─────────────
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

-- ── Step 2: Alert payments (modified Redash SQL for this timeframe) ───────
-- NOTE: Claude replaces date filters in the original alert SQL to match
-- the target timeframe. Assumptions about which date columns were modified
-- are written to assumptions_{alert_name}.txt before this query runs.
alert_payments AS (
    {MODIFIED_ALERT_SQL}
),

-- ── Step 3: First decision per payment (pre-filtered to alert payments) ────
-- entitydecisions is very large — filtering to alert_payments first is critical.
first_decisions AS (
    SELECT
        ed.entityid                                                          AS payment_id,
        ed.decision,
        ed.source,
        rdc.subcategory,
        ROW_NUMBER() OVER (PARTITION BY ed.entityid ORDER BY ed.createdat ASC) AS rn
    FROM FIVETRAN_CDC.decision_engine_decision.entitydecisions ed
    LEFT JOIN FIVETRAN_CDC.decision_engine_decision.riskdecisioncodes rdc
        ON rdc.legacyriskdecisioncode = ed.riskdecisioncodeid
    WHERE ed.entityid IN (SELECT payment_id FROM alert_payments)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ed.entityid ORDER BY ed.createdat ASC) = 1
),

-- ── Step 4: Incremental payments ──────────────────────────────────────────
-- Payments the alert catches that the ecosystem would NOT have caught:
-- first decision was auto-approve OR blocked for non-fraud (policy/compliance).
incremental_payments AS (
    SELECT ap.payment_id
    FROM alert_payments ap
    JOIN first_decisions fd ON fd.payment_id = ap.payment_id
    WHERE fd.subcategory IN ('policy', 'compliance')
       OR (fd.source = 'system' AND fd.decision = 'approve')
),

-- ── Step 5: Attach is_fraud and amount ────────────────────────────────────
payment_with_fraud AS (
    SELECT
        ip.payment_id,
        rp.amount,
        CASE
            -- (1) Declined for fraud / problematic vendor (not false decline)
            WHEN (
                    (rp.last_risk_decision_subcategory = 'fraud'
                     OR rp.last_risk_decision_code_description = 'problematic vendor')
                    AND (rp.false_decline_potential = false
                         OR rp.false_decline_potential IS NULL
                         OR ap2.money_flow = 'ar'
                         OR rp.last_engine_reason IN (
                             'declineKybMoneyInReject',
                             'declineVendorOrgRejectFraud'
                         ))
                 )
            -- (2) Org ODE reject with 'stolen data' label
              OR (od.risk_ode_decision = 'reject'
                  AND od.risk_ode_decision_labels::varchar ILIKE '%stolen data%')
            -- (3) Fraud-related MO label
              OR rpl.mo_label IN ('FRAUD OTHER','VENDOR-SIDE FRAUD','ATO','SF','3RD PARTY ATO')
            -- (4) Chargeback fraud claim (excluding friendly fraud)
              OR (lower(loss_attr.highlevelclaim) = 'fraud'
                  AND (lower(loss_attr.reason) <> 'friendly fraud'
                       OR loss_attr.reason IS NULL))
            -- (5) Manual fraud tag from risk intel / ops
              OR pft.payment_id IS NOT NULL
            THEN true ELSE false
        END AS is_fraud
    FROM incremental_payments ip
    JOIN PROD.ANALYTICS.RISK_PAYMENTS rp ON rp.payment_id = ip.payment_id
    LEFT JOIN prod.analytics.analytics_payments_history ap2
        ON ap2.payment_id = rp.payment_id AND ap2.data_interval_end = (SELECT MAX(data_interval_end) FROM prod.analytics.analytics_payments_history)
    LEFT JOIN prod.analytics.payments_parent_vendor_history p
        ON p.id = rp.payment_id AND p.data_interval_end = (SELECT MAX(data_interval_end) FROM prod.analytics.payments_parent_vendor_history)
    LEFT JOIN prod.analytics.organization_dim_history od
        ON od.organization_id = p.organizationid AND od.data_interval_end = (SELECT MAX(data_interval_end) FROM prod.analytics.organization_dim_history)
    LEFT JOIN prod.analytics.risk_payments_labeling_history rpl
        ON rpl.payment_id = rp.payment_id AND rpl.data_interval_end = (SELECT MAX(data_interval_end) FROM prod.analytics.risk_payments_labeling_history)
    LEFT JOIN FIVETRAN_CDC.FVTRN_MELIO.LOSSES loss_attr
        ON loss_attr.paymentid = rp.payment_id
    LEFT JOIN payment_fraud_tags pft ON pft.payment_id = ip.payment_id
)

-- ── Step 6: Aggregate all metrics ─────────────────────────────────────────
SELECT
    COUNT(*)                                                          AS manual_review_load,
    SUM(is_fraud::int)                                                AS fraud_count,
    ROUND(SUM(is_fraud::int)::float / NULLIF(COUNT(*), 0) * 100, 2)  AS bad_rate_pct,
    ROUND(SUM(CASE WHEN is_fraud THEN amount ELSE 0 END), 2)          AS fraud_tpv
FROM payment_with_fraud
