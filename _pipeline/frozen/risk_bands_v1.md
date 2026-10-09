# Risk-Band Rubric

**STATUS: FROZEN.** This file documents the risk-band definitions and the band-to-flag mapping
used in scoring. It is a **researcher-facing** reference, not model input: the operative wording
the model receives lives in the prompt files under `_pipeline/prompts/`. This file is not sent to
the model.

## 1. Purpose

This file documents the three-band scale (`low` / `medium` / `high`) that the LLM's
`risk_score` output takes, and the rule that maps a band to the binary flag/no-flag decision used
for scoring. Bands are kept **qualitative** — evidence-strength reasoning, not numeric cutoffs —
to preserve the LLM-vs-SQL distinction (evidence parity); hard numeric cutoffs are the SQL
baseline's job (`baseline.sql`), not this file or the prompts.

## 2. General band definitions

Bands combine **evidence strength × context**:

- **`high`** — anomaly signal present AND corroborating context → strong recommend-to-review.
- **`medium`** — part of what the finding requires is present, and the rest is neither confirmed
  nor ruled out by anything the record shows → still routed to review, at lower priority than
  `high`.
- **`low`** — no meaningful signal, or an ordinary business explanation accounts for what is
  present → no review needed.

`medium`'s criterion is evidence strength, not the analyst's own uncertainty: a case an ordinary
business explanation already accounts for is `low`, not `medium`, however unsure the model is
about it. The prompt text in `_pipeline/prompts/risk_rules.txt` matches this definition.

## 3. Operating point

- **Flag = `medium` OR `high`.**
- Sensitivity is reported as a comparison across the number of repeated runs (`k`) that the
  majority-vote aggregator combines, not as a high-only restriction.
- This maps the ordinal band to the binary decision the SQL baseline also makes, for RQ2 parity.

## 4. Per-scenario criteria

Per-scenario band criteria are written behaviourally in each scenario's grain file under
`_pipeline/prompts/`, so each criterion exists in exactly one place. See those files for what
distinguishes `low`/`medium`/`high` for `duplicate_payment`, `po_splitting`, `vendor_master_mod`,
`approval_bypass`, and `abnormal_amount`.

`vendor_master_mod` is scoped to the bank-account channel only: the bank account is the field
that decides where money goes, so a case labelled vendor-master fraud is one where a payment could
be diverted. A tax-identifier change is a different scheme — a permanent change to the supplier's
identity rather than an edit timed around a payment — and is out of scope. The generated data
contains no tax-ID audit edits, so the excluded channel does not arise in scoring either.

## 5. Grounding (concept only — no numbers imported from these papers)

The ordinal low/medium/high band reflects standard auditor red-flag prioritization:

- **ISA 240** — fraud risk factors / red flags.
- **Al-Dhubaibi & Sharaf-Addin (2022)** — auditors weight red flags differently, supporting ordinal prioritization rather than a flat binary.
- **Munteanu et al. (2024)** — per-indicator thresholds, where multiple medium signals compound into a higher risk profile.
- **COSO** risk assessment and **Vasarhelyi et al. (2004)** analytic-monitoring exception prioritization further support ordinal triage over a single binary cut.

## 6. Decision note

Three bands are used because the evaluation set is small (13 positives per grain for
`po_splitting` and `vendor_master_mod` on the eval side of the dev/eval split): more levels would
leave each cell with too little n to measure, and the band must collapse to a binary flag for the
RQ2 comparison anyway. Three levels also match auditor triage / HITL practice.

## 7. Development-split validation

The operating point in §3 was checked against the development split under three criteria: both
flagging bands must be used, the two flag sets under `medium`-or-`high` and `high`-only must
differ, and precision must not invert between bands where both are populated. Criteria 1 and 2
pass at every grain. Criterion 3 passes at the purchase-order and vendor-audit-trail grains. At
the vendor grain it inverts: all four true positives sit in `medium`, and the one `high` verdict
is a false positive, so `high`-only would drop `po_splitting` recall to 0.00. This is why the
operating point is `medium`-or-`high`.
