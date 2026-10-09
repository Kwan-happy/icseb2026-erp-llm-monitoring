#!/usr/bin/env python
"""build_normal.py - dataset generation, stage 2: push planned POs into Odoo.

Consumes `plan["pos"]` (built by plan_dataset.build_plan — nearly all sampling already
happened there) and `master` (vendors/products already created in Odoo by
build_master.apply_master). For every planned PO it:

Exception: process-step LATENCIES (RFQ/confirm/approve/receipt/bill/payment gaps, and the
order's own time-of-day) and the APPROVER pick for manager-authored POs are sampled HERE, not
in the plan. Both use a dedicated, deterministically
seeded RNG keyed on (run seed, po_key, a step-specific salt) — see `_po_rng()` below — which
gives the same seed-in/dataset-out reproducibility as PLAN-phase sampling would, without
touching the plan file. Every other value on `plan_po` (amounts, vendor, dates, anomaly flags,
labels) is still decided entirely in plan_dataset.py.

  1. creates the purchase.order AS the plan's buyer_session, tax-free, with lines exactly as
     planned (never recomputes a price/qty);
  2. verifies amount_untaxed == amount_total and amount_total matches the plan within 0.01c
     (a tax/rounding drift would silently invalidate the 20000 approval threshold);
  3. optionally sends the RFQ (state -> 'sent') so state_log records the transition;
  4. confirms the PO as the buyer, then — if it lands in 'to approve' — approves it as a
     manager session (never "fixes" a manager-authored PO that self-approves: that IS the
     scenario-4 signal);
  5. backdates date_approve;
  6. validates the picking(s) Odoo created on confirm, so qty_received is filled;
  7. if full_tail, creates+posts a vendor bill via the PO's own action_create_invoice and
     registers a reconciled payment through the account.payment.register wizard; if the plan
     marks the PO as an S1 hard negative (anomaly.two_bills == "down_payment_then_final"),
     instead produces TWO legitimate bills — a scaled-down down-payment draft, posted+paid,
     then a second action_create_invoice call that picks up the unbilled remainder, posted+
     paid — and asserts the two bills sum to amount_total (label stays null);
  8. records every "now"-stamped column into `tsmap`, including one ordered chatter timestamp
     per state transition via tsmap.record_message_sequence().

po.create() calls are batched per buyer_session (create_many) since the acting user is the
same for every PO in a batch; every later step (state changes, receipt, bill, payment) is
issued individually because the actor/state differs per PO. On ANY failure this raises
BuildNormalError naming the po_key and the step that failed — a half-generated dataset must
be obvious, not silent. Every id created is appended to gen_out/created_ids.jsonl as it goes.

Run from the repo root (imported by generate.py; not meant to run standalone, since it
needs a `master` dict — see build_master.py's own __main__ for how to obtain one for a
standalone smoke test).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
import sys
import time
from collections import defaultdict

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent          # _pipeline/generate
OUT_DIR = HERE / "gen_out"
CREATED_IDS_LOG = OUT_DIR / "created_ids.jsonl"

sys.path.insert(0, str(HERE))

# ---------------------------------------------------------------------------------
# Per-PO process-latency sampling.
#
# Each latency draw uses a DEDICATED, deterministically seeded RNG derived from (run seed, the PO's stable po_key, a step-specific salt) via
# numpy.random.default_rng. This gives the same reproducibility guarantee (same seed +
# same plan -> same dataset) as PLAN-phase sampling, and it is immune to re-sorting of
# plan.json, since it keys off po_key rather than list position.
# The seed is read from params.json (never hard-coded) - see _SEED below.
# ---------------------------------------------------------------------------------
_PARAMS = json.loads((HERE / "params.json").read_text(encoding="utf-8"))
_SEED = _PARAMS["seed"]
_LAT = _PARAMS["process_latency"]

# Evasive S4 "rubber-stamp" approval (anomaly_spec.md §2 S4): far faster than the
# existing fast_approval co-signal (an HOURS-scale compression, still "approved quickly") — this
# is SECONDS-scale, "nobody could have read it". Not a params.json distribution: this value is a hard requirement of the evasive recipe
# itself rather than a calibrated background parameter. Drawn from a distribution (never a
# constant) via the same seeded _po_rng() every other latency in this module uses.
_RUBBER_STAMP_SECONDS = {"median": 25.0, "sigma_log": 0.6, "min": 5.0, "max": 180.0}


def _po_rng(po_key, salt):
    """A fresh, deterministic RNG for one (po_key, salt) draw. Recomputing with the same
    (po_key, salt) always returns a generator with the same initial state, independent of
    call order elsewhere in the run - so the same order_date/latency is produced whether
    this is called once or (as with order_base_time, needed in both the batch-create loop
    and _apply_one) twice for the same PO."""
    h = hashlib.sha256(f"{_SEED}:{po_key}:{salt}".encode("utf-8")).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "big"))


def _draw_lognormal(rng, spec):
    """A single draw from a log-normal clipped to [spec['min'], spec['max']]. `median` is
    the distribution's median (not the underlying normal's mean), so mean_log = ln(median)."""
    val = float(rng.lognormal(mean=np.log(spec["median"]), sigma=spec["sigma_log"]))
    return min(max(val, spec["min"]), spec["max"])


def _sample_order_base_time(po_key):
    """The order's time-of-day within a plausible SME working day (params.json's
    process_latency.order_base_time) Returns (hour, minute)."""
    spec = _LAT["order_base_time"]
    rng = _po_rng(po_key, "order_base_time")
    hour = float(rng.normal(spec["mean_hour"], spec["sd_hours"]))
    hour = min(max(hour, spec["min_hour"]), spec["max_hour"])
    h = int(hour)
    m = int(round((hour - h) * 60)) % 60
    return h, m


class BuildNormalError(RuntimeError):
    """Raised with the po_key and the step that failed - never swallow this."""

    def __init__(self, po_key, step, detail):
        super().__init__(f"[{po_key}] failed at step {step!r}: {detail}")
        self.po_key = po_key
        self.step = step


# ---------- small helpers ----------
def _log_created(kind, **fields):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CREATED_IDS_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, **fields}, default=str) + "\n")


def _combine(date_str, h=9, m=0):
    """A plan 'YYYY-MM-DD' date string + a time-of-day -> a naive datetime."""
    d = _dt.date.fromisoformat(date_str)
    return _dt.datetime.combine(d, _dt.time(h, m))


def _odoo_dt(ts):
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _odoo_date(ts):
    return ts.strftime("%Y-%m-%d")


def _lookup(mapping, idx):
    """mapping may be a dict keyed by int OR by str(idx) (JSON round-trips coerce int keys to
    strings), so try both before giving up."""
    if idx in mapping:
        return mapping[idx]
    if str(idx) in mapping:
        return mapping[str(idx)]
    raise KeyError(f"index {idx!r} not found in master map (have {list(mapping)[:5]}...)")


def _index_master(master):
    """Normalize master into {idx: odoo_id} maps for vendors and products. build_master.
    apply_master returns vendor_by_idx/product_by_idx already in this {idx: id} shape; fall
    back to building it from the plain lists if those keys are ever missing."""
    vendor_map = master.get("vendor_by_idx")
    if not vendor_map:
        vendor_map = {v.get("idx", i): v["id"] for i, v in enumerate(master["vendors"])}
    product_map = master.get("product_by_idx")
    if not product_map:
        product_map = {p.get("idx", i): p["id"] for i, p in enumerate(master["products"])}
    return vendor_map, product_map


# ---------- Odoo-facing steps ----------
def _order_lines(plan_po, product_info, product_idx_map, date_planned_str):
    cmds = []
    for l in plan_po["lines"]:
        pid = _lookup(product_idx_map, l["product_idx"])
        info = product_info[pid]
        uom = info.get("uom_id")
        cmds.append((0, 0, {
            "product_id": pid,
            "product_qty": l["qty"],
            "product_uom_id": uom[0] if uom else False,
            "price_unit": l["unit_price"],
            "name": info["name"],
            "date_planned": date_planned_str,
            "tax_ids": [(6, 0, [])],           # tax-free dataset (amount_untaxed==amount_total)
        }))
    return cmds


def _set_move_done_qty(sess, move_id, qty):
    """Odoo 17+ renamed stock.move's 'done quantity' field to `quantity` (was `qty_done`).
    Try the current name first, fall back to the old one so this survives either schema."""
    try:
        sess.write("stock.move", move_id, {"quantity": qty})
    except Exception:
        sess.write("stock.move", move_id, {"qty_done": qty})


def _post_and_pay_bill(buyer, bill_id, bill_date, tsmap, po_key):
    """Backdate + post `bill_id`, register a reconciled payment against it, record both into
    `tsmap`, log the pair to gen_out/created_ids.jsonl, and return the new payment id. Shared
    by the ordinary single-bill tail and the S1 down-payment/final hard-negative tail (step 7
    of _apply_one)."""
    buyer.write("account.move", bill_id, {
        "invoice_date": _odoo_date(bill_date),
        "date": _odoo_date(bill_date),
    })
    buyer.call_button("account.move", bill_id, "action_post")
    tsmap.record_account_move(bill_id, create_date=bill_date, write_date=bill_date)

    # Salted with bill_id (not just po_key) so the S1 down-payment/final
    # hard negative's two bills (same po_key, different bill_id) draw independently.
    pay_date = _clamp(bill_date + _dt.timedelta(
        days=_draw_lognormal(_po_rng(f"{po_key}:{bill_id}", "bill_to_payment"),
                              _LAT["bill_to_payment_days"])))
    payment_id = _register_payment(buyer, bill_id, pay_date)
    tsmap.record_payment(payment_id, create_date=pay_date, write_date=pay_date)
    _log_created("bill", po_key=po_key, bill_id=bill_id, payment_id=payment_id)
    return payment_id


def _new_invoice_id(buyer, po_id, before_ids, step_label):
    """Call action_create_invoice on `po_id` and return the single new draft bill id it
    produced, identified by set-difference on purchase.order.invoice_ids (mirrors
    _register_payment's approach - the button's own return value shape is not stable)."""
    buyer.call_button("purchase.order", po_id, "action_create_invoice")
    after_ids = set(buyer.execute(
        "purchase.order", "read", [[po_id], ["invoice_ids"]])[0]["invoice_ids"])
    new_ids = sorted(after_ids - before_ids)
    if len(new_ids) != 1:
        raise RuntimeError(
            f"expected exactly 1 new draft bill from action_create_invoice ({step_label}), "
            f"got {new_ids} (before={len(before_ids)} after={len(after_ids)})")
    return new_ids[0]


def _register_payment(sess, bill_id, pay_date):
    """Register a payment against `bill_id` through the account.payment.register wizard (NOT
    by creating account.payment directly) so it lands in account_partial_reconcile - the only
    way po_context.sql's 'payments' join can see it. The wizard's return value shape is not
    documented as stable across versions, so identify the new payment by set-difference on
    account.payment ids instead of trusting the button's return dict."""
    before = set(sess.search("account.payment", []))
    ctx = {"active_model": "account.move", "active_ids": [bill_id], "active_id": bill_id}
    wiz_sess = sess.with_context(ctx)
    wiz_id = wiz_sess.create("account.payment.register", {"payment_date": _odoo_date(pay_date)})
    wiz_sess.call_button("account.payment.register", wiz_id, "action_create_payments")
    after = set(sess.search("account.payment", []))
    new_ids = sorted(after - before)
    if len(new_ids) != 1:
        raise RuntimeError(
            f"expected exactly 1 new account.payment after registering bill {bill_id}, "
            f"got {new_ids} (before={len(before)} after={len(after)})")
    return new_ids[0]


_PERIOD_END = None          # set by apply_normal() from plan["params_echo"]["generation_end"]


def _clamp(ts):
    """Keep a derived downstream date inside the generation period. The receipt/bill/payment
    tail is built by adding days to the order date, so a PO ordered in late December settles in
    January - putting 2026 timestamps in a dataset that claims to cover calendar year 2025 (one
    such payment, PBNK1/2026/00001, appeared in the smoke run). Only the tail of the last few
    days' orders is affected."""
    if _PERIOD_END is None or ts is None:
        return ts
    end = _dt.datetime.combine(_PERIOD_END, _dt.time(23, 0)) if isinstance(_PERIOD_END, _dt.date) \
        else _PERIOD_END
    if isinstance(ts, _dt.datetime):
        return min(ts, end)
    return min(ts, end.date())


def _apply_one(po_id, plan_po, product_info, product_idx_map, cluster, manager, tsmap):
    po_key = plan_po["po_key"]
    buyer = cluster.as_(plan_po["buyer_session"])
    step = "create_po"
    try:
        h, m = _sample_order_base_time(po_key)
        order_date = _combine(plan_po["date_order"], h, m)

        # ---- verify tax-free + amount (step 2) ----
        step = "verify_amount"
        po_data = buyer.execute("purchase.order", "read",
                                 [[po_id], ["amount_untaxed", "amount_total", "order_line"]])[0]
        if abs(po_data["amount_untaxed"] - po_data["amount_total"]) > 0.01:
            raise RuntimeError(
                f"amount_untaxed={po_data['amount_untaxed']} != amount_total="
                f"{po_data['amount_total']} - a tax leaked onto a line")
        if abs(po_data["amount_total"] - plan_po["amount_total"]) > 0.01:
            raise RuntimeError(
                f"Odoo amount_total={po_data['amount_total']} vs plan amount_total="
                f"{plan_po['amount_total']} - rounding drift")

        # line ids, in the same order the (0,0,{...}) commands were sent
        line_ids = po_data["order_line"]
        line_id_by_product_idx = {
            l["product_idx"]: lid for l, lid in zip(plan_po["lines"], line_ids)
        }
        for lid in line_ids:
            tsmap.record_po_line(lid, create_date=order_date, write_date=order_date)

        anomaly = plan_po.get("anomaly") or {}
        msg_ts = []
        cur_ts = order_date

        # ---- 3. RFQ send (state -> 'sent') ----
        # The RFQ-sent and skip-RFQ paths are sampled from INDEPENDENT
        # distributions (rather than "+1h if sent, always +2h more") so the confirm-time
        # distribution does not reduce to a two-valued readout of rfq_sent - see
        # process_latency in params.json / params_derivation.md §3a.
        if plan_po["rfq_sent"]:
            step = "send_rfq"
            sent_ts = _clamp(cur_ts + _dt.timedelta(
                hours=_draw_lognormal(_po_rng(po_key, "order_to_rfq"),
                                       _LAT["order_to_rfq_hours"])))
            # A direct write() still produces a mail_tracking_value row on 'state' (that's all
            # state_log reads) - the mail-composer "Send by Email" button is not practical over
            # XML-RPC (it opens a wizard whose "send" action both emails and sets the state),
            # so we use the direct write. See module docstring / final report.
            buyer.write("purchase.order", po_id, {"state": "sent"})
            msg_ts.append(sent_ts)
            confirm_ts = _clamp(sent_ts + _dt.timedelta(
                hours=_draw_lognormal(_po_rng(po_key, "rfq_to_confirm"),
                                       _LAT["rfq_to_confirm_hours"])))
        else:
            confirm_ts = _clamp(cur_ts + _dt.timedelta(
                hours=_draw_lognormal(_po_rng(po_key, "order_to_confirm_no_rfq"),
                                       _LAT["order_to_confirm_hours_no_rfq"])))

        # ---- 4. confirm (buyer), approve (manager) if routed to 'to approve' ----
        step = "confirm"
        buyer.call_button("purchase.order", po_id, "button_confirm")
        msg_ts.append(confirm_ts)

        # fast_approval (scenario-4 positives only) draws from the
        # COMPRESSED confirm_to_approve_hours_fast distribution instead of the ordinary one -
        # a genuinely fast rubber-stamp approval, materially shorter but not instantaneous.
        approve_spec = (_LAT["confirm_to_approve_hours_fast"] if anomaly.get("fast_approval")
                         else _LAT["confirm_to_approve_hours"])
        approve_ts = confirm_ts   # unless routed to 'to approve' below, approval is instantaneous
        state = buyer.execute("purchase.order", "read", [[po_id], ["state"]])[0]["state"]
        if state == "to approve":
            step = "approve"
            if anomaly.get("rubber_stamp"):
                # evasive S4: seconds, not hours — see _RUBBER_STAMP_SECONDS above.
                approve_ts = _clamp(confirm_ts + _dt.timedelta(
                    seconds=_draw_lognormal(_po_rng(po_key, "confirm_to_approve_rubber_stamp"),
                                             _RUBBER_STAMP_SECONDS)))
            else:
                approve_ts = _clamp(confirm_ts + _dt.timedelta(
                    hours=_draw_lognormal(_po_rng(po_key, "confirm_to_approve"), approve_spec)))
            manager.call_button("purchase.order", po_id, "button_approve")
            msg_ts.append(approve_ts)
        elif state != "purchase":
            raise RuntimeError(f"unexpected state after confirm: {state!r}")

        # ---- 5. date_approve ----
        step = "set_date_approve"
        buyer.write("purchase.order", po_id, {"date_approve": _odoo_dt(approve_ts)})

        # ---- 5b. cancellation ----
        # plan_po["cancel_after"] is set only on OLD, full_tail=False normal-background POs
        # (plan_dataset.py's cancellation pass) to "confirm" (below-threshold — button_confirm
        # alone already lands the PO in 'purchase', it never enters 'to approve') or "approve"
        # (above-threshold — button_approve above already ran). Both cases converge here, right
        # after the PO has reached its final pre-cancel state ('purchase') and date_approve is
        # set, which is the earliest point after either recipe's trigger action has actually
        # happened — so a single call site correctly implements both branches of the plan's
        # cancel_after value without duplicating the cancel call at two different points in the
        # confirm/approve branch above.
        #
        # Verified against a live Odoo DB (smoke run): both the 'purchase'
        # (below-threshold, "confirm") and post-approval 'purchase' (above-threshold, "approve")
        # pre-cancel states are legal transitions to 'cancel', cascading to cancel the
        # not-yet-validated picking(s) created on confirm - no receipt/bill/payment exists yet at
        # this point (step 6 has not run). One quirk found and handled: button_cancel's XML-RPC
        # response fails to marshal (a server-side None-return/allow_none=False mismatch in this
        # Odoo build - odoo_client.call_button's documented, narrowly-scoped exception), even
        # though the cancellation itself commits successfully. Rather than trust that pattern
        # blindly, the state is independently re-read below and asserted - if the call ever
        # doesn't reach the DB for some other reason, this catches it instead of silently
        # producing a PO that looks cancelled in the plan but isn't in Odoo.
        cancel_after = plan_po.get("cancel_after")
        if cancel_after:
            step = "cancel"
            cancel_ts = _clamp(approve_ts + _dt.timedelta(
                days=_draw_lognormal(_po_rng(po_key, "approve_to_cancel"),
                                      _LAT["approve_to_receipt_days"])))
            buyer.call_button("purchase.order", po_id, "button_cancel")
            msg_ts.append(cancel_ts)

            tsmap.record_po(po_id, create_date=order_date, write_date=cancel_ts)
            tsmap.record_message_sequence("purchase.order", po_id, msg_ts)

            po_read = buyer.execute("purchase.order", "read", [[po_id], ["name", "state"]])[0]
            if po_read["state"] != "cancel":
                raise RuntimeError(
                    f"[{po_key}] cancel_after={cancel_after!r} but state after button_cancel is "
                    f"{po_read['state']!r}, not 'cancel' - the RPC call did not take effect")
            po_number = po_read["name"]
            return {
                "po_key": po_key,
                "po_id": po_id,
                "po_number": po_number,
                "line_ids": line_id_by_product_idx,
                "picking_ids": [],      # cancellation cascades to cancel any created picking(s);
                "bill_ids": [],         # never validated, so no receipt/bill/payment steps run.
                "payment_ids": [],
            }

        tsmap.record_po(po_id, create_date=order_date, write_date=approve_ts)
        tsmap.record_message_sequence("purchase.order", po_id, msg_ts)

        # ---- 6. receive ----
        step = "validate_receipt"
        picking_ids = buyer.execute("purchase.order", "read",
                                     [[po_id], ["picking_ids"]])[0]["picking_ids"]
        recv_ts = _clamp(approve_ts + _dt.timedelta(
            days=_draw_lognormal(_po_rng(po_key, "approve_to_receipt"),
                                  _LAT["approve_to_receipt_days"])))
        for picking_id in picking_ids:
            moves = buyer.search_read("stock.move", [("picking_id", "=", picking_id)],
                                       ["id", "product_uom_qty"])
            for mv in moves:
                _set_move_done_qty(buyer, mv["id"], mv["product_uom_qty"])
            buyer.call_button("stock.picking", picking_id, "button_validate")
            tsmap.record_picking(picking_id, recv_ts)
            for mv in moves:
                tsmap.record_move(mv["id"], recv_ts)

        # ---- 7. bill + payment tail ----
        bill_ids, payment_ids = [], []
        if plan_po["full_tail"]:
            bill_date = _clamp(recv_ts + _dt.timedelta(
                days=_draw_lognormal(_po_rng(po_key, "receipt_to_bill"),
                                      _LAT["receipt_to_bill_days"])))

            if anomaly.get("two_bills") == "down_payment_then_final":
                # §6 hard negative: a LEGITIMATE two-bill PO (down payment + final invoice),
                # label stays null. Must sum to amount_total (not ~200% like a real duplicate)
                # or the ground truth is corrupted - see step "verify_two_bill_total" below.
                step = "create_invoice_down_payment"
                ratio = anomaly.get("down_payment_ratio", 0.3)
                before_ids = set(buyer.execute(
                    "purchase.order", "read", [[po_id], ["invoice_ids"]])[0]["invoice_ids"])
                down_bill_id = _new_invoice_id(buyer, po_id, before_ids, "down payment")

                step = "scale_down_payment_lines"
                inv_lines = buyer.search_read(
                    "account.move.line",
                    [("move_id", "=", down_bill_id), ("purchase_line_id", "!=", False)],
                    ["id", "quantity"])
                if not inv_lines:
                    raise RuntimeError(
                        f"down-payment draft bill {down_bill_id} has no purchase-linked "
                        "lines to scale")
                for il in inv_lines:
                    scaled_qty = round(il["quantity"] * ratio, 2)
                    if scaled_qty <= 0:
                        scaled_qty = 0.01
                    buyer.write("account.move.line", il["id"], {"quantity": scaled_qty})

                step = "post_bill"
                down_payment_id = _post_and_pay_bill(buyer, down_bill_id, bill_date, tsmap, po_key)
                bill_ids.append(down_bill_id)
                payment_ids.append(down_payment_id)

                step = "create_invoice_final"
                # A distributed gap (not a constant), so the S1 down-payment/final hard negative
                # cannot be identified by arithmetic - see
                # process_latency.down_payment_to_final_days in params.json.
                final_date = _clamp(bill_date + _dt.timedelta(
                    days=_draw_lognormal(_po_rng(po_key, "down_payment_to_final"),
                                          _LAT["down_payment_to_final_days"])))
                before_ids2 = set(buyer.execute(
                    "purchase.order", "read", [[po_id], ["invoice_ids"]])[0]["invoice_ids"])
                final_bill_id = _new_invoice_id(buyer, po_id, before_ids2, "final")

                step = "post_bill"
                final_payment_id = _post_and_pay_bill(buyer, final_bill_id, final_date, tsmap, po_key)
                bill_ids.append(final_bill_id)
                payment_ids.append(final_payment_id)

                step = "verify_two_bill_total"
                totals = buyer.execute(
                    "account.move", "read",
                    [[down_bill_id, final_bill_id], ["amount_total"]])
                bill_sum = round(sum(t["amount_total"] for t in totals), 2)
                if abs(bill_sum - po_data["amount_total"]) > 0.02:
                    raise RuntimeError(
                        f"down-payment+final bills sum to {bill_sum} but PO amount_total="
                        f"{po_data['amount_total']} (diff "
                        f"{abs(bill_sum - po_data['amount_total']):.4f}) - a hard negative "
                        "must sum to exactly the PO total, not duplicate it")
            else:
                step = "create_invoice"
                buyer.call_button("purchase.order", po_id, "action_create_invoice")
                inv_ids = buyer.execute("purchase.order", "read",
                                         [[po_id], ["invoice_ids"]])[0]["invoice_ids"]
                for bill_id in inv_ids:
                    step = "post_bill"
                    payment_id = _post_and_pay_bill(buyer, bill_id, bill_date, tsmap, po_key)
                    bill_ids.append(bill_id)
                    payment_ids.append(payment_id)

        po_number = buyer.execute("purchase.order", "read", [[po_id], ["name"]])[0]["name"]
        return {
            "po_key": po_key,
            "po_id": po_id,
            "po_number": po_number,
            "line_ids": line_id_by_product_idx,
            "picking_ids": picking_ids,
            "bill_ids": bill_ids,
            "payment_ids": payment_ids,
        }
    except BuildNormalError:
        raise
    except Exception as e:
        raise BuildNormalError(po_key, step, e) from e


# ---------- entry point ----------
def apply_normal(cluster, plan, master, tsmap):
    """Push every planned PO into Odoo. Returns {"pos": [...], "by_key": {po_key: {...}}} and
    also writes it to gen_out/applied.json so a later stage (inject_anomalies) can resume."""
    global _PERIOD_END
    end = plan["params_echo"].get("generation_end")
    _PERIOD_END = _dt.date.fromisoformat(end) if end else None

    vendor_idx_map, product_idx_map = _index_master(master)

    # ---- apply the plan's vendor/bank create_date realism ----
    # build_master.py stamps EVERY vendor + its onboarding bank account with the SAME constant
    # "2025-01-01 00:00:00". plan_dataset.py computes a plausible, varied create_date per
    # vendor (including the S4-evasive one-off vendors), but build_master.py's own return value
    # (`master`, this function's argument) does not carry that field forward — only
    # plan["master"]["vendors"] does. TimestampMap cells are last-write-wins (see its
    # docstring), so calling record_partner/record_partner_bank again here, with the plan's
    # dates, safely overrides build_master's uniform stamp before tsmap.flush() runs. This does
    # NOT touch record_messages_for for these records (that log is append-only and build_master
    # already recorded the vendor's creation-chatter timestamp) — only the create_date/write_date
    # CELLS, which is what vendor_audit.sql's `bank_account_additions[].created_at` actually
    # reads.
    bank_id_by_idx = {v.get("idx", i): v.get("bank_id") for i, v in enumerate(master["vendors"])}
    for plan_v in plan["master"]["vendors"]:
        cd = plan_v.get("create_date")
        if not cd:
            continue
        vid = _lookup(vendor_idx_map, plan_v["idx"])
        tsmap.record_partner(vid, create_date=cd, write_date=cd)
        bank_id = bank_id_by_idx.get(plan_v["idx"])
        if bank_id:
            tsmap.record_partner_bank(bank_id, create_date=cd, write_date=cd)

    managers = plan["params_echo"]["managers"]
    manager_sessions = [cluster.as_(m) for m in managers]
    manager_session_by_name = dict(zip(managers, manager_sessions))
    managers_set = set(managers)

    reader = manager_sessions[0]
    all_pids = list(product_idx_map.values())
    prod_rows = reader.search_read("product.product", [("id", "in", all_pids)],
                                    ["name", "uom_id"])
    product_info = {r["id"]: r for r in prod_rows}

    pos_plan = plan["pos"]
    n = len(pos_plan)

    # ---- batch-create per buyer_session (step 1) ----
    grouped = defaultdict(list)
    for plan_po in pos_plan:
        grouped[plan_po["buyer_session"]].append(plan_po)

    po_id_by_key = {}
    for buyer_name, group in grouped.items():
        sess = cluster.as_(buyer_name)
        vals_list = []
        for plan_po in group:
            vendor_id = _lookup(vendor_idx_map, plan_po["vendor_idx"])
            h, m = _sample_order_base_time(plan_po["po_key"])
            order_date = _combine(plan_po["date_order"], h, m)
            vals_list.append({
                "partner_id": vendor_id,
                "date_order": _odoo_dt(order_date),
                "order_line": _order_lines(plan_po, product_info, product_idx_map,
                                            _odoo_dt(order_date)),
            })
        try:
            ids = sess.create_many("purchase.order", vals_list)
        except Exception as e:
            keys = [p["po_key"] for p in group]
            raise BuildNormalError(f"<batch:{buyer_name}, {len(keys)} POs: {keys[:5]}...>",
                                    "create_po_batch", e) from e
        for plan_po, po_id in zip(group, ids):
            po_id_by_key[plan_po["po_key"]] = po_id
            _log_created("po", po_key=plan_po["po_key"], po_id=po_id,
                          buyer_session=buyer_name)

    # ---- per-PO steps (2-8), in the plan's own order for readable progress output ----
    results = []
    by_key = {}
    t0 = time.time()
    for i, plan_po in enumerate(pos_plan, start=1):
        # The approver must differ from the PO's author whenever the
        # author is itself a manager session - otherwise a manager-authored above-threshold
        # PO self-approves in Odoo and silently becomes an UNLABELLED scenario-4 positive
        # (ground-truth corruption). The mandatory exception is scenario-4 POSITIVES
        # (anomaly_spec.md's canonical S4 label "approval_bypass"): those must keep
        # self-approving - that self-approval IS their labelled anchor. The approver is drawn
        # with the same deterministic per-PO RNG used for latency sampling, so re-sorting
        # plan.json doesn't change any PO's approver.
        author = plan_po["buyer_session"]
        anomaly = plan_po.get("anomaly") or {}
        # scenario == "approval_bypass" covers BOTH the canonical anchor (self_approval True —
        # the author self-approves) and the evasive "rubber-stamp" case (self_approval False —
        # a real, different approver), so branch on the anomaly's own self_approval flag.
        is_s4_canonical_anchor = (plan_po.get("scenario") == "approval_bypass"
                                  and anomaly.get("self_approval") is True)
        is_s4_evasive = (plan_po.get("scenario") == "approval_bypass"
                         and anomaly.get("self_approval") is False)
        if is_s4_canonical_anchor:
            # the author MUST be a manager and MUST approve their own PO — that self-approval
            # IS the canonical label's anchor (anomaly_spec.md).
            manager = manager_session_by_name.get(author)
            if manager is None:
                raise BuildNormalError(
                    plan_po["po_key"], "select_approver",
                    f"canonical S4 author {author!r} is not a configured manager session "
                    f"(managers={managers}) - self-approval cannot happen")
        elif is_s4_evasive:
            # buyer != approver is mandatory here (self_approval must come out False) — route to
            # a real manager, deterministically per-PO, never positionally.
            rng = _po_rng(plan_po["po_key"], "approver")
            manager = manager_session_by_name[managers[int(rng.integers(0, len(managers)))]]
        elif author in managers_set:
            eligible = [m for m in managers if m != author]
            if not eligible:
                raise BuildNormalError(
                    plan_po["po_key"], "select_approver",
                    f"manager-authored PO but no OTHER manager session configured "
                    f"(managers={managers}) - cannot avoid self-approval")
            rng = _po_rng(plan_po["po_key"], "approver")
            manager = manager_session_by_name[eligible[int(rng.integers(0, len(eligible)))]]
        else:
            rng = _po_rng(plan_po["po_key"], "approver")
            manager = manager_session_by_name[managers[int(rng.integers(0, len(managers)))]]
        res = _apply_one(po_id_by_key[plan_po["po_key"]], plan_po, product_info,
                          product_idx_map, cluster, manager, tsmap)
        results.append(res)
        by_key[res["po_key"]] = res
        if i % 50 == 0 or i == n:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(f"  build_normal: {i}/{n} POs done ({rate:.2f}/s, {elapsed:.1f}s elapsed)")

    out = {"pos": results, "by_key": by_key}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "applied.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out
