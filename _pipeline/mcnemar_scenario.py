"""Scenario-aware McNemar exact tests: LLM flag file vs the rule-based SQL baseline.

A positive unit is correct only when it is flagged with its true anomaly scenario; a negative
unit is correct when it is not flagged. Reports, per level, the units only the LLM handled
correctly and the units only the baseline handled correctly, with the two-sided exact p-value.

Usage (from the repo root):
    python _pipeline/mcnemar_scenario.py --llm _pipeline/llm_out/_majority_eval_ds_all.flags
"""
import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import score as S  # noqa: E402


def correct(preds, labels, grain, universe):
    flagged = {}
    for g, u, s in preds:
        if g == grain and u in universe:
            flagged.setdefault(u, set()).add(s)
    ok = set()
    for u in universe:
        truth = labels[grain][u]
        if (truth is None and u not in flagged) or (truth is not None and truth in flagged.get(u, ())):
            ok.add(u)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", required=True)
    ap.add_argument("--baseline", default=str(HERE / "out" / "flags" / "sql_baseline.flags"))
    ap.add_argument("--split", default=str(HERE / "out" / "split.json"))
    ap.add_argument("--side", default="eval", choices=["dev", "eval"])
    a = ap.parse_args()

    labels = S.load_labels()
    roles, _ = S.load_roles()
    side = S.load_split(a.split)[a.side]
    pv = S.po_vendor_map()
    keep = lambda g, u: (pv.get(u) in side) if g == "po" else (u in side)
    llm, sql = S.load_flags(a.llm), S.load_flags(a.baseline)

    print("level | units | LLM only | baseline only | p")
    for grain in ("po", "vendor", "audit"):
        uni = S.scored_units(labels, grain, keep, roles, drop_siblings=True)
        ca, cb = correct(llm, labels, grain, uni), correct(sql, labels, grain, uni)
        b, c = len(ca - cb), len(cb - ca)
        print(f"{grain} | {len(uni)} | {b} | {c} | {S.mcnemar_exact(b, c):.4f}")
    hn = {u for u in S.scored_units(labels, "po", keep) if roles.get(u) == "hard_negative"}
    ca, cb = correct(llm, labels, "po", hn), correct(sql, labels, "po", hn)
    b, c = len(ca - cb), len(cb - ca)
    print(f"po hard negatives | {len(hn)} | {b} | {c} | {S.mcnemar_exact(b, c):.4f}")


if __name__ == "__main__":
    main()
