-- po_context.sql
-- One JSON "context" row per Purchase Order, cross-document links resolved via the
-- line-level FK spine (purchase_order_line.id <- stock_move.purchase_line_id /
-- account_move_line.purchase_line_id). Header text-links (invoice_origin) captured as a
-- fallback and flagged. jsonb unwrapped by company (code_store/name/standard_price).
-- SQL does the heavy join+nesting; extract.py derives timeline + baselines + vendor rollup.
SELECT po.name AS po_name,
json_build_object(
  'po_identity', json_build_object(
    'po_number',    po.name,
    'state',        po.state,
    'company_id',   po.company_id,
    'vendor',       json_build_object('id', po.partner_id, 'name', rp.name,
                                      'commercial_partner_id', rp.commercial_partner_id),
    -- name is a synthetic label derived from user_id, not the real Odoo login (PRE-FREEZE-REVIEW B1)
    'buyer',        json_build_object('user_id', po.user_id,
                       'name', CASE WHEN po.user_id IS NULL THEN NULL
                                    ELSE 'user_' || po.user_id::text END),
    'currency_id',  po.currency_id,
    'amount_untaxed', po.amount_untaxed,
    'amount_total', po.amount_total,
    'date_order',   po.date_order,
    'date_approve', po.date_approve,
    'create_date',  po.create_date,
    'create_uid',   po.create_uid,
    'receipt_status', po.receipt_status,
    'invoice_status', po.invoice_status
  ),
  'lines', (
    SELECT coalesce(json_agg(json_build_object(
      'line_id',       pol.id,
      'product_id',    pol.product_id,
      'product',       pt.name->>'en_US',
      'default_code',  pp.default_code,
      'qty',           pol.product_qty,
      'unit_price',    pol.price_unit,
      'price_subtotal',pol.price_subtotal,
      'qty_received',  pol.qty_received,
      'qty_invoiced',  pol.qty_invoiced,
      'standard_price',(pp.standard_price->>(po.company_id::text))::numeric
    ) ORDER BY pol.sequence, pol.id), '[]'::json)
    FROM purchase_order_line pol
    LEFT JOIN product_product pp ON pp.id = pol.product_id
    LEFT JOIN product_template pt ON pt.id = pp.product_tmpl_id
    WHERE pol.order_id = po.id AND coalesce(pol.display_type,'') = ''
  ),
  'receipts', (
    SELECT coalesce(json_agg(x ORDER BY x->>'date_done'), '[]'::json) FROM (
      SELECT DISTINCT ON (sp.id) json_build_object(
        'picking',      sp.name,
        'state',        sp.state,
        'date_done',    sp.date_done,
        'origin',       sp.origin,
        'picking_type', spt.code,
        'linked_via',   'spine'
      ) AS x
      FROM stock_move sm
      JOIN stock_picking sp ON sp.id = sm.picking_id
      JOIN stock_picking_type spt ON spt.id = sp.picking_type_id
      JOIN purchase_order_line pol ON pol.id = sm.purchase_line_id
      WHERE pol.order_id = po.id
      ORDER BY sp.id
    ) q
  ),
  'bills', (
    SELECT coalesce(json_agg(b ORDER BY b->>'date'), '[]'::json) FROM (
      SELECT json_build_object(
        'doc_number',    am.name,
        'ref',           am.ref,
        'move_type',     am.move_type,
        'invoice_date',  am.invoice_date,
        'date',          am.date,
        'amount_total',  am.amount_total,
        'state',         am.state,
        'payment_state', am.payment_state,
        'invoice_origin',am.invoice_origin,
        'linked_via', CASE WHEN EXISTS (
            SELECT 1 FROM account_move_line l
            JOIN purchase_order_line p ON p.id = l.purchase_line_id
            WHERE l.move_id = am.id AND p.order_id = po.id) THEN 'spine' ELSE 'text' END,
        'postings', (
          SELECT coalesce(json_agg(json_build_object(
            'code',    aa.code_store->>(po.company_id::text),
            'account', aa.name->>'en_US',
            'debit',   s.dr,
            'credit',  s.cr)), '[]'::json)
          FROM (SELECT aml.account_id, sum(aml.debit) dr, sum(aml.credit) cr
                FROM account_move_line aml WHERE aml.move_id = am.id GROUP BY aml.account_id) s
          JOIN account_account aa ON aa.id = s.account_id
        )
      ) AS b
      FROM account_move am
      WHERE am.move_type IN ('in_invoice','in_receipt','in_refund')
        AND ( EXISTS (SELECT 1 FROM account_move_line l
                      JOIN purchase_order_line p ON p.id = l.purchase_line_id
                      WHERE l.move_id = am.id AND p.order_id = po.id)
              OR (am.invoice_origin IS NOT NULL
                  AND am.invoice_origin ~ ('(^|[ ,])' || po.name || '([ ,]|$)')) )
    ) q
  ),
  'payments', (
    SELECT coalesce(json_agg(p ORDER BY p->>'date'), '[]'::json) FROM (
      SELECT DISTINCT ON (ap.id) json_build_object(
        'doc_number',    ap.name,
        'payment_type',  ap.payment_type,
        'amount',        ap.amount,
        'date',          ap.date,
        'state',         ap.state,
        'journal',       coalesce(aj.name->>'en_US', aj.code),
        'reconciled_bill', bm.name,
        -- Which vendor bank account this payment was sent to (level B fact, no verdict): lets a
        -- detector match a payment against res_partner_bank.account_ordinal (vendor_audit.sql)
        -- to notice money moving to a recently-added, non-first account.
        'destination_bank_account_id', ap.partner_bank_id
      ) AS p
      FROM account_payment ap
      JOIN account_move pm ON pm.id = ap.move_id
      JOIN account_move_line pml ON pml.move_id = pm.id
      JOIN account_partial_reconcile apr
        ON (apr.debit_move_id = pml.id OR apr.credit_move_id = pml.id)
      JOIN account_move_line bml
        ON bml.id = CASE WHEN apr.debit_move_id = pml.id
                         THEN apr.credit_move_id ELSE apr.debit_move_id END
      JOIN account_move bm ON bm.id = bml.move_id
      LEFT JOIN account_journal aj ON aj.id = ap.journal_id
      WHERE bm.move_type IN ('in_invoice','in_receipt')
        AND EXISTS (SELECT 1 FROM account_move_line l
                    JOIN purchase_order_line p2 ON p2.id = l.purchase_line_id
                    WHERE l.move_id = bm.id AND p2.order_id = po.id)
      ORDER BY ap.id
    ) q
  ),
  'state_log', (
    -- Neutral chatter fact: each purchase.order.state transition (via mail_tracking_value on
    -- the 'state' field, resolved by ir_model_fields.name — field_info jsonb is empty/unusable).
    -- Transition VALUES arrive as human selection LABELS in *_value_char ('RFQ','RFQ Sent',
    -- 'To Approve','Purchase Order','Cancelled'); *_value_integer is empty for this selection.
    -- Actor/timestamp come from the owning mail_message. Level B: facts only, no verdicts.
    SELECT coalesce(json_agg(json_build_object(
      'from_state', tv.old_value_char,
      'to_state',   tv.new_value_char,
      'timestamp',  m.date,
      'actor_uid',  m.create_uid
    ) ORDER BY m.date), '[]'::json)
    FROM mail_tracking_value tv
    JOIN mail_message m ON m.id = tv.mail_message_id
    JOIN ir_model_fields f ON f.id = tv.field_id
    WHERE m.res_id = po.id AND m.model = 'purchase.order' AND f.name = 'state'
  ),
  'vendor_other_pos', (
    -- Neutral listing of this vendor's other purchase orders and their bills (excludes the
    -- current PO). Level B: facts only, no comparison/derivation — lets a bill booked against
    -- a different PO of the same vendor stay visible in the current PO's own context.
    SELECT coalesce(json_agg(op ORDER BY op->>'date_order'), '[]'::json) FROM (
      SELECT json_build_object(
        'po_number',    po2.name,
        'date_order',   po2.date_order,
        'amount_total', po2.amount_total,
        'bills', (
          SELECT coalesce(json_agg(json_build_object(
            'doc_number',   am2.name,
            'ref',          am2.ref,
            'amount_total', am2.amount_total,
            'date',         am2.date
          ) ORDER BY am2.date), '[]'::json)
          FROM account_move am2
          -- partner_id first: without it this correlated sub-select rescans every bill in the
          -- company once per (PO, sibling PO) pair. Restricting to the vendor's own bills makes
          -- the same result cheap, and a bill of another vendor could never belong to po2 anyway.
          WHERE am2.partner_id = po.partner_id
            AND am2.move_type IN ('in_invoice','in_receipt','in_refund')
            AND ( EXISTS (SELECT 1 FROM account_move_line l2
                          JOIN purchase_order_line p2 ON p2.id = l2.purchase_line_id
                          WHERE l2.move_id = am2.id AND p2.order_id = po2.id)
                  OR (am2.invoice_origin IS NOT NULL
                      AND am2.invoice_origin ~ ('(^|[ ,])' || po2.name || '([ ,]|$)')) )
        )
      ) AS op
      FROM purchase_order po2
      WHERE po2.partner_id = po.partner_id AND po2.id <> po.id
    ) q
  )
) AS context
FROM purchase_order po
LEFT JOIN res_partner rp ON rp.id = po.partner_id
ORDER BY po.name;
