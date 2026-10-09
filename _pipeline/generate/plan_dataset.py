#!/usr/bin/env python
"""plan_dataset.py — the PLAN phase of generation: decide the whole dataset on paper.

Pure Python. No Odoo, no database, no I/O except writing the plan out. Everything random
happens here, through the single seeded `Samplers` instance, so that:
  * `generate.py --dry-run` can inspect the entire dataset before a single record is written;
  * re-running with the same seed reproduces the same dataset;
  * the APPLY modules (build_master / build_normal / inject_anomalies) contain no sampling
    at all — they only carry out instructions, which keeps "what the dataset is" in one file.

The plan implements `anomaly_spec.md` §2 (recipes), §5 (per-grain allocation) and §6
(hard negatives). It does not restate any frozen number: counts arrive from generate.py's
`scaled_counts()`, distributions from params.json via Samplers, and the policy constants
(approval_threshold, splitting_window_days) from _pipeline/config.json.

LEVEL B: the plan records what was INJECTED and the ground-truth label. It never
writes a verdict into anything the extraction pipeline will see — labels travel in the
separate `labels_*.json` sidecars, never into the Odoo records or the context JSON.
"""
import collections
import datetime
import json
import statistics

# Canonical label strings — anomaly_spec.md §1 "Canonical label values".
S1 = "duplicate_payment"
S2 = "po_splitting"
S3 = "vendor_master_mod"
S4 = "approval_bypass"
S5 = "abnormal_amount"


# ---------- small helpers ----------
def _round2(x):
    return round(float(x), 2)


def _dt_to_odoo(d):
    """A datetime.date -> 'YYYY-MM-DD 00:00:00', the naive-timestamp string TimestampMap /
    fix_timestamps.sql expect for create_date/write_date cells."""
    return datetime.datetime.combine(d, datetime.time.min).isoformat(sep=" ")


def _po_total(lines):
    """amount_total EMERGES from the lines (params.json's bottom-up design). The dataset is
    tax-free, so amount_untaxed == amount_total and this is what the 20000 threshold sees."""
    return _round2(sum(_round2(l["qty"] * l["unit_price"]) for l in lines))


def _build_lines(samplers, vendor_idx, catalog):
    """A normal PO's lines: unit_price ≈ standard_price (ratio ~1), so scenario 5 has a clean
    baseline to deviate from."""
    out = []
    for l in samplers.build_po_lines(vendor_idx, catalog):
        out.append({"product_idx": l["product_idx"], "qty": l["qty"],
                    "standard_price": _round2(l["standard_price"]),
                    "unit_price": _round2(l["unit_price"])})
    return out


def _build_lines_to_amount(samplers, vendor_idx, catalog, target, rng):
    """Build lines whose total reaches (or lands close to) `target` the way an ORDINARY large
    PO does: by adding MORE LINES, each drawn from the exact same distributions
    `_build_lines`/`samplers.build_po_lines` use for every line of every normal PO — product
    from the vendor's own category-affinity draw, qty from the ordinary qty distribution, price
    from standard_price * the ordinary noise multiplier. Nothing here ever chooses a product
    FOR its price, and quantity is never inflated beyond an ordinary draw.

    Product choice is never optimized for price and qty is never used as a lever: every added
    line is an ordinary draw, so reaching a larger target can only mean MORE lines. The caller
    handles a possible overshoot of `target` and asserts/nudges the final `amount_total` into
    the band the scenario's recipe requires — see the shared `_nudge_amount_into_band()`, used
    by both S2 (sub-threshold band) and S4 (in-distribution band).

    The starting point is the CLOSEST of `n_candidates` INDEPENDENT ordinary PO draws (each with
    its own line-count draw from `sample_lines_per_po`), so the starting line count carries the
    same natural variety a real PO landing near `target` would show — some candidates get there
    via few lines, some via many, as the normal background's own tail does. Only the leftover
    shortfall (usually small) is closed by appending more ordinary lines."""
    # PREFER a candidate that already reaches `target` on its own (any
    # padding line is itself a small departure from "this is exactly what a natural large PO
    # looks like", so avoiding padding altogether whenever a natural draw already gets there is
    # better than getting arbitrarily close and padding the last few dollars). Falls back to the
    # closest-by-absolute-distance candidate (padded up to target below) only if none of the K
    # draws reaches target unaided.
    best_over, best_over_gap = None, None
    best_any, best_any_gap = None, None
    for _ in range(3000):   # cheap: pure in-memory sampling, no I/O
        cand = _build_lines(samplers, vendor_idx, catalog)
        t = _po_total(cand)
        gap = abs(t - target)
        if best_any_gap is None or gap < best_any_gap:
            best_any, best_any_gap = cand, gap
        if t >= target and (best_over_gap is None or (t - target) < best_over_gap):
            best_over, best_over_gap = cand, t - target
    lines = best_over if best_over is not None else best_any

    def total():
        return _po_total(lines)

    max_extra_lines = 80   # generous safety cap; catalog prices are modest (median far below
                            # $4,900) so a wide target can need many ordinary lines now that
                            # price is never optimized for fit — see the docstring above.
    guard = 0
    while total() < target and guard < max_extra_lines:
        guard += 1
        p_idx = int(samplers.pick_products([vendor_idx])[0])   # SAME affinity draw as normals
        qty = int(samplers.sample_qty_per_line(1)[0])           # ORDINARY qty draw
        noise = float(samplers.sample_unit_price_noise(1)[0])   # ORDINARY price noise
        standard_price = float(catalog[p_idx])
        lines.append({"product_idx": p_idx, "qty": qty,
                       "standard_price": _round2(standard_price),
                       "unit_price": _round2(standard_price * noise)})

    return lines


def _nudge_amount_into_band(po, lo, hi, qty_cap, threshold, guard_max=4000):
    """Bounded last-resort qty nudge (never touches unit_price/product —
    neither price nor product choice may carry a construction signal) to land
    `po['amount_total']` inside `[lo, hi]`, then ASSERTS success rather than trusting the
    arithmetic — a failed assert aborts the plan instead of silently shipping a drifted case.
    Shared by every scenario whose recipe requires a specific amount band: S2 siblings (the
    sub-threshold bunching band) and S4 (the in-distribution above-threshold band)."""
    guard = 0
    while (po["amount_total"] < lo or po["amount_total"] > hi) and guard < guard_max:
        guard += 1
        if po["amount_total"] < lo:
            room = [l for l in po["lines"] if l["qty"] < qty_cap]
            if not room:
                break
            l = min(room, key=lambda l: l["unit_price"])
            l["qty"] += 1
        else:
            room = [l for l in po["lines"] if l["qty"] > 1]
            if not room:
                break
            l = min(room, key=lambda l: l["unit_price"])
            l["qty"] -= 1
        po["amount_total"] = _po_total(po["lines"])
    po["above_threshold"] = po["amount_total"] > threshold
    assert lo <= po["amount_total"] <= hi, (
        f"{po['po_key']} amount {po['amount_total']} outside [{lo}, {hi}] band after nudging")


def _random_bank_acc(rng):
    """ONE generator shared by tampered and legitimate bank-account edits, so no prefix,
    digit-count, or checksum pattern distinguishes the two populations. Prefix drawn from the same 3-digit branch-code-like range either way."""
    prefix = int(rng.integers(100, 1000))
    mid = int(rng.integers(1000000, 10000000))
    tail = int(rng.integers(0, 10))
    return f"{prefix}-{mid}-{tail}"


def _spread_dates(rng, anchor, n, window_days):
    """n distinct dates inside [anchor, anchor + window_days) — the sibling POs of a
    scenario-2 splitting cluster must fall inside one splitting_window_days window."""
    span = max(1, window_days - 2)
    offsets = sorted({int(rng.integers(0, span)) for _ in range(n * 3)})[:n]
    while len(offsets) < n:
        offsets.append(min(span, (offsets[-1] if offsets else 0) + 1))
    return [anchor + datetime.timedelta(days=o) for o in offsets]


# ---------- master data ----------
_STEMS = ["Sirikul", "Bangkok", "Thonburi", "Chao Phraya", "Rattana", "Pattana", "Siam",
          "Ayutthaya", "Krungthep", "Sathorn", "Chatuchak", "Rayong", "Chonburi", "Nakhon",
          "Suvarna", "Metro", "Golden Gate", "Union", "Apex", "Delta", "Horizon", "Summit",
          "Pioneer", "Vantage", "Meridian", "Anchor", "Crestline", "Northstar", "Silverline",
          "Riverside", "Eastgate", "Lotus Park", "Bangna", "Sriracha", "Lamphun"]
_SUFFIX = ["Trading Co., Ltd.", "Supply Co., Ltd.", "Industries Co., Ltd.",
           "Enterprises Co., Ltd.", "Distribution Co., Ltd."]
_CATEGORIES = ["Office Supplies", "IT Hardware", "Raw Materials", "Packaging Materials",
               "MRO & Maintenance", "Logistics & Freight"]
_CITIES = ["Bangkok", "Chonburi", "Samut Prakan", "Rayong", "Pathum Thani"]

# Realistic SME procurement item names, 10 per
# _CATEGORIES entry. The catalog's lognormal floor is $107 (nothing in this dataset is a cheap
# consumable), so every name is deliberately bulk-framed (carton/pallet/drum/reel/per-shipment)
# or moved up the value ladder into equipment, never priced "per unit" the way a real pen or
# marker would be. Each list is stored CHEAPEST -> MOST EXPENSIVE; `_plan_master` below ranks
# each category's products by their actual drawn `standard_price` and assigns names by that
# rank, NOT by construction position — so the pairing holds even if the catalog sampler is
# reseeded or its per-position values change, as long as the same 10 names are used per
# category. Still a fixed deterministic list (no rng draw), so two dry runs stay byte-identical.
_PRODUCT_NAMES = {
    "Office Supplies": [
        "A4 Copy Paper 80gsm, 50-ream Carton", "Laser Toner Cartridge Multi-pack (Box of 4)",
        "Heavy-Duty Cross-cut Paper Shredder", "Steel Filing Cabinet, 4-drawer",
        "Desktop Laminator, A3", "Ergonomic Task Chair, Mesh Back",
        "Office Desk, Adjustable Height", "Color Laser Printer, A4",
        "Desktop Photocopier, A4/A3", "Multifunction A3 Laser Printer (Print/Scan/Copy)",
    ],
    "IT Hardware": [
        "Wireless Access Point", "27-inch LED Monitor",
        "Laptop Docking Station, Dual-monitor", "External SSD, 2TB",
        "24-port Gigabit Managed Switch", "Handheld Barcode Scanner, Industrial",
        "NAS Storage Unit, 4-bay", "UPS Battery Backup, 2200VA",
        "Business Laptop, Entry-level", "Business Desktop Workstation, Mid-range",
    ],
    "Raw Materials": [
        "Cold-rolled Steel Sheet, 2mm (per sheet)", "Polypropylene Granules, 25kg Bag",
        "Fiberglass Cloth Roll, 50m", "Treated Pine Timber Plank Bundle",
        "Stainless Steel Rod Bundle, 1m x 10", "Aluminum Extrusion Bar Bundle, 6m x 5",
        "Hot-dip Galvanized Steel Coil", "Copper Wire Coil, 2.5mm (100m reel)",
        "Industrial Rubber Sheet Roll, 5mm", "PVC Resin Pellets, 1-tonne Bulk Bag",
    ],
    "Packaging Materials": [
        "Bubble Wrap Roll, 1.2m x 100m", "Corrugated Carton Box, Bundle of 25",
        "Stretch Wrap Film, Pallet Lot (20 rolls)", "Void Fill Paper, Bulk Roll 30kg",
        "Woven PP Sack, Bale of 100", "Packing Tape, Case of 36 Rolls",
        "Foam Packing Sheets, Pallet Lot (2,000 sheets)", "Wooden Pallets, Lot of 20",
        "Strapping Reel & Buckle Set, Bulk Case", "Semi-automatic Carton Sealing Machine",
    ],
    "MRO & Maintenance": [
        "Industrial Grease Cartridge, Case of 24", "Machine Lubricant Drum, 20L",
        "Industrial Air Filter Cartridge Set", "LED High-bay Light Fixture",
        "Galvanized Pipe Fitting Kit, Bulk", "Industrial Ball Bearing Set, Assorted",
        "Industrial V-Belt Set, Case of 10", "Hydraulic Oil ISO VG 46, 200L Drum",
        "Reconditioned Industrial Gearbox", "Rotary Screw Air Compressor",
    ],
    "Logistics & Freight": [
        "Freight Insurance, per shipment", "Last-mile Delivery, per parcel batch",
        "Container Trucking, 20ft, per trip", "Cold Chain Transport, per trip",
        "Domestic LTL Freight, per shipment", "Warehouse Handling Fee, per pallet lot",
        "Customs Brokerage Service, per shipment", "Full Truckload Freight, per trip",
        "Cross-dock Transfer Service, per pallet lot", "FCL Container Haulage, per container",
    ],
}


def _vendor_record(idx, n_cat):
    """One vendor's master-data dict, fully determined by `idx` (no rng draw) — reused both for
    the n_vendors "population" vendors _plan_master builds below AND for any
    dedicated one-off vendors (build_plan appends those at idx >= n_vendors, see §2 S4 below);
    since every field here is a pure function of idx, appending more of them later never
    perturbs the population vendors' values or consumes rng state."""
    stem = _STEMS[idx % len(_STEMS)]
    return {
        "idx": idx,
        "name": f"{stem} {_SUFFIX[idx % len(_SUFFIX)]}",
        "category_idx": idx % n_cat,          # params.json: one_per_vendor_round_robin
        "street": f"{100 + idx} Industrial Estate Rd.",
        "city": _CITIES[idx % len(_CITIES)],
        "phone": f"+66-2-{300 + idx}-{1000 + idx * 7 % 9000}",
        "vat": f"0{1050000000000 + idx * 7919}",
        "bank_acc_number": f"{100 + idx}-{1000000 + idx * 13337}-{idx % 10}",
    }


def _plan_master(samplers, params, counts, rng):
    n_vendors, n_products = counts["n_vendors"], counts["n_products"]
    aff = params["vendor_product_affinity"]
    n_cat = aff["n_categories"]
    per_cat = max(1, n_products // n_cat)

    vendors = [_vendor_record(i, n_cat) for i in range(n_vendors)]

    # A name must plausibly cost the price it lands on, and the catalog sampler
    # is not guaranteed to draw prices in position order within a category. So price is decided
    # first (pure function of k, no name involved), then names are assigned by PRICE RANK within
    # each category — cheapest product gets _PRODUCT_NAMES[cat][0], priciest gets [-1] — which
    # keeps the pairing monotonic even under a reseed. Still fully deterministic: no rng draw,
    # only the fixed catalog + a stable sort.
    prices = {k: _round2(samplers.catalog[k]) for k in range(n_products)}
    cat_of = {k: min(k // per_cat, n_cat - 1) for k in range(n_products)}
    by_cat = collections.defaultdict(list)
    for k in range(n_products):
        by_cat[cat_of[k]].append(k)
    name_by_k = {}
    for cat, ks in by_cat.items():
        cat_name = _CATEGORIES[cat % len(_CATEGORIES)]
        names = _PRODUCT_NAMES.get(cat_name, [])
        ranked = sorted(ks, key=lambda k: prices[k])   # cheapest -> priciest, stable sort
        for rank, k in enumerate(ranked):
            name_by_k[k] = names[rank] if rank < len(names) else f"{cat_name} Item {rank + 1}"

    products = []
    for k in range(n_products):
        cat = cat_of[k]
        products.append({
            "idx": k,
            "name": name_by_k[k],
            "default_code": f"P-C{cat + 1}-{k % per_cat + 1:03d}",
            "standard_price": prices[k],
            "category_idx": cat,
        })
    return {"vendors": vendors, "products": products,
            "category_names": _CATEGORIES[:n_cat], "n_categories": n_cat}


# ---------- the plan ----------
def build_plan(samplers, params, pipe, counts):
    rng = samplers.rng                       # one RNG for the whole plan (seeded from params)
    threshold = pipe["params"]["approval_threshold"]
    window = pipe["params"]["splitting_window_days"]
    catalog = samplers.catalog

    master = _plan_master(samplers, params, counts, rng)
    n_vendors = counts["n_vendors"]
    buyers = ["buyer", "buyer2", "buyer3"]   # resolved against gen_config.json at APPLY time
    managers = ["manager", "manager2"]

    # period bounds
    period_start = datetime.date.fromisoformat(params["generation_period"]["start_date"])
    period_days = (datetime.date.fromisoformat(params["generation_period"]["end_date"])
                   - period_start).days

    # Vendor/bank create_date realism happens in a FINAL pass, after every PO-adding
    # section (including S4-evasive) — see "final pass" below, right before
    # scenario 3. It needs the FINISHED per-vendor first-PO dates, not a snapshot taken here
    # before any PO exists; build_normal.py applies whatever `master["vendors"][*]["create_date"]`
    # ends up holding via tsmap (TimestampMap cells are last-write-wins, safely overriding
    # build_master.py's uniform "2025-01-01" stamp once the real Odoo ids exist).

    pos = []
    seq = [0]

    def new_po(vendor_idx, date_order, lines, *, buyer_session, scenario=None,
               role="normal", anomaly=None, full_tail=False, rfq_sent=True, variant_class=None,
               cancel_after=None):
        seq[0] += 1
        total = _po_total(lines)
        pos.append({
            "po_key": f"PO{seq[0]:05d}",
            "vendor_idx": vendor_idx,
            "buyer_session": buyer_session,
            "date_order": date_order.isoformat(),
            "lines": lines,
            "amount_total": total,
            "above_threshold": total > threshold,
            "full_tail": full_tail,
            "rfq_sent": rfq_sent,
            "scenario": scenario,
            "role": role,
            "anomaly": anomaly or {},
            # "canonical" | "evasive" | None (normals/hard negatives/siblings) — carried
            # alongside `scenario` so labels.py can emit the variant split additively (§7).
            "variant_class": variant_class,
            # None | "confirm" | "approve" — which point in the lifecycle
            # build_normal.py should call button_cancel at. Set on normal-background POs only,
            # AFTER every scenario conversion has run (see the cancellation pass below) — never
            # passed at construction time, so this default stays None for every call site above.
            "cancel_after": cancel_after,
        })
        return pos[-1]

    # ---- 1. normal background (params.json distributions, untouched) ----
    alloc = samplers.vendor_allocation(counts["total_normal_pos"])
    dates_by_vendor = samplers.order_timing_dates(alloc)
    tail_share = 0.25                        # anomaly_spec §4 "full-tail normals": ~20-30%

    # full_tail depends on the PO's AGE, not a flat coin flip — a year-old confirmed PO with
    # no invoice at all is not realistic. A PO more than `cutoff_days` before the end of the
    # generation period is "old enough that its bill/payment tail would have completed by now
    # if one were coming"; such POs get a HIGH full_tail probability (old_rate). POs still
    # within the cutoff window of "today" (the end of the generation period) are recent enough
    # that not yet having a bill is unremarkable, so they keep close to the original tail_share
    # (new_rate). This also supplies the cancellation pool below: an OLD PO that still drew
    # full_tail=False (the ~8% minority even at old_rate=0.92) is exactly the "old, unbilled,
    # and nothing is coming" case a cancellation explains.
    def _full_tail_prob(d, period_start, period_days, cutoff_days=35,
                        old_rate=0.92, new_rate=0.25):
        period_end = period_start + datetime.timedelta(days=period_days)
        return old_rate if (period_end - d).days > cutoff_days else new_rate

    manager_normal_share = 0.05   # managers do place ordinary orders in a real SME; without
    # this, "buyer is a manager" would be a perfect proxy for S4 (every S4 positive is
    # manager-authored and no other PO ever is). Kept mostly below threshold below so
    # build_normal.py's approver-must-differ-from-manager-author guard
    # has little to do — an above-threshold manager-authored PO self-approves in Odoo and
    # would become an unlabelled S4 unless that guard routes it to the OTHER manager.
    for v_idx, dates in enumerate(dates_by_vendor):
        for d in dates:
            if rng.random() < manager_normal_share:
                buyer_session = managers[int(rng.integers(0, len(managers)))]
            else:
                buyer_session = buyers[int(rng.integers(0, len(buyers)))]
            new_po(v_idx, d, _build_lines(samplers, v_idx, catalog),
                   buyer_session=buyer_session,
                   full_tail=bool(rng.random() < _full_tail_prob(d, period_start, period_days)),
                   # a minority of ordinary POs skip "RFQ Sent" (phone/recurring orders) —
                   # anomaly_spec §2 S4: this alone is a legitimate process variant
                   rfq_sent=bool(rng.random() > 0.15))

    normal_pool = list(pos)                  # snapshot before any injection

    # Used only by the post-hoc band nudge below (a bounded last-resort +-1 qty tweak to
    # land an S2 sibling inside its exact band): the max line qty that EVER appears in the
    # normal background, so that even a last-resort nudge never pushes a value toward or past
    # what a normal PO could show. _build_lines_to_amount does not use qty as a lever
    # at all, so this is NOT a per-line ceiling for injected POs — see its docstring.
    qty_cap = max((l["qty"] for p in normal_pool for l in p["lines"]), default=20)

    # ---- 2. scenario 1 — duplicate invoicing / payment (per-PO) ----
    # Every S1 case needs a real bill+payment tail to duplicate, so pick from the POs that
    # already have one and upgrade the rest.
    s1_targets = _sample_pos(rng, normal_pool, counts["s1_canonical"])
    variants = ["identical", "rounding_delta", "different_date", "bill_only"]
    for i, po in enumerate(s1_targets):
        po["full_tail"] = True
        po["scenario"] = S1
        po["role"] = "positive"
        po["variant_class"] = "canonical"
        variant = variants[i % len(variants)]
        po["anomaly"] = {
            "type": S1, "variant": variant,
            # vary conspicuousness (§2 S1): not every case is a pixel-perfect duplicate
            "amount_delta": _round2(rng.uniform(-0.6, 0.6)) if variant == "rounding_delta" else 0.0,
            "date_offset_days": int(rng.integers(3, 21)) if variant == "different_date" else 0,
            "second_payment": variant != "bill_only",
        }

    # ---- 2b. scenario 1 EVASIVE (anomaly_spec.md) — same vendor invoice re-billed
    # against a DIFFERENT PO of the same vendor. §2 S1's counting pin: this produces PAIRS —
    # `s1_evasive_pairs` pairs = 2x that many LABELLED POs. Each PO of a pair ends up with
    # exactly one bill (count_bills > 1 never trips), so the frozen R1 is expected recall 0 here
    # — the duplication is only visible by comparing bills ACROSS the vendor's POs, which is
    # exactly what the vendor rollup's per-PO bills[] (doc_number/amount_total/date) supports.
    # The ANCHOR po keeps its normal build_normal.py-built bill+payment (full_tail=True); the
    # DUPLICATE po gets full_tail=FALSE here so build_normal.py builds it NO natural bill, and
    # inject_anomalies.py's cross-PO S1 path creates its single bill+payment manually, forced to
    # (near-)match the anchor's bill amount — see that module's _apply_s1_cross_po().
    vendor_normal_pos = {}
    for p in normal_pool:
        if p["role"] == "normal" and p["scenario"] is None:
            vendor_normal_pos.setdefault(p["vendor_idx"], []).append(p)
    eligible_s1_vendors = sorted(v for v, ps in vendor_normal_pos.items() if len(ps) >= 2)
    n_pairs = min(counts.get("s1_evasive_pairs", 0), len(eligible_s1_vendors))
    if n_pairs:
        pair_vendors = sorted(int(v) for v in
                              rng.choice(eligible_s1_vendors, size=n_pairs, replace=False))
        for i, v_idx in enumerate(pair_vendors):
            pool = vendor_normal_pos[v_idx]
            idxs = rng.choice(len(pool), size=2, replace=False)
            anchor_po, dup_po = pool[int(idxs[0])], pool[int(idxs[1])]
            shared_ref = f"VINV-{i:04d}"
            anchor_po["full_tail"] = True
            anchor_po["scenario"] = S1
            anchor_po["role"] = "positive"
            anchor_po["variant_class"] = "evasive"
            anchor_po["anomaly"] = {"type": S1, "variant": "cross_po", "pair_role": "anchor",
                                    "pair_id": i, "shared_ref": shared_ref}
            dup_po["full_tail"] = False           # build_normal.py must NOT give it its own bill
            dup_po["scenario"] = S1
            dup_po["role"] = "positive"
            dup_po["variant_class"] = "evasive"
            dup_po["anomaly"] = {
                # NOTE: no anchor_po_key here on purpose. po_key gets reassigned in place by
                # the chronological renumbering pass below, so any po_key string captured
                # here would go stale. The anchor is instead found later by matching pair_id +
                # pair_role == "anchor" within the same vendor, which is stable under renumbering.
                "type": S1, "variant": "cross_po", "pair_role": "duplicate", "pair_id": i,
                "shared_ref": shared_ref,
                # §2 S1 conspicuousness: not every duplicate is pixel-perfect
                "amount_delta": _round2(rng.uniform(-0.6, 0.6)) if rng.random() < 0.5 else 0.0,
                "date_offset_days": int(rng.integers(3, 16)),   # "posted days apart"
            }

    # ---- 3. scenario 5 — abnormal purchase amount (per-PO) ----
    s5_targets = _sample_pos(rng, [p for p in normal_pool if p["scenario"] is None],
                             counts["s5_canonical"])
    for po in s5_targets:
        factor = float(rng.uniform(1.5, 3.0))      # §2 S5: spread across the band, not all 3x
        n_hit = 1 if len(po["lines"]) == 1 else int(rng.integers(1, len(po["lines"]) + 1))
        # A RANDOM subset of line positions, not always the prefix — otherwise the
        # over-priced line is always index 0, a construction artefact of its own.
        hit_idx = set(int(i) for i in rng.choice(len(po["lines"]), size=n_hit, replace=False))
        for j, l in enumerate(po["lines"]):
            if j in hit_idx:
                l["unit_price"] = _round2(l["standard_price"] * factor)
        po["amount_total"] = _po_total(po["lines"])
        po["above_threshold"] = po["amount_total"] > threshold
        po["scenario"] = S5
        po["role"] = "positive"
        po["variant_class"] = "canonical"
        po["anomaly"] = {"type": S5, "over_price_factor": round(factor, 3), "lines_hit": n_hit}

    # ---- 3b. scenario 5 EVASIVE (anomaly_spec.md) — sustained over-payment at
    # 1.2-1.4x standard cost (below R5's 1.5x cutoff) on a line the SAME vendor previously sold
    # CHEAPER (a genuine trend against the vendor's own price history, not a single reading).
    # The PO total must also stay under 3x the vendor average so R5's amount branch stays quiet
    # too. Converts an existing normal PO, like canonical S5 — never a fresh PO.
    #
    # Find (vendor, product) pairs that appear on >=2 of that vendor's POs (across the WHOLE
    # dataset built so far — the earlier PO need not still be "normal"; extract.py's
    # vendor_prior_avg_unit_price is computed from date_order alone, any role). The LATEST PO in
    # such a group is the evasive candidate: modify ONLY its line for that product (never a
    # random subset here — the trend must land on the product with prior history), so
    # `vendor_prior_avg_unit_price` on that exact line is populated and ~1.0x.
    vp_hist = {}
    po_by_key_s5 = {p["po_key"]: p for p in normal_pool}
    for p in normal_pool:
        # dedupe: a PO with the SAME product on two lines must only contribute ONE history
        # point, or a two-line PO could look like its own "prior sale" (len(hist)==2 both from
        # the very PO being considered for conversion) and the "prior" would not be prior at all.
        seen_products = set()
        for l in p["lines"]:
            pid = l["product_idx"]
            if pid in seen_products:
                continue
            seen_products.add(pid)
            vp_hist.setdefault((p["vendor_idx"], pid), []).append((p["date_order"], p["po_key"]))
    catalog_median = float(statistics.median(catalog))
    s5_evasive_candidates = []
    for (v_idx, p_idx), hist in vp_hist.items():
        if len(hist) < 2 or float(catalog[p_idx]) < catalog_median:   # "a HIGH-VALUE line"
            continue
        hist_sorted = sorted(hist)                      # (date_order iso str, po_key)
        later_date, later_key = hist_sorted[-1]
        prior_date = hist_sorted[0][0]
        if not (prior_date < later_date):
            # extract.py's vendor_prior_avg_unit_price only counts STRICTLY earlier dates —
            # two POs sharing the exact same date_order never count each other as "prior"
            # (po_context.sql tie-break groups by date), so this pair would not actually
            # populate the field we need. Skip it rather than plant an evasive case the extract
            # can't support.
            continue
        later_po = po_by_key_s5.get(later_key)
        if later_po is None or later_po["role"] != "normal" or later_po["scenario"] is not None:
            continue
        # the PRIOR po's price for this product must still be ~1.0x (untouched by any other
        # scenario) — otherwise "the vendor previously sold it cheaper" is not actually true
        # (e.g. if that earlier line was itself an S5-canonical target, its price could already
        # be inflated ABOVE the 1.2-1.4x this variant is about to apply).
        prior_po = po_by_key_s5.get(hist_sorted[0][1])
        if prior_po is None or prior_po["role"] != "normal" or prior_po["scenario"] is not None:
            continue
        s5_evasive_candidates.append((v_idx, p_idx, later_po, prior_date))
    order = list(rng.permutation(len(s5_evasive_candidates)))
    used_po_keys = set()
    n_taken = 0
    for oi in order:
        if n_taken >= counts.get("s5_evasive", 0):
            break
        v_idx, p_idx, po, prior_date = s5_evasive_candidates[int(oi)]
        if po["po_key"] in used_po_keys:
            continue
        line = next((l for l in po["lines"] if l["product_idx"] == p_idx), None)
        if line is None:
            continue
        factor = float(rng.uniform(1.2, 1.4))            # below R5's 1.5x cutoff
        new_total = _po_total([l if l is not line else
                               {**l, "unit_price": _round2(line["standard_price"] * factor)}
                               for l in po["lines"]])
        # keep the total under 3x this vendor's normal-population average — an approximation
        # of vstats (computed only at extract time) using the normal POs seen so far for this
        # vendor, which is the best information available at plan time.
        vendor_amounts = [p["amount_total"] for p in normal_pool if p["vendor_idx"] == v_idx]
        approx_avg = statistics.mean(vendor_amounts) if vendor_amounts else new_total
        if new_total > 3 * approx_avg:
            continue
        line["unit_price"] = _round2(line["standard_price"] * factor)
        po["amount_total"] = _po_total(po["lines"])
        po["above_threshold"] = po["amount_total"] > threshold
        po["scenario"] = S5
        po["role"] = "positive"
        po["variant_class"] = "evasive"
        po["anomaly"] = {"type": S5, "variant": "evasive_trend", "over_price_factor": round(factor, 3),
                         "product_idx": p_idx, "prior_sale_date": prior_date}
        used_po_keys.add(po["po_key"])
        n_taken += 1

    # ---- 4. scenario 4 — approval bypass (per-PO) ----
    # MANDATORY ANCHOR: above threshold AND the real approver is the buyer of record.
    # In Odoo that means a purchase MANAGER authors and confirms it, self-approving. So these
    # POs are added (not converted from normals) with a manager session as buyer.
    #
    # The S4 amount band must stay inside the normal background's above-threshold range, or
    # `amount_total` alone would separate S4. _build_lines_to_amount draws full ordinary POs and
    # keeps whichever lands in band, so the ACCEPTED amounts follow the normal distribution's own
    # shape restricted to that band. The band is 1.15-2.0x threshold = [$23,000, $40,000],
    # derived defensively (never hard-coded): min(2.0x threshold, a small headroom below the REALISED normal
    # above-threshold maximum), so it still can't exceed what the normal background actually
    # produced even if params.json/the catalogue change.
    normal_above_threshold = sorted(
        p["amount_total"] for p in normal_pool
        if p["role"] == "normal" and p["scenario"] is None and p["amount_total"] > threshold)
    if normal_above_threshold:
        # 5% headroom below the realised MAX (the sampling itself, not the ceiling, is what
        # keeps S4 in-distribution).
        normal_amount_ceiling = normal_above_threshold[-1] * 0.95
    else:
        normal_amount_ceiling = threshold * 2.0
    s4_hi = min(threshold * 2.0, normal_amount_ceiling)
    s4_lo = threshold * 1.15
    if s4_hi <= s4_lo:
        raise RuntimeError(
            f"the normal background's above-threshold ceiling ({normal_amount_ceiling:,.0f}, "
            f"derived from {len(normal_above_threshold)} normal POs above {threshold:,.0f}) "
            f"leaves no room for the S4 band above {s4_lo:,.0f} — params.json's catalogue may "
            "need widening; the plan aborts here rather than silently shrinking the band "
            "or introducing an out-of-distribution amount.")
    qty_cap_s4 = qty_cap   # S4 band nudge reuses the same normal-population qty ceiling as S2

    for i in range(counts["s4_canonical"]):
        v_idx = int(rng.integers(0, n_vendors))
        target = float(rng.uniform(s4_lo, s4_hi))   # comfortably above threshold, in-distribution
        d = period_start + datetime.timedelta(days=int(rng.integers(0, period_days)))
        lines = _build_lines_to_amount(samplers, v_idx, catalog, target, rng)
        s4_po = new_po(v_idx, d, lines,
               buyer_session=managers[i % len(managers)],
               scenario=S4, role="positive", variant_class="canonical",
               full_tail=bool(rng.random() < _full_tail_prob(d, period_start, period_days)),
               # co-signals are optional and mixed, so none of them alone carries a case (§2 S4)
               rfq_sent=bool(rng.random() > 0.5),
               anomaly={"type": S4, "self_approval": True,
                        "fast_approval": bool(rng.random() < 0.5)})
        # Enforce the [s4_lo, s4_hi] band exactly, rather than trusting
        # _build_lines_to_amount's arithmetic (it can overshoot `target` by a whole extra line) —
        # mirrors the S2 sibling band nudge. A failed assert aborts the plan.
        _nudge_amount_into_band(s4_po, s4_lo, s4_hi, qty_cap_s4, threshold)
        # mandatory anchor: S4 is only a valid case if it IS above threshold — always true
        # here since s4_lo > threshold by construction, but assert rather than trust it.
        assert s4_po["above_threshold"], (
            f"S4 {s4_po['po_key']} amount {s4_po['amount_total']} not above threshold "
            f"{threshold} (target was {target})")

    # S4-evasive itself is placed further down (section 6b, after S2 + hard negatives) — see the
    # note there for why it must run last among the PO-adding sections.

    # ---- 5. scenario 2 — PO/invoice splitting (per-VENDOR) ----
    splitter_vendors = sorted(int(v) for v in
                              rng.choice(n_vendors, size=min(counts["s2_splitter_vendors"],
                                                             n_vendors), replace=False))
    vendor_labels = {v: None for v in range(n_vendors)}
    for v_idx in splitter_vendors:
        n_sib = int(rng.integers(2, 5))               # 2-4 siblings per cluster (§2 S2)
        anchor = period_start + datetime.timedelta(days=int(rng.integers(0, max(1, period_days - window))))
        for d in _spread_dates(rng, anchor, n_sib, window):
            # 65-95% of threshold: above baseline.sql's 0.60 sibling floor with clean margin,
            # below the threshold itself — the near-threshold bunching signature (Palguta &
            # Pertold). NOT parked at 99%, so the case is not trivially separable.
            target = threshold * float(rng.uniform(0.65, 0.95))
            sib_lines = _build_lines_to_amount(samplers, v_idx, catalog, target, rng)
            sib = new_po(v_idx, d, sib_lines,
                   buyer_session=buyers[int(rng.integers(0, len(buyers)))],
                   scenario=None,          # §7: sibling POs are labelled null at the PO grain
                   role="s2_sibling",
                   full_tail=bool(rng.random() < _full_tail_prob(d, period_start, period_days)))
            # Enforce the [0.65, 0.95] x threshold band exactly rather than trusting the
            # line-building arithmetic to land in it. Shared with
            # S4's band nudge (_nudge_amount_into_band, above) — same qty-only,
            # never-touch-price/product technique, asserted rather than trusted.
            lo, hi = threshold * 0.65, threshold * 0.95
            _nudge_amount_into_band(sib, lo, hi, qty_cap, threshold)
        vendor_labels[v_idx] = S2

    # ---- 6. hard negatives (§6) — extra legitimate-but-suspicious-looking cases ----
    # per-kind count, not a total split three ways, so each kind has enough cases to measure
    # S1/S4/S5 precision.
    hn_each = max(2, round((counts["s1_canonical"] + counts.get("s1_evasive_pairs", 0) * 2)
                           * 0.25))  # full: 40*0.25=10; smoke: 2
    non_splitters = [v for v in range(n_vendors) if vendor_labels[v] is None]
    # Half the S4 hard negatives go to a manager session too — otherwise "buyer is a
    # manager" alone would separate S4-kind hard negatives from the rest. Chosen via the seeded rng, not a fixed slot, so no
    # construction-order tell survives either.
    n_mgr_hn = round(hn_each / 2)
    mgr_hn_slots = set(int(x) for x in rng.choice(hn_each, size=min(n_mgr_hn, hn_each),
                                                   replace=False))
    # A ROUND-ROBIN sequence over an independently reshuffled buyer order each lap: which buyer
    # lands in which slot still comes from the seeded rng, but no buyer can appear more than
    # ceil(n / len(buyers)) times, so the <=60% bound below holds BY CONSTRUCTION rather than by
    # chance, for any n and any seed.
    def _balanced_session_sequence(sessions, n):
        if not sessions or n <= 0:
            return []
        seq = []
        while len(seq) < n:
            seq.extend(sessions[int(i)] for i in rng.permutation(len(sessions)))
        seq = seq[:n]
        return [seq[int(i)] for i in rng.permutation(len(seq))]

    n_s4_hn_buyer_slots = hn_each - min(n_mgr_hn, hn_each)
    s4_hn_buyer_seq = _balanced_session_sequence(buyers, n_s4_hn_buyer_slots)
    s1_hn_buyer_seq = _balanced_session_sequence(buyers, hn_each)
    s5_hn_buyer_seq = _balanced_session_sequence(buyers, hn_each)
    s4_hn_counter = s4_buyer_i = s1_buyer_i = s5_buyer_i = 0
    for i in range(3 * hn_each):
        v_idx = int(rng.integers(0, n_vendors))
        d = period_start + datetime.timedelta(days=int(rng.integers(0, period_days)))
        kind = i % 3
        # Draw the hard-negative buyer independently of `kind`, so buyer identity is not a
        # proxy for hard-negative kind.
        if kind == 0:
            # S4 HN: below-threshold routine reorder that skips "RFQ Sent" and/or is approved
            # fast. A lone sequence deviation is never suspicious.
            is_mgr_hn = s4_hn_counter in mgr_hn_slots
            s4_hn_counter += 1
            if is_mgr_hn:
                buyer_session = managers[int(rng.integers(0, len(managers)))]
            else:
                buyer_session = s4_hn_buyer_seq[s4_buyer_i]
                s4_buyer_i += 1
            new_po(v_idx, d, _build_lines(samplers, v_idx, catalog),
                   buyer_session=buyer_session, role="hard_negative",
                   rfq_sent=False, anomaly={"hard_negative_for": S4})
        elif kind == 1:
            # S1 HN: a legitimately-two-bills PO (down payment + final invoice).
            po = new_po(v_idx, d, _build_lines(samplers, v_idx, catalog),
                        buyer_session=s1_hn_buyer_seq[s1_buyer_i],
                        role="hard_negative", full_tail=True,
                        anomaly={"hard_negative_for": S1, "two_bills": "down_payment_then_final",
                                 "down_payment_ratio": 0.3})
            s1_buyer_i += 1
            po["full_tail"] = True
        else:
            # S5 HN: a genuine premium sitting at the LOW end of the over-price band, i.e. AT
            # or just above the frozen R5 1.5x cutoff — the precision battleground for S5.
            lines = _build_lines(samplers, v_idx, catalog)
            f = float(rng.uniform(1.5, 1.6))
            lines[0]["unit_price"] = _round2(lines[0]["standard_price"] * f)
            new_po(v_idx, d, lines, buyer_session=s5_hn_buyer_seq[s5_buyer_i],
                   role="hard_negative",
                   anomaly={"hard_negative_for": S5, "legitimate_premium_factor": round(f, 3)})
            s5_buyer_i += 1
    # S2 HN needs no injection: ordinary vendors whose small routine orders merely sum past the
    # threshold in a window arise naturally from the parametric background (§6).

    # Acceptance check: no buyer session may account for more than 60% of any single
    # hard-negative kind. Assert rather than trust the draw — a failed assert stops the plan.
    hn_pos = [p for p in pos if p["role"] == "hard_negative"]
    by_kind = {}
    for p in hn_pos:
        k = p["anomaly"].get("hard_negative_for")
        by_kind.setdefault(k, []).append(p["buyer_session"])
    for k, sessions in by_kind.items():
        if len(sessions) < 5:   # too few draws for the 60% check to be meaningful (smoke scale)
            continue
        counts_by_buyer = {b: sessions.count(b) for b in set(sessions)}
        top_share = max(counts_by_buyer.values()) / len(sessions)
        assert top_share <= 0.60, (
            f"hard-negative kind {k!r} is {top_share:.0%} one buyer session "
            f"({counts_by_buyer}) — buyer identity still proxies hard-negative kind")

    # ---- 6b. scenario 4 EVASIVE — the "rubber-stamp" approval.
    # Buyer and approver are genuinely different people (self_approval FALSE — the frozen R4
    # never fires), but the approval is not a real control: granted seconds after confirmation,
    # "RFQ Sent" skipped, and the vendor created only days earlier. No single co-signal is
    # decisive alone; the conjunction is what makes this a positive.
    #
    # Each evasive PO is placed as an EARLY ORDER of an EXISTING population vendor (idx < n_vendors) — dated before every OTHER
    # PO that vendor has, so it becomes that vendor's first order. `vendor_po_count` and
    # `vendor_span_days` then look completely ordinary (that vendor still has its usual ~10+
    # POs spread across the year); only the identity of WHICH of its POs happens to be earliest
    # changes. This section runs LAST among the PO-adding sections (after S1/S5/S4-canonical/S2
    # siblings/hard negatives) so "that vendor's current earliest PO" is the FINAL earliest —
    # nothing added afterwards can undercut it (scenario 3, below, adds no new POs, only
    # vendor-master edits). `master["vendors"]` and `n_vendors` are untouched — no extra
    # vendors, so the vendor-grain denominator stays exactly n_vendors.
    vendor_min_date_so_far = {}
    for p in pos:
        v, d = p["vendor_idx"], p["date_order"]
        if v not in vendor_min_date_so_far or d < vendor_min_date_so_far[v]:
            vendor_min_date_so_far[v] = d

    # Prefer vendors with enough headroom before their current earliest PO to place a genuinely
    # EARLIER order without clamping into the generation period's start — avoids a same-day tie
    # with that vendor's other earliest PO (a tie is not a correctness bug, just a slightly
    # weaker case, so this is a preference, not a hard requirement).
    s4_evasive_n = counts.get("s4_evasive", 0)
    MIN_HEADROOM_DAYS = 12
    roomy = [v for v in range(n_vendors)
             if v in vendor_min_date_so_far and
             (datetime.date.fromisoformat(vendor_min_date_so_far[v][:10]) - period_start).days
             >= MIN_HEADROOM_DAYS]
    pool = roomy if len(roomy) >= s4_evasive_n else \
        [v for v in range(n_vendors) if v in vendor_min_date_so_far]
    s4_evasive_vendors = sorted(int(v) for v in
                                rng.choice(pool, size=min(s4_evasive_n, len(pool)), replace=False))

    for v_idx in s4_evasive_vendors:
        cur_min = datetime.date.fromisoformat(vendor_min_date_so_far[v_idx][:10])
        headroom = max((cur_min - period_start).days, 0)
        gap_days = min(int(rng.integers(1, 8)), headroom) if headroom else 0
        d = cur_min - datetime.timedelta(days=gap_days)   # strictly (or, worst case, tied) earlier

        target = float(rng.uniform(s4_lo, s4_hi))   # same in-distribution band as canonical
        lines = _build_lines_to_amount(samplers, v_idx, catalog, target, rng)
        buyer_session = buyers[int(rng.integers(0, len(buyers)))]   # a genuine non-manager buyer
        s4e_po = new_po(v_idx, d, lines,
               buyer_session=buyer_session,
               scenario=S4, role="positive", variant_class="evasive",
               full_tail=bool(rng.random() < _full_tail_prob(d, period_start, period_days)),
               rfq_sent=False,                     # mandatory co-signal for the evasive case
               anomaly={"type": S4, "self_approval": False, "rubber_stamp": True,
                        "variant": "evasive"})
        # same band assert as canonical S4.
        _nudge_amount_into_band(s4e_po, s4_lo, s4_hi, qty_cap_s4, threshold)
        assert s4e_po["above_threshold"], (
            f"S4-evasive {s4e_po['po_key']} amount {s4e_po['amount_total']} not above "
            f"threshold {threshold} (target was {target})")
        vendor_min_date_so_far[v_idx] = s4e_po["date_order"]   # this section's own bookkeeping

    # ---- 6c. cancellation — paired with the age-dependent full_tail above (§1). Runs AFTER every scenario/hard-negative section that
    # converts or adds POs (S1/S1-evasive/S5/S5-evasive/S4/S2 siblings/hard negatives/S4-evasive),
    # so filtering on the PO's CURRENT role=="normal" and scenario is None is sufficient to
    # exempt every anomaly-bearing PO and every hard negative BY CONSTRUCTION — no separate
    # exclusion set is needed, because S1/S5 convert existing normal_pool objects IN PLACE (same
    # dict identity) and by this point every conversion has already happened. A cancelled PO
    # remains role="normal", scenario=None — a null-labelled normal, still in the PO-grain
    # denominator (per anomaly_spec's ratio, which counts normal POs regardless of tail/cancel
    # state).
    #
    # Eligibility: an ordinary normal PO, older than the same age cutoff used for full_tail
    # above, that landed full_tail=False — i.e. "old enough that a bill would plausibly have
    # arrived by now, and none did." That is exactly the case a cancellation explains, so the
    # age-dependent full_tail directly supplies the cancellation candidate pool.
    CANCEL_CUTOFF_DAYS = 35   # same cutoff _full_tail_prob uses above
    period_end = period_start + datetime.timedelta(days=period_days)
    cancel_candidates = [
        p for p in pos
        if p["role"] == "normal" and p["scenario"] is None and not p["full_tail"]
        and (period_end - datetime.date.fromisoformat(p["date_order"])).days > CANCEL_CUTOFF_DAYS
    ]
    # Cap: derived from counts["total_normal_pos"] (never hardcoded), sized on the same ~1:8
    # order of magnitude as the PO-grain anomaly ratio itself — enough cancelled POs to be a
    # real, visible population, not so many that cancellations start to dominate the normal
    # background. Cancelling a PO does NOT change the anomaly ratio (a cancelled PO stays a
    # null-labelled normal in the denominator either way) — the cap exists purely to bound how
    # unusual the cancellation rate looks, not to protect the ratio.
    cancel_cap = max(1, round(counts["total_normal_pos"] / 8))
    n_cancel = min(cancel_cap, len(cancel_candidates))
    if n_cancel:
        chosen_idx = rng.choice(len(cancel_candidates), size=n_cancel, replace=False)
        for i in chosen_idx:
            p = cancel_candidates[int(i)]
            p["cancel_after"] = "approve" if p["above_threshold"] else "confirm"

    # Assert the PO-grain anomaly ratio stays ~1:8 (anomaly_spec.md §5:
    # PO grain ~120:1000). Cancellation marks existing normal POs in place and adds none, so this
    # is a guard against a future edit accidentally shrinking the normal denominator, not a
    # consequence of the cancellation pass itself.
    n_positive = sum(1 for p in pos if p["role"] == "positive")
    n_normal_denom = sum(1 for p in pos if p["role"] == "normal")
    if n_normal_denom:
        ratio = n_positive / n_normal_denom
        assert 1 / 12 <= ratio <= 1 / 5, (
            f"PO-grain anomaly ratio drifted to {n_positive}:{n_normal_denom} "
            f"(={ratio:.3f}) — expected ~1:8 (0.083-0.20 tolerance band)")

    # ---- final pass — vendor create_date anchored to each vendor's TRUE first PO.
    # Must run HERE: after every PO-adding section (including S4-evasive just above), so "first
    # PO" reflects the FINAL dataset, not a snapshot taken before injection. build_master.py
    # stamps every vendor + its onboarding bank account
    # at the SAME constant "2025-01-01" — without giving EVERY vendor a plausible, varied
    # create_date the same way, only the deliberately-early S4-evasive ones would look
    # different. Anchoring to each vendor's own first PO is also more realistic — a supplier record gets created when you first buy from them — and it
    # means "this vendor's create_date sits shortly before its earliest order" is true for ALL
    # n_vendors vendors, not just the 12 evasive ones, so being newly onboarded relative to
    # one's own first order is not on its own diagnostic (per anomaly_spec.md §2 S4
    # evasive's design: no single co-signal decides the case).
    final_vendor_min_date = {}
    for p in pos:
        v, d = p["vendor_idx"], p["date_order"]
        if v not in final_vendor_min_date or d < final_vendor_min_date[v]:
            final_vendor_min_date[v] = d
    for v in master["vendors"]:
        idx = v["idx"]
        first_po_date_s = final_vendor_min_date.get(idx)
        anchor_date = (datetime.date.fromisoformat(first_po_date_s[:10])
                      if first_po_date_s else period_start)
        offset_days = int(rng.integers(3, 30))    # "shortly before its own first order"
        v["create_date"] = _dt_to_odoo(anchor_date - datetime.timedelta(days=offset_days))

    # ---- 7. scenario 3 — vendor-master modification (per vendor-trail) ----
    # SCOPE PIN (§2 S3): every POSITIVE, canonical AND evasive, is a
    # BANK-ACCOUNT case — the payoff channel is the bank account (a reverted vat edit diverts
    # nothing). No positive below ever touches vat. The dataset is
    # tax-free by design (amount_untaxed == amount_total for every PO), so vendor VAT numbers
    # have no functional role — there is no legit-vat hard-negative pool (see
    # the noise loop's phone/street edits and the legit-bank pool below for the audit-trail
    # denominator).
    s3_canonical_n = counts["s3_canonical"]
    s3_evasive_n = counts.get("s3_evasive", 0)
    s3_total = s3_canonical_n + s3_evasive_n
    tampered = sorted(int(v) for v in rng.choice(
        non_splitters or list(range(n_vendors)),
        size=min(s3_total, len(non_splitters) or n_vendors),
        replace=False))
    canonical_tampered = tampered[:s3_canonical_n]
    evasive_tampered = tampered[s3_canonical_n:s3_canonical_n + s3_evasive_n]
    audit_labels = {}
    audit_variant_labels = {}      # "canonical" | "evasive", same key set as audit_labels
    audit_edits = []

    # legitimate-edit noise across many vendors defines the denominator: every vendor with
    # >= 1 tracked change is a scoring unit, so the tampered ones must not be the only vendors
    # that have any audit trail at all. Sized generously so there is still room, after removing
    # `tampered`, for the legit-bank hard-negative pool below.
    noise_vendors = sorted(int(v) for v in rng.choice(
        n_vendors, size=min(max(s3_total * 3, 20), n_vendors), replace=False))
    for v_idx in sorted(set(noise_vendors) | set(tampered)):
        d = period_start + datetime.timedelta(days=int(rng.integers(0, period_days)))
        field, new_value = ("phone", f"+66-2-{700 + v_idx}-{2000 + v_idx * 3}") if v_idx % 2 \
            else ("street", f"{200 + v_idx} Bangna-Trad Rd.")
        audit_edits.append({
            "vendor_idx": v_idx, "kind": "legit", "model": "res.partner", "field": field,
            "new": new_value, "at": d.isoformat(),
            "actor_session": managers[v_idx % len(managers)],
        })
        audit_labels[v_idx] = None

    for v_idx in canonical_tampered:
        # change-then-revert around a payment run: the bank account NUMBER is altered shortly
        # BEFORE the payment and restored AFTER it, so the trail holds two tracked changes on
        # res.partner.bank (§2 S3 canonical, bank-only scope pin).
        pay_day = period_start + datetime.timedelta(days=int(rng.integers(30, max(31, period_days - 10))))
        before = pay_day - datetime.timedelta(days=int(rng.integers(1, 4)))
        after = pay_day + datetime.timedelta(days=int(rng.integers(1, 6)))
        audit_edits.append({
            "vendor_idx": v_idx, "kind": "tamper", "model": "res.partner.bank",
            "field": "acc_number", "new": _random_bank_acc(rng),
            "at": before.isoformat(), "revert_at": after.isoformat(),
            "payment_run_at": pay_day.isoformat(),
            # A SECOND user makes the edit — the actor differs from the usual maintainer, which
            # is the contextual cue the LLM must read (the extract carries no 'approved' flag).
            # Writing res.partner.bank requires base.group_partner_manager, which a plain
            # purchase manager does not have, so this is pinned to the symbolic "setup" session
            # (resolved at APPLY time to whichever manager holds the elevated rights). A manager
            # with system rights altering payee bank details is itself the segregation-of-duties
            # weakness this scenario is about, so this is realistic rather than a workaround.
            "actor_session": "setup",
        })
        audit_labels[v_idx] = S3
        audit_variant_labels[v_idx] = "canonical"

    # ---- 7b. scenario 3 EVASIVE (anomaly_spec.md) — a NEW bank account is added and
    # paid to; the original account is never touched, so `changes[]` gets nothing and the frozen
    # R3 (which only watches EDITS to an existing bank field) cannot fire. Preferentially
    # anchored to a vendor that already has >= 1 full-tail normal PO, so a real payment
    # plausibly follows shortly after — a best-effort heuristic at plan time only (the actual
    # bill/payment dates are drawn later, in build_normal.py's process-latency sampling, so this
    # is NOT verified against a real payment date here).
    full_tail_by_vendor = {}
    for p in pos:
        if p["role"] == "normal" and p.get("full_tail"):
            full_tail_by_vendor.setdefault(p["vendor_idx"], []).append(p)
    for v_idx in evasive_tampered:
        candidates = full_tail_by_vendor.get(v_idx)
        if candidates:
            anchor_po = candidates[int(rng.integers(0, len(candidates)))]
            anchor_date = datetime.date.fromisoformat(anchor_po["date_order"][:10])
        else:
            anchor_date = period_start + datetime.timedelta(
                days=int(rng.integers(30, max(31, period_days - 30))))
        # rough receipt -> bill -> payment chain (a couple of weeks), so the addition lands
        # shortly BEFORE the vendor's likely next payment.
        est_payment = anchor_date + datetime.timedelta(days=int(rng.integers(14, 28)))
        add_date = est_payment - datetime.timedelta(days=int(rng.integers(1, 4)))
        audit_edits.append({
            "vendor_idx": v_idx, "kind": "bank_add", "model": "res.partner.bank",
            # No "field" — an addition is not a field edit (that's the whole point of the
            # evasive variant), so there is genuinely nothing to name here. This is a
            # plan-bookkeeping-only structure; the real extracted schema
            # (vendor_audit.schema.json's `bank_account_additions[]`) has no "field" key at all
            # for additions either, so a detector reading the actual extract never sees this
            # None.
            "field": None,
            "new": _random_bank_acc(rng), "at": add_date.isoformat(),
            "payment_run_at_estimate": est_payment.isoformat(),
            # Non-routine actor (spec) — see generate.py's cluster.alias() call for the
            # ACCESS-RIGHTS caveat: this resolves to the SAME session as "setup" unless
            # a second elevated (base.group_partner_manager) user is provisioned.
            "actor_session": "s3_evasive_adder",
        })
        audit_labels[v_idx] = S3
        audit_variant_labels[v_idx] = "evasive"

    # legitimate SENSITIVE-field hard negatives: without these, "was the bank account ever
    # touched?" separates tamper from legit by construction, since the noise loop above only
    # ever edits phone/street. A supplier genuinely changing banks is a single PERMANENT change
    # — no change-then-revert, not timed around a payment run — and stays labelled null. Drawn
    # from vendors already in the noise set (so they're legitimately in the audit denominator)
    # and not already tampered.
    tampered_set = set(tampered)
    eligible = [v for v in noise_vendors if v not in tampered_set]
    if not eligible:
        eligible = [v for v in range(n_vendors) if v not in tampered_set]
    n_legit_bank = max(1, round(s3_total * 0.5))  # full: 18*0.5=9; smoke: 1
    n_legit_bank = min(n_legit_bank, len(eligible))
    legit_bank_vendors = sorted(int(v) for v in
                                rng.choice(eligible, size=n_legit_bank, replace=False))
    for v_idx in legit_bank_vendors:
        # first ~120 days of the period, well away from any payment run — tampers sit at
        # pay_day >= day 30 plus their own before/after spread; keep this simple and
        # deterministic rather than cross-checking every tamper's window.
        d = period_start + datetime.timedelta(days=int(rng.integers(0, min(120, period_days))))
        audit_edits.append({
            "vendor_idx": v_idx, "kind": "legit", "model": "res.partner.bank",
            "field": "acc_number",
            # same shared generator the tamper edits use — no prefix distinguishes legit
            # from tamper.
            "new": _random_bank_acc(rng),
            "at": d.isoformat(),
            # writing res.partner.bank needs base.group_partner_manager, which only the setup
            # session holds (see inject_anomalies.py's ACCESS RIGHTS note) — a routine
            # maintainer making one ordinary bank-detail change is exactly the benign case.
            "actor_session": "setup",
        })
        # audit_labels[v_idx] is already None from the noise loop above — not a relabel.

    # `pos` is append-ordered by construction PHASE (normals -> S4 -> S2 siblings ->
    # hard negatives), so the PO number would correlate with role, not date. Sort chronologically and reassign the
    # po_key sequence so PO00001 is genuinely the earliest order. build_normal.py, inject_
    # anomalies.py, and labels.py look POs up by po_key in a dict built AFTER this pass, never
    # by list position, so mutating po_key here in place is safe for THOSE lookups — but any
    # po_key string captured earlier (before this pass) and stashed inside a PO's own data
    # (e.g. a cross-referenced sibling's key) goes stale here and must not be relied on; see
    # the S1 evasive pair anomaly block above, which deliberately avoids doing that. Ties on
    # the same date_order
    # break via a seeded permutation, not construction order — otherwise role would still leak
    # through tie order.
    tie_rank = rng.permutation(len(pos))
    order = sorted(range(len(pos)), key=lambda i: (pos[i]["date_order"], int(tie_rank[i])))
    pos = [pos[i] for i in order]
    for new_idx, p in enumerate(pos, start=1):
        p["po_key"] = f"PO{new_idx:05d}"
    assert all(pos[i]["date_order"] <= pos[i + 1]["date_order"] for i in range(len(pos) - 1)), (
        "pos is not sorted by date_order after resequencing")

    # Verify the label denominators exactly match the plan's ACTUAL vendor population.
    # score.py scores a flag on a vendor it has no label for as a false positive, never dropped
    # — so any vendor present in the plan but absent from vendor_labels would be scored
    # unfairly, and any label referencing a vendor id that doesn't exist would silently pad a
    # denominator that is supposed to be a pinned spec number (S2's 18-of-50).
    plan_vendor_ids = {v["idx"] for v in master["vendors"]}
    assert plan_vendor_ids == set(range(n_vendors)), (
        f"master['vendors'] no longer matches range(n_vendors) — {len(plan_vendor_ids)} vendors "
        f"vs n_vendors={n_vendors}")
    assert set(vendor_labels.keys()) == plan_vendor_ids, (
        "vendor_labels does not cover exactly the plan's vendor population — symmetric "
        f"difference: {sorted(set(vendor_labels.keys()) ^ plan_vendor_ids)}")
    assert set(audit_labels.keys()) <= plan_vendor_ids, (
        "audit_labels references vendor id(s) that don't exist in the plan: "
        f"{sorted(set(audit_labels.keys()) - plan_vendor_ids)}")

    return {
        "master": master,
        "pos": pos,
        "vendor_labels": vendor_labels,
        "audit_labels": audit_labels,
        "audit_variant_labels": audit_variant_labels,
        "audit_edits": audit_edits,
        "params_echo": {"approval_threshold": threshold, "splitting_window_days": window,
                        "buyers": buyers, "managers": managers,
                        # APPLY clamps the receipt/bill/payment tail to this date: a PO ordered
                        # in late December would otherwise settle in January and put 2026 dates
                        # in a dataset that claims to cover calendar year 2025.
                        "generation_start": params["generation_period"]["start_date"],
                        "generation_end": params["generation_period"]["end_date"]},
    }


def _sample_pos(rng, pool, k):
    """Pick k distinct POs from `pool` (fewer if the pool is smaller — the smoke scale)."""
    pool = [p for p in pool if p["role"] == "normal"]
    k = min(k, len(pool))
    idx = rng.choice(len(pool), size=k, replace=False) if k else []
    return [pool[int(i)] for i in idx]


# ---------- output ----------
def write_plan(plan, out_dir, dry_run=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    name = "dry_run_plan.json" if dry_run else "plan.json"
    (out_dir / name).write_text(json.dumps(plan, ensure_ascii=False, indent=2, default=str),
                                encoding="utf-8")
    return out_dir / name


def print_summary(plan, threshold):
    pos = plan["pos"]
    amounts = [p["amount_total"] for p in pos]
    amounts_sorted = sorted(amounts)

    def pct(q):
        return amounts_sorted[min(len(amounts_sorted) - 1, int(q * len(amounts_sorted)))]

    by_scenario = {}
    for p in pos:
        by_scenario[p["scenario"] or p["role"]] = by_scenario.get(p["scenario"] or p["role"], 0) + 1

    vendor_counts = {}
    for p in pos:
        vendor_counts[p["vendor_idx"]] = vendor_counts.get(p["vendor_idx"], 0) + 1
    top = sorted(vendor_counts.values(), reverse=True)

    print(f"\n  planned POs: {len(pos)}   vendors: {len(plan['master']['vendors'])}   "
          f"products: {len(plan['master']['products'])}")
    print(f"  ALL POs      : median={statistics.median(amounts):,.0f}  p90={pct(0.90):,.0f}  "
          f"above {threshold:,} = {sum(a > threshold for a in amounts) / len(amounts):.1%}")

    # The frozen calibration in params_derivation.md describes the NORMAL BACKGROUND only.
    # Injected positives are deliberately large (S4 is above threshold by construction, S2
    # siblings sit at 65-95% of it, S5 marks prices up 1.5-3x), so comparing the pooled
    # figures against that record would misread the fidelity of the background.
    norm = sorted(p["amount_total"] for p in pos if p["role"] == "normal")
    if norm:
        n_top = {}
        for p in pos:
            if p["role"] == "normal":
                n_top[p["vendor_idx"]] = n_top.get(p["vendor_idx"], 0) + 1
        nt = sorted(n_top.values(), reverse=True)
        print(f"  NORMAL only  : median={statistics.median(norm):,.0f}  "
              f"p90={norm[int(0.90 * len(norm))]:,.0f}  "
              f"above {threshold:,} = {sum(a > threshold for a in norm) / len(norm):.1%}   "
              f"(frozen target: median ~5,000, 5-10% above)")
        print(f"  vendor concentration (normal): top={nt[0]/len(norm):.1%}  "
              f"top3={sum(nt[:3])/len(norm):.1%}   (target 10.0% / 21.7%)")
    print("  per-PO grain: " + ", ".join(f"{k}={v}" for k, v in sorted(by_scenario.items())))
    print(f"  vendor grain: splitters={sum(1 for v in plan['vendor_labels'].values() if v)}"
          f"/{len(plan['vendor_labels'])}")
    print(f"  audit grain : tampered={sum(1 for v in plan['audit_labels'].values() if v)}"
          f"/{len(plan['audit_labels'])} vendors with a trail  "
          f"({len(plan['audit_edits'])} edits)")

    # canonical/evasive split, per anomaly_spec.md §5's table.
    variant_counts = {}
    for p in pos:
        if p["scenario"]:
            variant_counts.setdefault(p["scenario"], {"canonical": 0, "evasive": 0})
            vc = p.get("variant_class")
            if vc in ("canonical", "evasive"):
                variant_counts[p["scenario"]][vc] += 1
    for v in plan.get("audit_variant_labels", {}).values():
        if v in ("canonical", "evasive"):
            variant_counts.setdefault(S3, {"canonical": 0, "evasive": 0})
            variant_counts[S3][v] += 1
    print("  canonical/evasive: " + ", ".join(
        f"{k}={v['canonical']}/{v['evasive']}" for k, v in sorted(variant_counts.items())))
