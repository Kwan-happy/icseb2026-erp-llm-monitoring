#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Aggregate k repeated harness runs into one flag file by majority vote.

k = 5 runs per condition is the fixed operating point, with a majority decision across them, because
run-to-run variation at a fixed temperature was measured on the development split and found large
enough to reverse a comparison between detectors. This program is that decision rule. It exists as
a committed tool rather than as an ad-hoc command so that the aggregation step is reproducible in
the same way the querying step already is.

It does three things:

  1. **Refuses to aggregate runs that are not replicates.** Runs must agree on the model, the side,
     the split file, the decoding settings, and the assembled prompt for every grain they share.
     Aggregating across a configuration change would silently average two different experiments.
  2. **Majority vote per unit, per grain.** A unit is flagged when strictly more than half of the
     runs covering its grain flagged it. The reported anomaly type is the most frequent type among
     the runs that flagged it; ties break alphabetically and are counted in the manifest, so a tie
     is visible rather than resolved by chance.
  3. **Writes a flag file `score.py` reads unchanged**, plus a manifest naming every source run.

Grains are aggregated independently. A run that does not cover a grain simply does not vote on it,
which is what lets one set of runs cover po+vendor and another cover audit. Within a grain, every
contributing run must carry the identical unit set — a unit missing from one run would otherwise
lower that unit's vote count without saying so.

Run from the repo root:

    python _pipeline/aggregate_runs.py \\
        --runs <run_id_1> <run_id_2> <run_id_3> \\
        --k 3 --out _pipeline/llm_out/_majority_dev.flags

    ... --k 5 --strict-k          # fail unless every grain has exactly k voters
"""

import argparse
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LLM_OUT = ROOT / "_pipeline" / "llm_out"

# Settings that must match across replicates. A difference in any of these means the runs are not
# repeats of one condition, and averaging them would describe no condition at all.
MUST_MATCH = ["model_id", "side", "split_sha256", "temperature", "max_tokens", "json_mode",
              "variant", "seed"]


def load_run(run_id):
    d = LLM_OUT / run_id
    man, resp = d / "run_manifest.json", d / "responses.jsonl"
    if not man.exists():
        sys.exit(f"{run_id}: no run_manifest.json at {man}")
    if not resp.exists():
        sys.exit(f"{run_id}: no responses.jsonl at {resp}")
    manifest = json.loads(man.read_text(encoding="utf-8"))

    # One verdict per (grain, unit). A unit answered after an earlier API error has two records;
    # the answered one wins, which is why errors are skipped rather than counted as a non-flag.
    verdicts, errored = {}, set()
    for line in resp.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("dry_run"):
            continue
        key = (rec.get("grain"), rec.get("unit_id"))
        if rec.get("error"):
            errored.add(key)
            continue
        p = rec.get("parsed") or {}
        flagged = p.get("risk_score") in ("medium", "high") and bool(p.get("anomaly_type"))
        verdicts[key] = p.get("anomaly_type") if flagged else None
    unanswered = errored - set(verdicts)
    return manifest, verdicts, unanswered


def check_replicates(runs):
    """Every run must be the same experiment. Reports all mismatches, not just the first."""
    base_id, (base, _, _) = runs[0][0], runs[0][1]
    problems = []
    for run_id, (man, _, _) in runs[1:]:
        for field in MUST_MATCH:
            if man.get(field) != base.get(field):
                problems.append(f"{field}: {base_id}={base.get(field)!r} vs "
                                f"{run_id}={man.get(field)!r}")
        # The assembled prompt is the operative artefact; the per-file hashes can differ in files
        # a grain does not use, so only shared grains are compared.
        a, b = base.get("assembled_prompt_sha256", {}), man.get("assembled_prompt_sha256", {})
        for g in set(a) & set(b):
            if a[g] != b[g]:
                problems.append(f"assembled prompt for grain {g}: {base_id} vs {run_id} differ")
    if problems:
        sys.exit("REFUSING TO AGGREGATE - these runs are not replicates of one condition:\n  "
                 + "\n  ".join(problems))


def main():
    ap = argparse.ArgumentParser(description="Majority-vote aggregation over repeated runs")
    ap.add_argument("--runs", nargs="+", required=True, help="run ids under _pipeline/llm_out/")
    ap.add_argument("--k", type=int, required=True,
                    help="declared number of runs per condition; recorded in the manifest")
    ap.add_argument("--strict-k", action="store_true",
                    help="fail unless every grain has exactly --k voters (use for the eval run)")
    ap.add_argument("--out", required=True, help="flag file to write")
    ap.add_argument("--manifest", default=None,
                    help="aggregation manifest (defaults to <out>.manifest.json)")
    args = ap.parse_args()

    if len(set(args.runs)) != len(args.runs):
        sys.exit("a run id was given twice - that would let one run vote more than once")

    runs = [(r, load_run(r)) for r in args.runs]
    check_replicates(runs)

    # Which runs cover which grain, and every unit seen at that grain.
    by_grain = collections.defaultdict(dict)          # grain -> run_id -> {unit: type|None}
    for run_id, (_man, verdicts, unanswered) in runs:
        if unanswered:
            sys.exit(f"{run_id}: {len(unanswered)} unit(s) have no verdict (API errors never "
                     f"retried). Resume that run before aggregating - an unanswered unit would "
                     f"otherwise count as a silent 'not flagged'. First few: "
                     f"{sorted(unanswered)[:5]}")
        for (grain, unit), t in verdicts.items():
            by_grain[grain].setdefault(run_id, {})[unit] = t

    lines, summary, ties = [], {}, []
    for grain in sorted(by_grain):
        voters = by_grain[grain]
        unit_sets = {r: set(v) for r, v in voters.items()}
        first = sorted(unit_sets)[0]
        for r, us in unit_sets.items():
            if us != unit_sets[first]:
                missing = unit_sets[first] ^ us
                sys.exit(f"grain {grain}: run {r} does not cover the same units as {first} "
                         f"({len(missing)} differ, e.g. {sorted(missing)[:5]}). Aggregating "
                         f"would give those units fewer votes without saying so.")
        n = len(voters)
        if args.strict_k and n != args.k:
            sys.exit(f"grain {grain}: {n} run(s) cover it but --k is {args.k} and --strict-k is "
                     f"set. Run the missing replicates before aggregating.")
        if n != args.k:
            print(f"warning: grain {grain} has {n} voter(s), not the declared k={args.k}")

        flagged = 0
        for unit in sorted(unit_sets[first]):
            votes = [voters[r][unit] for r in voters]
            yes = [v for v in votes if v]
            if len(yes) * 2 <= n:                      # strict majority required
                continue
            counts = collections.Counter(yes)
            top = max(counts.values())
            winners = sorted(t for t, c in counts.items() if c == top)
            if len(winners) > 1:
                ties.append({"grain": grain, "unit_id": unit, "tied_types": winners,
                             "chosen": winners[0]})
            lines.append(f"{grain}|{unit}|{winners[0]}")
            flagged += 1
        summary[grain] = {"voters": n, "units": len(unit_sets[first]), "flagged": flagged,
                          "source_runs": sorted(voters)}

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")

    base = runs[0][1][0]
    man_path = pathlib.Path(args.manifest) if args.manifest else out.with_suffix(
        out.suffix + ".manifest.json")
    man_path.write_text(json.dumps({
        "aggregation": "majority vote across repeated runs of one condition",
        "rule": ("a unit is flagged when strictly more than half of the runs covering its grain "
                 "flagged it; the reported type is the most frequent type among the flagging "
                 "runs; ties break alphabetically and are listed in full below"),
        "declared_k": args.k,
        "strict_k": args.strict_k,
        "source_runs": list(args.runs),
        "condition": {f: base.get(f) for f in MUST_MATCH},
        "assembled_prompt_sha256": base.get("assembled_prompt_sha256"),
        "per_grain": summary,
        "flags_written": len(lines),
        "ties": ties,
        "tie_count": len(ties),
        "flags_file": str(out),
    }, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    print(f"wrote {out}  ({len(lines)} flags)")
    for g, s in summary.items():
        print(f"  {g:7} {s['voters']} voter(s) x {s['units']} unit(s) -> {s['flagged']} flagged")
    print(f"  ties broken alphabetically: {len(ties)}")
    print(f"wrote {man_path}")


if __name__ == "__main__":
    main()
