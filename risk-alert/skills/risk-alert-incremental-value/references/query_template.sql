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

-- ── Step 1: Alert payments (modified Redash SQL for this timeframe) ───────
-- NOTE: Claude replaces date filters in the original alert SQL to match
-- the target timeframe. Assumptions about which date columns were modified
-- are written to assumptions_{alert_name}.txt before this query runs.
-- The subquery MUST expose a column named `payment_id`.
alert_payments AS (
    {MODIFIED_ALERT_SQL}
),

-- ── Step 2: First decision per payment (pre-filtered to alert payments) ────
-- entitydecisions is very large — filtering to alert_payments first is critical.
first_decisions AS (
    SELECT
        ed.entityid AS payment_id,
        ed.decision,
        ed.source,
        rdc.subcategory
    FROM FIVETRAN_CDC.decision_engine_decision.entitydecisions ed
    LEFT JOIN FIVETRAN_CDC.decision_engine_decision.riskdecisioncodes rdc
        ON rdc.legacyriskdecisioncode = ed.riskdecisioncodeid
    WHERE ed.entityid IN (SELECT payment_id FROM alert_payments)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ed.entityid ORDER BY ed.CREATEDAT ASC) = 1
),

-- ── Step 3: Subsequent non-policy blocks ──────────────────────────────────
-- Payments that got a non-policy/compliance block after an initial policy block.
-- These are NOT incremental — the ecosystem would have caught them anyway.
subsequent_nonpolicy_blocks AS (
    SELECT DISTINCT ed.entityid AS payment_id
    FROM FIVETRAN_CDC.decision_engine_decision.entitydecisions ed
    LEFT JOIN FIVETRAN_CDC.decision_engine_decision.riskdecisioncodes rdc
        ON rdc.legacyriskdecisioncode = ed.riskdecisioncodeid
    JOIN first_decisions fd
        ON fd.payment_id = ed.entityid
        AND fd.subcategory IN ('policy', 'compliance')
        AND ed.CREATEDAT > fd.first_decision_at
    WHERE ed.entitytype = 'payment'
      AND ed.decision NOT IN ('approved', 'pending')
      AND COALESCE(rdc.subcategory, '') NOT IN ('policy', 'compliance')
),

-- ── Step 4: Incremental payments ──────────────────────────────────────────
-- Payments the alert catches that the ecosystem would NOT have caught:
-- first decision was auto-approve OR blocked for policy/compliance with no
-- subsequent non-policy block (meaning the ecosystem wouldn't have flagged it).
incremental_payments AS (
    SELECT ap.payment_id
    FROM alert_payments ap
    JOIN first_decisions fd ON fd.payment_id = ap.payment_id
    LEFT JOIN subsequent_nonpolicy_blocks snb ON snb.payment_id = ap.payment_id
    WHERE (fd.source = 'system' AND fd.decision = 'approve')
       OR (fd.subcategory IN ('policy', 'compliance') AND snb.payment_id IS NULL)
),

-- ── Step 4: Attach is_fraud and amount from RISK_PAYMENTS ─────────────────
payment_with_fraud AS (
    SELECT
        ip.payment_id,
        rp.amount,
        rp.is_fraud
    FROM incremental_payments ip
    JOIN PROD.ANALYTICS.RISK_PAYMENTS rp ON rp.payment_id = ip.payment_id
)

-- ── Step 5: Aggregate all metrics ─────────────────────────────────────────
SELECT
    COUNT(*)                                                          AS manual_review_load,
    SUM(is_fraud::int)                                                AS fraud_count,
    ROUND(SUM(is_fraud::int)::float / NULLIF(COUNT(*), 0) * 100, 2)  AS bad_rate_pct,
    ROUND(SUM(CASE WHEN is_fraud THEN amount ELSE 0 END), 2)          AS fraud_tpv
FROM payment_with_fraud
