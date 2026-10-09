#!/usr/bin/env python
"""labels.py - dataset generation, stage 4: the three ground-truth sidecars
(anomaly_spec.md §7). Pure bookkeeping - no Odoo, no sampling; every label already
exists on the plan (plan_dataset.py decided it), this module just re-keys it by the REAL Odoo
id/number so it lines up with what the extraction pipeline (po_context.sql / vendor_audit.sql)
actually emits, and writes it out. Never fed into the LLM prompt - these sidecars populate
`ground_truth.injected_anomaly` at scoring time only.

  labels_po.json     - keyed by po_number ("P00001"); scenarios 1/4/5 + null for everything
                        else, INCLUDING scenario-2 sibling POs (scored at the vendor grain only).
  labels_vendor.json - keyed by real vendor id; "po_splitting" or null. One entry per vendor.
  labels_audit.json  - keyed by real vendor id; "vendor_master_mod" or null. One entry per
                        vendor with >= 1 tracked change (that set is exactly plan["audit_labels"]'s
                        keys - see plan_dataset.py's noise_vendors | tampered union).
  labels_summary.json - counts per label per grain, for a quick split/scoring sanity check.

  labels_variant.json (anomaly_spec.md §5/§7) - ADDITIVE sidecar, not read by
                        `_pipeline/score.py`; it changes nothing about labels_po/vendor/audit's
                        existing keys or value types. Mirrors their key shape exactly (po_number
                        for the "po" section, real vendor id for "vendor"/"audit") so a caller
                        can zip e.g. labels_variant["po"][po_number] against
                        labels_po.json[po_number] without re-deriving anything from the plan.
                        Value is "canonical" | "evasive" | null (null for every non-positive
                        unit, and for scenario 2, which has no evasive variant — anomaly_spec.md
                        §2's S2 note — so its splitter vendors are recorded "canonical").
"""
import json
import pathlib


def _by_idx(items, idx):
    for it in items:
        if it.get("idx") == idx:
            return it
    return items[idx]


def _vendor(master, idx):
    """Always the vendor RECORD DICT ({"idx","id","bank_id","name"}), never a bare id — every
    call site reads vendor["id"] off it. (master["vendor_by_idx"] maps idx -> id only.)"""
    return _by_idx(master["vendors"], int(idx))


def _counts(values):
    c = {}
    for v in values:
        key = v if v is not None else "null"
        c[key] = c.get(key, 0) + 1
    return c


def write_labels(plan, master, applied, injected, out_dir):
    """applied/injected are accepted per the required signature but not needed here: every
    label already lives on the plan (plan['pos'][*]['scenario'], plan['vendor_labels'],
    plan['audit_labels']) - `applied` supplies only the REAL po_number/vendor id each plan
    entity maps to. Returns {"labels_po": path, "labels_vendor": path, "labels_audit": path,
    "labels_summary": path} (all strings)."""
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_by_key = {p["po_key"]: p for p in plan["pos"]}

    # ---- labels_po.json : keyed by REAL po_number, from `applied` (not the plan's po_key) ----
    labels_po = {}
    variant_po = {}     # parallel "canonical"/"evasive"/null, same keys — see labels_variant
    for entry in applied["pos"]:
        po_key = entry.get("po_key")
        po_number = entry.get("po_number")
        plan_po = plan_by_key.get(po_key)
        if plan_po is None:
            raise KeyError(
                f"applied po_key {po_key!r} not found in plan['pos'] - labels.py must run "
                "against the same plan build_normal.py/inject_anomalies.py applied"
            )
        # plan_po['scenario'] is already S1/S4/S5 for positives and None for everything else
        # (normal, hard_negative, s2_sibling) - exactly the labels_po contract.
        labels_po[po_number] = plan_po.get("scenario")
        variant_po[po_number] = plan_po.get("variant_class")

    # ---- labels_vendor.json : scenario 2, keyed by REAL vendor id, one entry per vendor ----
    labels_vendor = {}
    variant_vendor = {}
    for idx, label in plan["vendor_labels"].items():
        vendor = _vendor(master, idx)
        labels_vendor[str(vendor["id"])] = label
        # S2 has no evasive variant (anomaly_spec.md §2's S2 note) — every splitter is
        # "canonical" by definition; non-splitters stay null.
        variant_vendor[str(vendor["id"])] = "canonical" if label else None

    # ---- labels_audit.json : scenario 3, keyed by REAL vendor id; denominator = every vendor
    # with >= 1 tracked change, which is exactly plan['audit_labels']'s key set (built from
    # noise_vendors | tampered in plan_dataset.py) ----
    labels_audit = {}
    variant_audit = {}
    audit_variant_labels = plan.get("audit_variant_labels", {})
    for idx, label in plan["audit_labels"].items():
        vendor = _vendor(master, idx)
        labels_audit[str(vendor["id"])] = label
        variant_audit[str(vendor["id"])] = audit_variant_labels.get(idx)

    summary = {
        "labels_po": {"total": len(labels_po), "by_label": _counts(labels_po.values())},
        "labels_vendor": {"total": len(labels_vendor), "by_label": _counts(labels_vendor.values())},
        "labels_audit": {"total": len(labels_audit), "by_label": _counts(labels_audit.values())},
    }

    # anomaly_spec.md §5/§7: canonical/evasive split, additive — never read by the
    # frozen scoring harness, mirrors labels_po/vendor/audit's key shape exactly.
    labels_variant = {"po": variant_po, "vendor": variant_vendor, "audit": variant_audit}

    files = {
        "labels_po": labels_po,
        "labels_vendor": labels_vendor,
        "labels_audit": labels_audit,
        "labels_summary": summary,
        "labels_variant": labels_variant,
    }
    paths = {}
    for name, data in files.items():
        p = out_dir / f"{name}.json"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                     encoding="utf-8")
        paths[name] = str(p)
    return paths
