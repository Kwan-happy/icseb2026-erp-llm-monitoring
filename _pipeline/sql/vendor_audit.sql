-- vendor_audit.sql — Scenario-3 audit-trail extract (off the PO FK spine).
-- Reconstructs vendor master-data change history from Odoo mail-tracking, spanning BOTH
-- res.partner (name/email/phone/vat/address) AND res.partner.bank (acc_number/acc_holder_name/…),
-- because bank accounts live in a separate model and res.partner.bank_ids is NOT tracked — a
-- res.partner-only query would miss bank changes. Grouped by the owning vendor.
-- Level B: facts only (who / which field / old->new / when). No "authorized" flag.
-- Returns one row per vendor: (vendor_id, audit jsonb).
--
-- Bank-account ROW CREATION
-- facts, for the S3 "new bank account added and paid to" variant, where the original account is
-- never edited so `edits` below (mail_tracking_value only fires on a subsequent EDIT of an
-- existing value) has nothing to see. In Odoo every
-- mail_tracking_value row for res.partner.bank has a non-null old_value_char — Odoo never tracks
-- the INSERT itself (res.partner.bank_ids is not a tracked field on res.partner), only later
-- edits to a field on an already-existing row. Falls back to res_partner_bank.create_date/
-- create_uid.
--
-- These creation facts live in their OWN top-level key, `bank_account_additions` — NOT inside
-- `changes[]`, so the frozen R3 rule (`EXISTS changes[].target = 'bank_account'`) stays
-- behaviour-identical: it still only sees edits to an existing value, and genuinely cannot see
-- an addition, which is the whole point of the evasive variant this supports — the frozen rule
-- must MISS it, not be quietly handed a new way to catch it. A post-hoc sensitivity rule (or the
-- LLM) is free to read `bank_account_additions` directly.
--
-- Every bank account is emitted INSIDE a document, once that document exists (first account
-- included, no rank filter on the facts themselves) — level B says state the facts and let the
-- detector discriminate; filtering a vendor's first account out of its own document would be a
-- curation decision made by the extractor. Each row carries `account_ordinal` (1 = the vendor's
-- earliest account by create_date/id) so a later rule can test "not the vendor's first account"
-- without the extractor pre-deciding what that means.
--
-- WHICH vendors get a document at all is a separate question, answered by qualifying_additions
-- below: an ordinal-1 account is routine onboarding and does not by itself earn a vendor a
-- document (every vendor has one — counting it would silently redefine the S3 evaluation
-- denominator per anomaly_spec.md).
WITH edits AS (
  SELECT
    -- owning vendor: partner itself, or the bank account's partner_id
    CASE WHEN mm.model = 'res.partner.bank' THEN rpb.partner_id ELSE mm.res_id END AS vendor_id,
    CASE WHEN mm.model = 'res.partner.bank' THEN 'bank_account' ELSE 'partner_core' END AS target,
    mm.model AS model,
    imf.name AS field,                                   -- field_id is model-specific → correct field name
    COALESCE(mtv.old_value_char, mtv.old_value_text, mtv.old_value_datetime::text,
             mtv.old_value_integer::text, mtv.old_value_float::text) AS old_value,
    COALESCE(mtv.new_value_char, mtv.new_value_text, mtv.new_value_datetime::text,
             mtv.new_value_integer::text, mtv.new_value_float::text) AS new_value,
    mm.create_uid AS changed_by_uid,                     -- the acting user (pin at injection; else = API user)
    -- synthetic label derived from the uid, not the real Odoo login (PRE-FREEZE-REVIEW B1)
    CASE WHEN mm.create_uid IS NULL THEN NULL ELSE 'user_' || mm.create_uid::text END AS changed_by_name,
    mm.date AS changed_at,                               -- magic timestamp (realigned by fix_timestamps.sql)
    CASE WHEN mm.model = 'res.partner.bank' THEN mm.res_id ELSE NULL END AS bank_ref
  FROM mail_tracking_value mtv
  JOIN mail_message   mm  ON mm.id  = mtv.mail_message_id
  JOIN ir_model_fields imf ON imf.id = mtv.field_id
  LEFT JOIN res_partner_bank rpb ON mm.model = 'res.partner.bank' AND rpb.id = mm.res_id
  WHERE mm.model IN ('res.partner', 'res.partner.bank')
),
additions AS (
  SELECT
    rpb.partner_id                       AS vendor_id,
    rpb.id                               AS bank_ref,
    rpb.acc_number                       AS acc_number,
    rpb.create_uid                       AS created_by_uid,
    -- synthetic label derived from the uid, not the real Odoo login (PRE-FREEZE-REVIEW B1)
    CASE WHEN rpb.create_uid IS NULL THEN NULL ELSE 'user_' || rpb.create_uid::text END AS created_by_name,
    rpb.create_date                      AS created_at,
    row_number() OVER (PARTITION BY rpb.partner_id ORDER BY rpb.create_date, rpb.id) AS account_ordinal
  FROM res_partner_bank rpb
),
edit_agg AS (
  -- Exactly the original 'changes'/'change_count' computation (unchanged inputs, unchanged
  -- shape) — just pre-aggregated per vendor here so it can be combined with addition_agg below.
  SELECT
    vendor_id,
    jsonb_agg(
      jsonb_build_object(
        'target', target, 'model', model, 'field', field,
        'old_value', old_value, 'new_value', new_value,
        'changed_by', jsonb_build_object('user_id', changed_by_uid, 'name', changed_by_name),
        'changed_at', changed_at, 'bank_ref', bank_ref
      ) ORDER BY changed_at NULLS LAST
    ) AS changes,
    count(*) AS change_count
  FROM edits
  WHERE vendor_id IS NOT NULL
  GROUP BY vendor_id
),
addition_agg AS (
  SELECT
    vendor_id,
    jsonb_agg(
      jsonb_build_object(
        'bank_ref', bank_ref,
        'acc_number', acc_number,
        'created_by', jsonb_build_object('user_id', created_by_uid, 'name', created_by_name),
        'created_at', created_at,
        'account_ordinal', account_ordinal
      ) ORDER BY created_at NULLS LAST, account_ordinal
    ) AS bank_account_additions
  FROM additions
  WHERE vendor_id IS NOT NULL
  GROUP BY vendor_id
),
qualifying_additions AS (
  -- Which vendors get a document (the evaluation denominator, anomaly_spec.md §5/§7's "every
  -- vendor with >=1 tracked change") is a DIFFERENT question from what facts a document contains
  -- once it exists. A vendor's ordinal-1 account is routine onboarding, not an auditable event —
  -- every vendor has one, so counting it here would silently redefine the S3 denominator to
  -- "every vendor" and inflate the negative pool with units no
  -- detector could ever act on. Only an account BEYOND the first (account_ordinal > 1) is a
  -- genuine addition and earns the vendor a place in the denominator; that account and its
  -- ordinal-1 sibling both still appear, uncurated, inside addition_agg's bank_account_additions
  -- once the vendor qualifies (via this CTE or via edit_agg) — see the note on additions above.
  SELECT DISTINCT vendor_id FROM additions WHERE account_ordinal > 1
),
audit_vendors AS (
  -- A vendor becomes an audit unit if it has EITHER a tracked edit OR a qualifying (ordinal > 1)
  -- bank-account addition — a vendor whose ONLY audit-relevant event is an unedited NEW account
  -- must still get a document (the evasive S3 case this feature exists for), but a vendor with
  -- only its original onboarding account must not (see qualifying_additions above).
  SELECT vendor_id FROM edit_agg
  UNION
  SELECT vendor_id FROM qualifying_additions
)
SELECT
  av.vendor_id,
  jsonb_build_object(
    'vendor', jsonb_build_object(
        'id', av.vendor_id,
        'name', v.name,
        'commercial_partner_id', v.commercial_partner_id,
        'supplier_rank', v.supplier_rank),
    'changes', COALESCE(ea.changes, '[]'::jsonb),
    'change_count', COALESCE(ea.change_count, 0),
    'bank_account_additions', COALESCE(aa.bank_account_additions, '[]'::jsonb)
  ) AS audit
FROM audit_vendors av
JOIN res_partner v ON v.id = av.vendor_id
LEFT JOIN edit_agg ea ON ea.vendor_id = av.vendor_id
LEFT JOIN addition_agg aa ON aa.vendor_id = av.vendor_id
-- VENDORS ONLY. Odoo tracks changes on partners that are not suppliers at all: the company
-- record itself, OdooBot, and the partner behind every internal user each pick up tracked
-- edits during installation and user setup. Left in, they become audit-grain scoring units
-- with no ground-truth label, inflating the denominator of the scenario-3 P/R/F1 with rows
-- no detector was ever meant to judge. supplier_rank > 0 is Odoo's own marker for "this
-- partner has been used as a vendor". (Excludes e.g. 'My Company' and 'OdooBot'.)
WHERE coalesce(v.supplier_rank, 0) > 0;
