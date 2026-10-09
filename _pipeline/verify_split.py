# -*- coding: utf-8 -*-
"""Independent property checker for the dev/eval vendor split.

This script is independent of `_pipeline/make_split.py` (the *producer* of the split).
Every property checked below is re-derived from the frozen split
specification in `_pipeline/generate/anomaly_spec.md` §7 (30% dev / 70% eval, stratified
BY VENDOR then by vendor class — splitter / tampered / normal —, fixed `random_seed: 123`,
rounding rule `dev = floor(0.30 * n)` per class, "every scenario is
represented on both sides") and from the ground-truth label sidecars in
`_pipeline/generate/gen_out/`, plus the extracted per-PO context in `_pipeline/out/po/`.
Nothing here imports or inspects the producer's logic. If these checks merely
re-implemented how the producer probably computed the split, they would be worthless as an
independent check — the goal is to catch a producer bug, not to agree with it.

It uses a `chk(name, got, want)` helper prints
`OK`/`FAIL` and records failures; a separate `warn(name, msg)` helper prints non-fatal
`WARN` lines for properties that are accepted and reported rather than treating as blocking (e.g. a thin dev side on a rare scenario); a
final `RESULT:` line; `sys.exit(1 if fails else 0)`.

Run from the repo root:

    python _pipeline/verify_split.py

If `_pipeline/out/split.json` does not exist yet, this prints a clear "artifacts not
produced yet" message and exits 1 (no traceback). If `split.json` exists but
`_pipeline/out/dev/` does not, the split-level checks still run and the
materialisation checks (6) are skipped with an explicit note.
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEN = ROOT / "_pipeline" / "generate" / "gen_out"
OUT = ROOT / "_pipeline" / "out"
SPLIT_JSON = OUT / "split.json"
DEV_DIR = OUT / "dev"
EVAL_DIR = OUT / "eval"

DEV_FRACTION = 0.30  # frozen in anomaly_spec.md §7; rounding rule = floor(0.30 * n) per class

fails = []
warnings = []


def chk(name, got, want):
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {name}: got {got}" + ("" if ok else f" want {want}"))
    if not ok:
        fails.append(name)


def warn(name, msg):
    print(f"WARN {name}: {msg}")
    warnings.append(name)


def info(msg):
    print(f"INFO {msg}")


def load(path):
    return json.load(open(path, encoding="utf-8"))


# ================================================================================
# Ground truth (independent of any split artifact) -- these run regardless of
# whether the split has been produced yet.
# ================================================================================

if not GEN.exists():
    print(f"FAIL setup: ground-truth directory not found: {GEN}")
    sys.exit(1)

labels_vendor = {str(k): v for k, v in load(GEN / "labels_vendor.json").items()}
labels_audit = {str(k): v for k, v in load(GEN / "labels_audit.json").items()}
labels_po = load(GEN / "labels_po.json")
labels_variant = load(GEN / "labels_variant.json")

all_vendors = set(labels_vendor)
chk("labels_audit covers the same vendor set as labels_vendor",
    set(labels_audit) == all_vendors, True)

splitter = {v for v, s in labels_vendor.items() if s is not None}
tampered = {v for v, s in labels_audit.items() if s is not None}
normal = all_vendors - splitter - tampered

chk("total vendor count (from label sidecars)", len(all_vendors), 50)
chk("splitter class size", len(splitter), 18)
chk("tampered class size", len(tampered), 18)
chk("normal class size", len(normal), 14)

# ---- check 2: class disjointness -- the split (and its per-class stratification) relies
# on splitter and tampered being mutually exclusive vendor sets.
chk("splitter and tampered classes are disjoint", splitter & tampered, set())

# ---- vendor id per PO, derived independently from the extracted PO grain (not from
# split.json, not from any vendor-side rollup) ----------------------------------
po_files = sorted(OUT.glob("po/*.json"))
if not po_files:
    print(f"FAIL setup: no PO context files found under {OUT / 'po'}")
    sys.exit(1)

po_vendor = {}  # po_number -> vendor id (str)
for f in po_files:
    d = load(f)
    po_number = d["po_identity"]["po_number"]
    vendor_id = str(d["po_identity"]["vendor"]["id"])
    po_vendor[po_number] = vendor_id

chk("PO grain file count", len(po_files), 1125)
chk("every PO's vendor id appears in the vendor-grain label set",
    set(po_vendor.values()) <= all_vendors, True)


# ================================================================================
# Split artifacts. Bail out cleanly (no traceback) if split.json itself is missing --
# almost every remaining check depends on it.
# ================================================================================

if not SPLIT_JSON.exists():
    print(f"\nARTIFACTS NOT PRODUCED YET: {SPLIT_JSON} does not exist.")
    print("Cannot check the split partition, per-class proportions, sibling-leakage, "
          "materialisation, or scenario coverage until make_split.py has been run.")
    fails.append("split.json exists")
    print("\nRESULT:", f"{len(fails)} FAILED (artifacts not produced yet): {fails}")
    sys.exit(1)

split = load(SPLIT_JSON)
chk("split.json has 'dev' and 'eval' keys", {"dev", "eval"} <= set(split.keys()), True)
chk("split.json['dev'] elements are strings (per schema)",
    all(isinstance(x, str) for x in split.get("dev", [])), True)
chk("split.json['eval'] elements are strings (per schema)",
    all(isinstance(x, str) for x in split.get("eval", [])), True)
chk("no duplicate ids within split.json['dev']",
    len(split.get("dev", [])), len(set(split.get("dev", []))))
chk("no duplicate ids within split.json['eval']",
    len(split.get("eval", [])), len(set(split.get("eval", []))))

dev_ids = set(map(str, split.get("dev", [])))
eval_ids = set(map(str, split.get("eval", [])))

# ---- check 1 (most important -- score.py does not check this itself): partition ----
chk("dev ids are a subset of the 50-vendor set", dev_ids <= all_vendors, True)
chk("eval ids are a subset of the 50-vendor set", eval_ids <= all_vendors, True)
chk("dev and eval are disjoint", dev_ids & eval_ids, set())
chk("dev union eval equals the full 50-vendor set (no vendor silently dropped)",
    dev_ids | eval_ids, all_vendors)


def dev_eval_counts(cls):
    return len(cls & dev_ids), len(cls & eval_ids)


# ---- check 3: per-class proportions, dev = floor(0.30 * n) per class --------------
expected = {}
for name, cls in (("splitter", splitter), ("tampered", tampered), ("normal", normal)):
    n_dev = int(DEV_FRACTION * len(cls))  # floor, per the frozen rounding rule
    expected[name] = (n_dev, len(cls) - n_dev)

for name, cls in (("splitter", splitter), ("tampered", tampered), ("normal", normal)):
    chk(f"{name} class dev/eval split matches floor(0.30*n) rule",
        dev_eval_counts(cls), expected[name])

# concrete pinned numbers, so a silent spec drift (e.g. class sizes changing) is caught
# even if the floor-rule recomputation above were coincidentally satisfied some other way
chk("splitter dev/eval concrete counts", dev_eval_counts(splitter), (5, 13))
chk("tampered dev/eval concrete counts", dev_eval_counts(tampered), (5, 13))
chk("normal dev/eval concrete counts", dev_eval_counts(normal), (4, 10))
chk("overall dev/eval totals", (len(dev_ids), len(eval_ids)), (14, 36))

# ---- check 4: every class represented on both sides -------------------------------
for name, cls in (("splitter", splitter), ("tampered", tampered), ("normal", normal)):
    d, e = dev_eval_counts(cls)
    chk(f"{name} class present on both sides (dev>0, eval>0)", (d > 0, e > 0), (True, True))


# ================================================================================
# Materialisation-dependent checks. out/dev/ may legitimately not exist yet even
# though split.json does (split computed but not yet applied) -- degrade gracefully.
# ================================================================================

if not DEV_DIR.exists():
    print(f"\nARTIFACTS NOT PRODUCED YET: {DEV_DIR} does not exist.")
    print("Skipping sibling-leakage-via-materialisation and materialised-directory checks "
          "(5, 6). Split-level checks (1-4) above still ran.")
    fails.append("out/dev/ exists")
else:
    dev_po_dir = DEV_DIR / "po"
    dev_po_files = set(f.name for f in dev_po_dir.glob("*.json")) if dev_po_dir.exists() else set()

    # ---- check 5: no scenario-2 sibling leakage across the vendor boundary --------
    # For every splitter vendor, ALL of that vendor's POs (derived independently from
    # out/po/*.json, not from any vendor-grain po_refs list) must land on the same side
    # as the vendor itself in split.json.
    sibling_fail = []
    for v in splitter:
        vendor_pos = [p for p, vv in po_vendor.items() if vv == v]
        if v in dev_ids:
            missing = [p for p in vendor_pos if f"{p}.json" not in dev_po_files]
            if missing:
                sibling_fail.append({"vendor": v, "problem": "dev vendor PO missing from out/dev/po", "pos": missing})
        else:
            leaked = [p for p in vendor_pos if f"{p}.json" in dev_po_files]
            if leaked:
                sibling_fail.append({"vendor": v, "problem": "eval vendor PO leaked into out/dev/po", "pos": leaked})
    chk("no splitter-vendor PO crosses the dev/eval boundary independently of its vendor",
        sibling_fail, [])

    # ---- check 6: materialised dev directory is exactly right ---------------------
    # 6a. every file in out/dev/po/ belongs to a dev vendor (no eval contamination --
    # this is the isolation evidence).
    contamination = sorted(
        stem for stem in (pathlib.Path(name).stem for name in dev_po_files)
        if po_vendor.get(stem) not in dev_ids)
    chk("out/dev/po/ contains no eval-vendor POs (isolation)", contamination, [])

    # 6b. every dev-vendor PO in out/po is present in out/dev/po (nothing missing).
    missing_po = sorted(p for p, v in po_vendor.items()
                         if v in dev_ids and f"{p}.json" not in dev_po_files)
    chk("out/dev/po/ contains every dev-vendor PO (completeness)", missing_po, [])

    chk("out/dev/po/ file count equals dev-vendor PO count",
        len(dev_po_files), sum(1 for v in po_vendor.values() if v in dev_ids))

    # 6c. content matches the source (parsed-JSON equality, robust to re-serialisation).
    mismatched = []
    for p, v in po_vendor.items():
        if v in dev_ids:
            src, dst = OUT / "po" / f"{p}.json", dev_po_dir / f"{p}.json"
            if dst.exists():
                if load(src) != load(dst):
                    mismatched.append(p)
    chk("out/dev/po/ contents match out/po/ (parsed-JSON equality)", mismatched, [])

    # 6d/e. same shape for the vendor and vendor_audit grains (filename: vendor_<id>.json).
    for grain in ("vendor", "vendor_audit"):
        src_dir = OUT / grain
        dst_dir = DEV_DIR / grain
        dst_files = set(f.name for f in dst_dir.glob("*.json")) if dst_dir.exists() else set()
        expected_files = {f"vendor_{v}.json" for v in dev_ids}
        chk(f"out/dev/{grain}/ file count is exactly 14 (the dev-vendor count)",
            len(dst_files), 14)
        chk(f"out/dev/{grain}/ contains exactly the dev vendors (no more, no less)",
            dst_files, expected_files)
        mism = []
        for name in expected_files & dst_files:
            src, dst = src_dir / name, dst_dir / name
            if src.exists() and load(src) != load(dst):
                mism.append(name)
        chk(f"out/dev/{grain}/ contents match out/{grain}/ (parsed-JSON equality)", mism, [])

# ---- check 7: eval side is deliberately NOT materialised --------------------------
if EVAL_DIR.exists():
    warn("out/eval/ existence",
         f"{EVAL_DIR} exists -- the author's decision (per the task spec) was dev-only "
         "materialisation; reporting rather than hard-failing in case this was added deliberately later")
else:
    chk("out/eval/ does not exist (dev-only materialisation, by design)", EVAL_DIR.exists(), False)


# ================================================================================
# check 8: scenario coverage on each side, broken out by canonical/evasive variant.
# Report counts always; WARN (never FAIL) if a scenario has zero positives on a side --
# the author has pre-committed to honestly reporting a thin dev side rather than
# reshuffling the seed.
# ================================================================================

GRAIN_OF = {"duplicate_payment": "po", "approval_bypass": "po", "abnormal_amount": "po",
            "po_splitting": "vendor", "vendor_master_mod": "audit"}


def side_of_vendor(v):
    if v in dev_ids:
        return "dev"
    if v in eval_ids:
        return "eval"
    return None


def side_of_po(po_number):
    return side_of_vendor(po_vendor.get(po_number))


print("\n---- scenario coverage (counts; WARN only, never FAIL, if a side has zero) ----")
for scenario, grain in GRAIN_OF.items():
    counts = {"dev": {"canonical": 0, "evasive": 0, "other": 0},
              "eval": {"canonical": 0, "evasive": 0, "other": 0}}

    if grain == "po":
        variant_map = labels_variant.get("po", {})
        items = labels_po.items()
        side_of = side_of_po
    elif grain == "vendor":
        variant_map = labels_variant.get("vendor", {})
        items = labels_vendor.items()
        side_of = side_of_vendor
    else:  # audit
        variant_map = labels_variant.get("audit", {})
        items = labels_audit.items()
        side_of = side_of_vendor

    for unit_id, sc in items:
        if sc != scenario:
            continue
        side = side_of(unit_id)
        if side is None:
            continue
        variant = variant_map.get(unit_id) or "other"
        counts[side][variant if variant in ("canonical", "evasive") else "other"] += 1

    dev_total = sum(counts["dev"].values())
    eval_total = sum(counts["eval"].values())
    info(f"{scenario} ({grain} grain): dev={dev_total} {counts['dev']}  "
         f"eval={eval_total} {counts['eval']}")
    if dev_total == 0:
        warn(f"{scenario} dev-side coverage", "zero positives on the dev side")
    if eval_total == 0:
        warn(f"{scenario} eval-side coverage", "zero positives on the eval side")

# ---- check 9: expected eval power on the rare grains (~13 each, pre-registered) ----
s2_eval = len(splitter & eval_ids)
s3_eval = len(tampered & eval_ids)
info(f"eval-side S2 (po_splitting) positives: {s2_eval} (the design expects ~13)")
info(f"eval-side S3 (vendor_master_mod) positives: {s3_eval} (the design expects ~13)")
chk("eval-side S2 (po_splitting) positives match pre-registered ~13", s2_eval, 13)
chk("eval-side S3 (vendor_master_mod) positives match pre-registered ~13", s3_eval, 13)

print("\nRESULT:", "ALL CHECKS PASS" if not fails else f"{len(fails)} FAILED: {fails}")
if warnings:
    print(f"({len(warnings)} WARNING(s), non-fatal): {warnings}")
sys.exit(1 if fails else 0)
