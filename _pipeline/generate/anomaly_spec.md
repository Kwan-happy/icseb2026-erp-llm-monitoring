# Anomaly Injection Spec (pre-registration recipe)

> Freeze bundle. This file was committed **before** any evaluation data was generated, so it pre-registers the RQ2 LLM-vs-SQL comparison. It describes only *what to inject* and *how to label* — it introduces **no rule verdicts** into the extracted context (facts and neutral comparators only).
>
> Every scenario has a **canonical** variant and (except Scenario 2) an **evasive** variant (~30% of its positives, §2 and §5). A canonical positive is defined by the same observable fact the frozen SQL rule tests; an evasive positive lies outside that test. **Recall is reported split by variant (canonical vs evasive) and never pooled** — see §5 and §7.
>
> The frozen SQL baseline `baseline.sql` was not changed to suit the evasive cases; the evasive variants are deliberately outside what the frozen rules test.

## 1. Global parameters

- `total_pos`: ≈1120 (see §5 for the exact per-scenario allocation)
- `base_rate`: **per-grain** (PO grain ≈1:8; vendor & audit grains ≈18 positive vendors each — see §5 for the counting units and denominators)
- `approval_threshold`: **20000**, `currency`: **USD**
  - This is a **fixed, frozen policy constant** — never a percentile of the generated data. Before generation, `_pipeline/config.json` and the generation DB's Odoo `po_double_validation_amount` are both set to **20000 USD**, so the extraction-pipeline threshold and Odoo's own two-step (double-validation) approval trigger agree on the same number.
  - medium-sized persona
- `splitting_window_days`: 30
- `dev_eval_split`: **30% dev/tuning, 70% eval** (see §7 for more)
- `random_seed`: 123

### Canonical label values (`ground_truth.injected_anomaly`, string enum)

Defined once and used everywhere below. Typed `["string","null"]` per `po_context.schema.json`.

| Scenario | Label string |
|---|---|
| 1 — Duplicate Invoicing / Payment | `duplicate_payment` |
| 2 — PO / Invoice Splitting | `po_splitting` |
| 3 — Suspicious Vendor-Master Modification | `vendor_master_mod` |
| 4 — Unusual Approval Sequence / Approval Bypass | `approval_bypass` |
| 5 — Abnormal Purchase Amount | `abnormal_amount` |
| Normal / hard negative | `null` |

## 2. Per-scenario injection recipes (×5)

Each recipe states: grain, target fields, injection recipe, conspicuousness control (so cases are not trivially separable), grounding citation, and label value.

### Scenario 1 — Duplicate Invoicing / Payment  →  `duplicate_payment`

- **Grain / evaluation unit:** per-PO (scored per-PO).
- **Target fields:** `bills[]`, `payments[]`, `baselines.count_bills`, `baselines.count_payments`, `sum_billed_qty` vs `sum_received_qty`.
- **Recipe (canonical, ~28 of 40):** pick a normal PO → register a duplicate vendor bill (same or near-same `amount_total`) via the alternate ("Upload") path so 2–3 bills exist against one PO; in a subset, also register a second payment so `count_payments` > 1.
- **Recipe (evasive, ~12 of 40):** the same vendor invoice is re-billed against a **different PO of the same vendor** — same amount, same vendor invoice reference, posted days apart. Each PO individually carries exactly one bill, so `count_bills > 1` never trips; the duplication is only visible by comparing bills **across** that vendor's POs. Extract support: the vendor rollup carries each PO's bills (reference, amount, date). Labelled `duplicate_payment` on **both** POs of the pair — each one carries a bill for an invoice that was paid twice, and a reviewer must look at both to see it.

  **Counting pin (this is the one recipe whose case and unit differ):** the scoring unit is the **PO**, so ~12 evasive *labelled POs* means **~6 injected pairs**. The scenario total stays 40 labelled POs (~28 canonical + ~12 evasive), not 28 + 24.
- **Conspicuousness:** do not make every case a pixel-perfect duplicate — vary it: some share an identical amount, some differ by a small rounding delta, some post on a different date, some duplicate only the bill (not the payment).
- **Grounding:** duplicate invoicing is a **billing scheme** — a fraudulent-disbursement category the ACFE identifies as among the greatest risks by frequency and median loss, disproportionately affecting smaller organizations that lack compensating controls (ACFE, 2024). The enabling weakness is the **Odoo control gap**. Odoo 19 shows a **deterministic but non-blocking** duplicate-*bill* warning when another bill for the same company + currency + vendor matches **either** (same reference AND same year) **or** (different reference but same vendor + same amount + same `invoice_date`). That warning is **advisory only — it never blocks**, it exists **only on the bill form** (there is **no duplicate check at the payment step at all**), and it is **evadable** by changing both the reference and the date. The large-scale "duplicate invoices for the same vendor and amount" pattern is corroborated by procurement-fraud analytics (SAS, n.d.).
- **Note:** like S5, R1 catches every **canonical** injected case by construction (any second bill or payment trips it), so the canonical column is decided on precision — against the legitimate-two-bills hard negatives (§6).
- **Note:** the frozen R1 is expected to score recall **0** on the evasive subset, since it never looks past one PO. That subset is where S1's recall becomes a measurement.
- **Label:** `duplicate_payment`

### Scenario 2 — PO / Invoice Splitting  →  `po_splitting`

- **Grain / evaluation unit:** scored **per vendor** — one splitting cluster spanning N sibling POs = one labeled vendor.
- **Target fields:** `vendor_rollup.splitting_view` (`running_sum` / `group_sum` vs `approval_threshold` within `window_days`).
- **Recipe:** for a single vendor, split one intended purchase into 2–4 POs inside a `splitting_window_days` window so that each PO is below `approval_threshold` but the window `running_sum` materially exceeds it.
- **Conspicuousness:** do not park every PO at 99% of the threshold — randomize each sibling to 65–95% of the threshold and vary the number of siblings per cluster (2–4). The generator re-checks each sibling after rounding and asserts the [0.65, 0.95] invariant rather than trusting the arithmetic.
- **Note — S2 needs no evasive variant.** Its rule demands a *pattern across records* (every sibling near-threshold inside a window) rather than a single field test, so realistic order flow already breaks it. S2's recall is therefore a measurement rather than an arithmetic identity, which is the property the evasive variants add to S1, S3, S4 and S5.
- **Baseline-rule alignment:** the frozen SQL baseline (R2) flags a cluster only when every sibling sits between 60% and 100% of the threshold — the near-threshold "bunching" signature of structuring (Palguta & Pertold). Ordinary vendors whose small routine orders merely sum past the threshold in a month are not flagged.
- **Grounding:** SAS continuous-monitoring research names invoice splitting as "dividing a high-value invoice into smaller ones to sidestep higher approval requirements" and reports forensic split-invoicing losses (SAS, n.d.). By analysing real procurement data, Palguta & Pertold (2017) show that buyers deliberately keep purchase values just under the approval threshold to skip the required higher-level sign-off, and that these under-threshold purchases measurably raise costs. This is cited for the *behaviour*, not as a calibration dataset.
- **Label:** `po_splitting`

### Scenario 3 — Suspicious Vendor-Master Modification  →  `vendor_master_mod`

- **Grain / evaluation unit:** scored **per vendor-trail** — one unit per vendor with **≥1 tracked change, or a bank account added after onboarding** (an account addition is an audit-relevant event, and the evasive recipe below produces vendors whose *only* such event is an addition). A vendor is positive if its trail contains the injected tampering.
  - The vendor's **first** (onboarding) bank account does not by itself earn a place in the denominator — otherwise every vendor qualifies and the audit grain collapses into the vendor grain. Once a vendor *is* in the denominator, its onboarding account is still shown as context, with the ordinal, so a later addition can be read against it.
- **Target fields:** `mail_tracking_value` / `res_partner` / `res.partner.bank` / `res_users` / `mail_message` (the vendor-audit extract, third grain).
- **Scope pin (the payoff channel is the bank account).** Every positive in this scenario is a **bank-account** case. Vendor-master fraud is tampering that *diverts payments*; the bank account is the channel through which money actually moves. A tax-ID fraud story (input-VAT or withholding manipulation, or defeating a duplicate-vendor check) is a **different scheme** and would be a *permanent* identity change, not a revert timed to a payment. No positive rests on a tax-ID edit.
- **Recipe (canonical, ~12 of 18):** a second user edits the **bank account number** on `res.partner.bank` shortly **before** a scheduled payment run, with **no** approval. **Then, after the payment is disbursed, the field is restored to its original value** — so the audit trail contains **two** tracked changes (forward edit, then revert).
- **Recipe (evasive, ~6 of 18):** a **new bank account is added** to the vendor and paid to; the original account is never touched and there is no revert. Behaviourally this is the same theft with a cleaner cover: the payment leaves for an account the organisation never approved. The frozen R3 watches *changes to existing bank fields*, so nothing it tests ever fires. Extract support: bank-account **creation** events on the audit trail, plus the vendor payment context (below).
- **Conspicuousness:** interleave with legitimate edits (address, phone) as noise so the sensitive change is not the only modification in the trail; legitimate edits are sprinkled across **~40–45 vendors** so the denominator (every vendor with ≥1 tracked change) is well-defined. A subset of those legitimate edits changes a **sensitive** field permanently (a genuine bank-detail change, no revert, not timed around a payment run), so "a sensitive field was touched" cannot by itself separate tampered from clean trails. The account-number *values* on tampered and legitimate edits are drawn from **one shared generator**, so no string pattern separates them.
- **Payment context.** The audit grain carries the vendor's payment dates and amounts, so a detector can observe that a change landed shortly before a payment run. This is **facts-only and additive**: no proximity verdict, no flag, and both detectors gain it equally.
- **Timestamp trap:** `mail_tracking_value` (and `mail_message`) are stamped "now" by Odoo at write time; the injected change/revert timestamps are realigned to the intended payment-run dates by `fix_timestamps.sql`.
- **Grounding:** vendor-master-file fraud diverts payments by tampering with static supplier data, defended by segregation of duties and change-review controls per the COSO control-activities component (COSO, 2013); procurement-fraud studies list unauthorized supplier-record changes among core scheme types (SAS, n.d.). The ACFE describes check or payment tampering schemes in which a person steals the employer's funds by intercepting, forging, or altering a check or electronic payment, e.g. re-routing an outgoing electronic payment to a vendor into their own bank account (ACFE).
  [Facts only: the extract carries no `approved` flag — the LLM infers "unauthorized" from context.]
- **Declaration (mirrors S1/S4/S5):** because every canonical positive is a bank-account change and the frozen R3 flags any bank-account change, R3 reaches **recall 1.00 on the canonical subset by construction**. Its contested figure is **precision** — against the ~9 legitimate permanent bank changes (§6). On the **evasive** subset R3 is expected to score recall **0**. The dataset contains no VAT audit edits, so a `bank OR vat` widening of R3 is equivalent to R3 on this dataset.
- **Label:** `vendor_master_mod`

### Scenario 4 — Unusual Approval Sequence / Approval Bypass  →  `approval_bypass`

- **Grain / evaluation unit:** per-PO (scored per-PO).
- **Target fields:** `timeline[]` (`event`, `timestamp`, `actor_user_id`), `po_identity.date_approve`, `buyer.user_id`, `baselines.approval_threshold`, `amount_total`.
- **Recipe (canonical, ~28 of 40 — mandatory anchor):** create an above-threshold PO whose **real approver actor** (recovered from chatter state-tracking) equals the buyer — the extracted signal `self_approval` is present in **every canonical** S4 positive (the evasive subset below is deliberately the exception); this is the mandatory anchor of the canonical recipe. Optionally add co-signals on top: the "RFQ Sent" step absent → `rfq_sent_skipped`, a very short RFQ-to-`date_approve` interval, and/or a newly created vendor. All signals are **neutral extracted facts** the LLM interprets in context — **not** rule verdicts. `rfq_sent_skipped` alone, with no other suspicious context, is never a positive.
- **Recipe (evasive, ~12 of 40):** an above-threshold PO with a **rubber-stamp approval**. The buyer and the approver are genuinely different people, so `self_approval` is **false** and the frozen R4 never fires — but the approval is not a real control: it is granted seconds after confirmation, the "RFQ Sent" step is skipped, and the vendor was created days earlier. No human could have reviewed an above-threshold commitment in that time. The evidence is in `timeline` and `state_log`; the vendor's creation date is in the extract so "brand-new vendor" is observable. Contrast with the §6 hard negative: a *fast* approval on a below-threshold routine reorder is legitimate and must not be flagged. What makes this a positive is the **conjunction** — above-threshold value, a near-zero review interval, a skipped RFQ, **and** an unestablished vendor. No single one of those is decisive on its own.
- **Amount band (asserted).** The S4 amount must lie **inside the realised distribution of ordinary above-threshold POs**, never beyond it, or "amount above the largest normal PO" would separate S4. The band is **capped so its upper end stays below the normal above-threshold maximum**, and the realised amount is **asserted** into band after rounding, exactly as the S2 sibling band is. An approval bypass is someone moving a purchase *just past* the sign-off line, not placing the largest order in company history.
- **Conspicuousness:** mix the optional co-signals so no single one is decisive — vary self-approval cases with a short RFQ-to-approve interval, a newly created vendor, and/or a concurrent `rfq_sent_skipped`, so the anchor (self-approval) always carries the case but the co-signals alone never do.
- **Note:** cases that are "fast but below threshold" or "a lone benign sequence deviation" are **not** part of this positive recipe — they are generated as hard negatives instead (§6).
- **RFQ-skip framing:** skipping "RFQ Sent" is a **legitimate process variant** (phone/offline negotiation, recurring or urgent orders, MRP auto-RFQs). `rfq_sent_skipped` alone is never an anomaly; it becomes suspicious only in combination with the above-threshold self-approval anchor.
- **Declaration (mirrors S5):** because the ground truth anchors on the same signal the frozen R4 rule tests (above-threshold self-approval), the SQL baseline is expected to reach near-perfect precision and recall on the **canonical** subset by construction. The LLM's test there is parity plus, on the S4 hard negatives, **not over-flagging**.
- **Note:** on the **evasive** subset the frozen R4 is expected to score recall **0**, because `self_approval` is false in every one of those cases. The buyer's identity must also not give the answer away: managers also author ordinary orders (~5% of normals) and half the S4 hard negatives, and the approver is always a manager **other than** the author unless the case is a labelled positive.
- **Grounding:** segregation-of-duties / authorization controls per COSO (COSO, 2013); approval bypass is a recognized procurement-fraud scheme (SAS, n.d.).
- **Label:** `approval_bypass`

### Scenario 5 — Abnormal Purchase Amount  →  `abnormal_amount`

- **Grain / evaluation unit:** per-PO (scored per-PO).
- **Target fields:** `lines[].unit_price` vs `standard_price`, `baselines.line_unit_price_vs_cost_ratio`, `baselines.vendor_avg_amount_alltime`, `amount_total`.
- **Recipe (canonical, ~28 of 40):** set one or more line `unit_price` above `standard_price` by a factor (≈1.5–3×), and/or push `amount_total` to a large multiple of `vendor_avg_amount_alltime`.
- **Recipe (evasive, ~12 of 40):** a sustained over-payment at **1.2–1.4× standard cost** on a high-value line **that the same vendor previously sold cheaper**. The frozen R5 misses it twice over: the ratio sits below its 1.5× cutoff, and the order total stays under 3× the vendor average. The fraud is provable only against the vendor's own price history — so the extract carries, per line, that vendor's **prior average unit price for that same product** (strictly-earlier POs only, `null` when there is no prior sale). This is the one variant where the evidence is a *trend*, not a single reading: one order at 1.3× is negotiation, a repeated 1.3× against the vendor's own established price is not.
- **Conspicuousness:** spread the over-price factor across the 1.5–3× range rather than making every case 3×. The over-priced lines are chosen as a **random subset** of the PO's lines, not always the first line.
- **Grounding:** price/value outliers are a standard transaction-anomaly-detection target (SAS, n.d.); ML on accounting data isolates irregular entries via deep-autoencoder reconstruction error (Schreyer et al., 2017) and LLM embeddings of non-semantic categorical financial data (Bakumenko et al., 2025). Comparators stay neutral ratios; the fraud/negotiated/rush judgement is left to the reasoning layer.
- **Note — cutoff/injection overlap by design:** the frozen R5 unit-price-ratio cutoff (1.5×, an audit-materiality value fixed before generation) overlaps the injected 1.5–3× range **by design**, so the SQL baseline attains ~100% recall on the ratio signal *for canonical cases*. That column is therefore decided on **precision**: SQL over-flags via its 3×-vendor-average branch, plus the ~1.5× legitimate-premium hard negative, where the LLM's richer context can avoid the false positives. The 1.5×/3× cutoffs derive from audit materiality conventions, not from the injected range.
- **Note — point-in-time comparator.** `vendor_avg_amount_alltime` averages *all* of a vendor's POs for the whole year, **including the PO being judged and every anomaly injected into that vendor**. It is symmetric across detectors, so RQ2 stays internally valid, but it overstates what a live monitor could know. The extract therefore also carries `vendor_avg_amount_prior` / `vendor_po_count_prior` (strictly-earlier POs only). `vendor_avg_amount_alltime` is what the frozen R5 uses.
- **Note:** on the **evasive** subset the frozen R5 is expected to score recall **0** — 1.2–1.4× is below its cutoff and the totals stay under its amount branch.
- **Label:** `abnormal_amount`

## 3. Generation prerequisites

Scenarios 3 & 4 need **≥2 distinct Odoo users** — a **non-manager buyer** and a **purchase-manager approver**. A manager who confirms an above-threshold PO self-approves it directly (the scenario-4 abuse); a non-manager routes to a separate approver (the legitimate contrast).

**Pre-generation checklist:**
- `fix_timestamps.sql` realigns Odoo's forced `now()` timestamps (`mail_tracking_value` / `mail_message` / `create_date` / `write_date`).
- Create ≥2 Odoo users: a non-manager buyer and a purchase-manager approver.
- Provision a **fresh, empty** Odoo DB.
- Set Odoo `po_double_validation_amount = 20000` **and** `_pipeline/config.json`'s `dbname` to the new DB.
- The second freeze (prompt + risk-band mapping, tuned on dev, committed before eval analysis — see §7) happens after generation.

## 4. Generation mechanics

Synthetic normal POs are drawn from ~5–7 hand-set parametric distributions (USD), loaded into a fresh Odoo DB via XML-RPC; anomalies are injected on top. Odoo stamps `create_date` / `mail_message` at "now"; only some fields (`date_order`, `date_approve`) can be backdated via XML-RPC, and audit-trail timestamps are realigned by `fix_timestamps.sql`. The extraction pipeline then runs **unchanged** on the generated DB.

**Full-tail normals:** whether a NORMAL PO carries a full P2P tail (1 vendor bill + 1 payment) depends on **how old the order is**. POs older than ~35 days before the end of the generation period get a tail with probability ~0.92; POs inside that final window get ~0.25, because they are still legitimately in flight. Realised: **90.1% of normal POs carry a tail (9.9% do not).** This keeps "having bills/payments" from being an artifact unique to anomalous POs, avoids year-old confirmed orders that were never invoiced, and keeps `baselines.count_bills` informative.

**Cancelled orders.** POs old enough that a bill would be expected, which nonetheless drew no tail, become **cancelled** orders. Cancellation is applied **only to null-labelled normal-background POs**, never to an injected case or a hard negative, and only *after* every scenario section has run, so a converted S1/S5 PO can never also be cancelled. A cancelled PO gets no receipt, no bill and no payment. Realised: **68 cancelled (64 below threshold, 4 above)**, leaving the PO-grain ratio at 1:7.67.

Both are realism properties of the *normal background*; neither touches an injection recipe, a label, or `baseline.sql`.

**Construction must not be observable.** An injected case must differ from a normal one only in the *behaviour* the scenario describes — never in how the record was assembled:

- **Quantity.** Injected POs reach their target amount through more lines, within the normal quantity range — never by inflating line quantities. `unit_price` is never inflated to hit a target, which would plant an S5 signal inside an S2/S4 case.
- **Timestamps.** All process latencies are drawn from seeded, SME-plausible distributions (recorded in `params.json` / `params_derivation.md` as hand-set, not calibrated), including the S4 `fast_approval` co-signal. **This applies to the injection paths too** (the S1 duplicate payment and the S1 down-payment hard negative's second bill). The rule is: **if a timing value is constant and the records carrying it line up with a label, it is a leak.**
- **PO numbering.** POs are emitted in `date_order`, as a real ERP sequence would run, so the PO number does not correlate with the *role*.
- **Field values.** Tampered and legitimate bank accounts come from one shared generator; see the S3 conspicuousness note.
- **Product mix.** **Products on an injected PO are drawn from the same vendor-affinity distribution ordinary POs use.** Reaching a larger amount is allowed to mean *more lines*; it is not allowed to mean *a different kind of product*.

The right test for all of these is **conditional on amount**, not marginal. An injected above-threshold PO is larger than the average normal PO by definition, and that difference is the scenario, not an artefact. The question that matters is whether an injected PO is distinguishable from a **real PO of the same value**. The rule is general: **if a detector can separate the classes without using the scenario's stated evidence, the dataset is wrong, not the detector.**

## 5. Base rate and per-scenario allocation

- Ratios differ by grain: **PO grain ≈ 120 : 1000 ≈ 1:8** (S1/S4/S5, ~40 each); **vendor grain ≈ 18 positive : ~32 negative** (S2, of 50 vendors); **audit grain ≈ 18 tampered : rest-with-a-trail** (S3). Total ≈1120 POs. These ratios are **evaluation-balance choices for statistical power, not real-world prevalence claims**.
- **Counting unit differs by scenario:**
  - Scenarios **1, 4, 5** are counted **per PO** (each injected anomaly = one anomalous PO).
  - Scenario **2** is counted **per vendor** — one splitting cluster spans N sibling POs (N sub-threshold POs) but is one labeled vendor.
  - Scenario **3** is counted **per vendor-trail** — one vendor with ≥1 tracked change is one labeled unit; positive if the trail contains the injected tampering (off the transactional spine, not a PO).
- **Target counts, per-grain denominators:** total ≈1120 POs (`n_vendors` = 50, per `params.json`):
  - **S1, S4, S5** — scored **per-PO**: ~40 injected positives each, against the ≈1000 normal-PO pool (PO grain ≈120:1000 ≈1:8). Each scenario's 40 split **~28 canonical / ~12 evasive**.
  - **S2** — scored **per-VENDOR**, over all **50** vendors: ~18 splitter vendors (positive, one splitting cluster each at most) + the remaining non-splitter vendors (negative, `null`). No evasive variant — see the S2 note in §2.
  - **S3** — scored **per vendor-trail**: ~18 tampered vendors (positive) vs the other vendors with only legitimate edits (negative, `null`); denominator = every vendor with ≥1 tracked change **or a bank account added after onboarding**. The 18 split **~12 canonical** (bank-account change-then-revert) **/ ~6 evasive** (new bank account added and paid to). **No positive rests on a tax-ID edit.**
  - Exact per-scenario counts are drawn with `random_seed` and recorded in the `labels_*` sidecars at generation time.
  - With n=50 vendors and ~18 positives each at the vendor and audit grains, the eval side holds ~13 positives per grain (one case ≈8%).

### Canonical vs evasive — and why recall is reported split, never pooled

In the canonical recipes of four scenarios, a positive is defined by *the same single observable fact the frozen rule tests*: S1 injects a second bill and R1 tests `count_bills > 1`; S4 injects above-threshold self-approval and R4 tests exactly that; S5 injects a 1.5–3× price and R5 tests ≥ 1.5; S3 injects a bank change and R3 flags any bank change. A frozen-baseline recall of 1.00 on canonical cases is therefore **arithmetic, not measurement**.

A variant qualifies as **evasive** only if it is (a) unambiguously fraud to a procurement auditor, (b) outside the frozen rule's single-field test, and (c) decidable from facts already in the extract. All four satisfy all three.

| Scenario | Canonical / evasive | Frozen-rule recall predicted on the evasive subset | Why the rule misses it |
|---|---|---|---|
| S1 | ~28 / ~12 | 0 | `count_bills > 1` is per-PO; the duplicate spans two POs |
| S2 | 18 / — | n/a (already a cross-record pattern) | — |
| S3 | ~12 / ~6 | 0 | nothing was "changed"; a new account was added |
| S4 | ~28 / ~12 | 0 | `self_approval` is false; the approval is real but empty |
| S5 | ~28 / ~12 | 0 | 1.2–1.4× is below the 1.5 cutoff, total below 3× the average |

**Reporting requirement.** Recall is reported **split by variant — canonical versus evasive — and never pooled.** The canonical column is declared *construction-determined* and must be labelled as such wherever it appears. The evasive column is the measurement the RQ2 claim rests on, reported alongside precision at realistic prevalence. For evasive variants, **post-hoc sensitivity rules** may be written so the rule side can compete on the same evidence — the comparison must not become "the LLM sees a fact the SQL cannot".

**The RQ2 argument does not rest on recall.** It rests on three legs that survive a rule scoring 1.00: analyst workload at realistic prevalence (0.5–5%), coverage of patterns nobody wrote a rule for, and a reviewable explanation for the human in the loop. Where a rule genuinely suffices — the single-fact anomalies — that is reported plainly.

- **Hard negatives are additional**, not carved out of the ≈1000 normals — they are extra deliberately-tricky-but-legitimate cases added on top, so the normal population is not depleted by them. (~10 per per-PO kind, ~9 legitimate-bank S3 hard negatives, ~50 vendors with benign address/phone edits, and 4 above-threshold approved-then-cancelled POs. No VAT edits — see §6.)
- **A hard negative's buyer must not identify it.** Buyers are drawn independently of hard-negative kind.

## 6. Hard negatives (label = `null`, unambiguous by construction)

Cases that *look* suspicious but are legitimate. A hard negative must **never be genuinely ambiguous** — each is generated as normal, with no anomaly mechanism injected, so its `null` label is correct by construction.

- **Scenario 4:** a PO that skips "RFQ Sent" but is a routine recurring / low-value reorder → `null`. Also: a fast approval, or a lone RFQ-skip, on a below-threshold routine/recurring PO — fast timing or a single sequence deviation alone is never suspicious → `null`.
  - An **above-threshold PO that was fully and legitimately approved, then cancelled** → `null`. This looks like a large commitment worth scrutiny, but it was voided before any money moved, so it must not be flagged. It falls out of the age-dependent cancellation pass (§4) rather than being injected, and carries **no** `self_approval` signal (the approver is a different manager), so it does not overlap the S4-positive signature. Realised: **4 such POs**, all buyer-authored, all `state = cancel`.
- **Scenario 2:** a vendor with several in-window POs, each **well below 60% of the threshold** (~$5k routine orders), whose `running_sum` may still exceed the threshold — R2 correctly passes this as a true negative, since it arises naturally from the parametric background rather than from a splitting mechanism. Also: a vendor whose window `running_sum` stays **below** `approval_threshold`, so approval avoidance is not even possible.
- **Scenario 3:** a legitimate vendor-master edit (address/phone, or a properly-reviewed change) with no change-then-revert around a payment → `null`. In particular:
  - **~9 legitimate permanent bank-account changes** — a genuine change of banking details, no revert, not timed around a payment run. These are the contested figure for R3's precision.
  - **No tax-ID (`vat`) edits.** The dataset is tax-free by design (`amount_untaxed == amount_total`), so a vendor VAT number has no functional role and editing one is inert.
- **Scenario 1:** a legitimately-two-bills case — e.g. a **down-payment plus final invoice**, or **two partial deliveries** each correctly billed — looks like a duplicate but is correct → `null`.
- **Scenario 5:** a genuine price increase / rush order / low-volume premium sitting near the **low end** of the over-price band (~1.5–1.6×) — a real but legitimate elevated price → `null`. This sits AT/ABOVE the frozen R5 1.5× cutoff, so the SQL rule over-flags it — it is the precision test for S5; the LLM is expected to pass it using context.

## 7. Labeling & split

### Per-grain label sidecars (mirror the three output grains)

The pipeline emits three grains, so ground truth is stored in three sidecars (never fed into the LLM prompt; they populate `ground_truth.injected_anomaly`, the reserved null slot):

- `labels_po.json` — keyed by `po_number` (e.g. `"P00001"`); values from the canonical enum or `null`. Carries scenarios **1, 4, 5** and all normal/hard-negative POs. S2 sibling POs are present here too, but always as `null` — a per-PO LLM output of `po_splitting` is **not scored** (S2 is evaluated at the vendor grain only).
- `labels_vendor.json` — keyed by **vendor id** (exactly one label per vendor, `"po_splitting"` or `null`); carries the scenario-**2** splitter vendors and `null` for non-splitting vendors.
- `labels_audit.json` — keyed by **vendor id**: the vendor-audit extract carries no instance identifiers, the SQL rule R3 emits vendor-id flags, and the LLM reads per-vendor audit files. Exactly one label per vendor: `"vendor_master_mod"` or `null`.

Example (`labels_po.json`):
```json
{ "P00001": "abnormal_amount", "P00002": null, "P00003": "approval_bypass" }
```

### Evaluation unit for P / R / F1 (per scenario)

- Scenarios **1, 4, 5** → scored **per PO** (against `labels_po.json`).
- Scenario **2** → scored **per vendor** (against `labels_vendor.json`).
- Scenario **3** → scored **per vendor-trail** (against `labels_audit.json`).
- Within each scenario, **recall is additionally broken out by variant (canonical / evasive)** — never pooled. The `labels_variant.json` sidecar carries the canonical/evasive distinction so the split is recoverable at scoring time. Precision is reported per scenario, plus at prevalence-adjusted operating points.

### Dev / eval split

- Split is **stratified BY VENDOR** (not by PO), with a fixed `random_seed`, so scenario-2 sibling POs never leak across the dev/eval boundary. The split further stratifies by **vendor class** (splitter / tampered / normal) so every scenario lands on both sides.
- **30% dev/tuning, 70% eval.**
- **Two-stage freeze:** **Freeze 1** — the dataset spec, params, and SQL baseline rules (`baseline.sql`) are frozen **pre-generation**; they are **not** dev-tunable, since tuning the baseline comparator on the dev split would undermine the RQ2 comparison it anchors. **Freeze 2** — the LLM prompt and the risk-band → flag mapping are tuned **only on the dev split**, then frozen and committed **before** the eval split is analyzed (`_pipeline/frozen/prompt_v1.md`, `_pipeline/frozen/risk_bands_v1.md`). There is no LLM training, only prompt/threshold tuning.
- Every scenario is represented on **both** sides of the split.
- **Small-n caveat:** with 50 vendors split 30/70, the eval side holds **~13** S2/S3 positives. Vendor- and audit-grain metrics (P/R/F1) must be reported **alongside raw counts**, since small-n P/R/F1 alone is easy to misread.
