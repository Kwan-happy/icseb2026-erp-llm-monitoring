#!/usr/bin/env python
"""backfill_bill_refs.py - post-generation step: give every vendor bill a vendor-invoice
reference (account.move.ref).

Bills are classified from gen_out/plan.json, applied.json and injected.json; the live DB decides
which bills exist (`account.move`, move_type in in_invoice / in_receipt / in_refund):
  1. Evasive cross-PO duplicate pairs (anomaly.variant == 'cross_po') already share one
     reference per pair and are left untouched.
  2. Canonical duplicate_payment positives: both bills of the same order get ONE shared new
     reference (the same invoice billed twice).
  3. duplicate_payment hard negatives (down payment then final invoice): the two bills get TWO
     different new references (two genuine invoices).
  4. Every other bill: one distinct new reference.

A bill is owned by a po_key via applied['by_key'][po_key]['bill_ids'] (for the down-payment
hard negative: down payment first, final second) and injected['pos'][po_key]['duplicate_bill_id'].
A live bill not reachable through either map is reported, never guessed at.

References use the format `VINV-nnnn`. Units needing a new reference are sorted by their lowest
bill id and assigned the next free number, skipping numbers already in use. A bill that already
has a reference is never rewritten, so the step is idempotent. Dry run is the default; `--apply`
writes. `--db` defaults to odoo.dbname in gen_config.json.

    python _pipeline/generate/backfill_bill_refs.py                # dry run
    python _pipeline/generate/backfill_bill_refs.py --apply        # write
"""
import argparse
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import odoo_client  # noqa: E402

GEN_OUT = HERE / "gen_out"

DUPLICATE_PAYMENT = "duplicate_payment"
CANONICAL_VARIANTS = {"identical", "rounding_delta", "different_date", "bill_only"}

BILL_MOVE_TYPES = ["in_invoice", "in_receipt", "in_refund"]

REF_RE = re.compile(r"^VINV-(\d+)$")


# ---------------------------------------------------------------- loading

def _load(name):
    return json.loads((GEN_OUT / name).read_text(encoding="utf-8"))


def build_bill_owner_map(applied, injected):
    """bill_id -> po_key, from the two places a bill can be recorded as belonging to an order."""
    owner = {}
    for po_key, info in applied["by_key"].items():
        for bid in (info.get("bill_ids") or []):
            owner[bid] = po_key
    for po_key, rec in (injected.get("pos") or {}).items():
        bid = rec.get("duplicate_bill_id")
        if bid:
            owner[bid] = po_key
    return owner


def po_number_of(applied, po_key):
    info = applied["by_key"].get(po_key)
    return info["po_number"] if info else None


# ---------------------------------------------------------------- classification

def classify(plan_pos_by_key, applied, bill_owner, bill_id, ref):
    """Return (group, po_key_or_None, po_number_or_None, note_or_None).

    group is one of: 'evasive_existing' (already has ref - never touched),
    'canonical_shared', 'hn_distinct', 'other_distinct', 'unclassifiable'.
    """
    if ref:
        return "evasive_existing", bill_owner.get(bill_id), None, None

    po_key = bill_owner.get(bill_id)
    if po_key is None:
        return "unclassifiable", None, None, "bill id not found in applied.bill_ids or injected.duplicate_bill_id"

    po = plan_pos_by_key.get(po_key)
    if po is None:
        return "unclassifiable", po_key, None, f"po_key {po_key!r} not found in plan.json"

    po_number = po_number_of(applied, po_key)
    anomaly = po.get("anomaly") or {}

    if anomaly.get("type") == DUPLICATE_PAYMENT and anomaly.get("variant") == "cross_po":
        # Should have been caught by `if ref` above (both sides of an evasive pair already carry
        # shared_ref). Reaching here means the ref is unexpectedly missing - flag it loudly
        # rather than silently assigning a distinct ref, which would break the intended shared
        # signal for this pair.
        return ("unclassifiable", po_key, po_number,
                "cross_po duplicate_payment PO but this bill has NO ref - inconsistent with "
                "generator invariant, needs investigation, not auto-assignment")

    if anomaly.get("type") == DUPLICATE_PAYMENT and anomaly.get("variant") in CANONICAL_VARIANTS:
        return "canonical_shared", po_key, po_number, None

    if anomaly.get("type") == DUPLICATE_PAYMENT:
        return ("unclassifiable", po_key, po_number,
                f"duplicate_payment PO with unrecognised variant {anomaly.get('variant')!r}")

    if po.get("role") == "hard_negative" and anomaly.get("two_bills") == "down_payment_then_final":
        return "hn_distinct", po_key, po_number, None

    return "other_distinct", po_key, po_number, None


# ---------------------------------------------------------------- ref assignment

def assign_refs(units, used_numbers):
    """units: list of (sort_key, bill_ids) - one unit per NEW reference to hand out (a single
    bill, or a same-order pair that shares one). Returns {bill_id: new_ref}, sorted deterministically
    by sort_key (the lowest bill id in the unit) so a re-run reproduces the same assignment."""
    assignment = {}
    used = set(used_numbers)
    counter = 0
    for _, bill_ids in sorted(units, key=lambda u: u[0]):
        while counter in used:
            counter += 1
        used.add(counter)
        new_ref = f"VINV-{counter:04d}"
        for bid in bill_ids:
            assignment[bid] = new_ref
    return assignment


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Backfill account.move.ref on every vendor bill so 'has a ref' stops being "
                    "a perfect label for the evasive cross-PO duplicate_payment anomaly")
    ap.add_argument("--db", default=None,
                    help="target database (default: odoo.dbname in gen_config.json)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this the run only reports what it would do.")
    args = ap.parse_args()

    if args.db is None:
        args.db = json.loads((HERE / "gen_config.json").read_text(encoding="utf-8"))["odoo"]["dbname"]

    cfg = json.loads((HERE / "gen_config.json").read_text(encoding="utf-8"))
    odoo_cfg = dict(cfg["odoo"])
    odoo_cfg["dbname"] = args.db
    cluster = odoo_client.OdooCluster.from_gen_config(odoo_cfg)

    plan = _load("plan.json")
    applied = _load("applied.json")
    injected = _load("injected.json")

    plan_pos_by_key = {po["po_key"]: po for po in plan["pos"]}
    bill_owner = build_bill_owner_map(applied, injected)

    # any session can read; pick a manager if there is one, else whatever is configured.
    names = cluster.names()
    reader = cluster.as_(next((n for n in names if n.startswith("manager")), names[0]))

    print(f"db={args.db}  mode={'APPLY' if args.apply else 'DRY RUN'}")
    bills = reader.search_read(
        "account.move",
        [("move_type", "in", BILL_MOVE_TYPES)],
        ["id", "ref", "invoice_origin", "name", "state"],
        order="id asc",
    )
    print(f"total vendor bills in DB: {len(bills)}")

    rows = []          # (bill_id, po_number, group, note)
    existing_used_numbers = set()
    for b in bills:
        group, po_key, po_number, note = classify(plan_pos_by_key, applied, bill_owner, b["id"], b["ref"])
        po_number = po_number or b.get("invoice_origin")
        rows.append({
            "bill_id": b["id"], "ref": b["ref"], "po_key": po_key, "po_number": po_number,
            "group": group, "note": note,
        })
        if group == "evasive_existing" and b["ref"]:
            m = REF_RE.match(b["ref"])
            if m:
                existing_used_numbers.add(int(m.group(1)))

    by_group = {}
    for r in rows:
        by_group.setdefault(r["group"], []).append(r)

    # ---- resume safety ----
    # An earlier run can be interrupted (e.g. a slow XML-RPC batch killed by a timeout) after
    # writing one bill of a canonical pair but not its sibling. On re-run the written bill is
    # classified `evasive_existing` (it now has a ref) and drops out of `canonical_shared`,
    # leaving its sibling alone in a group of size 1 - which `assign_refs` would then hand a
    # BRAND-NEW ref, breaking the one invariant this repair exists to create (both bills of a
    # canonical order must share ONE reference). Detect this first: if a bill's po_key already
    # has a ref on ANY of its bills (written by an earlier interrupted run), the remaining null
    # bill(s) of that same po_key reuse that exact ref instead of going through assign_refs.
    existing_ref_by_pokey = {}
    for r in rows:
        if r["ref"] and r["po_key"]:
            existing_ref_by_pokey.setdefault(r["po_key"], r["ref"])

    resume_fix = {}  # bill_id -> ref, reused from an already-written sibling
    remaining_canonical = []
    for r in by_group.get("canonical_shared", []):
        prior_ref = existing_ref_by_pokey.get(r["po_key"])
        if prior_ref:
            resume_fix[r["bill_id"]] = prior_ref
        else:
            remaining_canonical.append(r)

    # ---- build assignment units ----
    units = []  # (sort_key, [bill_ids])

    canonical_by_pokey = {}
    for r in remaining_canonical:
        canonical_by_pokey.setdefault(r["po_key"], []).append(r["bill_id"])
    for po_key, bill_ids in canonical_by_pokey.items():
        units.append((min(bill_ids), sorted(bill_ids)))

    for r in by_group.get("hn_distinct", []):
        units.append((r["bill_id"], [r["bill_id"]]))

    for r in by_group.get("other_distinct", []):
        units.append((r["bill_id"], [r["bill_id"]]))

    assignment = assign_refs(units, existing_used_numbers)  # bill_id -> new ref
    if resume_fix:
        print(f"\nresuming {len(resume_fix)} bill(s) whose canonical sibling already has a ref "
              f"from an earlier interrupted run - reusing that ref, not assigning a new one")
    assignment.update(resume_fix)

    row_by_id = {r["bill_id"]: r for r in rows}
    for bid, new_ref in assignment.items():
        row_by_id[bid]["new_ref"] = new_ref

    # ---------------------------------------------------------------- report
    n_already = len(by_group.get("evasive_existing", []))
    n_canonical = len(by_group.get("canonical_shared", []))
    n_hn = len(by_group.get("hn_distinct", []))
    n_other = len(by_group.get("other_distinct", []))
    n_unclassifiable = len(by_group.get("unclassifiable", []))
    n_assigned = len(assignment)

    print("\n--- summary ---")
    print(f"{'group':<20} {'bills':>7}")
    print(f"{'evasive_existing':<20} {n_already:>7}   (already has ref - untouched; expect 12)")
    print(f"{'canonical_shared':<20} {n_canonical:>7}   "
          f"({len(canonical_by_pokey)} orders x 2 bills sharing 1 new ref each)")
    print(f"{'hn_distinct':<20} {n_hn:>7}   (down-payment + final, 2 different new refs each)")
    print(f"{'other_distinct':<20} {n_other:>7}   (one new ref per bill)")
    print(f"{'unclassifiable':<20} {n_unclassifiable:>7}")
    print(f"{'TOTAL':<20} {len(bills):>7}")
    print(f"\nnew refs to assign: {n_assigned}  "
          f"(distinct new ref values: {len(set(assignment.values()))})")

    if n_already != 12:
        print(f"\nWARNING: expected exactly 12 bills with a pre-existing ref, found {n_already}")

    print("\n--- sample of planned assignments (up to 15) ---")
    sample = [r for r in rows if "new_ref" in r][:15]
    print(f"{'bill_id':>8}  {'po_number':<10} {'group':<18} {'new_ref':<10}")
    for r in sample:
        print(f"{r['bill_id']:>8}  {str(r['po_number']):<10} {r['group']:<18} {r['new_ref']:<10}")

    unclass = by_group.get("unclassifiable", [])
    if unclass:
        print(f"\n--- UNCLASSIFIABLE bills ({len(unclass)}) ---")
        for r in unclass:
            print(f"  bill_id={r['bill_id']} ref={r['ref']!r} po_key={r['po_key']} "
                  f"po_number={r['po_number']} - {r['note']}")
    else:
        print("\nno unclassifiable bills.")

    if not args.apply:
        print("\nDRY RUN - nothing was written. Re-run with --apply to perform the backfill.")
        return

    print("\napplying...")
    n_written = 0
    for r in rows:
        if "new_ref" not in r:
            continue
        po = plan_pos_by_key.get(r["po_key"])
        session_name = po["buyer_session"] if po else None
        session = cluster.as_(session_name) if session_name else reader
        session.write("account.move", r["bill_id"], {"ref": r["new_ref"]})
        n_written += 1
    print(f"wrote ref on {n_written} bill(s).")


if __name__ == "__main__":
    main()
