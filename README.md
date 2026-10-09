# AI-Enhanced ERP Procurement Monitoring for SMEs — Artifacts

Artifacts for the ICSeB 2026 paper *"AI-Enhanced ERP Procurement Process Monitoring and Decision Support Framework for SMEs: A Case Study on Odoo ERP"*.

The repository holds the dataset-generation procedure, the extraction pipeline and its JSON schemas, the frozen LLM prompts, the rule-based SQL baseline, the evaluation scripts, and the evaluated data. All procurement data is synthetic. It contains no real company or personal data.

## Layout

| Paper section | Folder / file |
|---|---|
| §3, §4.1 Extraction pipeline | `_pipeline/extract.py`, `_pipeline/sql/`, `_pipeline/schema/`, `_pipeline/config.example.json` |
| §4.2 Synthetic dataset and anomaly scenarios | `_pipeline/generate/` (`anomaly_spec.md`, `params.json`, `params_derivation.md`, generator code), `_pipeline/generate/gen_out/` (plan and ground-truth labels) |
| §4.3 Rule-based SQL baseline | `_pipeline/generate/baseline.sql` |
| §4.4 Prompt design and risk scoring | `_pipeline/prompts/` (frozen prompt files), `_pipeline/frozen/` (SHA-256 manifest, risk bands) |
| §5.1 Evaluation | `_pipeline/llm_run.py`, `_pipeline/aggregate_runs.py`, `_pipeline/score.py`, `_pipeline/mcnemar_scenario.py`, `_pipeline/make_split.py` |
| Evaluated data | `_pipeline/out/` (`po_context.jsonl`, `vendor.jsonl`, `audit.jsonl`, `split.json`, `flags/sql_baseline.flags`) |
| LLM outputs (evaluation split) | `_pipeline/llm_out/eval_*` (every request and response, with a run manifest), `_pipeline/llm_out/_majority_eval_*.flags` (k=5 majority vote) |

## Requirements

- Odoo 19 Community with its PostgreSQL (only to regenerate the dataset)
- Python 3.11+ with `psycopg2-binary` and `jsonschema`
- `psql` for the SQL baseline
- API keys for DeepSeek and/or OpenAI (only to re-query the LLMs)

Copy `_pipeline/config.example.json` to `_pipeline/config.json`, `_pipeline/generate/gen_config.example.json` to `_pipeline/generate/gen_config.json`, and `_pipeline/.env.example` to `_pipeline/.env`, then fill in the placeholders. Run all commands from the repository root.

## Re-scoring the published results (no database or API needed)

```bash
python _pipeline/score.py --flags _pipeline/llm_out/_majority_eval_ds_all.flags  --ruleset llm    --split _pipeline/out/split.json --side eval
python _pipeline/score.py --flags _pipeline/llm_out/_majority_eval_gpt_all.flags --ruleset llm    --split _pipeline/out/split.json --side eval
python _pipeline/score.py --flags _pipeline/out/flags/sql_baseline.flags         --ruleset frozen --split _pipeline/out/split.json --side eval
python _pipeline/mcnemar_scenario.py --llm _pipeline/llm_out/_majority_eval_ds_all.flags
```

## Full pipeline

1. **Generate the dataset** into a fresh, empty Odoo database (the seed is set in `params.json`):
   ```bash
   python _pipeline/generate/generate.py --dry-run --scale full
   python _pipeline/generate/generate.py --scale full
   psql -d <odoo_db> -f _pipeline/generate/fix_timestamps.sql
   ```
2. **Post-generation repair steps.** Run each with `--apply` after checking its dry run:
   `repair_cross_po.py` → `backfill_bill_refs.py` → `repair_s3_payment_diversion.py` → `add_legit_bank_accounts.py` → `repair_s3_canonical_timing.py`.
   `repair_s3_canonical_timing.py` uses record ids from the released dataset, so it applies only to a database built with the same seed and steps.
3. **Extract** the context JSON: `python _pipeline/extract.py`
4. **SQL baseline** (any scratch PostgreSQL database):
   ```bash
   psql -q -tAF'|' -d <scratch_db> -f _pipeline/generate/baseline.sql > _pipeline/out/flags/sql_baseline.flags
   ```
5. **Dev/eval split**, stratified by vendor class: `python _pipeline/make_split.py`
6. **LLM runs.** Make 5 runs per model and level group, each with a new run id:
   ```bash
   python _pipeline/llm_run.py --side eval --model deepseek --grain po,vendor --json-mode --run-id <id>
   python _pipeline/llm_run.py --side eval --model deepseek --grain audit     --json-mode --run-id <id>
   ```
7. **Majority vote and score:**
   ```bash
   python _pipeline/aggregate_runs.py --k 5 --strict-k --out <out.flags> --runs <id1> ... <id5>
   python _pipeline/score.py --flags <out.flags> --ruleset llm --split _pipeline/out/split.json --side eval
   ```

### Notes on the run manifests

`grain_audit_A.txt` and `grain_audit_C.txt` were alternative drafts of the audit-level prompt, compared on the development split; `grain_audit_B.txt` was selected. The two drafts were still in the prompt folder during the evaluation runs, so each run manifest lists their file hashes. They were never sent to the model. The audit-level runs record `variant: B`. In the purchase-order and vendor-level runs, the `variant` field has no effect, because those levels do not use the audit prompt.

LLM outputs vary between identical requests, even at temperature 0. Re-querying will not reproduce the logged responses exactly. The logged responses in `_pipeline/llm_out/` are the ones the paper reports.

## License

Code: MIT (see `LICENSE`). Data, prompts and documentation: CC BY 4.0.
