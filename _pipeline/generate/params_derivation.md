# params.json — derivation & calibration note

Companion to `_pipeline/generate/params.json` (freeze bundle). Explains how the hand-set
distributions produce the target normal-PO amount distribution, and which reference statistic
justifies each choice.

## 1. What is being calibrated

Amounts are **bottom-up**: each normal PO is built from lines, so
`amount_total = Σ(qty × unit_price)` with `unit_price ≈ standard_price` (tiny noise). This is a
deliberate design choice — Scenario 5 (abnormal amount) needs a real per-product `standard_price`
to deviate from, and `line_unit_price_vs_cost_ratio ≈ 1` must hold for normals.

The calibration lever is therefore the **product/line/qty distributions**, tuned so the *emergent*
PO-amount distribution lands on:

| Target | Value |
|---|---|
| median PO amount | ≈ USD 5,000 |
| p90 | ≈ 15,000–19,000 |
| fraction above 20,000 threshold | ≈ 5–10% (legitimately-approved large POs) |
| shape | right-skewed, log-normal-like |

## 2. The arithmetic

A single line contributes `qty × price`. With `qty` median ≈ 3 and product `price` median ≈ 700,
a typical single line ≈ 2,100. A PO has a median of ~2 lines (λ=2.2, truncated), so a rough
median-of-sums lands near 4,000–5,000. But summing lines is **not** additive on the tail: averaging
2–3 log-normal lines shrinks relative spread (central-limit effect), so to reach p90 ≈ 16k and a
~6% tail above 20k the **per-product sigma must be wide** (0.85 natural-log sd), not the ~0.55 a
naive single-line calc would suggest.

The parameters were fixed by Monte-Carlo rather than closed form (sum-of-log-normals has no clean
quantile). **Chosen set:** `standard_price ~ lognormal(median=700, σ_log=0.85, min=15)`,
`lines_per_po ~ truncPoisson(λ=2.2, [1,10])`, `qty_per_line ~ truncPoisson(λ=3, [1,20])`,
`unit_price = standard_price × N(1.0, 0.03)` clipped [0.9,1.1].

**Verified emergent distribution** (seed 123, 300,000 simulated POs):

| stat | value |
|---|---|
| median | 4,890 |
| mean | 7,079 |
| p75 | 9,533 |
| p90 | 15,834 |
| p95 | 20,872 |
| **fraction > 20,000** | **5.6%** |
| p90 / median | 3.24 |

All four targets met: median ≈ 5k, p90 in the 15–19k band, ~5.6% above threshold, right-skewed
(p90/median 3.24, close to Online Retail's invoice p90/median ≈ 3.1 ⇒ natural-log σ ≈ 0.89).
An independent Monte-Carlo should reproduce these within sampling noise.

## 3. Per-distribution reference justification

> **Shape only — no value is calibrated from the reference data.** The vetted public datasets (ADB, Online Retail II) do **not** represent SME B2B procurement, so none of their magnitudes is imported. Every scale/value below is a **hand-set SME design choice anchored to the frozen USD 20,000 threshold** (median PO ≈ $5,000, product ≈ $700 — *not* Online Retail's $2.10 unit price or $304 invoice, *not* ADB's $48,700 contract). The reference datasets corroborate only the qualitative **shape**: amounts are right-skewed, line counts are small integers, and SME vendors are fairly even (not 80/20). This is face-validity, not calibration.

- **product_standard_price** — Online Retail II unit price is right-skewed (median $2.10, p90 $7.95).
  Only the right-skew **shape** is borrowed; the scale (median $700) is a hand-set SME choice, not
  Online Retail's $2.10. σ_log = **0.85** was **tuned by Monte-Carlo to hit the 20k-threshold target**
  (median ≈5k, ~6% above 20k), then checked for plausibility — it sits far below ADB's development-bank
  ~2.2 and near Online Retail's ~0.89, i.e. the *target-driven* value is also shape-plausible.
- **lines_per_po** — Online Retail invoices carry median 16 lines, but those are retail baskets.
  B2B POs use **far fewer** lines (median ~2–3, cap 10): truncated Poisson λ=2.2.
- **qty_per_line** — Online Retail qty/line **median** (3) is borrowed: truncated Poisson λ=3, capped
  at 20. Only the median/shape transfers; the heavy retail tail (p90 24) is deliberately compressed —
  truncPoisson(3) has p90 ≈ 5, a tighter, more B2B-plausible tail.
- **unit_price_noise** — not a reference stat; a modeling choice. Tight N(1.0, 0.03) keeps normal
  `unit_price ≈ standard_price` so Scenario 5's `vs_cost_ratio` baseline is clean (~1.0).
- **vendor_assignment** — ADB shows a few frequent vendors + a long tail of occasional ones (77.6% of
  vendors had exactly one contract after stripping placeholder pseudo-vendors) that is **not** strict
  Pareto and is in fact fairly even. Modeled as normalized **finite** weights ∝ 1/rank^s over
  `n_vendors` = 50 vendors, sampled as a categorical (NOT `numpy.random.zipf`, which needs s>1 and is
  unbounded). At **s=0.6** this gives **top ≈10.0%, top-3 ≈21.7%, tail ≈1.0%** (Σ 1/k^0.6 over
  k=1..50 ≈ 10.05 ⇒ top share ≈0.0995; Monte-Carlo confirms 0.0995 / 0.2166). This is "a few frequent
  + many occasional", the ADB-corroborated shape the parameter exists to express. Frequent vendors
  still accumulate enough history for vendor baselines and scenario-2 clusters. With 50 vendors, each
  vendor holds few enough POs that routine clustering of ordinary small orders does not trip the R2
  splitting rule (with R2's 60%-of-threshold sibling floor, see `baseline.sql`).
- **order_timing** — ADB inter-arrival median ≈ 12 d / mean ≈ 34 d informs the cadence, but actual
  spacing is **count-governed**: each vendor's PO dates are placed across the 2025 window with an
  exponential inter-arrival whose mean = window_days / (that vendor's Zipf-allocated PO count), so the
  per-vendor totals sum to `total_normal_pos`. At 1000 POs / 50 vendors / 365 d ⇒ ~20 POs/vendor ⇒
  ~18-day average spacing; the top vendor holds ~100 POs and tail vendors ~10. Frequent vendors
  thus still produce ≥2 POs inside any 30-day window (legitimate clustering + the scenario-2
  sub-threshold hard negative). The ADB ~75% same-day batch artifact is **excluded** (it would
  contaminate scenario-2/4 timing signals).

## 3a. `process_latency`

Every process-step timestamp (RFQ send, confirm, approve, receipt, bill, payment, and the order's
own time-of-day) is drawn from a distribution, never a constant offset. Constant offsets would give
the dataset only a few distinct order-to-approve gaps and would make the confirm timestamp a direct
readout of `rfq_sent` — timeline "tells" that let a trivial rule solve scenario 4 by gap size alone.

`process_latency` (in `params.json`) holds **hand-set SME-plausible distributions** — right-skewed
(log-normal) for step latencies, a clipped normal for the order's own time-of-day. **These are
explicitly NOT calibrated against any dataset**, unlike `product_standard_price` / `lines_per_po` /
`qty_per_line` above, which at least borrow a *shape* from Online Retail II. There is no public
reference for SME procurement-workflow latency, so this block carries the same epistemic status as
`unit_price_noise`: a modeling choice made for plausibility, not fit to evidence. Values were chosen
so that:

- an SME order is placed sometime in the working day (mean 10:00, sd 2.2h, clipped 07:30–18:30),
  not always at a fixed instant;
- the RFQ-send and no-RFQ paths are sampled **independently** (`order_to_rfq_hours` +
  `rfq_to_confirm_hours` vs. `order_to_confirm_hours_no_rfq`) so the confirm-time distribution
  does not collapse to two values keyed on `rfq_sent` — the two paths were tuned to overlap
  substantially rather than sit at disjoint means;
- the ordinary `confirm_to_approve_hours` (median 10h, σ_log=1.0) is generous enough that a
  material share of approvals — including the ~5–10% of *normal* background POs that land above
  the threshold and therefore need a second-level approval — fall under 4 hours by chance alone,
  without needing any anomaly flag;
- `confirm_to_approve_hours_fast` (median 45min, σ_log=0.5, capped at 3.5h) is the **compressed**
  draw used only when the plan's `anomaly.fast_approval` flag is set (scenario-4 positives) — a
  materially shorter, but not degenerate/instantaneous, rubber-stamp interval, per anomaly_spec.md
  §2 S4's "vary self-approval cases with a short RFQ-to-approve interval" conspicuousness clause;
- every `min` floor is strictly positive, so each step is `previous_ts + a positive draw` and the
  ordering invariant (created ≤ sent ≤ confirmed ≤ approved ≤ received ≤ billed ≤ paid) holds by
  construction, not by a post-hoc resample.

Sampling happens in `build_normal.py` (APPLY phase), not `plan_dataset.py` (PLAN phase) where the
rest of this file's parameters are consumed. Each draw uses a dedicated RNG seeded from a hash of
`(seed, po_key, step-salt)`, which is as deterministic/reproducible as sampling in PLAN, and is
documented in `build_normal.py`'s module docstring and inline comments.

## 3b. `bill_to_payment_days_dup_fast` / `down_payment_to_final_days`

The same rule applies on the paths only scenario-1 cases traverse: the duplicate payment
(`inject_anomalies.py`) and the second (final) bill of the S1 down-payment-then-final hard negative
(`build_normal.py`). A constant offset on either would let a detector identify those cases by
arithmetic alone. Both are distributed in `process_latency`:

- **`bill_to_payment_days_dup_fast`** (median 2.5 d, σ_log=0.55, clipped [0.3, 14]) — the same
  lognormal *family* as the normal tail's `bill_to_payment_days` (median 4.0 d, σ_log=0.5, clipped
  [0.5, 30]), shifted toward the fast end as a **tendency** only (mirrors how
  `confirm_to_approve_hours_fast` relates to `confirm_to_approve_hours` in §3a) — a duplicate bill
  plausibly gets paid a little sooner (less scrutiny on a second invoice), but the ranges overlap
  substantially by construction.
- **`down_payment_to_final_days`** (median 10 d, σ_log=0.6, clipped [1, 45]) — a down-payment
  followed by a final invoice realistically lands anywhere from a few days to a few weeks out.

Both are sampled with the same `(seed, po_key, step-salt)`-keyed deterministic RNG as §3a.
`inject_anomalies.py` imports `build_normal`'s `_po_rng`/`_draw_lognormal`/`_LAT`/`_clamp` rather
than duplicating that machinery, since `build_normal` is already imported earlier in
`generate.py`'s APPLY pipeline (so its params.json load and `_PERIOD_END` are already initialized)
and the two modules are not circularly dependent.

**General rule:** a timing value that is both constant *and* whose presence correlates with a
ground-truth label is a leak, regardless of which module or which "side" (injected-positive or
hard-negative) it lives on. The one intentionally-left constant-looking value is
`anomaly.get("date_offset_days", 0)` in `inject_anomalies.py`'s S1 `different_date` variant, which
is **not** a bare literal — it is itself a per-PO value sampled in `plan_dataset.py`
(`rng.integers(3, 21)`), so it varies across the S1 population and carries no leak risk of this kind.

## 4. Assumptions

1. **Tax = untaxed.** The 20,000 threshold compares against the **untaxed** amount. To keep
   this unambiguous, POs are **generated tax-free (0% tax)** so `amount_untaxed == amount_total`, and the
   Monte-Carlo calibration above (which sums `qty × unit_price`, i.e. untaxed) matches what the pipeline
   and Odoo's approval trigger see.
2. **Emergent, not asserted.** median≈5k / ~6%-above-threshold are *outcomes* of the Monte-Carlo, not
   inputs. If the generator's sampling differs (e.g. rounding `unit_price` to 2 decimals), re-run the MC
   and re-tune `median` / `σ_log`.
3. **σ_log = 0.85 is the sensitive knob.** It controls both the tail and the fraction-above-threshold.
   ±0.05 moves the >20k fraction by roughly ±1.5 points.
4. **Vendor count vs volume.** 50 vendors over 1000 POs ≈ 20 POs/vendor average, ~18-day mean
   inter-arrival; the Zipf tail vendors get ~10 POs each. If a minimum per-vendor history is ever
   needed for baselines, either lower `n_vendors` or floor the Zipf weights.
5. **Category partition is simplistic.** `vendor_product_affinity` uses one primary category per vendor
   (round-robin) with 80% in-category draws — enough to make vendor average amounts differ, but not a
   real supplier-catalog model. Fine for the RQ2 comparison; intentionally minimal.
6. **`min` floors** (price ≥ 15, qty ≥ 1, lines ≥ 1) truncate the left tail slightly; they have
   negligible effect on the median/tail targets but keep values physically sensible.
