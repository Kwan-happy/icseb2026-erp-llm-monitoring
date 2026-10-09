#!/usr/bin/env python
"""Materialise the pre-registered dev/eval split of the evaluation dataset.

Implements `anomaly_spec.md` §7 (Dev / eval split) EXACTLY — this file does not improve on
the protocol, it only executes it:
    - 30% dev / 70% eval, `random_seed` = 123.
    - Stratified BY VENDOR, never by PO (so Scenario-2 sibling POs never cross the boundary).
    - Stratified further by vendor class: splitter / tampered / normal.
    - Rounding is FLOOR, not round(): dev = floor(0.30 * n) per class (fixed).

It MUST run before any LLM prompt is tuned. `_pipeline/score.py`'s `load_split()`
(score.py:138-142) is the consumer; the `split.json` shape here matches that contract exactly.

Two outputs:
    1. `_pipeline/out/split.json`     -- {"dev": [vendor_id, ...], "eval": [...]}, vendor ids as
                                          sorted strings.
    2. `_pipeline/out/dev/{po,vendor,vendor_audit}/`
                                       -- copies (not moves) of the dev-side extract files.
                                          Only dev is materialised; eval is governed by
                                          split.json alone at scoring time.

Run from the repo root:
    python _pipeline/make_split.py --dry-run
    python _pipeline/make_split.py

No external dependencies: stdlib only (`argparse`, `collections`, `glob`, `json`, `pathlib`,
`random`, `shutil`). `random.Random` (stdlib) is used rather than numpy for the shuffle --
stdlib has no version-drift risk and the protocol does
not need numpy's RNG features.
"""
import argparse
import collections
import glob
import json
import pathlib
import random
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]          # repo root
GEN = ROOT / "_pipeline" / "generate" / "gen_out"            # label sidecars (read-only)
OUT = ROOT / "_pipeline" / "out"                              # extracted JSON (read-only) + split outputs

RANDOM_SEED = 123
DEV_FRACTION = 0.30

# Fixed processing order for the shuffle so a re-run is byte-identical regardless of any
# incidental change elsewhere in the script (dict ordering, etc.) -- the vendor id list within
# each class is sorted numerically before it is ever handed to the RNG, and the three classes
# are always shuffled in this same order against one `random.Random(RANDOM_SEED)` instance.
CLASS_ORDER = ("splitter", "tampered", "normal")

# Scenario -> grain, mirroring score.py's SCENARIO_GRAIN (anomaly_spec.md §5 / §7).
SCENARIO_GRAIN = {
    "duplicate_payment": "po",
    "approval_bypass": "po",
    "abnormal_amount": "po",
    "po_splitting": "vendor",
    "vendor_master_mod": "audit",
}


# ---------- inputs ----------
def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_labels():
    """The three ground-truth sidecars, keyed by grain, keys coerced to str (matches score.py)."""
    labels = {}
    for grain, fname in (("po", "labels_po.json"), ("vendor", "labels_vendor.json"),
                         ("audit", "labels_audit.json")):
        raw = load_json(GEN / fname)
        labels[grain] = {str(k): v for k, v in raw.items()}
    return labels


def load_variants():
    """labels_variant.json: {"po": {...}, "vendor": {...}, "audit": {...}}, values
    'canonical' / 'evasive' / null. Grain keys line up with `labels` above."""
    raw = load_json(GEN / "labels_variant.json")
    return {grain: {str(k): v for k, v in d.items()} for grain, d in raw.items()}


def po_vendor_map():
    """PO number -> vendor id (str), read from the extracted PO context JSON."""
    mapping = {}
    for f in sorted(glob.glob(str(OUT / "po" / "*.json"))):
        d = load_json(pathlib.Path(f))
        po_number = d["po_identity"]["po_number"]
        vendor_id = str(d["po_identity"]["vendor"]["id"])
        mapping[po_number] = vendor_id
    return mapping


# ---------- vendor classes ----------
def derive_vendor_classes(labels):
    """splitter = non-null in labels_vendor.json; tampered = non-null in labels_audit.json;
    normal = neither. Disjointness of splitter/tampered is checked here
    (and fails loudly) so a different dataset cannot silently violate the stratification."""
    vendor_ids = set(labels["vendor"])
    audit_ids = set(labels["audit"])
    if vendor_ids != audit_ids:
        sys.exit(
            f"labels_vendor.json and labels_audit.json disagree on the vendor id set:\n"
            f"  only in labels_vendor: {sorted(vendor_ids - audit_ids, key=int)}\n"
            f"  only in labels_audit:  {sorted(audit_ids - vendor_ids, key=int)}"
        )

    splitter = {vid for vid, v in labels["vendor"].items() if v is not None}
    tampered = {vid for vid, v in labels["audit"].items() if v is not None}
    overlap = splitter & tampered
    if overlap:
        sys.exit(
            f"vendor class assumption violated: {sorted(overlap, key=int)} are labelled both "
            f"'splitter' (labels_vendor.json) and 'tampered' (labels_audit.json). The spec "
            f"requires these classes to be disjoint (see anomaly_spec.md §7)."
        )
    normal = vendor_ids - splitter - tampered
    return {"splitter": splitter, "tampered": tampered, "normal": normal}


def check_po_vendor_ids(pv_map, all_vendor_ids):
    """Fail loudly if any out/po/*.json references a vendor id absent from the label sidecars."""
    unknown = {(po, vid) for po, vid in pv_map.items() if vid not in all_vendor_ids}
    if unknown:
        sample = sorted(unknown)[:10]
        sys.exit(
            f"{len(unknown)} PO(s) reference a vendor id not present in labels_vendor.json / "
            f"labels_audit.json (showing up to 10): {sample}"
        )


# ---------- the split itself ----------
def build_split(classes):
    """Returns (dev, eval, per_class) where dev/eval are sets of vendor id strings and
    per_class maps class name -> (dev_ids, eval_ids) for the summary table.

    One `random.Random(RANDOM_SEED)` instance is shuffled sequentially over CLASS_ORDER; each
    class's vendor id list is sorted numerically first so the outcome cannot depend on any
    incidental ordering of the input JSON. floor(), never round(), decides the per-class dev
    count -- the author's locked rounding rule."""
    rng = random.Random(RANDOM_SEED)
    dev, eval_ = set(), set()
    per_class = {}
    for cls in CLASS_ORDER:
        ids = sorted(classes[cls], key=int)
        rng.shuffle(ids)
        n_dev = int(len(ids) * DEV_FRACTION)          # floor for non-negative floats
        dev_ids, eval_ids = ids[:n_dev], ids[n_dev:]
        dev.update(dev_ids)
        eval_.update(eval_ids)
        per_class[cls] = (dev_ids, eval_ids)
    return dev, eval_, per_class


def verify_split(dev, eval_, all_vendor_ids):
    if dev & eval_:
        sys.exit(f"dev and eval overlap: {sorted(dev & eval_, key=int)}")
    covered = dev | eval_
    if covered != all_vendor_ids:
        missing = all_vendor_ids - covered
        extra = covered - all_vendor_ids
        sys.exit(
            f"dev/eval does not cover exactly the vendor universe.\n"
            f"  missing: {sorted(missing, key=int)}\n  extra: {sorted(extra, key=int)}"
        )


# ---------- summary ----------
def scenario_positive_units(scenario, labels):
    grain = SCENARIO_GRAIN[scenario]
    return grain, {u for u, v in labels[grain].items() if v == scenario}


def side_of(unit_id, grain, dev, eval_, pv_map):
    """Which split side a unit belongs to. PO units are keyed by their vendor's side;
    vendor/audit units are vendor ids and use their own side directly."""
    vid = pv_map[unit_id] if grain == "po" else unit_id
    if vid in dev:
        return "dev"
    if vid in eval_:
        return "eval"
    return None    # should not happen once verify_split has passed


def summarize(labels, variants, classes, dev, eval_, per_class, pv_map):
    lines = []
    A = lines.append

    A("=== Vendor classes (dev / eval) ===")
    A(f"{'class':<10}{'dev':>6}{'eval':>6}{'total':>7}")
    total_dev = total_eval = 0
    for cls in CLASS_ORDER:
        dev_ids, eval_ids = per_class[cls]
        A(f"{cls:<10}{len(dev_ids):>6}{len(eval_ids):>6}{len(dev_ids) + len(eval_ids):>7}")
        total_dev += len(dev_ids)
        total_eval += len(eval_ids)
    A(f"{'TOTAL':<10}{total_dev:>6}{total_eval:>6}{total_dev + total_eval:>7}")
    A("")

    A("=== Units per grain (dev / eval) ===")
    A(f"{'grain':<10}{'dev':>6}{'eval':>6}{'total':>7}")
    po_dev = sum(1 for po, vid in pv_map.items() if vid in dev)
    po_eval = sum(1 for po, vid in pv_map.items() if vid in eval_)
    A(f"{'po':<10}{po_dev:>6}{po_eval:>6}{po_dev + po_eval:>7}")
    A(f"{'vendor':<10}{total_dev:>6}{total_eval:>6}{total_dev + total_eval:>7}  "
      f"(= all 50 vendors; vendor grain is the vendor id itself)")
    A(f"{'audit':<10}{total_dev:>6}{total_eval:>6}{total_dev + total_eval:>7}  "
      f"(= all 50 vendor-audit trails, same reasoning)")
    A("")

    A("=== Positive counts per scenario, by variant (dev / eval) ===")
    A(f"{'scenario':<20}{'variant':<12}{'dev':>6}{'eval':>6}{'total':>7}")
    for scenario in SCENARIO_GRAIN:
        grain, units = scenario_positive_units(scenario, labels)
        var_map = variants.get(grain, {})
        counts = collections.defaultdict(lambda: [0, 0])   # variant -> [dev, eval]
        for u in units:
            s = side_of(u, grain, dev, eval_, pv_map)
            if s is None:
                continue
            variant = var_map.get(u) or "(no variant recorded)"
            counts[variant][0 if s == "dev" else 1] += 1
        if not counts:
            A(f"{scenario:<20}{'--':<12}{0:>6}{0:>6}{0:>7}")
            continue
        for variant, (d, e) in sorted(counts.items()):
            A(f"{scenario:<20}{variant:<12}{d:>6}{e:>6}{d + e:>7}")
    A("")

    # Scenario-2 sibling POs: an approximation, per the task brief -- a PO belongs to a
    # splitter vendor but was not itself picked as the labelled positive PO. This is derivable
    # from labels_po.json + labels_vendor.json + the vendor id on each PO alone, so it is
    # reported; anything needing plan.json/applied.json (true "role" metadata) is out of scope
    # for this script and is skipped below instead of guessed.
    splitter_ids = classes["splitter"]
    sibling_dev = sibling_eval = 0
    for po, vid in pv_map.items():
        if vid not in splitter_ids:
            continue
        if labels["po"].get(po) is not None:
            continue    # this PO is itself a labelled positive, not a "silent" sibling
        s = side_of(po, "po", dev, eval_, pv_map)
        if s == "dev":
            sibling_dev += 1
        elif s == "eval":
            sibling_eval += 1
    A("=== Scenario-2 sibling POs (approximation) ===")
    A(f"POs belonging to a splitter vendor with a null labels_po.json entry: "
      f"dev={sibling_dev}, eval={sibling_eval}")
    A("(Approximation: true generation 'role' metadata (plan.json/applied.json) is not one of "
      f"this script's inputs, so a normal PO of a splitter vendor is indistinguishable here "
      f"from a designed hard negative that happens to be null-labelled.)")
    A("")

    A("=== Hard negatives ===")
    A("Skipped: hard-negative status is generation-role metadata (plan.json/applied.json's "
      "`role` field, as read by score.py's load_roles()), not present in any of this script's "
      "inputs (labels_*.json / labels_variant.json / out/po,vendor,vendor_audit). Guessing at "
      "it from the label sidecars alone would not be a reliable count, so it is left to "
      "score.py rather than approximated here.")

    return "\n".join(lines)


# ---------- materialisation ----------
def clear_and_copy(src_dir, dst_dir, wanted_stems_by_vendor, pv_map=None, is_po=False):
    """Clear dst_dir, then copy every file from src_dir whose unit belongs to a dev vendor.
    `is_po` selects PO-file vendor lookup via pv_map; otherwise the vendor id is parsed off the
    filename (`vendor_<id>.json`)."""
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True)
    copied = 0
    for f in sorted(glob.glob(str(src_dir / "*.json"))):
        p = pathlib.Path(f)
        if is_po:
            po_number = p.stem
            vid = pv_map.get(po_number)
        else:
            # filename is vendor_<id>.json
            vid = p.stem.split("_", 1)[1] if "_" in p.stem else None
        if vid is not None and vid in wanted_stems_by_vendor:
            shutil.copy2(p, dst_dir / p.name)
            copied += 1
    return copied


def main():
    ap = argparse.ArgumentParser(
        description="Materialise the frozen §7 dev/eval split (seed 123, floor rounding, "
                    "stratified by vendor class). Must run before any LLM prompt is tuned."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print everything, write nothing")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    labels = load_labels()
    variants = load_variants()
    classes = derive_vendor_classes(labels)
    pv_map = po_vendor_map()

    all_vendor_ids = set(labels["vendor"])   # == set(labels["audit"]) per derive_vendor_classes
    check_po_vendor_ids(pv_map, all_vendor_ids)

    dev, eval_, per_class = build_split(classes)
    verify_split(dev, eval_, all_vendor_ids)

    print(summarize(labels, variants, classes, dev, eval_, per_class, pv_map))
    print("")

    split_path = OUT / "split.json"
    split_doc = {
        "dev": sorted(dev, key=int),
        "eval": sorted(eval_, key=int),
    }

    if args.dry_run:
        print(f"[dry-run] would write {split_path}")
        print(f"[dry-run] would (re)populate {OUT / 'dev' / 'po'}, "
              f"{OUT / 'dev' / 'vendor'}, {OUT / 'dev' / 'vendor_audit'}")
        return

    split_path.write_text(json.dumps(split_doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {split_path}")

    n_po = clear_and_copy(OUT / "po", OUT / "dev" / "po", dev, pv_map=pv_map, is_po=True)
    n_vendor = clear_and_copy(OUT / "vendor", OUT / "dev" / "vendor", dev)
    n_audit = clear_and_copy(OUT / "vendor_audit", OUT / "dev" / "vendor_audit", dev)
    print(f"materialised dev side: {n_po} po / {n_vendor} vendor / {n_audit} vendor_audit files")


if __name__ == "__main__":
    main()
