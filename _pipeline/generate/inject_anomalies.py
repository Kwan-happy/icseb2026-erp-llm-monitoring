#!/usr/bin/env python
"""inject_anomalies.py - dataset generation, stage 3: the two anomaly scenarios that need a
POST-hoc step against records build_normal.py already created.

Scenarios 2, 4, 5 need NOTHING here - they are fully expressed by the plan (amounts, dates,
which user acts) and build_normal.py applies them directly. This module handles ONLY:

  Scenario 1 (`duplicate_payment`) - register a SECOND vendor bill against an already-billed
  PO, spine-linked via `account_move_line.purchase_line_id` (po_context.sql's contract), and
  optionally a second reconciled payment via the account.payment.register wizard.

  Scenario 3 (`vendor_master_mod`) - edit a sensitive vendor field as a named session, either
  once (legit noise) or twice - change then revert (tamper) - so vendor_audit.sql's
  mail_tracking_value join finds the trail with the right actor (mail_message.create_uid).

Almost no sampling happens here: nearly every value (variant, delta, offset, actor, timestamps)
was already decided in plan_dataset.py. This module only executes it against a live Odoo DB via
XML-RPC.

Exception: the S1 duplicate-payment's payment_date-minus-invoice_date gap is sampled HERE, not
in the plan, like build_normal.py's process latencies. Reuses build_normal.py's `_po_rng` / `_draw_lognormal` / `_LAT` / `_clamp` (that
module is already imported earlier in generate.py's pipeline, so its params.json load and
_PERIOD_END are already initialized by the time this module runs) rather than duplicating that
machinery.

VERIFIED end-to-end on the full generated dataset:
  - res.partner.vat IS tracked: a manager-session write produces a mail_tracking_value row with
    field='vat' and the acting user in mail_message.create_uid.
  - res.partner.bank.acc_number IS tracked, and Odoo additionally MIRRORS the change onto the
    owning partner's chatter, so one bank edit yields tracking rows on BOTH models. Both are
    stamped by mail_message.id (not by rank) so the change-then-revert pair keeps two distinct
    dates - see the comment at the tamper branch.

ACCESS RIGHTS: "You are not allowed ... allowed for: Contact/Creation" when writing
res.partner.bank is a missing grant (see ir_model_access / res_groups_implied_rel):
res.partner.bank needs base.group_partner_manager and product.product
needs product.group_product_manager, and in a stock Odoo 19 DB both are implied only by
base.group_system. The generator therefore routes master-data creation and the scenario-3
bank-account edits through the SETUP session (the manager holding base.group_system, resolved by
preflight()); reading mail.tracking.value also needs those rights, so the reader is separated
from the actor in _apply_s3 - the write still happens as the plan's named user, which is what
lands in mail_message.create_uid and becomes the evidence the model reasons over.
"""
import datetime
import json
import pathlib

import build_normal as _bn   # reuse _po_rng/_draw_lognormal/_LAT/_clamp - see module docstring

OUT_DIR = pathlib.Path(__file__).resolve().parent / "gen_out"

S1 = "duplicate_payment"


# ---------- small helpers ----------
def _round2(x):
    return round(float(x), 2)


def _iso(d):
    if isinstance(d, str):
        return d
    return d.isoformat()


def _by_idx(items, idx):
    """`items` is a list of dicts (build_master.py's / the documented plan-consumer contract's
    shape), created in idx order. Prefer an explicit 'idx' key if present; otherwise idx ==
    position in the list, which holds for every master-data builder in this generator."""
    for it in items:
        if it.get("idx") == idx:
            return it
    return items[idx]


# These always return the RECORD DICT from build_master (e.g. {"idx","id","bank_id","name"}),
# never a bare id. `master["vendor_by_idx"]` / `product_by_idx` map idx -> id only, and S3 needs
# `bank_id` off the vendor record, so resolving through the lists is the form every call site
# here expects (`vendor["id"]`, `vendor["bank_id"]`, `product["id"]`).
def _vendor(master, idx):
    return _by_idx(master["vendors"], int(idx))


def _product(master, idx):
    return _by_idx(master["products"], int(idx))


def _tracking_message_ids(session, model, res_id, limit):
    """Ids of the last `limit` chatter messages on (model, res_id) that carry a tracked field
    change, oldest first. Only tracking-bearing messages count, so Odoo's own plain notes cannot
    shift the alignment the way rank matching does."""
    msgs = session.search_read("mail.message", [("model", "=", model), ("res_id", "=", res_id)],
                               ["id"], order="id desc", limit=20)
    ids = [m["id"] for m in msgs]
    if not ids:
        return []
    tvs = session.search_read("mail.tracking.value", [("mail_message_id", "in", ids)],
                              ["mail_message_id"], order="id desc", limit=40)

    def mid_of(tv):
        v = tv["mail_message_id"]
        return v[0] if isinstance(v, (list, tuple)) else v

    return sorted({mid_of(tv) for tv in tvs})[-limit:]


def _applied_po(applied, po_key):
    by_key = applied.get("by_key")
    if by_key and po_key in by_key:
        return by_key[po_key]
    for p in applied["pos"]:
        if p.get("po_key") == po_key:
            return p
    return None


def _line_id(applied_po, product_idx):
    line_ids = applied_po["line_ids"]
    if product_idx in line_ids:
        return line_ids[product_idx]
    return line_ids[str(product_idx)]


def _close_gap(line_vals, target_total):
    """Nudge the LAST line's price_unit so the constructed lines sum to exactly
    `target_total`, without touching quantities (mirrors plan_dataset.py's
    _build_lines_to_amount rationale: never contaminate scenario-5's price-ratio signal by
    scaling price for an unrelated reason - here it's just closing a rounding gap)."""
    raw = sum(v["quantity"] * v["price_unit"] for _, _, v in line_vals)
    gap = target_total - raw
    last = line_vals[-1][2]
    qty = max(last["quantity"], 1)
    last["price_unit"] = _round2(last["price_unit"] + gap / qty)


def _record_write_date(tsmap, model, res_id, ts):
    if model == "res.partner":
        tsmap.record_partner(res_id, write_date=ts)
    elif model == "res.partner.bank":
        tsmap.record_partner_bank(res_id, write_date=ts)


# ---------- Scenario 1 ----------
def _apply_s1(session, po, applied_po, master, tsmap):
    """Create a duplicate vendor bill (and, per variant, a second reconciled payment) against
    `po`. `session` is the plan's buyer_session for this PO - a plausible actor for "someone
    uploads a second bill", and S1 (unlike S3) carries no actor-based signal per the frozen
    spec, so no particular session is mandated."""
    anomaly = po["anomaly"]
    vendor = _vendor(master, po["vendor_idx"])

    bill_ids = applied_po.get("bill_ids") or []
    if not bill_ids:
        raise RuntimeError(
            f"S1 target {po['po_key']!r} has no existing bill to duplicate - build_normal.py "
            "must set full_tail=True (it is set on every S1 plan PO) and bill it before "
            "inject_anomalies.py runs"
        )
    first_bill_id = bill_ids[0]
    first_bill = session.search_read(
        "account.move", [("id", "=", first_bill_id)],
        ["invoice_date", "date", "amount_total"])[0]
    base_date_s = first_bill.get("invoice_date") or first_bill.get("date") or po["date_order"]
    base_date = datetime.date.fromisoformat(str(base_date_s)[:10])

    variant = anomaly["variant"]
    invoice_date = base_date
    if variant == "different_date":
        invoice_date = base_date + datetime.timedelta(days=int(anomaly.get("date_offset_days", 0)))

    target_total = _round2(
        first_bill["amount_total"]
        + (anomaly.get("amount_delta", 0.0) if variant == "rounding_delta" else 0.0)
    )

    line_vals = []
    for l in po["lines"]:
        product = _product(master, l["product_idx"])
        line_id = _line_id(applied_po, l["product_idx"])
        line_vals.append((0, 0, {
            "product_id": product["id"],
            "quantity": l["qty"],
            "price_unit": l["unit_price"],
            "purchase_line_id": line_id,   # THE spine link po_context.sql's `bills[].linked_via` checks
            "name": product.get("name") or "",
        }))
    _close_gap(line_vals, target_total)

    move_id = session.create("account.move", {
        "move_type": "in_invoice",
        "partner_id": vendor["id"],
        "invoice_date": invoice_date.isoformat(),
        "date": invoice_date.isoformat(),
        "invoice_origin": applied_po["po_number"],
        "invoice_line_ids": line_vals,
    })
    session.call_button("account.move", move_id, "action_post")
    tsmap.record_account_move(move_id, create_date=invoice_date, write_date=invoice_date)

    payment_id = None
    if anomaly.get("second_payment"):
        # Drawn from the fast end of the same lognormal family the
        # normal tail uses (process_latency.bill_to_payment_days_dup_fast), a tendency not a
        # constant, tuned to overlap the normal population substantially - see
        # params_derivation.md §3b.
        payment_date = _bn._clamp(invoice_date + datetime.timedelta(
            days=_bn._draw_lognormal(_bn._po_rng(po["po_key"], "duplicate_payment_to_bill"),
                                      _bn._LAT["bill_to_payment_days_dup_fast"])))
        wiz_session = session.with_context({"active_model": "account.move", "active_ids": [move_id]})
        wiz_id = wiz_session.create("account.payment.register",
                                     {"payment_date": payment_date.isoformat()})
        wiz_session.call_button("account.payment.register", wiz_id, "action_create_payments")
        pay_rows = session.search_read(
            "account.payment", [("reconciled_invoice_ids", "in", [move_id])],
            ["id"], order="id desc", limit=1)
        if pay_rows:
            payment_id = pay_rows[0]["id"]
            tsmap.record_payment(payment_id, create_date=payment_date, write_date=payment_date)

    return {
        "po_number": applied_po["po_number"], "scenario": S1, "variant": variant,
        "duplicate_bill_id": move_id, "duplicate_payment_id": payment_id,
        "target_total": target_total,
    }


# ---------- Scenario 1 EVASIVE (anomaly_spec.md) ----------
def _apply_s1_cross_po(cluster, anchor_po, dup_po, master, applied, tsmap):
    """The same vendor invoice re-billed against a DIFFERENT PO of the same vendor. `anchor_po`
    already has its own natural bill+payment (build_normal.py, full_tail=True). `dup_po` was
    built with full_tail=False, so build_normal.py gave it NO bill at all — this creates its
    ONE bill (spine-linked to dup_po's own lines, so `bills[].linked_via == 'spine'`), forced to
    (near-)match the anchor's already-posted bill amount, then registers a payment against it —
    both POs end up with exactly one bill each, so R1's `count_bills > 1` never trips; the
    duplication is only visible by comparing amounts across the vendor's POs (the vendor
    rollup's per-PO bills[])."""
    anchor_applied = _applied_po(applied, anchor_po["po_key"])
    dup_applied = _applied_po(applied, dup_po["po_key"])
    if anchor_applied is None or dup_applied is None:
        raise KeyError(
            f"S1-evasive pair ({anchor_po['po_key']!r}, {dup_po['po_key']!r}) missing from "
            "applied['pos'] - build_normal.py must apply both POs before inject_anomalies.py runs")
    anchor_bill_ids = anchor_applied.get("bill_ids") or []
    if not anchor_bill_ids:
        raise RuntimeError(
            f"S1-evasive anchor {anchor_po['po_key']!r} has no bill to duplicate - "
            "build_normal.py must set full_tail=True on the anchor")

    anchor_session = cluster.as_(anchor_po["buyer_session"])
    anchor_bill_id = anchor_bill_ids[0]
    anchor_bill = anchor_session.search_read(
        "account.move", [("id", "=", anchor_bill_id)],
        ["invoice_date", "date", "amount_total"])[0]
    base_date_s = anchor_bill.get("invoice_date") or anchor_bill.get("date") or anchor_po["date_order"]
    base_date = datetime.date.fromisoformat(str(base_date_s)[:10])

    dup_anomaly = dup_po["anomaly"]
    shared_ref = dup_anomaly.get("shared_ref")
    if shared_ref:
        # Same vendor invoice reference on both bills — a realistic detail, though the extract
        # does not currently surface account_move.ref, so detection rests on amount/date, not ref.
        anchor_session.write("account.move", anchor_bill_id, {"ref": shared_ref})

    dup_date = base_date + datetime.timedelta(days=int(dup_anomaly.get("date_offset_days", 7)))
    target_total = _round2(anchor_bill["amount_total"] + dup_anomaly.get("amount_delta", 0.0))

    vendor = _vendor(master, dup_po["vendor_idx"])
    line_vals = []
    for l in dup_po["lines"]:
        product = _product(master, l["product_idx"])
        line_id = _line_id(dup_applied, l["product_idx"])
        line_vals.append((0, 0, {
            "product_id": product["id"], "quantity": l["qty"], "price_unit": l["unit_price"],
            "purchase_line_id": line_id,   # spine link into dup_po, NOT the anchor
            "name": product.get("name") or "",
        }))
    _close_gap(line_vals, target_total)

    dup_session = cluster.as_(dup_po["buyer_session"])
    move_id = dup_session.create("account.move", {
        "move_type": "in_invoice",
        "partner_id": vendor["id"],
        "invoice_date": dup_date.isoformat(),
        "date": dup_date.isoformat(),
        "invoice_origin": dup_applied["po_number"],
        "ref": shared_ref,
        "invoice_line_ids": line_vals,
    })
    dup_session.call_button("account.move", move_id, "action_post")
    tsmap.record_account_move(move_id, create_date=dup_date, write_date=dup_date)

    # Both need a full P2P tail (anomaly_spec.md §2 S1 evasive) — register a payment too.
    pay_date = _bn._clamp(dup_date + datetime.timedelta(
        days=_bn._draw_lognormal(_bn._po_rng(dup_po["po_key"], "s1_cross_po_bill_to_payment"),
                                  _bn._LAT["bill_to_payment_days"])))
    payment_id = _bn._register_payment(dup_session, move_id, pay_date)
    tsmap.record_payment(payment_id, create_date=pay_date, write_date=pay_date)

    return {
        "po_number": dup_applied["po_number"], "scenario": S1, "variant": "cross_po",
        "anchor_po_number": anchor_applied["po_number"], "duplicate_bill_id": move_id,
        "duplicate_payment_id": payment_id, "target_total": target_total,
    }


# ---------- Scenario 3 ----------
def _apply_s3(cluster, plan, master, tsmap):
    # Reading mail.tracking.value requires administrator rights, which a plain purchase manager
    # does not have. Separate the READER from the ACTOR: the edits are still written by the
    # session the plan names - that is what lands in mail_message.create_uid and becomes the
    # evidence the model reasons over - while the read-back that resolves the new message ids
    # goes through the elevated setup session. Reading changes no data and no actor.
    reader = cluster.as_(plan["params_echo"].get("setup_session") or "setup")
    results = {}
    for edit in plan.get("audit_edits", []):
        v_idx = edit["vendor_idx"]
        vendor = _vendor(master, v_idx)
        model = edit["model"]
        session = cluster.as_(edit["actor_session"])

        bucket = results.setdefault(str(v_idx), {"vendor_id": vendor["id"], "edits": []})

        if edit["kind"] == "bank_add":
            # evasive S3 (anomaly_spec.md): a NEW res.partner.bank row, never a write to
            # the existing one, so mail_tracking_value has nothing to see (Odoo never tracks the
            # INSERT itself - vendor_audit.sql's comment). This is the whole point of the
            # evasive variant: `changes[]` stays empty; only `bank_account_additions[]`
            # (sourced from res_partner_bank.create_date/create_uid) sees it.
            new_bank_id = session.create("res.partner.bank", {
                "partner_id": vendor["id"],
                "acc_number": edit["new"],
                "acc_holder_name": vendor["name"],
            })
            at = edit["at"]
            tsmap.record_partner_bank(new_bank_id, create_date=at, write_date=at)
            bucket["edits"].append({
                "kind": "bank_add", "model": model, "bank_id": new_bank_id,
                "new_value": edit["new"], "at": at,
            })
            continue

        field = edit["field"]
        res_id = vendor["bank_id"] if model == "res.partner.bank" else vendor["id"]

        if edit["kind"] == "legit":
            session.write(model, res_id, {field: edit["new"]})
            at = edit["at"]
            if model == "res.partner.bank":
                # Odoo mirrors a res.partner.bank change onto the owning partner's chatter too,
                # so rank matching (record_messages_for) can misdate the mirror exactly as it
                # would for the tamper branch below - stamp by real message id here as well.
                # Legit is a single change, so limit=1 (vs. the tamper branch's 2).
                for msg_id in _tracking_message_ids(reader, model, res_id, 1):
                    tsmap.record_message(msg_id, at)
                for msg_id in _tracking_message_ids(reader, "res.partner", vendor["id"], 1):
                    tsmap.record_message(msg_id, at)
            else:
                tsmap.record_messages_for(model, res_id, at)
            _record_write_date(tsmap, model, res_id, at)
            bucket["edits"].append({
                "kind": "legit", "model": model, "field": field, "res_id": res_id,
                "new_value": edit["new"], "at": at,
            })
            continue

        # kind == "tamper": read the real original value, write the tamper, then restore it -
        # two tracked changes on the same (model, res_id), matching §2 S3's change-then-revert.
        current = session.execute(model, "read", [[res_id], [field]])[0][field]
        original = current if current is not False else None
        session.write(model, res_id, {field: edit["new"]})
        session.write(model, res_id, {field: original})

        at, revert_at = edit["at"], edit["revert_at"]
        _record_write_date(tsmap, model, res_id, revert_at)
        # Stamp the two tracked changes BY MESSAGE ID rather than by rank. Rank matching is
        # too fragile here: Odoo posts a variable number of extra messages per record (creation
        # notes, and it MIRRORS a res.partner.bank change onto the owning partner's chatter, so
        # writing acc_number yields tracking rows on BOTH models - verified on the smoke run).
        # Any miscount shifts every rank and collapses the change-then-revert pair onto one
        # date, destroying the very pattern this scenario is defined by.
        for msg_id, ts in zip(_tracking_message_ids(reader, model, res_id, 2),
                              [at, revert_at]):
            tsmap.record_message(msg_id, ts)
        if model == "res.partner.bank":
            for msg_id, ts in zip(
                    _tracking_message_ids(reader, "res.partner", vendor["id"], 2),
                    [at, revert_at]):
                tsmap.record_message(msg_id, ts)
        bucket["edits"].append({
            "kind": "tamper", "model": model, "field": field, "res_id": res_id,
            "original_value": original, "tampered_value": edit["new"],
            "at": at, "revert_at": revert_at,
        })
    return results


# ---------- entry point ----------
def apply_anomalies(cluster, plan, master, applied, tsmap):
    """Runs S1 (duplicate bill/payment, canonical AND the cross-PO evasive variant) over
    every plan PO whose anomaly type is 'duplicate_payment', and S3 (vendor-master
    change-then-revert / bank-account addition / legit noise) over every entry in
    plan['audit_edits']. Returns a dict describing what was injected and also writes it to
    gen_out/injected.json."""
    out = {"pos": {}, "audit": {}}
    plan_by_key = {p["po_key"]: p for p in plan["pos"]}

    for po in plan["pos"]:
        anomaly = po.get("anomaly") or {}
        if anomaly.get("type") != S1:
            continue
        if anomaly.get("variant") == "cross_po":
            # evasive S1: only act once per pair, on the "duplicate" member — it creates
            # BOTH POs' one bill (the anchor's is already built by build_normal.py; this call
            # duplicates it onto the second PO). The "anchor" member needs no separate action.
            if anomaly.get("pair_role") != "duplicate":
                continue
            # Resolve the anchor by pair_id + pair_role, NOT by a stashed po_key: plan_dataset.py's
            # chronological renumbering pass reassigns every po_key in place AFTER these anomaly
            # blocks are built, so a po_key captured earlier would go stale and silently resolve
            # to an unrelated PO. pair_id is assigned after renumbering-independent construction and is
            # never mutated, so it stays a reliable join key.
            pair_id = anomaly.get("pair_id")
            candidates = [p for p in plan["pos"]
                          if (p.get("anomaly") or {}).get("type") == S1
                          and (p.get("anomaly") or {}).get("variant") == "cross_po"
                          and (p.get("anomaly") or {}).get("pair_id") == pair_id
                          and (p.get("anomaly") or {}).get("pair_role") == "anchor"]
            if len(candidates) != 1:
                raise KeyError(
                    f"S1-evasive {po['po_key']!r} (pair_id={pair_id!r}) resolved to "
                    f"{len(candidates)} anchor candidates, expected exactly 1")
            anchor_po = candidates[0]
            # Hard guard: a wrong-vendor pairing must never pass silently — fail loudly instead
            # of quietly writing a cross-vendor duplicate bill.
            if anchor_po["vendor_idx"] != po["vendor_idx"]:
                raise ValueError(
                    f"S1-evasive pair_id={pair_id!r}: duplicate {po['po_key']!r} "
                    f"(vendor_idx={po['vendor_idx']!r}) resolved to anchor "
                    f"{anchor_po['po_key']!r} (vendor_idx={anchor_po['vendor_idx']!r}) — "
                    "vendor mismatch, refusing to inject a cross-vendor duplicate bill")
            out["pos"][po["po_key"]] = _apply_s1_cross_po(cluster, anchor_po, po, master,
                                                           applied, tsmap)
            continue
        applied_po = _applied_po(applied, po["po_key"])
        if applied_po is None:
            raise KeyError(
                f"plan po_key {po['po_key']!r} missing from applied['pos'] - build_normal.py "
                "must apply every plan PO before inject_anomalies.py runs"
            )
        session = cluster.as_(po["buyer_session"])
        out["pos"][po["po_key"]] = _apply_s1(session, po, applied_po, master, tsmap)

    out["audit"] = _apply_s3(cluster, plan, master, tsmap)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "injected.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out
