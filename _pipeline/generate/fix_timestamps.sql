-- fix_timestamps.sql — post-generation timestamp repair for the synthetic dataset.
--
-- WHY THIS EXISTS
-- ----------------
-- The generator loads ~1000 POs into a fresh Odoo 19 DB over XML-RPC. Odoo only
-- lets the caller backdate a handful of fields directly (purchase_order.date_order /
-- date_approve, account_move.invoice_date / date, account_payment.date) — every other
-- timestamp column (create_date/write_date on every model, stock_picking.date_done,
-- mail_message.date, mail_tracking_value.*) is stamped with the wall-clock "now" at
-- write time, regardless of what the generator sends. Left alone, the whole dataset
-- would read as "generated on one day in 2026" instead of "happened across 2025" —
-- and, worse, the extraction pipeline reads mail_message.date as the state-transition
-- timestamp (po_context.sql state_log block) and vendor_audit.sql's changed_at, so an
-- un-repaired run corrupts the timeline/audit-trail data itself, not just
-- cosmetic metadata.
--
-- This script realigns those forced-now() columns to the intended 2025 dataset period,
-- using the two CSVs timestamps.py (the generator-side collector) wrote during
-- generation: gen_out/timestamp_map.csv (direct table.column cells) and
-- gen_out/message_timestamps.csv (chatter messages, matched by (model,res_id) +
-- mail_message.id creation order — see the ORDERING ASSUMPTION note below and in
-- timestamps.py's module docstring).
--
-- HOW TO RUN
-- ----------
-- Run from the repo root (the \copy paths below are relative to it), AFTER the
-- generation run has finished writing gen_out/*.csv, and BEFORE _pipeline/extract.py
-- (extract.py reads mail_message.date / stock_picking.date_done / etc. as ground
-- truth for the timeline — it must see the repaired values):
--
--   export PGPASSWORD='<the generation DB password>'
--   psql -h localhost -p 5432 -U <db_user> -d <GENERATION_DB> -f _pipeline/generate/fix_timestamps.sql
--
-- Replace <GENERATION_DB> with the fresh generation DB name from gen_config.json
-- (odoo.dbname). Never run it against any other database.
--
-- Everything below runs inside one transaction: a partial repair (e.g. a \copy that
-- fails halfway) rolls back cleanly rather than leaving the DB half-realigned.
BEGIN;

-- ---------------------------------------------------------------------------------
-- 0. Generation-period bounds (single declaration point).
--    Mirrors params.json's `generation_period` (2025-01-01..2025-12-31) — do NOT
--    hard-code these dates anywhere else in this file; the verification block below
--    reads them from this temp table.
-- ---------------------------------------------------------------------------------
DROP TABLE IF EXISTS gen_period;
CREATE TEMP TABLE gen_period (start_date date, end_date_exclusive date);
INSERT INTO gen_period VALUES ('2025-01-01', '2026-01-01');  -- end is exclusive (< 2026-01-01)

-- ---------------------------------------------------------------------------------
-- 1. Load the two CSVs the generator wrote via timestamps.py's flush().
-- ---------------------------------------------------------------------------------
DROP TABLE IF EXISTS tsmap;
CREATE TEMP TABLE tsmap (
  table_name   text NOT NULL,
  record_id    integer NOT NULL,
  column_name  text NOT NULL,
  intended_ts  timestamp NOT NULL
);
\copy tsmap (table_name, record_id, column_name, intended_ts) FROM '_pipeline/generate/gen_out/timestamp_map.csv' WITH (FORMAT csv, HEADER true)

DROP TABLE IF EXISTS msgts;
CREATE TEMP TABLE msgts (
  model        text NOT NULL,
  res_id       integer NOT NULL,
  seq          integer NOT NULL,
  intended_ts  timestamp NOT NULL
);
\copy msgts (model, res_id, seq, intended_ts) FROM '_pipeline/generate/gen_out/message_timestamps.csv' WITH (FORMAT csv, HEADER true)

-- ---------------------------------------------------------------------------------
-- 2. Explicit per-table UPDATEs driven by tsmap.
--    SQL can't parameterize table/column names, so each (table, column) pair the
--    generator can emit gets its own statement, filtered on tsmap.table_name /
--    column_name. Adding a new column later = add one more UPDATE here, matching a
--    new (table, column) value timestamps.py's record() can produce.
-- ---------------------------------------------------------------------------------

-- purchase_order.create_date / write_date
-- (date_order / date_approve are set directly at creation via XML-RPC — not repaired here)
UPDATE purchase_order po
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'purchase_order' AND t.column_name = 'create_date' AND po.id = t.record_id;

UPDATE purchase_order po
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'purchase_order' AND t.column_name = 'write_date' AND po.id = t.record_id;

-- purchase_order_line.create_date / write_date
UPDATE purchase_order_line pol
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'purchase_order_line' AND t.column_name = 'create_date' AND pol.id = t.record_id;

UPDATE purchase_order_line pol
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'purchase_order_line' AND t.column_name = 'write_date' AND pol.id = t.record_id;

-- stock_picking: create_date, write_date, scheduled_date, date_done
UPDATE stock_picking sp
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'stock_picking' AND t.column_name = 'create_date' AND sp.id = t.record_id;

UPDATE stock_picking sp
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'stock_picking' AND t.column_name = 'write_date' AND sp.id = t.record_id;

UPDATE stock_picking sp
SET scheduled_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'stock_picking' AND t.column_name = 'scheduled_date' AND sp.id = t.record_id;

UPDATE stock_picking sp
SET date_done = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'stock_picking' AND t.column_name = 'date_done' AND sp.id = t.record_id;

-- stock_move: create_date, write_date, date
UPDATE stock_move sm
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'stock_move' AND t.column_name = 'create_date' AND sm.id = t.record_id;

UPDATE stock_move sm
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'stock_move' AND t.column_name = 'write_date' AND sm.id = t.record_id;

UPDATE stock_move sm
SET date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'stock_move' AND t.column_name = 'date' AND sm.id = t.record_id;

-- account_move (vendor bill): create_date, write_date
-- (invoice_date / date are set directly at creation via XML-RPC — not repaired here)
UPDATE account_move am
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'account_move' AND t.column_name = 'create_date' AND am.id = t.record_id;

UPDATE account_move am
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'account_move' AND t.column_name = 'write_date' AND am.id = t.record_id;

-- account_payment: create_date, write_date
-- (date is set directly at creation via XML-RPC — not repaired here)
UPDATE account_payment ap
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'account_payment' AND t.column_name = 'create_date' AND ap.id = t.record_id;

UPDATE account_payment ap
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'account_payment' AND t.column_name = 'write_date' AND ap.id = t.record_id;

-- res_partner (vendor master record — scenario-3 target): create_date, write_date
UPDATE res_partner rp
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'res_partner' AND t.column_name = 'create_date' AND rp.id = t.record_id;

UPDATE res_partner rp
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'res_partner' AND t.column_name = 'write_date' AND rp.id = t.record_id;

-- res_partner_bank (vendor bank account — scenario-3 target): create_date, write_date
UPDATE res_partner_bank rpb
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'res_partner_bank' AND t.column_name = 'create_date' AND rpb.id = t.record_id;

UPDATE res_partner_bank rpb
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'res_partner_bank' AND t.column_name = 'write_date' AND rpb.id = t.record_id;

-- mail_message rows the generator already had an id for at collection time
-- (TimestampMap.record_message(); sets create_date, write_date, and date all at once).
UPDATE mail_message mm
SET create_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'mail_message' AND t.column_name = 'create_date' AND mm.id = t.record_id;

UPDATE mail_message mm
SET write_date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'mail_message' AND t.column_name = 'write_date' AND mm.id = t.record_id;

UPDATE mail_message mm
SET date = t.intended_ts
FROM tsmap t
WHERE t.table_name = 'mail_message' AND t.column_name = 'date' AND mm.id = t.record_id;

-- ---------------------------------------------------------------------------------
-- 3. mail_message.date (+ create_date/write_date) from message_timestamps.csv.
--
--    ORDERING ASSUMPTION (mirrors timestamps.py's module docstring): within one
--    (model, res_id) group, msgts.seq (1, 2, 3, ...) is matched to that group's
--    mail_message rows IN mail_message.id ASCENDING ORDER — id order == Odoo's own
--    creation order, since ids are assigned sequentially at insert time. This is how
--    the generator's "N messages on this record, in this order, at these times"
--    intent (record_message_sequence / record_messages_for) is resolved to concrete
--    mail_message rows without ever needing to know their ids up front.
--
--    GROUP-SIZE MISMATCH — LAST TIMESTAMP CARRIES FORWARD. Odoo writes chatter the
--    generator does not explicitly enumerate (a record's creation log, an automatic
--    "Vendor Bill created" note, a tracking message for a field the generator did not
--    plan for), so a (model,res_id) group routinely holds MORE mail_message rows than
--    the CSV lists seqs. Ranks beyond the last recorded seq therefore reuse the LAST
--    recorded timestamp for that group (LEAST(rn, max_seq)) instead of being skipped.
--    This is what makes timestamps.py's record_messages_for(model, res_id, T) mean what
--    it says — "every chatter message on this record belongs at T" — with a single CSV
--    row, and it is also right for the ordered form: extra trailing chatter belongs at
--    or after the last planned event, never back at the wall-clock "now" of generation.
--    Skipping them instead would leave 2026 timestamps inside a 2025 dataset, which is
--    exactly the corruption this script exists to prevent. The reverse mismatch (CSV
--    lists MORE seqs than the group has messages) is never guessed at — those rows are
--    reported by verification query 4b below.
-- ---------------------------------------------------------------------------------
WITH ranked_messages AS (
  SELECT mm.id,
         mm.model,
         mm.res_id,
         row_number() OVER (PARTITION BY mm.model, mm.res_id ORDER BY mm.id) AS rn
  FROM mail_message mm
  WHERE (mm.model, mm.res_id) IN (SELECT DISTINCT model, res_id FROM msgts)
    -- An EXPLICIT id-based stamp always wins over rank matching. The generator pins the
    -- scenario-3 change-then-revert messages by their real mail_message id (rank matching is
    -- unreliable there: Odoo mirrors a bank-account change onto the owning partner's chatter
    -- and adds creation notes, so ranks shift). Those ids are updated in section 2 above; if
    -- they were also eligible here, the carry-forward rule would immediately overwrite them
    -- and collapse the change and the revert back onto one date.
    AND mm.id NOT IN (SELECT record_id FROM tsmap WHERE table_name = 'mail_message')
),
group_max AS (
  SELECT model, res_id, max(seq) AS max_seq FROM msgts GROUP BY model, res_id
),
matched AS (
  SELECT rm.id AS message_id, t.intended_ts
  FROM ranked_messages rm
  JOIN group_max g
    ON g.model = rm.model AND g.res_id = rm.res_id
  JOIN msgts t
    ON t.model = rm.model AND t.res_id = rm.res_id
   AND t.seq = LEAST(rm.rn, g.max_seq)   -- carry the last recorded ts forward
)
UPDATE mail_message mm
SET date = matched.intended_ts,
    create_date = matched.intended_ts,
    write_date = matched.intended_ts
FROM matched
WHERE mm.id = matched.message_id;

-- mail_tracking_value follows its owning mail_message.date (the tracking row has no
-- independent "when did this change happen" timestamp of its own — po_context.sql
-- and vendor_audit.sql both read the timestamp off the owning mail_message, so
-- keeping mail_tracking_value's own create_date/write_date in step is for DB
-- consistency, not because anything downstream reads it directly).
UPDATE mail_tracking_value mtv
SET create_date = mm.date,
    write_date = mm.date
FROM mail_message mm
WHERE mtv.mail_message_id = mm.id;

COMMIT;

-- ===================================================================================
-- VERIFICATION (run after COMMIT; read-only). A clean run prints 0 for every
-- "outside period" count below. Re-run this block any time with the transaction
-- already committed — it does not modify data.
-- ===================================================================================

-- 4a. Rows updated per table (post-hoc: counts of rows whose id appears in tsmap /
--     msgts, i.e. rows this script touched — not a live UPDATE rowcount, but a
--     reproducible proxy you can re-run any time).
SELECT 'purchase_order' AS table_name, count(DISTINCT record_id) AS rows_targeted
FROM tsmap WHERE table_name = 'purchase_order'
UNION ALL
SELECT 'purchase_order_line', count(DISTINCT record_id) FROM tsmap WHERE table_name = 'purchase_order_line'
UNION ALL
SELECT 'stock_picking', count(DISTINCT record_id) FROM tsmap WHERE table_name = 'stock_picking'
UNION ALL
SELECT 'stock_move', count(DISTINCT record_id) FROM tsmap WHERE table_name = 'stock_move'
UNION ALL
SELECT 'account_move', count(DISTINCT record_id) FROM tsmap WHERE table_name = 'account_move'
UNION ALL
SELECT 'account_payment', count(DISTINCT record_id) FROM tsmap WHERE table_name = 'account_payment'
UNION ALL
SELECT 'res_partner', count(DISTINCT record_id) FROM tsmap WHERE table_name = 'res_partner'
UNION ALL
SELECT 'res_partner_bank', count(DISTINCT record_id) FROM tsmap WHERE table_name = 'res_partner_bank'
UNION ALL
SELECT 'mail_message (direct, via tsmap)', count(DISTINCT record_id) FROM tsmap WHERE table_name = 'mail_message'
UNION ALL
SELECT 'mail_message (sequence, via msgts)', count(*) FROM msgts
ORDER BY 1;

-- 4b. message_timestamps.csv rows that did NOT find a matching mail_message
--     (group row-count mismatch — reported, not guessed at). Should be empty; if not,
--     the generator created fewer/more chatter messages for that (model,res_id) than
--     it recorded timestamps for.
SELECT t.model, t.res_id, t.seq, t.intended_ts
FROM msgts t
LEFT JOIN (
  SELECT mm.id, mm.model, mm.res_id,
         row_number() OVER (PARTITION BY mm.model, mm.res_id ORDER BY mm.id) AS rn
  FROM mail_message mm
  WHERE (mm.model, mm.res_id) IN (SELECT DISTINCT model, res_id FROM msgts)
) rm ON rm.model = t.model AND rm.res_id = t.res_id AND rm.rn = t.seq
WHERE rm.id IS NULL
ORDER BY t.model, t.res_id, t.seq;

-- 4c. THE IMPORTANT CHECK: any remaining timestamp outside the generation period
--     (>= 2026-01-01, or < 2025-01-01) across the columns the timeline/audit
--     trail actually reads. Must be 0 for a clean run.
SELECT
  (SELECT count(*) FROM mail_message mm, gen_period gp
     WHERE mm.date IS NOT NULL AND (mm.date < gp.start_date OR mm.date >= gp.end_date_exclusive)) AS mail_message_date_outside_period,
  (SELECT count(*) FROM purchase_order po, gen_period gp
     WHERE po.create_date IS NOT NULL AND (po.create_date < gp.start_date OR po.create_date >= gp.end_date_exclusive)) AS po_create_date_outside_period,
  (SELECT count(*) FROM stock_picking sp, gen_period gp
     WHERE sp.date_done IS NOT NULL AND (sp.date_done < gp.start_date OR sp.date_done >= gp.end_date_exclusive)) AS picking_date_done_outside_period,
  (SELECT count(*) FROM account_move am, gen_period gp
     WHERE am.date IS NOT NULL AND (am.date < gp.start_date OR am.date >= gp.end_date_exclusive)) AS account_move_date_outside_period;

-- 4d. min/max of mail_message.date, for a quick eyeball that the whole chatter
--     history now sits inside 2025.
SELECT min(date) AS mail_message_date_min, max(date) AS mail_message_date_max
FROM mail_message;

-- 4e. If 4c is non-zero, this says WHICH records still carry an out-of-period chatter
--     timestamp. A row here means the generator created chatter on a (model,res_id) it
--     never recorded an intended timestamp for — fix the generator (add a
--     record_messages_for call for that model), then drop the DB and re-generate.
--     Re-running this script alone will NOT repair them: nothing in the CSVs targets them.
SELECT mm.model, count(*) AS messages_outside_period,
       min(mm.date) AS earliest, max(mm.date) AS latest
FROM mail_message mm, gen_period gp
WHERE mm.date IS NOT NULL
  AND (mm.date < gp.start_date OR mm.date >= gp.end_date_exclusive)
GROUP BY mm.model
ORDER BY messages_outside_period DESC;
