-- =====================================================================
-- baseline.sql  —  Frozen RQ2 rule-based SQL baseline
-- =====================================================================
-- This is the frozen rule-based SQL baseline for RQ2 (LLM vs. SQL).
-- It runs on the SAME extracted JSON the LLM sees, loaded into Postgres
-- as jsonb (evidence parity): the extracted context carries only
-- facts + neutral comparators and NO rule verdicts, so this rule layer —
-- not the data preparation — is what computes the verdicts. Both detectors
-- therefore see identical evidence and differ only in how they reason.
--
-- Pre-registration: these rules were fixed BEFORE any evaluation data was
-- generated. They are drawn from Odoo's own controls (the double-validation
-- approval threshold, the duplicate-bill warning) and the §3.2 anomaly
-- definitions — NOT tuned to the injected values. This script was
-- committed before the dataset was generated.
--
-- Output: one row (grain, unit_id, scenario) per FLAGGED unit only.
-- Scenario label strings match the frozen enum in anomaly_spec.md:
--   duplicate_payment | po_splitting | vendor_master_mod
--   approval_bypass   | abnormal_amount
--
-- Run from the repo root, e.g.:
--   psql \
--       -h localhost -p 5432 -U <user> -d <scratch_db> -f _pipeline/generate/baseline.sql
-- (any throwaway Postgres DB works — this only reads the JSON files below,
--  it does not touch the live Odoo tables).
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. Staging tables (one jsonb doc per row, three grains)
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS po_ctx;
DROP TABLE IF EXISTS vendor_ctx;
DROP TABLE IF EXISTS audit_ctx;

CREATE UNLOGGED TABLE po_ctx     (doc jsonb);   -- per-PO context     (out/po/*.json)
CREATE UNLOGGED TABLE vendor_ctx (doc jsonb);   -- per-vendor rollup  (out/vendor/*.json)
CREATE UNLOGGED TABLE audit_ctx  (doc jsonb);   -- vendor-master audit(out/vendor_audit/*.json)


-- ---------------------------------------------------------------------
-- 2. Load the extracted JSON  (<<< ADJUST THESE PATHS if needed >>>)
-- ---------------------------------------------------------------------
-- po_context.jsonl is ALREADY one-JSON-per-line, so it loads directly:
\copy po_ctx(doc) FROM '_pipeline/out/po_context.jsonl'

-- vendor.jsonl and audit.jsonl are written by extract.py:
\copy vendor_ctx(doc) FROM '_pipeline/out/vendor.jsonl'
\copy audit_ctx(doc)  FROM '_pipeline/out/audit.jsonl'


-- ---------------------------------------------------------------------
-- 3. The 5 frozen rules  ->  sql_baseline_flags
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sql_baseline_flags;
CREATE TEMP VIEW sql_baseline_flags AS

-- === R1  duplicate_payment  (grain: po) ==============================
-- More than one bill OR more than one payment recorded against one PO.
-- Reads baselines.count_bills / baselines.count_payments (facts the
-- pipeline counted; the JSON carries no is_duplicate verdict).
-- NOTE: this deliberately over-flags the scenario-1 hard negatives
-- (down-payment + final invoice, or two partial deliveries each correctly
-- billed) — the rigid rule cannot tell a legitimate second bill from a
-- duplicate. Catching that over-flag is the point of RQ2.
SELECT
    'po'                                            AS grain,
    p.doc->'po_identity'->>'po_number'              AS unit_id,
    'duplicate_payment'                             AS scenario
FROM po_ctx p
WHERE COALESCE((p.doc->'baselines'->>'count_payments')::int, 0) > 1
   OR COALESCE((p.doc->'baselines'->>'count_bills')::int,    0) > 1

UNION ALL

-- === R2  po_splitting  (grain: vendor) ===============================
-- Flag a vendor if ANY splitting-window cluster has a window total above
-- the approval threshold WHILE every individual PO in that window sits
-- below it — i.e. a purchase carved into sub-threshold siblings that only
-- crosses the sign-off line when summed. Reads splitting_view[].group_sum,
-- splitting_view[].amounts, splitting_view[].approval_threshold.
-- Near-threshold sibling FLOOR: every sibling must ALSO sit at or above 60% of
-- the threshold. Ordinary vendors naturally accumulate several small,
-- routine POs (~$5k each) inside a 30-day window whose sum exceeds the
-- threshold; without the floor, R2 flagged ~99% of normal vendors
-- (Monte-Carlo, seed 123). Real structuring parks values just BELOW the
-- sign-off line, so genuine splitting siblings cluster near (not far
-- below) the threshold. The 60% floor matches that signature and stops
-- over-flagging vendors whose small orders merely sum past the threshold.
SELECT
    'vendor'                                        AS grain,
    v.doc->'vendor'->>'id'                          AS unit_id,
    'po_splitting'                                  AS scenario
FROM vendor_ctx v
WHERE EXISTS (
    SELECT 1
    FROM jsonb_array_elements(v.doc->'splitting_view') AS cluster
    WHERE (cluster->>'group_sum')::numeric
              > COALESCE((cluster->>'approval_threshold')::numeric, (v.doc->'params'->>'approval_threshold')::numeric)
      -- every (non-null) sibling amount is strictly below the threshold
      -- AND at or above 60% of it (near-threshold structuring floor):
      AND COALESCE((
            SELECT bool_and(
                     amt::numeric <  COALESCE((cluster->>'approval_threshold')::numeric, (v.doc->'params'->>'approval_threshold')::numeric)
                 AND amt::numeric >= 0.60 * COALESCE((cluster->>'approval_threshold')::numeric, (v.doc->'params'->>'approval_threshold')::numeric)
                   )
            FROM jsonb_array_elements_text(cluster->'amounts') AS amt
            WHERE amt IS NOT NULL
          ), false)
)

UNION ALL

-- === R3  vendor_master_mod  (grain: audit) ===========================
-- Flag a vendor's audit trail if ANY tracked change targets a bank
-- account (changes[].target = 'bank_account'). res.partner.bank edits are
-- the payment-diversion surface (COSO change-review control).
-- NOTE: like R1/R5 this over-flags the legitimate hard negative — a
-- properly-reviewed bank-account change with no change-then-revert around
-- a payment. The rule sees the field, not the context; RQ2 measures that.
SELECT
    'audit'                                         AS grain,
    a.doc->'vendor'->>'id'                          AS unit_id,
    'vendor_master_mod'                             AS scenario
FROM audit_ctx a
WHERE EXISTS (
    SELECT 1
    FROM jsonb_array_elements(a.doc->'changes') AS c
    WHERE c->>'target' = 'bank_account'
)

UNION ALL

-- === R4  approval_bypass  (grain: po) ================================
-- Flag a PO whose approver was the buyer (self_approval = true) AND whose
-- value is above the approval threshold — an above-threshold order that
-- skipped independent sign-off. self_approval lives in po_identity;
-- amount_total exists in both baselines and po_identity, so prefer
-- baselines.amount_total and coalesce to po_identity.amount_total.
-- NOTE: rfq_sent_skipped alone is deliberately NOT a rule. Per §3.2,
-- skipping the "RFQ Sent" step is a legitimate process variant (offline /
-- recurring / urgent / MRP-auto orders), so it is never flagged on its own.
SELECT
    'po'                                            AS grain,
    p.doc->'po_identity'->>'po_number'              AS unit_id,
    'approval_bypass'                               AS scenario
FROM po_ctx p
WHERE COALESCE((p.doc->'po_identity'->>'self_approval')::boolean, false) = true
  AND COALESCE(
        (p.doc->'baselines'->>'amount_total')::numeric,
        (p.doc->'po_identity'->>'amount_total')::numeric
      ) > (p.doc->'baselines'->>'approval_threshold')::numeric

UNION ALL

-- === R5  abnormal_amount  (grain: po) ================================
-- Flag a PO if EITHER a line is priced well over standard cost OR the PO
-- total dwarfs the vendor's historical average:
--   * MAX of baselines.line_unit_price_vs_cost_ratio (a JSON array, may
--     contain nulls) >= 1.5   -> a unit price >=50% over standard cost is
--     a material price variance;
--   * amount_total > 3 x baselines.vendor_avg_amount_alltime  -> an order
--     triple the vendor's historical average.
-- 1.5 and 3x are conventional audit-materiality cutoffs, fixed before
-- generation — NOT read off the injected 1.5-3x over-price range.
-- The vendor-average branch is guarded against a null / zero average.
-- NOTE: this over-flags the scenario-5 hard negative (a genuine rush /
-- low-volume premium near the ~1.5x low end); RQ2 measures that cost.
SELECT
    'po'                                            AS grain,
    p.doc->'po_identity'->>'po_number'              AS unit_id,
    'abnormal_amount'                               AS scenario
FROM po_ctx p
WHERE COALESCE((
        SELECT max(r::numeric)
        FROM jsonb_array_elements_text(
                 p.doc->'baselines'->'line_unit_price_vs_cost_ratio') AS r
        WHERE r IS NOT NULL
      ), 0) >= 1.5
   OR (
        (p.doc->'baselines'->>'vendor_avg_amount_alltime') IS NOT NULL
        AND (p.doc->'baselines'->>'vendor_avg_amount_alltime')::numeric > 0
        AND COALESCE(
              (p.doc->'baselines'->>'amount_total')::numeric,
              (p.doc->'po_identity'->>'amount_total')::numeric
            ) > 3 * (p.doc->'baselines'->>'vendor_avg_amount_alltime')::numeric
      );


-- ---------------------------------------------------------------------
-- 4. Emit the flags
-- ---------------------------------------------------------------------
SELECT * FROM sql_baseline_flags
ORDER BY grain, scenario, unit_id;
