#!/usr/bin/env python
"""synthetic-dataset generator — orchestrator.

Implements the FROZEN spec (`anomaly_spec.md` + `params.json`) by loading synthetic purchase orders into a fresh Odoo 19
DB over XML-RPC and then
injecting the five anomaly scenarios on top. This file owns the run: config, seed, staging,
and the manifest. It defines NO statistical parameter and NO policy constant of its own —
every number comes from `params.json` (frozen) or `_pipeline/config.json`.

Two-phase by design, so the sampling can be inspected before any database is touched:
    PLAN   pure Python: draw vendors/products/POs/anomalies -> a list of plain dicts
    APPLY  push that plan into Odoo as the right user, recording intended timestamps

Run from the repo root:
    python _pipeline/generate/generate.py --dry-run
    python _pipeline/generate/generate.py --scale smoke
    python _pipeline/generate/generate.py --scale full

After a run: apply `fix_timestamps.sql`, point `_pipeline/config.json` at the generation DB,
then run `_pipeline/extract.py` unchanged.
"""
import argparse, copy, datetime, hashlib, json, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent          # _pipeline/generate
ROOT = HERE.parents[1]                                  # repo root
sys.path.insert(0, str(HERE))                           # sibling modules import flat


# ---------- config ----------
def load_configs(config_path=None):
    """gen_config.json = connection + credentials + scale (git-ignored, holds passwords).
    params.json = the frozen distributions. _pipeline/config.json = the policy constants the
    extraction pipeline also reads, so both sides of the comparison see the same threshold."""
    cfg_file = pathlib.Path(config_path) if config_path else HERE / "gen_config.json"
    if not cfg_file.exists():
        sys.exit(f"missing {cfg_file}\n  -> copy gen_config.example.json to gen_config.json "
                 f"and fill in the DB name and the two user logins")
    cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    params = json.loads((HERE / "params.json").read_text(encoding="utf-8"))
    pipe = json.loads((ROOT / "_pipeline" / "config.json").read_text(encoding="utf-8"))

    # The threshold is a frozen policy constant that must agree in three places (params.json's
    # freeze record, the extraction config, and Odoo's po_double_validation_amount). Two of
    # them are files, so check them here; Odoo's copy is checked by preflight().
    frozen = params["fixed_constants"]["approval_threshold"]
    live = pipe["params"]["approval_threshold"]
    if frozen != live:
        sys.exit(f"approval_threshold disagrees: params.json={frozen} config.json={live}")
    return cfg, params, pipe


def _split_canonical_evasive(total, evasive_share, min_total_for_evasive):
    """anomaly_spec.md §5: ~30% of S1/S4/S5/S3 positives are the EVASIVE variant (outside
    the frozen rule's single-field test), the rest canonical. At `total`=40/0.30 this returns
    exactly (28, 12) and at 18/(1/3) exactly (12, 6) — the spec table's pinned numbers. Below
    `min_total_for_evasive` (smoke-scale rehearsals) the split collapses to all-canonical: a
    handful of evasive cases at n<4 is not a meaningful rehearsal of the property, and forcing
    one would risk driving canonical to 0."""
    if total < min_total_for_evasive:
        return total, 0
    evasive = max(1, round(total * evasive_share))
    evasive = min(evasive, total - 1)          # keep at least 1 canonical
    return total - evasive, evasive


def scaled_counts(params, cfg, scale):
    """Full scale = the frozen counts. Smoke scale shrinks the COUNTS proportionally for an
    end-to-end rehearsal; it never touches a distribution, so smoke data is not evaluation data."""
    n_normal = params["counts"]["total_normal_pos"]
    n_vendors = params["counts"]["n_vendors"]
    n_products = params["counts"]["n_products"]
    # per-grain targets, anomaly_spec.md §5
    per_po_scenarios = 40          # S1, S4, S5 each (TOTAL — canonical + evasive)
    splitter_vendors = 18          # S2, of 50 vendors
    tampered_vendors = 18          # S3, of 50 vendors (TOTAL — canonical + evasive)

    if scale == "smoke":
        total = cfg["run"].get("smoke_total_pos", 30)
        f = total / (n_normal + 3 * per_po_scenarios)
        n_normal = max(10, round(n_normal * f))
        n_vendors = max(4, round(n_vendors * f))
        n_products = max(6, round(n_products * f))
        per_po_scenarios = max(2, round(per_po_scenarios * f))
        splitter_vendors = max(1, round(splitter_vendors * f))
        tampered_vendors = max(1, round(tampered_vendors * f))

    # anomaly_spec.md §5: each per-PO scenario's 40 splits ~28
    # canonical / ~12 evasive; S3's 18 splits ~12 canonical / ~6 evasive. S1's evasive count is
    # further halved into PAIRS (§2 S1's counting pin: ~12 evasive POs = ~6 injected pairs).
    s1_canonical, s1_evasive_pos = _split_canonical_evasive(per_po_scenarios, 0.30, 4)
    s1_evasive_pairs = s1_evasive_pos // 2 if s1_evasive_pos else 0
    s1_evasive_pos = s1_evasive_pairs * 2      # keep the derived POs count consistent with pairs
    s1_canonical = per_po_scenarios - s1_evasive_pos
    s4_canonical, s4_evasive = _split_canonical_evasive(per_po_scenarios, 0.30, 4)
    s5_canonical, s5_evasive = _split_canonical_evasive(per_po_scenarios, 0.30, 4)
    s3_canonical, s3_evasive = _split_canonical_evasive(tampered_vendors, 1 / 3, 3)

    return {
        "scale": scale,
        "total_normal_pos": n_normal,
        "n_vendors": n_vendors,
        "n_products": n_products,
        "s1_duplicate_payment": per_po_scenarios,
        "s4_approval_bypass": per_po_scenarios,
        "s5_abnormal_amount": per_po_scenarios,
        "s2_splitter_vendors": splitter_vendors,
        "s3_tampered_vendors": tampered_vendors,
        # canonical/evasive split (anomaly_spec.md §5)
        "s1_canonical": s1_canonical, "s1_evasive_pairs": s1_evasive_pairs,
        "s4_canonical": s4_canonical, "s4_evasive": s4_evasive,
        "s5_canonical": s5_canonical, "s5_evasive": s5_evasive,
        "s3_canonical": s3_canonical, "s3_evasive": s3_evasive,
    }


def params_for(params, counts):
    """`Samplers` reads `counts` and `vendor_assignment.n` straight out of params.json, so a
    smoke run must be handed a params COPY with the scaled counts substituted — otherwise the
    catalog and vendor weights are built at full size while the plan is built at smoke size,
    and the plan references product/vendor indices the smoke master never creates.

    At `--scale full` this returns an exact copy of the frozen params (n_vendors 50,
    n_products 60 = 6 categories x 10), so the freeze is untouched: the substitution only ever
    shrinks a rehearsal.
    """
    p = copy.deepcopy(params)
    n_cat = min(p["vendor_product_affinity"]["n_categories"], counts["n_products"])
    per_cat = max(1, counts["n_products"] // n_cat)
    n_products = n_cat * per_cat          # distributions asserts n_cat * per_cat == n_products
    p["counts"]["total_normal_pos"] = counts["total_normal_pos"]
    p["counts"]["n_vendors"] = counts["n_vendors"]
    p["counts"]["n_products"] = n_products
    p["vendor_assignment"]["n"] = counts["n_vendors"]
    p["vendor_product_affinity"]["n_categories"] = n_cat
    p["vendor_product_affinity"]["products_per_category"] = per_cat
    counts["n_products"] = n_products     # keep the plan and the samplers in agreement
    return p


# ---------- provenance ----------
def file_hash(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()[:16]


def git_head():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


def write_manifest(out_dir, cfg, counts, started, extra, filename="run_manifest.json"):
    """The generation record: what was run, against which frozen inputs.

    `filename` exists so a --dry-run cannot overwrite the record of the last REAL generation.
    Dry runs write `dry_run_manifest.json` instead.
    """
    manifest = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.time() - started, 1),
        "scale": counts["scale"],
        "db": cfg["odoo"]["dbname"],
        "seed": extra.get("seed"),
        "counts": counts,
        "frozen_inputs": {
            "params.json": file_hash(HERE / "params.json"),
            "anomaly_spec.md": file_hash(HERE / "anomaly_spec.md"),
            "baseline.sql": file_hash(HERE / "baseline.sql"),
        },
        "git_head_at_generation": git_head(),
        **extra,
    }
    (out_dir / filename).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return manifest


# ---------- stages ----------
def main():
    ap = argparse.ArgumentParser(description="synthetic-dataset generator")
    ap.add_argument("--scale", choices=["smoke", "full"], default=None,
                    help="overrides run.scale in gen_config.json")
    ap.add_argument("--stage", choices=["all", "plan", "master", "normal", "anomalies", "labels"],
                    default="all")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan and report the sampling only; touches no database")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    started = time.time()
    cfg, params, pipe = load_configs(args.config)
    scale = args.scale or cfg["run"].get("scale", "smoke")
    counts = scaled_counts(params, cfg, scale)
    out_dir = ROOT / cfg["paths"]["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    seed = params["seed"]
    print(f"scale={scale} seed={seed} db={cfg['odoo']['dbname']}")
    print("  counts: " + ", ".join(f"{k}={v}" for k, v in counts.items() if k != "scale"))

    # -- PLAN (pure Python, no DB) --
    import distributions, plan_dataset
    params_run = params_for(params, counts)
    samplers = distributions.Samplers(seed, params_run)
    plan = plan_dataset.build_plan(samplers, params_run, pipe, counts)
    plan_dataset.write_plan(plan, out_dir, dry_run=args.dry_run)
    plan_dataset.print_summary(plan, pipe["params"]["approval_threshold"])

    if args.dry_run or args.stage == "plan":
        write_manifest(out_dir, cfg, counts, started, {"seed": seed, "mode": "dry-run"},
                       filename="dry_run_manifest.json")
        print(f"\ndry run complete - no database touched. plan -> {out_dir}")
        return 0

    # -- APPLY (Odoo XML-RPC) --
    import odoo_client, build_master, build_normal, inject_anomalies, labels, timestamps
    cluster = odoo_client.OdooCluster.from_gen_config(cfg["odoo"])
    pre = odoo_client.preflight(cluster, expected={
        "approval_threshold": pipe["params"]["approval_threshold"]})
    print(f"  preflight ok: setup_session={pre['setup_session']} "
          f"buyers={pre['roles']['buyers']} managers={pre['roles']['managers']}")
    # Resolve the plan's symbolic "setup" actor (used for master data and for the scenario-3
    # bank-account edits, which need base.group_partner_manager) to a real session name. The
    # plan is built before any connection exists, so it cannot know which manager holds it.
    plan["params_echo"]["setup_session"] = pre["setup_session"]
    cluster.alias("setup", pre["setup_session"])
    # evasive S3 (anomaly_spec.md): the bank-account ADDITION should come from a
    # NON-ROUTINE actor, distinct from "setup" (which performs the canonical tamper edits).
    # preflight() only guarantees ONE elevated (base.group_system) session on this DB, and
    # res.partner.bank creation needs base.group_partner_manager, implied only by
    # base.group_system in a stock Odoo 19 install — so there is currently no technically
    # different session available to alias this to. Aliasing to setup_session is a same-actor
    # FALLBACK, not the intended contrast; it keeps the run from failing on a missing session
    # rather than silently mislabelling the evidence. Provisioning a second manager with
    # base.group_partner_manager (without full base.group_system) before a --scale full run
    # would let this alias point somewhere genuinely different.
    cluster.alias("s3_evasive_adder", pre["setup_session"])

    tsmap = timestamps.TimestampMap()
    master = build_master.build_master(cluster, plan, cfg, tsmap)
    applied = build_normal.apply_normal(cluster, plan, master, tsmap)
    injected = inject_anomalies.apply_anomalies(cluster, plan, master, applied, tsmap)
    label_files = labels.write_labels(plan, master, applied, injected, out_dir)

    tsmap.flush(out_dir)
    manifest = write_manifest(out_dir, cfg, counts, started, {
        "seed": seed, "mode": "generate", "preflight": pre,
        "created": {"vendors": len(master["vendors"]), "products": len(master["products"]),
                    "pos": len(applied["pos"])},
        "labels": label_files, "xmlrpc_stats": cluster.stats(),
    })
    print(f"\ndone in {manifest['elapsed_seconds']}s -> {out_dir}")
    print("NEXT: 1) run fix_timestamps.sql   2) point _pipeline/config.json at "
          f"'{cfg['odoo']['dbname']}'   3) run _pipeline/extract.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
