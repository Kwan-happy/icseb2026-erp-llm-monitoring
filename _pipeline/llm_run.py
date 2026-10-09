#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM harness — assemble prompt, query the model, log, emit flags.

It does five things:

  1. Assembles the request from `_pipeline/prompts/` (four shared files + one grain file) and
     the unit's extracted context, with `ground_truth` removed.
  2. Queries one pinned model at temperature 0 with a fixed seed, and records the backend
     configuration (`system_fingerprint`) the provider reports for every reply.
  3. Logs every request and every raw response to `_pipeline/llm_out/<run>/responses.jsonl`.
  4. Parses the responses into `(grain, unit_id, anomaly_type)` flag files that
     `_pipeline/score.py --ruleset llm` reads directly.
  5. Writes a run manifest recording everything needed to reproduce or audit the run.

Two guarantees are enforced in code rather than trusted to the operator:

  * **Dev/eval isolation.** `--side` is required and has no default. Units are selected by
    filtering the full extract through `_pipeline/out/split.json` — the same file
    `score.py --split` uses — so the harness and the scorer cannot disagree about which
    vendors belong to which side.
  * **No ground-truth leakage.** The `ground_truth` field is stripped from every context object
    and its absence is asserted immediately before the request is built. A unit whose payload
    still carries it aborts the run.

Scoring never re-queries the model: metrics come from the logged responses.

Run from the repo root:

    python _pipeline/llm_run.py \
        --side dev --model deepseek --json-mode --run-id <new_run_id>

    ... --side dev --model gpt --limit 20        # small trial
    ... --side dev --model gpt --dry-run          # assemble + log, no API call
    ... --side dev --model gpt --rebuild          # re-derive outputs from the log

API keys are read from `_pipeline/.env` (copy `_pipeline/.env.example`).

Re-running the same run directory resumes: units already present in `responses.jsonl` are
skipped, so an interrupted run continues instead of starting over and re-spending credit.
"""

import argparse
import glob
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "_pipeline" / "prompts"
OUT = ROOT / "_pipeline" / "out"
LLM_OUT = ROOT / "_pipeline" / "llm_out"
ENV_FILE = ROOT / "_pipeline" / ".env"

TEMPERATURE = 0            # fixed, not a CLI option — it must not vary between runs
# A fixed seed, sent only to providers that support it (see `supports_seed` below). Where it
# is supported it is best-effort, not a guarantee; it removes one source of run-to-run
# variation that is under our control, and `system_fingerprint` reports what remains.
SEED = 20260808
# Grain-vendor replies walk through every 30-day window before reaching the verdict fields;
# 1500 leaves room for that reasoning so replies are not cut off before risk_score/anomaly_type.
# It is a fixed setting, not a knob to fit results.
MAX_TOKENS = 1500
TIMEOUT_S = 120
MAX_RETRIES = 5

SHARED_FILES = [
    "company_profile.txt",
    "procurement_policy.txt",
    "approval_matrix.txt",
    "risk_rules.txt",
]

# Which anomaly types each grain may legitimately return. A value outside its grain is NOT
# dropped — the metric protocol requires malformed predictions to be scored as false positives, so it
# is passed through to the flag file and counted here for the manifest.
GRAIN_TYPES = {
    "po":     {"duplicate_payment", "approval_bypass", "abnormal_amount"},
    "vendor": {"po_splitting"},
    "audit":  {"vendor_master_mod"},
}

# Model registry. `snapshot_pinned` records whether the provider exposes a dated model id; where
# it does not, the logged responses plus the query timestamp are the canonical record instead
# (this asymmetry is disclosed, not silently normalised).
#
# `supports_seed` and `supports_json_mode` record the same kind of asymmetry one level down: the
# two providers do not offer the same reproducibility controls, and the manifest must say which
# controls were actually in force for each run. Both endpoints accept both parameters without an
# error, so acceptance cannot be used to detect support — a parameter a provider ignores would
# otherwise be recorded as if it had been applied, which would overstate reproducibility. The
# flags are therefore set from the providers' documentation, not from the HTTP status.
MODELS = {
    "gpt": {
        # A dated snapshot in the "mini" cost tier, so that one of the two models is pinned.
        "model_id": "gpt-5.4-mini-2026-03-17",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "env_key": "OPENAI_API_KEY",
        "snapshot_pinned": True,
        "supports_seed": True,
        "supports_json_mode": True,
        # This snapshot is a reasoning model; unlike deepseek-v4-flash it defaults to reasoning
        # OFF (`reasoning_effort: "none"`), but the temperature constraint below is conditional
        # on that default actually being in effect, so it is requested explicitly rather than
        # relied on — the same "don't trust the default" reasoning already applied to DeepSeek's
        # thinking flag. Confirmed by probing the live API: `reasoning_effort: "none"` is the
        # only setting under which `temperature: 0` is accepted at all — any non-"none" effort
        # forces temperature to the provider's default (1) and TEMPERATURE=0 would be rejected
        # outright (HTTP 400), which is what makes this non-optional rather than a preference.
        "extra_body": {"reasoning_effort": "none"},
        # Chat Completions renamed this parameter for the GPT-5 family; the old name is rejected
        # outright (HTTP 400) rather than ignored, so it cannot be left as a shared constant.
        "max_tokens_param": "max_completion_tokens",
        # Observed empty on every call during testing — this model does not expose the field the
        # 4.1 generation did (OpenAI appears to have dropped it going into GPT-5.x). Not a sign of
        # instability: manifest logic already treats a missing fingerprint as "(none reported)",
        # which is the accurate reading here.
    },
    "deepseek": {
        "model_id": "deepseek-v4-flash",
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "env_key": "DEEPSEEK_API_KEY",
        "snapshot_pinned": False,
        # DeepSeek's API reference states the seed parameter is no longer supported and does
        # not take effect. The request still returns 200 when it is sent, so it is withheld
        # here rather than sent and reported as a control that was applied.
        "supports_seed": False,
        "supports_json_mode": True,
        # deepseek-v4-flash defaults to thinking mode ON; disabled here to match this
        # project's non-reasoning, single-shot design.
        "extra_body": {"thinking": {"type": "disabled"}},
    },
}


# ---------------------------------------------------------------- helpers

def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def load_env():
    """Read _pipeline/.env without printing or logging any value."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def git_head():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return None


def assert_frozen_for_eval():
    """--side eval is refused unless `_pipeline/frozen/` is fully committed.

    The second freeze (prompt + risk bands) is only meaningful if the eval split is never
    queried before the prompt and rubric are frozen. This makes "frozen" a git fact: everything under `_pipeline/frozen/` must be tracked
    and have no uncommitted changes, which is exactly what landing the freeze commit produces, so
    the gate opens itself the moment that commit exists and stays shut with no separate step to
    remember or forget."""
    result = subprocess.run(["git", "status", "--porcelain", "--", "_pipeline/frozen/"],
                            cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0 or result.stdout.strip():
        dirty = result.stdout.strip() or "(git status could not be checked)"
        sys.exit(
            "REFUSING --side eval: _pipeline/frozen/ is not fully committed:\n" + dirty +
            "\n\nThe eval split must not be queried before the prompt freeze is committed. "
            "Commit everything under _pipeline/frozen/ first, then re-run.")


def dataset_db_from_extract_manifest():
    # Read the extract DB name from _pipeline/out/manifest.json so the run manifest always
    # records the database the extract actually came from.
    manifest_path = OUT / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data.get("db")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- prompt assembly

def build_prompt(grain, variant):
    """Concatenate the four shared files then the grain file, in the order listed in frozen/prompt_v1.md."""
    grain_file = {
        "po": "grain_po.txt",
        "vendor": "grain_vendor.txt",
        "audit": f"grain_audit_{variant}.txt",
    }[grain]
    parts, used = [], []
    for name in SHARED_FILES + [grain_file]:
        p = PROMPTS / name
        if not p.exists():
            sys.exit(f"missing prompt file: {p}")
        parts.append(p.read_text(encoding="utf-8").strip())
        used.append(name)
    return "\n\n".join(parts), used


# ---------------------------------------------------------------- unit selection

def select_units(side, grains):
    """Units for one side, chosen by filtering the full extract through split.json.

    Every grain keys onto a vendor id, so one membership test serves all three.
    """
    split_path = OUT / "split.json"
    if not split_path.exists():
        sys.exit(f"missing split file: {split_path} — run make_split.py first")
    split = json.load(open(split_path, encoding="utf-8"))
    if side not in split:
        sys.exit(f"--side {side} not present in split.json")
    keep = {str(v) for v in split[side]}
    other = {str(v) for v in split["eval" if side == "dev" else "dev"]}
    if keep & other:
        sys.exit("split.json sides overlap — refusing to run")

    units = []
    if "po" in grains:
        for f in sorted(glob.glob(str(OUT / "po" / "*.json"))):
            d = json.load(open(f, encoding="utf-8"))
            if str(d["po_identity"]["vendor"]["id"]) in keep:
                units.append(("po", d["po_identity"]["po_number"], f))
    if "vendor" in grains:
        for f in sorted(glob.glob(str(OUT / "vendor" / "*.json"))):
            d = json.load(open(f, encoding="utf-8"))
            if str(d["vendor"]["id"]) in keep:
                units.append(("vendor", str(d["vendor"]["id"]), f))
    if "audit" in grains:
        for f in sorted(glob.glob(str(OUT / "vendor_audit" / "*.json"))):
            d = json.load(open(f, encoding="utf-8"))
            if str(d["vendor"]["id"]) in keep:
                units.append(("audit", str(d["vendor"]["id"]), f))
    return units


def load_context(path):
    """Load a context object with ground_truth removed. Absence is asserted, not assumed."""
    d = json.load(open(path, encoding="utf-8"))
    d.pop("ground_truth", None)
    payload = json.dumps(d, ensure_ascii=False, indent=1, sort_keys=False)
    if "ground_truth" in payload or "injected_anomaly" in payload:
        sys.exit(f"LEAKAGE GUARD: ground truth still present in payload for {path}")
    return payload


# ---------------------------------------------------------------- model call

def call_model(cfg, api_key, prompt, payload, json_mode=False):
    body = {
        "model": cfg["model_id"],
        "temperature": TEMPERATURE,
        cfg.get("max_tokens_param", "max_tokens"): MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt + "\n\nRECORD\n\n" + payload}],
    }
    if cfg["supports_seed"]:
        body["seed"] = SEED
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    body.update(cfg.get("extra_body", {}))
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(cfg["endpoint"], headers=headers, json=body, timeout=TIMEOUT_S)
            if r.status_code == 200:
                return r.json(), None
            # 429 and 5xx are transient; anything else is a real error worth surfacing
            if r.status_code not in (429, 500, 502, 503, 504):
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            last = f"HTTP {r.status_code}"
        except Exception as e:                       # network-level failure
            last = f"{type(e).__name__}: {e}"
        time.sleep(min(2 ** attempt, 30))
    return None, f"gave up after {MAX_RETRIES} attempts ({last})"


# ---------------------------------------------------------------- response parsing

FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

# A member separator the model forgot. JSON forbids a literal newline inside a string, so a raw
# newline is always structural — which makes "value-end, newline, next key" an unambiguous place
# for a missing comma. Applied only after strict parsing has already failed, and the result is
# kept only if it then parses, so a bad guess degrades to an unparseable reply rather than to a
# wrong reading. This keeps a well-formed verdict from being discarded over one absent comma.
MISSING_COMMA = re.compile(r'([}\]"0-9el])([ \t]*\r?\n\s*)(")')

# The opposite mistake: a comma left before a closing bracket. Also JSON-illegal, also only
# resolvable by removing rather than inserting a character, so it needs its own pass rather than
# reusing MISSING_COMMA. It tolerates a trailing comma so a well-formed verdict is not discarded.
TRAILING_COMMA = re.compile(r',(\s*[}\]])')


def extract_json(text):
    """Recover one JSON object from the reply, tolerating fences, stray prose, a lost comma, or
    a trailing one."""
    if not text:
        return None
    t = FENCE.sub("", text.strip())
    missing_fixed = MISSING_COMMA.sub(r"\1,\2\3", t)
    candidates = (t, missing_fixed, TRAILING_COMMA.sub(r"\1", t),
                 TRAILING_COMMA.sub(r"\1", missing_fixed))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            pass
        depth, start = 0, None
        for i, ch in enumerate(candidate):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(candidate[start:i + 1])
                    except Exception:
                        start = None
    return None


def normalise_parsed(parsed, grain, unit_id):
    """Apply the response contract to one parsed object.

    Returns `(parsed, violation, wrong_type)`. The identifiers are authoritative here, not the
    model's echo of them. Shared by the live path and by the rebuild path so the two cannot
    read the same reply differently.
    """
    parsed["grain"] = grain
    parsed["unit_id"] = unit_id
    score = str(parsed.get("risk_score", "")).strip().lower()
    atype = parsed.get("anomaly_type")
    violation = wrong_type = None
    if score not in ("low", "medium", "high"):
        violation = f"risk_score={parsed.get('risk_score')!r}"
    elif score in ("medium", "high") and not atype:
        violation = "flagged band with null anomaly_type"
    elif atype and atype not in GRAIN_TYPES[grain]:
        # passed through to the flag file on purpose (scored as a false positive)
        wrong_type = atype
    parsed["risk_score"] = score
    return parsed, violation, wrong_type


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="LLM harness")
    ap.add_argument("--side", required=True, choices=["dev", "eval"],
                    help="REQUIRED. Which split side to run. No default, by design.")
    ap.add_argument("--model", required=True, choices=sorted(MODELS))
    ap.add_argument("--variant", default="B", choices=["B"],
                    help="Audit-level prompt file (grain_audit_B.txt).")
    ap.add_argument("--grain", default="po,vendor,audit",
                    help="comma-separated subset of po,vendor,audit")
    ap.add_argument("--limit", type=int, default=None, help="stop after N units (trial runs)")
    ap.add_argument("--units-file", default=None,
                    help="optional: restrict to specific units for a targeted trial. "
                         "Plain text, one 'grain|unit_id' per line (e.g. 'po|P00179'), "
                         "'#' comments allowed. Still filtered through --side's split "
                         "membership first, so this cannot be used to sneak eval units "
                         "into a dev run or vice versa.")
    ap.add_argument("--run-id", default=None, help="resume or name a run directory")
    ap.add_argument("--json-mode", action="store_true",
                    help="ask the provider to constrain the reply to a JSON object "
                         "(response_format). OFF by default; the frozen configuration "
                         "uses it. Recorded in the manifest either way.")
    ap.add_argument("--dry-run", action="store_true",
                    help="assemble and log requests without calling the API")
    ap.add_argument("--rebuild", action="store_true",
                    help="regenerate flags and manifest from an existing run's logged replies "
                         "without calling the API. Use after a parser fix: replies an earlier "
                         "run could not read are recovered from the log instead of re-bought.")
    args = ap.parse_args()

    if args.side == "eval" and not args.rebuild:
        assert_frozen_for_eval()

    grains = [g.strip() for g in args.grain.split(",") if g.strip()]
    for g in grains:
        if g not in GRAIN_TYPES:
            sys.exit(f"unknown grain: {g}")

    cfg = MODELS[args.model]
    if args.json_mode and not cfg["supports_json_mode"]:
        sys.exit(f"--json-mode: {args.model} does not support it. Sending it anyway would "
                 f"record a control in the manifest that was never applied.")
    load_env()
    api_key = os.environ.get(cfg["env_key"])
    if not api_key and not (args.dry_run or args.rebuild):
        sys.exit(f"{cfg['env_key']} not set (expected in {ENV_FILE})")

    run_id = args.run_id or f"{args.side}_{args.model}_{args.variant}"
    run_dir = LLM_OUT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    resp_path = run_dir / "responses.jsonl"

    if args.rebuild:
        if not resp_path.exists():
            sys.exit(f"--rebuild: nothing to rebuild from, no log at {resp_path}")
        prompts = {g: build_prompt(g, args.variant) for g in grains}
        # Do NOT re-dump the prompt texts here. The copies in the run directory are the archive of
        # what this run actually sent; rewriting them from the current prompt files would replace
        # that record with a prompt the run never used.
        print(f"rebuild {run_id}: re-reading {resp_path.name}, no API calls")
        # Rebuild must not overwrite the run's provenance. Left to itself this path would stamp
        # `started_utc`/`finished_utc` with today and drop `units_selected`, so a manifest
        # regenerated to fix one field would silently lose three others. Carry the recorded
        # values forward from the manifest already on disk: they describe the invocation that
        # actually queried the model, which a rebuild does not repeat and cannot re-observe.
        #
        # Deriving them from the log instead would be wrong on a resumed run: when a run is
        # resumed, `responses.jsonl` accumulates the earlier attempt's replies too, so the
        # earliest logged `ts` is not when the recorded invocation began. Falling back to the
        # log is therefore only for a run that has no manifest yet.
        prior = {}
        manifest_path = run_dir / "run_manifest.json"
        if manifest_path.exists():
            try:
                prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                prior = {}

        ts_values = []
        logged_json_modes = set()
        for line in resp_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ts"):
                ts_values.append(rec["ts"])
            if "json_mode" in rec:
                logged_json_modes.add(bool(rec["json_mode"]))

        rebuilt_started = prior.get("started_utc") or (min(ts_values) if ts_values else now_iso())
        rebuilt_finished = prior.get("finished_utc") or (max(ts_values) if ts_values else now_iso())

        # The logged records are the record of how the replies were obtained, so they decide
        # json_mode here rather than the command line. Only an unambiguous log overrides it; a
        # log mixing both modes falls back to the flag rather than picking one silently.
        rebuilt_json_mode = (logged_json_modes.pop() if len(logged_json_modes) == 1 else None)

        rebuilt_n_units = prior.get("units_selected")
        if rebuilt_n_units is None:
            units_path = run_dir / "units.txt"
            if units_path.exists():
                rebuilt_n_units = sum(
                    1 for l in units_path.read_text(encoding="utf-8").splitlines() if l.strip())

        carry = {k: prior[k] for k in CARRIED_PROVENANCE if k in prior}
        write_outputs(run_dir, resp_path, run_id, args, cfg, prompts,
                      rebuilt_started, rebuilt_n_units, finished=rebuilt_finished,
                      json_mode=rebuilt_json_mode, carry=carry)
        return

    # resume: skip units that already have a REPLY. A unit whose only record is an API error is
    # deliberately not skipped — it has no verdict, and a unit with no verdict contributes no flag,
    # so leaving it unanswered silently scores it as a missed detection. Retrying it is the whole
    # point of resuming. (Observed: a run left 8 units unanswered after transient errors and a
    # second invocation skipped all 8, because every logged record counted as done.)
    done, retry = set(), set()
    if resp_path.exists():
        logged_models, logged_modes = set(), set()
        for line in resp_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                # A dry run logs a request without ever calling the model — write_outputs and
                # aggregate_runs.py both already skip these records because they carry no verdict.
                # Resume must do the same: without this, a --dry-run under the same --run-id as
                # the real run (or a second --dry-run) marks every unit "done" from a log entry
                # that answers nothing, and the real run silently sends zero requests.
                if rec.get("dry_run"):
                    continue
                key = (rec["grain"], rec["unit_id"])
                if rec.get("error"):
                    retry.add(key)
                else:
                    done.add(key)
                if rec.get("model"):
                    logged_models.add(rec["model"])
                logged_modes.add(bool(rec.get("json_mode")))
            except Exception:
                continue
        retry -= done                      # already answered on a later line
        if retry:
            print(f"retrying {len(retry)} unit(s) that previously failed with an API error")
        # Same argument as the model check below: JSON mode changes what the model returns,
        # so a directory must not end up holding half its replies from each setting.
        if logged_modes - {bool(args.json_mode)}:
            sys.exit(f"REFUSING TO RESUME: {resp_path} holds replies logged with "
                     f"json_mode={sorted(logged_modes)}, but this run is "
                     f"json_mode={bool(args.json_mode)}. Use a fresh --run-id.")
        # A run directory holds the replies of exactly one model. Resuming into a log written
        # by a different one would let the skip-list carry the old model's verdicts into this
        # run's flags while the manifest names only the current model.
        stale = logged_models - {cfg["model_id"]}
        if stale:
            sys.exit(f"REFUSING TO RESUME: {resp_path} already holds replies from "
                     f"{sorted(stale)}, but this run is {cfg['model_id']}. Start a fresh "
                     f"--run-id, or delete the directory if the old replies are throwaway.")
        if done:
            print(f"resuming: {len(done)} unit(s) already logged in {resp_path.name}")

    prompts = {g: build_prompt(g, args.variant) for g in grains}
    dump_prompts(run_dir, prompts)
    units = select_units(args.side, grains)

    if args.units_file:
        wanted = set()
        for line in pathlib.Path(args.units_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            g, u = line.split("|", 1)
            wanted.add((g.strip(), u.strip()))
        units = [u for u in units if (u[0], u[1]) in wanted]
        missing = wanted - {(u[0], u[1]) for u in units}
        if missing:
            print(f"warning: {len(missing)} requested unit(s) not in --side {args.side}: "
                  f"{sorted(missing)}")

    if args.limit:
        units = units[:args.limit]

    # The unit list this run is responsible for, written before any querying. Completeness is then
    # checked against the actual identifiers rather than against a count, which is what makes the
    # check work on a resumed or rebuilt run too: neither of those re-derives the selection.
    units_path = run_dir / "units.txt"
    selected = [f"{g}|{u}" for g, u, _ in units]
    if units_path.exists():
        previous = [l for l in units_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if previous != selected and not args.limit:
            sys.exit(f"REFUSING TO RESUME: {units_path} lists {len(previous)} unit(s) but this "
                     f"invocation selected {len(selected)}. The selection changed under the same "
                     f"run id (different --grain, --units-file, or a changed split). Use a fresh "
                     f"--run-id.")
    elif not args.limit:
        units_path.write_text("\n".join(selected) + "\n", encoding="utf-8", newline="\n")

    print(f"run {run_id}: {len(units)} unit(s), model {cfg['model_id']}, temperature {TEMPERATURE}")
    if args.dry_run:
        print("DRY RUN - no API calls will be made")

    counts = {"ok": 0, "skipped": 0, "unparsed": 0, "api_error": 0,
              "contract_violation": 0, "wrong_grain_type": 0}
    started = now_iso()
    tokens_in = tokens_out = 0

    with open(resp_path, "a", encoding="utf-8") as log:
        for i, (grain, unit_id, path) in enumerate(units, 1):
            if (grain, unit_id) in done:
                counts["skipped"] += 1
                continue

            prompt, _ = prompts[grain]
            payload = load_context(path)
            rec = {"ts": now_iso(), "grain": grain, "unit_id": unit_id,
                   "model": cfg["model_id"], "temperature": TEMPERATURE,
                   "seed": SEED if cfg["supports_seed"] else None,
                   "json_mode": bool(args.json_mode),
                   "variant": args.variant, "side": args.side,
                   "prompt_sha256": sha256_text(prompt),
                   "payload_sha256": sha256_text(payload)}

            if args.dry_run:
                rec["dry_run"] = True
                log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                log.flush()
                continue

            raw, err = call_model(cfg, api_key, prompt, payload, json_mode=args.json_mode)
            if err:
                rec["error"] = err
                counts["api_error"] += 1
            else:
                content = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
                usage = raw.get("usage") or {}
                tokens_in += usage.get("prompt_tokens") or 0
                tokens_out += usage.get("completion_tokens") or 0
                rec["raw_response"] = content
                rec["usage"] = usage
                rec["response_model"] = raw.get("model")
                # The provider's identifier for the backend configuration that served this
                # call. It is the only signal available for silent version drift within a
                # single model id, which matters most for the provider that exposes no dated
                # id at all. Absent from a provider's reply means "not offered", not "stable".
                rec["system_fingerprint"] = raw.get("system_fingerprint")

                parsed = extract_json(content)
                if parsed is None:
                    rec["parse_error"] = "no JSON object found"
                    counts["unparsed"] += 1
                else:
                    parsed, violation, wrong_type = normalise_parsed(parsed, grain, unit_id)
                    if violation:
                        rec["contract_violation"] = violation
                        counts["contract_violation"] += 1
                    if wrong_type:
                        rec["wrong_grain_type"] = wrong_type
                        counts["wrong_grain_type"] += 1
                    rec["parsed"] = parsed
                    counts["ok"] += 1

            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            if i % 25 == 0 or i == len(units):
                print(f"  {i}/{len(units)}  ok={counts['ok']} err={counts['api_error']} "
                      f"unparsed={counts['unparsed']}")

    if args.dry_run:
        print("dry run complete - requests logged, no flags written")
        return

    write_outputs(run_dir, resp_path, run_id, args, cfg, prompts, started, len(units))


def dump_prompts(run_dir, prompts):
    """Write each grain's fully assembled prompt text to the run directory once per run, so
    every request can be reproduced exactly (frozen/prompt_v1.md). Writes the same string that
    gets hashed into assembled_prompt_sha256, and is idempotent: re-running or resuming the
    same run id overwrites each file with identical content."""
    for g, (prompt, _) in prompts.items():
        (run_dir / f"prompt_{g}.txt").write_text(prompt, encoding="utf-8", newline="\n")


# Manifest fields that record the state of the world when the model was actually queried, and
# that nothing in the log can reconstruct: the code revision, the prompt texts, the split, the
# dataset, the output cap. A rebuild re-reads a log; it does not re-observe the world, so it must
# carry these forward from the manifest it is regenerating rather than sample them again. Left to
# recompute, they would silently drift the moment a commit lands or a prompt file is edited — and
# nine manifests would then attest to a run that never happened that way.
CARRIED_PROVENANCE = ("git_head_at_run", "prompt_files", "assembled_prompt_sha256", "max_tokens",
                      "split_file", "split_sha256", "dataset_db", "dataset_commit",
                      "model_key", "model_id", "snapshot_pinned", "side", "variant", "grains")


def write_outputs(run_dir, resp_path, run_id, args, cfg, prompts, started, n_units, finished=None,
                  json_mode=None, carry=None):
    """Derive flag files and the manifest from the logged responses only.

    Every figure is recomputed from the log rather than carried over from the run loop, so a
    `--rebuild` after a parser fix yields the manifest a fresh run would have produced from the
    same replies — without re-querying the model.

    `finished` defaults to None, meaning "use now_iso()" (the live-run path: the run just
    finished, so now is correct). The rebuild path passes the true finish time recovered from
    the log instead, so re-running --rebuild does not keep advancing finished_utc to today.
    """
    flags, flags_high = [], []
    counts = {"ok": 0, "unparsed": 0, "api_error": 0, "contract_violation": 0,
              "wrong_grain_type": 0, "recovered_from_log": 0}
    tokens_in = tokens_out = 0
    seen = 0
    fingerprints, response_models = {}, {}

    # A unit that failed once and succeeded on a later attempt is answered, not failed. Collect
    # the answered units first so `api_error` counts units still without a reply, which is the
    # number that matters: those are the ones scored as missed detections.
    answered = set()
    for line in resp_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not rec.get("dry_run") and not rec.get("error"):
            answered.add((rec.get("grain"), rec.get("unit_id")))
    answered_keys = {f"{g}|{u}" for g, u in answered}    # same shape as units.txt

    for line in resp_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("dry_run"):
            continue
        if rec.get("error"):
            if (rec.get("grain"), rec.get("unit_id")) not in answered:
                counts["api_error"] += 1      # still unanswered after every attempt
            continue

        usage = rec.get("usage") or {}
        tokens_in += usage.get("prompt_tokens") or 0
        tokens_out += usage.get("completion_tokens") or 0

        # Counted per reply, not de-duplicated away: more than one value means the backend
        # changed mid-run, and the counts show how the run divides between them.
        fp = rec.get("system_fingerprint")
        fingerprints[fp if fp else "(none reported)"] = \
            fingerprints.get(fp if fp else "(none reported)", 0) + 1
        rm = rec.get("response_model")
        if rm:
            response_models[rm] = response_models.get(rm, 0) + 1

        p = rec.get("parsed")
        violation = rec.get("contract_violation")
        wrong_type = rec.get("wrong_grain_type")
        if not p and rec.get("raw_response"):
            # Second chance from the log itself, not from the network: a reply an earlier run
            # could not parse may be recoverable now. Same logged text, re-read.
            retry = extract_json(rec["raw_response"])
            if retry is not None:
                p, violation, wrong_type = normalise_parsed(retry, rec["grain"], rec["unit_id"])
                counts["recovered_from_log"] += 1
        if not p:
            counts["unparsed"] += 1
            continue

        seen += 1
        counts["ok"] += 1
        if violation:
            counts["contract_violation"] += 1
        if wrong_type:
            counts["wrong_grain_type"] += 1

        score, atype = p.get("risk_score"), p.get("anomaly_type")
        if not atype:
            continue
        if score in ("medium", "high"):
            flags.append(f"{p['grain']}|{p['unit_id']}|{atype}")
        if score == "high":
            flags_high.append(f"{p['grain']}|{p['unit_id']}|{atype}")

    (run_dir / "flags.txt").write_text("\n".join(flags) + ("\n" if flags else ""),
                                       encoding="utf-8")
    (run_dir / "flags_high_only.txt").write_text(
        "\n".join(flags_high) + ("\n" if flags_high else ""), encoding="utf-8")

    manifest = {
        "run_id": run_id,
        "started_utc": started,
        "finished_utc": finished if finished is not None else now_iso(),
        "side": args.side,
        "variant": args.variant,
        "grains": sorted(prompts),
        "model_key": args.model,
        "model_id": cfg["model_id"],
        "snapshot_pinned": cfg["snapshot_pinned"],
        "snapshot_note": (
            "Provider exposes a dated model id; the id alone pins the version."
            if cfg["snapshot_pinned"] else
            "Provider exposes no dated model id. The logged responses plus the per-call "
            "timestamps are the canonical record of what answered."
        ),
        "temperature": TEMPERATURE,
        "seed": SEED if cfg["supports_seed"] else None,
        "seed_note": (
            "A fixed seed was sent with every request. The provider describes it as "
            "best-effort, so it reduces run-to-run variation without guaranteeing identical "
            "replies." if cfg["supports_seed"] else
            "No seed was sent. This provider's API reference states the seed parameter is no "
            "longer supported and does not take effect, so sending it would have recorded a "
            "control that was never applied. The two models therefore run under different "
            "reproducibility controls, which is disclosed rather than normalised."
        ),
        # On a rebuild this comes from the log, not from the command line: --json-mode describes
        # how the replies were obtained, and a rebuild does not re-obtain them. Taking it from
        # args would let a rebuild invoked without the flag record that a run used plain decoding
        # when the logged records show otherwise.
        "json_mode": bool(getattr(args, "json_mode", False)) if json_mode is None else json_mode,
        "max_tokens": MAX_TOKENS,
        "system_fingerprints": fingerprints,
        "response_models": response_models,
        "drift_note": (
            "`system_fingerprints` counts the backend configuration the provider reported "
            "for each reply in this run. One value means every reply was served by the same "
            "configuration. More than one value means the backend changed during the run, "
            "and the run is not a single-configuration measurement. '(none reported)' means "
            "the provider does not expose the field, which is not evidence of stability. "
            "`response_models` records the model id the provider itself returned, which can "
            "differ from the requested id when an alias resolves to a dated snapshot."
        ),
        "prompt_files": {name: sha256_file(PROMPTS / name)
                         for name in sorted(os.listdir(PROMPTS))
                         if name.endswith(".txt")},
        "assembled_prompt_sha256": {g: sha256_text(p) for g, (p, _) in prompts.items()},
        "split_file": "_pipeline/out/split.json",
        "split_sha256": sha256_file(OUT / "split.json"),
        "dataset_db": dataset_db_from_extract_manifest(),
        "dataset_commit": "54ec541",
        "git_head_at_run": git_head(),
        "units_selected": n_units,
        "responses_logged": seen,
        "rebuilt_from_log": bool(args.rebuild),
        "counts": counts,
        "recovery_note": (
            "`recovered_from_log` counts replies that strict JSON parsing rejected and the "
            "comma-repair step then recovered. The logged reply is unchanged; only the derived "
            "verdict is re-read from it."
        ),
        "tokens": {"prompt": tokens_in, "completion": tokens_out},
        "ground_truth_sent": False,
        "flags_file": "flags.txt",
        "flags_high_only_file": "flags_high_only.txt",
        "operating_point": "flag = medium OR high; high-only reported as sensitivity",
    }
    # Restore the as-executed provenance last, so a rebuild cannot overwrite it with today's
    # world (see CARRIED_PROVENANCE). Empty on a live run, which observes it for real.
    manifest.update(carry or {})
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # A unit with no verdict contributes no flag, so an incomplete run does not fail loudly — it
    # scores as a set of missed detections. Two separate defects reached results this way before
    # the check existed: replies truncated by the output limit, and units skipped on resume after
    # an API error. The run is therefore refused rather than reported when anything is unanswered.
    units_path = run_dir / "units.txt"
    if units_path.exists():
        expected = {l for l in units_path.read_text(encoding="utf-8").splitlines() if l.strip()}
        missing = expected - answered_keys
        if missing:
            print(f"\nINCOMPLETE RUN: {len(missing)} of {len(expected)} unit(s) have no verdict.")
            for m in sorted(missing)[:10]:
                print(f"  {m}")
            if len(missing) > 10:
                print(f"  ... and {len(missing) - 10} more")
            print("Re-run the same --run-id to retry them. Flag files were written but MUST NOT "
                  "be scored: an unanswered unit counts as a missed detection.")
            sys.exit(1)

    print(f"\nwrote {run_dir}")
    print(f"  responses.jsonl   {seen} parsed"
          + (f" (incl. {counts['recovered_from_log']} recovered by comma repair)"
             if counts["recovered_from_log"] else ""))
    print(f"  flags.txt         {len(flags)} flagged (medium or high)")
    print(f"  flags_high_only.txt {len(flags_high)} flagged (high only)")
    print(f"  run_manifest.json")
    print(f"\nscore with:\n  python _pipeline/score.py "
          f"--flags {run_dir / 'flags.txt'} --ruleset llm "
          f"--split _pipeline/out/split.json --side {args.side}")


if __name__ == "__main__":
    main()
