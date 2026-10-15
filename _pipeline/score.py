#!/usr/bin/env python
"""RQ2 scoring harness — implements the pre-registered metric protocol.

Consumes a flag file in the frozen `(grain, unit_id, scenario)` shape that both detectors
produce, plus the ground-truth sidecars, and emits every table the protocol requires. The same
harness therefore scores `baseline.sql` and the Layer-3 LLM without
modification — which is what keeps the comparison symmetric.

No detector logic lives here. This file only counts.

Run from the repo root, e.g.:
    python _pipeline/score.py --flags _pipeline/out/flags/sql_baseline.flags \\
        --split _pipeline/out/split.json --side eval
    python _pipeline/score.py --flags _pipeline/llm_out/_majority_eval_ds_all.flags --ruleset llm \\
        --compare _pipeline/out/flags/sql_baseline.flags --compare-label sql_baseline \\
        --split _pipeline/out/split.json --side eval --mcnemar strict

Flag file format: one prediction per line, `grain|unit_id|scenario` (psql -tAF'|' output), or the
same three fields as JSON lines. Blank lines and psql notices are ignored. A prediction whose
grain is wrong for its scenario, or whose unit id does not exist, is scored as a false positive —
never silently dropped.
"""
import argparse, collections, glob, json, math, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]      # repo root
GEN = ROOT / "_pipeline" / "generate" / "gen_out"

# Which grain each scenario is scored at (anomaly_spec.md §5). The label sidecars mirror these.
SCENARIO_GRAIN = {
    "duplicate_payment": "po",
    "approval_bypass":   "po",
    "abnormal_amount":   "po",
    "po_splitting":      "vendor",
    "vendor_master_mod": "audit",
}
# Which parts of the frozen SQL baseline's score are fixed by label construction. Printed only
# for the frozen rule set. They hold on any split.
BY_CONSTRUCTION = {
    "duplicate_payment": "canonical recall is fixed at 1.00 by construction (the rule tests the "
                         "same signal the canonical form injects); evasive recall is measured",
    "abnormal_amount":   "canonical recall is fixed at 1.00 by construction (the rule tests the "
                         "same signal the canonical form injects); evasive recall is measured",
    "approval_bypass":   "canonical recall is fixed at 1.00 by construction (the rule tests the "
                         "same signal the canonical form injects); evasive recall is measured",
    "vendor_master_mod": "canonical recall is fixed at 1.00 by construction (the rule tests the "
                         "same signal the canonical form injects); evasive recall and precision "
                         "on hard negatives are measured",
    "po_splitting":      "nothing is fixed by construction; there is no evasive form and the "
                         "rule's recall is measured",
}
# Which scenarios each rule set claims to cover. A scenario a rule set never emits is reported
# as "not covered", never as a score of zero — a zero would misread as a detector that failed.
RULESET_COVERAGE = {
    "frozen":         set(SCENARIO_GRAIN),
    "llm":            set(SCENARIO_GRAIN),
}
DEFAULT_LABEL = {
    "frozen":         "frozen SQL baseline (PRIMARY, pre-registered)",
    "llm":            "Layer-3 LLM (frozen prompt)",
}
PREVALENCES = (0.005, 0.01, 0.05)                        # fixed here, never later
SENSITIVE_FIELDS = {"acc_number", "vat"}
BALANCE_NOTE = ("Precision is a consequence of the test set's designed composition — the base "
                "rates are an evaluation-balance choice for statistical power, not a claim about "
                "real-world prevalence.")


# ---------- inputs ----------
def load_labels():
    """The three ground-truth sidecars, keyed by grain. Keys are strings on every grain."""
    out = {}
    for grain, fname in (("po", "labels_po.json"), ("vendor", "labels_vendor.json"),
                         ("audit", "labels_audit.json")):
        raw = json.loads((GEN / fname).read_text(encoding="utf-8"))
        out[grain] = {str(k): v for k, v in raw.items()}
    return out


def load_roles():
    """PO role per unit — `normal`, `hard_negative`, `s2_sibling`, `positive`.

    The role is what lets the stratified rates split false positives into "fell for a designed trap"
    versus "over-flagged routine business", and what implements sibling exclusion. It is
    generation metadata, never part of the evidence either detector reads.
    """
    plan = json.loads((GEN / "plan.json").read_text(encoding="utf-8"))
    applied = json.loads((GEN / "applied.json").read_text(encoding="utf-8"))
    key2num = {a["po_key"]: a["po_number"] for a in applied["pos"]}
    roles, hn_kind = {}, {}
    for p in plan["pos"]:
        num = key2num.get(p["po_key"])
        if num is None:
            continue
        roles[num] = p["role"]
        if p["role"] == "hard_negative":
            hn_kind[num] = (p.get("anomaly") or {}).get("hard_negative_for")
    return roles, hn_kind


def _read_jsonl(path):
    """One JSON record per non-empty line (the extract's combined .jsonl outputs)."""
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def s3_hard_negatives(labels):
    """Audit-grain hard negatives: null-labelled trails that nevertheless touch a sensitive
    field — the legitimate permanent bank change (anomaly_spec.md §6). They are the S3
    equivalent of the per-PO designed traps, so the stratified rates score them as their own stratum rather
    than blending them into ordinary clean trails."""
    out = set()
    for d in _read_jsonl(ROOT / "_pipeline" / "out" / "audit.jsonl"):
        vid = str(d["vendor"]["id"])
        if labels["audit"].get(vid) is not None:
            continue
        if any(c.get("field") in SENSITIVE_FIELDS or c.get("target") == "bank_account"
               for c in d.get("changes", [])):
            out.add(vid)
    return out


def load_flags(path):
    """Every prediction as a (grain, unit_id, scenario) triple, de-duplicated.

    Nothing is filtered here. An unrecognised scenario name still carries a grain and a unit, so
    it must reach the detection view; dropping it at parse
    time would silently erase exactly the kind of output an LLM produces.
    """
    preds = set()
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            d = json.loads(line)
            grain, unit, scen = d["grain"], str(d["unit_id"]), d["scenario"]
        elif line.count("|") == 2:
            grain, unit, scen = line.split("|")
        else:
            continue                                     # psql NOTICE / header noise
        preds.add((grain.strip(), str(unit).strip(), scen.strip()))
    return preds


def load_split(path):
    """dev/eval split file: {"dev": [vendor_id, ...], "eval": [...]} — vendor ids, since the
    split is stratified by vendor class (anomaly_spec.md §7)."""
    spec = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return {k: {str(v) for v in vs} for k, vs in spec.items()}


def po_vendor_map():
    """PO number -> vendor id, read from the extract (needed only when a split is applied)."""
    out = {}
    for d in _read_jsonl(ROOT / "_pipeline" / "out" / "po_context.jsonl"):
        out[d["po_identity"]["po_number"]] = str(d["po_identity"]["vendor"]["id"])
    return out


# ---------- statistics ----------
def wilson(k, n, z=1.96):
    """Wilson score interval — the sane choice at n=10 and at rates of exactly 0 or 1,
    where the normal approximation gives a zero-width interval."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def prf(tp, fp, fn):
    """Precision/recall/F1, with None where the quantity is undefined (no predictions, or no
    positives in scope) — an undefined precision must not print as a measured 0.00."""
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    if p is None or r is None:
        return p, r, None
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def prevalence_adjusted(recall, fpr, pi):
    """Precision the same detector would show at an assumed base rate `pi`."""
    num = pi * recall
    den = num + (1 - pi) * fpr
    return num / den if den else 0.0


def mcnemar_exact(b, c):
    """Two-sided exact McNemar p-value from the two discordant counts (binomial, q=0.5)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


# ---------- views ----------
def scored_units(labels, grain, keep, roles=None, drop_siblings=False):
    u = {x for x in labels[grain] if keep(grain, x)}
    if drop_siblings and grain == "po" and roles is not None:
        u -= {x for x in u if roles.get(x) == "s2_sibling"}
    return u


def strict_per_scenario(labels, preds, keep):
    """a prediction is a TP only when the unit AND the scenario both match.

    A prediction emitted at the wrong grain for its scenario, or on a unit id that does not
    exist, is a false positive for that scenario, not a
    row to be quietly discarded.
    """
    rows = []
    for scen, grain in SCENARIO_GRAIN.items():
        universe = scored_units(labels, grain, keep)
        truth = {u for u in universe if labels[grain][u] == scen}
        tp = fp = bad_grain = bad_unit = 0
        for g, u, s in preds:
            if s != scen:
                continue
            if g != grain:                                # right type, wrong grain
                if u in labels.get(g, {}) and not keep(g, u):
                    continue                              # other side of the split
                fp += 1
                bad_grain += 1
            elif u not in labels[grain]:                  # unit id does not exist
                fp += 1
                bad_unit += 1
            elif not keep(grain, u):
                continue                                  # other side of the split
            elif u in truth:
                tp += 1
            else:
                fp += 1
        fn = len(truth) - tp
        p, r, f = prf(tp, fp, fn)
        rows.append(dict(scenario=scen, grain=grain, tp=tp, fp=fp, fn=fn,
                         precision=p, recall=r, f1=f, n_positive=len(truth),
                         n_units=len(universe), fp_wrong_grain=bad_grain,
                         fp_unknown_unit=bad_unit))
    return rows


def stratified_rates(labels, preds, roles, hn_kind, keep, audit_hn=frozenset()):
    """recall, and false-positive rate split by negative stratum.

    Hard negatives are matched to the scenario they were designed to trap, so "fell for the
    designed trap" is measured against the right denominator rather than against all negatives.
    """
    rows = []
    for scen, grain in SCENARIO_GRAIN.items():
        universe = scored_units(labels, grain, keep)
        truth = {u for u in universe if labels[grain][u] == scen}
        pred = {u for (g, u, s) in preds if s == scen and g == grain and u in universe}
        recall = len(truth & pred) / len(truth) if truth else None

        if grain == "po":
            hn = {u for u in universe if roles.get(u) == "hard_negative"
                  and hn_kind.get(u) == scen}
            ordinary = {u for u in universe if roles.get(u) == "normal"
                        and labels[grain][u] is None}
        elif grain == "audit":
            hn = {u for u in universe if u in audit_hn}
            ordinary = {u for u in universe if labels[grain][u] is None} - hn
        else:
            # Scenario 2 has no injected hard negatives — the spec (§6) relies on ordinary
            # vendors whose routine orders merely sum past the threshold, which arise from the
            # parametric background and are not separately identifiable.
            hn = set()
            ordinary = {u for u in universe if labels[grain][u] is None}
        rows.append(dict(
            scenario=scen, grain=grain, recall=recall, n_positive=len(truth),
            fp_hard_negative=len(pred & hn), n_hard_negative=len(hn),
            fpr_hard_negative=(len(pred & hn) / len(hn)) if hn else None,
            ci_hard_negative=wilson(len(pred & hn), len(hn)) if hn else None,
            fp_ordinary=len(pred & ordinary), n_ordinary=len(ordinary),
            fpr_ordinary=(len(pred & ordinary) / len(ordinary)) if ordinary else None,
            ci_ordinary=wilson(len(pred & ordinary), len(ordinary)) if ordinary else None,
        ))
    return rows


def detected_units(preds, labels, grain, universe):
    """Units of `grain` this detector flagged, by : any scenario name counts (the type
    need not be right), the grain the detector emitted is the grain the flag applies to, and
    flags on units outside the scored universe (siblings, other split side) are ignored here.
    Flags naming a non-existent unit are returned separately, to be charged as false positives.
    """
    hit, invalid = set(), 0
    for g, u, _s in preds:
        if g != grain:
            continue
        if u in universe:
            hit.add(u)
        elif u not in labels.get(grain, {}):
            invalid += 1
    return hit, invalid


def detection_view(labels, preds, roles, keep):
    """type-agnostic: was the unit flagged at all, at its own grain?

    Sibling POs are dropped from the PO-grain denominator entirely: ground truth labels
    them null while they genuinely belong to an injected anomaly, so scoring them would measure
    the labelling convention rather than the detector.
    """
    rows = []
    for grain in ("po", "vendor", "audit"):
        full = scored_units(labels, grain, keep)
        universe = scored_units(labels, grain, keep, roles, drop_siblings=True)
        truth = {u for u in universe if labels[grain][u] is not None}
        pred, invalid = detected_units(preds, labels, grain, universe)
        tp, fp, fn = len(truth & pred), len(pred - truth) + invalid, len(truth - pred)
        p, r, f = prf(tp, fp, fn)
        rows.append(dict(grain=grain, tp=tp, fp=fp, fn=fn, precision=p, recall=r, f1=f,
                         n_units=len(universe), n_positive=len(truth),
                         excluded_siblings=len(full) - len(universe),
                         fp_unknown_unit=invalid))
    return rows


def type_confusion(labels, preds, roles, keep, audit_hn=frozenset()):
    """predicted scenario x true label, the bridge between the two views."""
    tab = collections.Counter()
    for g, u, s in preds:
        expected = SCENARIO_GRAIN.get(s)
        name = s if expected else f"{s} (unrecognised type)"
        if expected and g != expected:
            tab[(name, f"wrong grain (emitted {g}, scored at {expected})")] += 1
            continue
        if u not in labels.get(g, {}):
            tab[(name, "unknown unit id")] += 1
            continue
        if not keep(g, u):
            continue
        true = labels[g][u]
        if true is None:
            if g == "po" and roles.get(u) == "s2_sibling":
                true = "null (S2 sibling)"
            elif g == "po" and roles.get(u) == "hard_negative":
                true = "null (hard negative)"
            elif g == "audit" and u in audit_hn:
                true = "null (hard negative)"
            else:
                true = "null (ordinary)"
        tab[(name, true)] += 1
    return tab


def correct_units_strict(labels, preds, grain, universe):
    """Units handled correctly under the primary metric's definition: a positive is correct
    only when flagged at this grain with its true scenario; a negative is correct when it
    carries no flag at all, whatever the scenario name."""
    flagged, _ = detected_units(preds, labels, grain, universe)
    out = set()
    for u in universe:
        true = labels[grain][u]
        if true is None:
            if u not in flagged:
                out.add(u)
        elif (grain, u, true) in preds:
            out.add(u)
    return out


def paired_comparison(labels, preds_a, preds_b, roles, keep, strict=False):
    """McNemar's exact test on matched units, with both discordant counts.

    Two scopes are reported: detection agreement within each grain, and the better-powered
    pooled test over the per-PO hard negatives, where the event of interest is "avoided the
    designed trap".

    strict=False (default) is the type-agnostic definition: a positive is correct if flagged
    under any scenario name. strict=True uses the primary metric's definition instead (see
    correct_units_strict). The hard-negative scope is identical under both, because a hard
    negative has no true scenario and is correct only when unflagged.
    """
    rows = []
    for grain in ("po", "vendor", "audit"):
        universe = scored_units(labels, grain, keep, roles, drop_siblings=True)
        if not universe:
            continue
        truth = {u for u in universe if labels[grain][u] is not None}
        a, _ = detected_units(preds_a, labels, grain, universe)
        b, _ = detected_units(preds_b, labels, grain, universe)
        # a unit is handled correctly when a positive is flagged and a negative is not
        if strict:
            ca = correct_units_strict(labels, preds_a, grain, universe)
            cb = correct_units_strict(labels, preds_b, grain, universe)
        else:
            ca = {u for u in universe if (u in a) == (u in truth)}
            cb = {u for u in universe if (u in b) == (u in truth)}
        only_a, only_b = len(ca - cb), len(cb - ca)
        rows.append(dict(scope=f"{grain} grain, all units", n=len(universe),
                         only_a=only_a, only_b=only_b, p=mcnemar_exact(only_a, only_b)))

    hn = {u for u in scored_units(labels, "po", keep) if roles.get(u) == "hard_negative"}
    if hn:
        a, _ = detected_units(preds_a, labels, "po", hn)
        b, _ = detected_units(preds_b, labels, "po", hn)
        only_a, only_b = len(b - a), len(a - b)          # avoiding the trap = being correct
        rows.append(dict(scope="pooled per-PO hard negatives (avoided the trap)", n=len(hn),
                         only_a=only_a, only_b=only_b, p=mcnemar_exact(only_a, only_b)))
    return rows


# ---------- reporting ----------
def fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def fmt_ci(ci):
    return f"[{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "—"


def render(label, strict, strat, detect, confusion, compare, dataset_note, covers,
           construction_notes):
    L = []
    A = L.append
    A(f"# RQ2 scoring — {label}")
    A("")
    A(dataset_note)
    A("")
    A("## 1. Primary metric — strict per-scenario")
    A("")
    A("A prediction counts only when the unit **and** the scenario label both match. A flag "
      "emitted at the wrong grain, or on a unit id that does not exist, is a false positive.")
    A("")
    A("| Scenario | Grain | TP | FP | FN | Precision | Recall | F1 | Fixed by construction |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for r in strict:
        if r["scenario"] not in covers:
            A(f"| `{r['scenario']}` | {r['grain']} | — | — | — | — | — | — | "
              f"*not covered by this rule set* |")
            continue
        note = BY_CONSTRUCTION.get(r["scenario"], "—") if construction_notes else "—"
        A(f"| `{r['scenario']}` | {r['grain']} | {r['tp']} | {r['fp']} | {r['fn']} | "
          f"{fmt(r['precision'])} | {fmt(r['recall'])} | {fmt(r['f1'])} | {note} |")
    scored = [r for r in strict if r["scenario"] in covers and r["grain"] == "po"]
    A("")
    if scored:
        tp = sum(r["tp"] for r in scored)
        fp = sum(r["fp"] for r in scored)
        fn = sum(r["fn"] for r in scored)
        p, r_, f = prf(tp, fp, fn)
        A(f"PO-grain micro-average: TP {tp} / FP {fp} / FN {fn} → "
          f"P {fmt(p)}, R {fmt(r_)}, F1 {fmt(f)}.")
    else:
        A("PO-grain micro-average: *not covered by this rule set*.")
    bad_grain = sum(r["fp_wrong_grain"] for r in strict if r["scenario"] in covers)
    bad_unit = sum(r["fp_unknown_unit"] for r in strict if r["scenario"] in covers)
    if bad_grain or bad_unit:
        A("")
        A(f"Malformed predictions charged as false positives: {bad_grain} emitted at the wrong "
          f"grain, {bad_unit} naming a unit id that does not exist.")
    A("")
    A(BALANCE_NOTE + " Table 2 carries the quantities that do not move with that choice.")
    A("")
    A("## 2. Stratified rates — recall and false-positive rate by negative stratum")
    A("")
    A("| Scenario | Recall | FP on hard negatives | 95% CI | FP on ordinary negatives | 95% CI |")
    A("|---|---:|---|---|---|---|")
    for r in strat:
        if r["scenario"] not in covers:
            continue
        hn = (f"{r['fp_hard_negative']}/{r['n_hard_negative']} = {r['fpr_hard_negative']:.2f}"
              if r["n_hard_negative"] else "—")
        od = (f"{r['fp_ordinary']}/{r['n_ordinary']} = {r['fpr_ordinary']:.3f}"
              if r["n_ordinary"] else "—")
        A(f"| `{r['scenario']}` | {fmt(r['recall'])} ({r['n_positive']}) | {hn} | "
          f"{fmt_ci(r['ci_hard_negative'])} | {od} | {fmt_ci(r['ci_ordinary'])} |")
    A("")
    A("## 3. Precision at assumed real-world prevalence")
    A("")
    A("Computed from the measured recall and the blended false-positive rate; the prevalence "
      "values are fixed in the protocol, not chosen after seeing results. " + BALANCE_NOTE)
    A("")
    A("| Scenario | measured FPR (blended) | "
      + " | ".join(f"P at π={p:.1%}" for p in PREVALENCES) + " |")
    A("|---|---:|" + "---:|" * len(PREVALENCES))
    for r in strat:
        if r["scenario"] not in covers or r["recall"] is None:
            continue
        n_neg = (r["n_hard_negative"] or 0) + (r["n_ordinary"] or 0)
        if not n_neg:
            continue
        fpr = ((r["fp_hard_negative"] or 0) + (r["fp_ordinary"] or 0)) / n_neg
        cells = " | ".join(f"{prevalence_adjusted(r['recall'], fpr, pi):.2f}"
                           for pi in PREVALENCES)
        A(f"| `{r['scenario']}` | {fpr:.4f} | {cells} |")
    A("")
    A("## 4. Secondary metric — detection view, type-agnostic")
    A("")
    A("A unit counts as detected if it was flagged at all, whatever scenario name was used. "
      "Computed within each grain; Scenario-2 sibling POs are excluded from the PO-grain "
      "denominator; hard negatives remain negatives. " + BALANCE_NOTE)
    A("")
    A("| Grain | units scored | positives | TP | FP | FN | Precision | Recall | F1 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    covered_grains = {SCENARIO_GRAIN[s] for s in covers}
    for r in detect:
        if r["grain"] not in covered_grains:
            continue
        A(f"| {r['grain']} | {r['n_units']} | {r['n_positive']} | {r['tp']} | {r['fp']} | "
          f"{r['fn']} | {fmt(r['precision'])} | {fmt(r['recall'])} | {fmt(r['f1'])} |")
    excl = sum(r["excluded_siblings"] for r in detect if r["grain"] in covered_grains)
    if excl:
        A("")
        A(f"({excl} Scenario-2 sibling POs excluded from the PO grain.)")
    A("")
    A("## 5. Type-confusion matrix")
    A("")
    A("| Flagged as | True label | Count |")
    A("|---|---|---:|")
    for (scen, true), n in sorted(confusion.items(), key=lambda kv: (-kv[1], kv[0])):
        base = scen.split(" (")[0]
        if base in SCENARIO_GRAIN and base not in covers:
            continue
        A(f"| `{scen}` | {true} | {n} |")
    for mode, rows, label_b in (compare or []):
        A("")
        if mode == "strict":
            A("## 6. Paired comparison — McNemar exact, scenario-aware")
            A("")
            A(f"Both detectors read identical evidence, so every unit is a matched pair. **A** = "
              f"{label}; **B** = {label_b}. Correctness follows the primary metric: a positive "
              f"unit is correct only when flagged with its true scenario, and a negative unit is "
              f"correct when it is not flagged at all.")
        else:
            A("## 6. Paired comparison — McNemar exact, type-agnostic")
            A("")
            A(f"Both detectors read identical evidence, so every unit is a matched pair. **A** = "
              f"{label}; **B** = {label_b}. A positive unit is correct when it is flagged under "
              f"any scenario name, and a negative unit is correct when it is not flagged.")
        A("")
        A("| Scope | units | A correct, B wrong | B correct, A wrong | p (two-sided exact) |")
        A("|---|---:|---:|---:|---:|")
        for r in rows:
            A(f"| {r['scope']} | {r['n']} | {r['only_a']} | {r['only_b']} | {r['p']:.4f} |")
    A("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="RQ2 scoring harness")
    ap.add_argument("--flags", required=True, help="flag file: grain|unit_id|scenario per line")
    ap.add_argument("--ruleset", choices=["frozen", "llm"],
                    default="frozen",
                    help="which scenarios this flag file claims to cover; a scenario the rule "
                         "set does not emit is reported as 'not covered', never as a zero")
    ap.add_argument("--label", default=None, help="report title (defaults per --ruleset)")
    ap.add_argument("--compare", default=None,
                    help="second flag file to pair against, for the McNemar test")
    ap.add_argument("--compare-label", default="comparator")
    ap.add_argument("--mcnemar", choices=["loose", "strict", "both"], default="loose",
                    help="paired-test definition of a correct unit: 'loose' = type-agnostic "
                         "(default), 'strict' = scenario-aware, matching the primary metric, "
                         "'both' = both tables")
    ap.add_argument("--split", default=None,
                    help="JSON {'dev': [vendor_id...], 'eval': [...]}; requires --side")
    ap.add_argument("--side", choices=["dev", "eval"], default=None)
    ap.add_argument("--out", default=None, help="write the markdown report here")
    ap.add_argument("--json", default=None, help="write the machine-readable sidecar here")
    args = ap.parse_args()

    # The Windows console here is cp874; the report contains section marks and box characters.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    label = args.label or DEFAULT_LABEL[args.ruleset]
    labels = load_labels()
    roles, hn_kind = load_roles()
    preds = load_flags(args.flags)

    if bool(args.split) != bool(args.side):
        sys.exit("--split and --side must be given together")
    if args.split:
        split = load_split(args.split)
        if args.side not in split:
            sys.exit(f"split file has no '{args.side}' key (found: {sorted(split)})")
        # The split must PARTITION the vendor universe: covered exactly once each, not zero,
        # not twice. Without this check a vendor missing from both sides is silently dropped
        # from every denominator (wrong numbers, no diagnostic); a vendor in both sides is
        # scored on both, double-counting it. This does not change any metric for a valid
        # split file — it only rejects invalid split files.
        universe = set(labels["vendor"]) | set(labels["audit"])
        dev_s, eval_s = split.get("dev", set()), split.get("eval", set())
        both, missing, unknown = (dev_s & eval_s, universe - (dev_s | eval_s),
                                   (dev_s | eval_s) - universe)
        if both or missing or unknown:
            sys.exit(
                f"split file does not partition the vendor set — "
                f"in both dev and eval: {sorted(both)}; "
                f"missing from both: {sorted(missing)}; "
                f"unknown vendor ids: {sorted(unknown)}"
            )
        side = split[args.side]
        pv = po_vendor_map()
        def keep(grain, unit):
            return (pv.get(unit) in side) if grain == "po" else (unit in side)
        note = (f"Scored on the **{args.side}** split ({len(side)} vendors) of the dataset, "
                f"under the pre-registered protocol. Flags: `{pathlib.Path(args.flags).name}`.")
    else:
        def keep(grain, unit):
            return True
        note = (f"Scored on the **whole dataset**. Flags: "
                f"`{pathlib.Path(args.flags).name}`. Whole-dataset figures are sanity checks; "
                f"the RQ2 headline comes from the held-out evaluation split after the prompt "
                f"freeze.")

    audit_hn = s3_hard_negatives(labels)
    strict = strict_per_scenario(labels, preds, keep)
    strat = stratified_rates(labels, preds, roles, hn_kind, keep, audit_hn)
    detect = detection_view(labels, preds, roles, keep)
    confusion = type_confusion(labels, preds, roles, keep, audit_hn)
    compare = []
    if args.compare:
        preds_b = load_flags(args.compare)
        modes = ["loose", "strict"] if args.mcnemar == "both" else [args.mcnemar]
        for mode in modes:
            compare.append((mode, paired_comparison(labels, preds, preds_b, roles, keep,
                                                    strict=(mode == "strict")),
                            args.compare_label))
    by_mode = {mode: rows for mode, rows, _ in compare}
    if args.compare and "loose" not in by_mode:            # the JSON always carries the loose rows
        by_mode["loose"] = paired_comparison(labels, preds, preds_b, roles, keep)

    covers = RULESET_COVERAGE[args.ruleset]
    report = render(label, strict, strat, detect, confusion, compare, note, covers,
                    construction_notes=(args.ruleset == "frozen"))
    print(report)
    if args.out:
        pathlib.Path(args.out).write_text(report, encoding="utf-8", newline="\n")
    if args.json:
        covered_grains = {SCENARIO_GRAIN[s] for s in covers}
        pathlib.Path(args.json).write_text(json.dumps({
            "label": label, "flags": args.flags, "ruleset": args.ruleset, "side": args.side,
            "scenarios_covered": sorted(covers),
            "strict_per_scenario": [dict(r, covered=r["scenario"] in covers) for r in strict],
            "stratified_rates": [dict(r, covered=r["scenario"] in covers) for r in strat],
            "detection_view": [dict(r, covered=r["grain"] in covered_grains) for r in detect],
            "type_confusion": [{"flagged_as": k[0], "true_label": k[1], "n": v}
                               for k, v in sorted(confusion.items())],
            "paired_comparison": by_mode.get("loose"),
            "paired_comparison_strict": by_mode.get("strict"),
            "prevalences": list(PREVALENCES),
        }, indent=2, default=str), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
