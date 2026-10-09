#!/usr/bin/env python
"""Extraction pipeline: Odoo Postgres -> per-PO context JSON (+ vendor rollup).

SQL (sql/po_context.sql) does the heavy spine-resolved join + JSON nesting; this script
orchestrates: run the query, derive timeline + neutral baselines + vendor rollup, validate
every output against the JSON Schemas, and write a manifest. Re-runnable on the synthetic
dataset by editing config.json only.

Run from the repo root:
    python _pipeline/extract.py
"""
import json, os, re, sys, shutil, datetime, collections, pathlib
import psycopg2
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parents[1]          # repo root
CFG = json.loads((ROOT / "_pipeline" / "config.json").read_text(encoding="utf-8"))
THRESHOLD = CFG["params"]["approval_threshold"]
WINDOW = CFG["params"]["splitting_window_days"]


def rel(p):  # config paths are relative to the repo root
    return ROOT / p


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(str(s))
    except ValueError:
        return None


def num(x):
    return float(x) if x is not None else 0.0


REDACTION_CFG = CFG["redaction"]
PII_PATTERNS = {name: re.compile(pat) for name, pat in REDACTION_CFG["forbidden_patterns"].items()}
IDENTITY_LABEL_RE = re.compile(REDACTION_CFG["identity_label_pattern"])
IDENTITY_FIELDS = REDACTION_CFG["identity_fields"]


def _walk_field(obj, parts):
    """Yield every value reachable from `obj` by following dotted path `parts`, where a part
    ending in '[]' (e.g. 'changes[]') means "for each item in this list". Missing keys/lists
    yield nothing rather than raising, since not every object carries every identity field."""
    if not parts:
        yield obj
        return
    part, rest = parts[0], parts[1:]
    if part.endswith("[]"):
        items = obj.get(part[:-2]) if isinstance(obj, dict) else None
        for item in items or []:
            yield from _walk_field(item, rest)
    else:
        if isinstance(obj, dict) and part in obj:
            yield from _walk_field(obj[part], rest)


def assert_no_pii(label, obj):
    """Defense-in-depth, driven by config.json's `redaction` block:
    po_context.sql/vendor_audit.sql already emit synthetic user_<id> labels instead of real
    Odoo login names, so this exists to catch anything that slips through
    regardless, before it reaches disk or an LLM request. Two independent checks: (1) the whole
    serialised object against every declared forbidden pattern (email, phone, ...), which catches
    unexpected leaks anywhere; (2) each declared identity field, where present, must itself match
    the expected user_<id> shape, which catches a real name that isn't email/phone-shaped (e.g.
    a plain "John Smith") even though check (1) would miss it."""
    payload = json.dumps(obj, ensure_ascii=False, default=str)
    for pname, pat in PII_PATTERNS.items():
        m = pat.search(payload)
        if m:
            sys.exit(f"PII GUARD: {pname}-shaped string {m.group()!r} found in {label}")

    for field in IDENTITY_FIELDS:
        for value in _walk_field(obj, field.split(".")):
            if value is not None and not IDENTITY_LABEL_RE.match(str(value)):
                sys.exit(
                    f"PII GUARD: identity field {field!r} in {label} = {value!r} "
                    f"does not match {REDACTION_CFG['identity_label_pattern']!r}")


# ---------- derived sections (Python; facts + neutral comparators only) ----------
# logical phase order so that same-day events (bills/receipts carry date-only granularity,
# PO events carry full timestamps) read in workflow order rather than by midnight.
PHASE = {"created": 0, "order": 1, "sent": 1.5, "approved": 2, "received": 3, "returned": 3,
         "billed": 4, "paid": 5}


def build_timeline(ctx):
    idy = ctx["po_identity"]
    state_log = ctx.get("state_log") or []
    ev = []

    def add(name, ts, actor=None):
        if ts:
            ev.append({"event": name, "timestamp": ts, "actor_user_id": actor})

    # State values arrive through tracking as English selection LABELS ('Purchase Order',
    # 'RFQ Sent'); the generation DB is English so an exact label compare holds.
    # Real approver = actor of the (last) transition INTO 'Purchase Order'; fall back to the
    # buyer of record only when the chatter has no such transition.
    approver_entries = [s for s in state_log if s.get("to_state") == "Purchase Order"]
    approver_uid = approver_entries[-1]["actor_uid"] if approver_entries else idy["buyer"].get("user_id")
    sent_entries = [s for s in state_log if s.get("to_state") == "RFQ Sent"]

    add("created", idy.get("create_date"), idy.get("create_uid"))
    add("order", idy.get("date_order"), idy["buyer"].get("user_id"))
    if sent_entries:
        add("sent", sent_entries[0].get("timestamp"), sent_entries[0].get("actor_uid"))
    add("approved", idy.get("date_approve"), approver_uid)
    for r in ctx["receipts"]:
        kind = "received" if r.get("picking_type") == "incoming" else "returned"
        add(f"{kind}:{r.get('picking')}", r.get("date_done"))
    for b in ctx["bills"]:
        add("billed:" + str(b.get("doc_number")), b.get("date"))
    for pm in ctx["payments"]:
        add("paid:" + str(pm.get("doc_number")), pm.get("date"))

    def key(e):
        dt = parse_dt(e["timestamp"])
        d = dt.date() if dt else datetime.date.max
        return (d, PHASE.get(e["event"].split(":")[0], 9), dt or datetime.datetime.max)

    ev.sort(key=key)
    return ev


def build_baselines(ctx, vstats, vprior):
    lines = ctx["lines"]
    ratios = []
    for l in lines:
        sp = l.get("standard_price")
        ratios.append(round(num(l.get("unit_price")) / num(sp), 4) if sp else None)
    vs = vstats.get(ctx["po_identity"]["vendor"]["id"], {})
    pr = vprior.get(ctx["po_identity"]["po_number"], {})
    return {
        "approval_threshold": THRESHOLD,
        "amount_total": ctx["po_identity"].get("amount_total"),
        "vendor_po_count_alltime": vs.get("count"),
        "vendor_avg_amount_alltime": vs.get("avg"),
        "vendor_po_count_prior": pr.get("count"),
        "vendor_avg_amount_prior": pr.get("avg"),
        "count_bills": len(ctx["bills"]),
        "count_payments": len(ctx["payments"]),
        "sum_received_qty": round(sum(num(l.get("qty_received")) for l in lines), 4),
        "sum_billed_qty": round(sum(num(l.get("qty_invoiced")) for l in lines), 4),
        "line_unit_price_vs_cost_ratio": ratios,
    }


def build_data_quality(ctx):
    dq = []
    for b in ctx["bills"]:
        if b.get("linked_via") == "text":
            dq.append(f"bill {b.get('doc_number')} linked to PO by invoice_origin text only (no spine line)")
    # Approver-actor provenance: the PO was approved but the chatter has no 'Purchase Order'
    # state transition, so the timeline's 'approved' actor fell back to buyer.user_id.
    state_log = ctx.get("state_log") or []
    if ctx["po_identity"].get("date_approve") and not any(
            s.get("to_state") == "Purchase Order" for s in state_log):
        dq.append("approved actor fell back to buyer.user_id (no 'Purchase Order' state transition in chatter)")
    return dq


def derive_approval_facts(ctx):
    """Attach two NEUTRAL descriptors to po_identity from the state_log chatter (level B, no
    verdicts): self_approval = the real approver is the buyer of record; rfq_sent_skipped =
    the PO reached 'Purchase Order' with no 'RFQ Sent' transition before it. State values arrive
    through tracking as English selection LABELS (the generation DB is English)."""
    idy = ctx["po_identity"]
    state_log = ctx.get("state_log") or []
    approver_entries = [s for s in state_log if s.get("to_state") == "Purchase Order"]
    approver_uid = approver_entries[-1]["actor_uid"] if approver_entries else None

    idy["self_approval"] = (approver_uid is not None) and (approver_uid == idy["buyer"].get("user_id"))

    if approver_entries:
        po_ts = parse_dt(approver_entries[-1].get("timestamp"))
        sent_before = any(
            s.get("to_state") == "RFQ Sent"
            and (po_ts is None or (parse_dt(s.get("timestamp")) or datetime.datetime.min) <= po_ts)
            for s in state_log)
        idy["rfq_sent_skipped"] = not sent_before
    else:
        idy["rfq_sent_skipped"] = None  # never reached 'Purchase Order' — question doesn't apply


# ---------- vendor rollup (derived from PO contexts) ----------
def splitting_clusters(pos):
    """Greedy clusters of a vendor's POs whose date_order falls within WINDOW days of the
    cluster's first PO. Facts only (amounts + running sum) — the LLM decides splitting."""
    dated = sorted([p for p in pos if parse_dt(p.get("date_order"))],
                   key=lambda p: parse_dt(p["date_order"]))
    clusters, cur, anchor = [], [], None
    for p in dated:
        d = parse_dt(p["date_order"])
        if anchor is None or (d - anchor).days <= WINDOW:
            cur.append(p)
            anchor = anchor or d
        else:
            clusters.append(cur)
            cur, anchor = [p], d
    if cur:
        clusters.append(cur)
    out = []
    for c in clusters:
        if len(c) < 2:
            continue
        amounts, running, s = [], [], 0.0
        for p in c:
            a = num(p.get("amount_total"))
            s += a
            amounts.append(p.get("amount_total"))
            running.append(round(s, 2))
        out.append({
            "window_days": WINDOW,
            "po_numbers": [p["po_number"] for p in c],
            "amounts": amounts,
            "running_sum": running,
            "group_sum": round(s, 2),
            "approval_threshold": THRESHOLD,
        })
    return out


def build_vendor_rollups(contexts):
    by_vendor = collections.defaultdict(list)
    for ctx in contexts:
        by_vendor[ctx["po_identity"]["vendor"]["id"]].append(ctx)
    rollups = []
    for vid, ctxs in by_vendor.items():
        pos = [{
            "po_number": c["po_identity"]["po_number"],
            "amount_total": c["po_identity"].get("amount_total"),
            "date_order": c["po_identity"].get("date_order"),
            "state": c["po_identity"].get("state"),
            # S1 support: this PO's bills (doc number/supplier ref/amount/date only), so a bill
            # re-billed against a DIFFERENT PO of the same vendor is visible at the vendor grain
            # even though each PO's own bill count looks clean (count_bills == 1 on each).
            "bills": [{
                "doc_number": b.get("doc_number"),
                "ref": b.get("ref"),
                "amount_total": b.get("amount_total"),
                "date": b.get("date"),
            } for b in c.get("bills", [])],
        } for c in ctxs]
        total = round(sum(num(p["amount_total"]) for p in pos), 2)
        rollups.append({
            "vendor": {"id": vid, "name": ctxs[0]["po_identity"]["vendor"].get("name")},
            "po_refs": [p["po_number"] for p in pos],
            "pos": pos,
            "alltime": {
                "po_count": len(pos),
                "avg_amount": round(total / len(pos), 2) if pos else None,
                "total": total,
            },
            "splitting_view": splitting_clusters(pos),
            "params": {"approval_threshold": THRESHOLD, "splitting_window_days": WINDOW},
        })
    return rollups


def vendor_stats(contexts):
    g = collections.defaultdict(list)
    for c in contexts:
        g[c["po_identity"]["vendor"]["id"]].append(num(c["po_identity"].get("amount_total")))
    return {vid: {"count": len(a), "avg": round(sum(a) / len(a), 2)} for vid, a in g.items()}


# ---------- point-in-time vendor comparator (facts only, no future leakage) ----------
def _dated_groups(items):
    """items: list of (date, key, value). Sort by (date, key) — key is the deterministic
    tie-break for POs/lines sharing an identical date (see module docstring below) — then yield
    (date, [(key, value), ...]) groups in ascending date order. Two entries with the SAME date
    never count each other as "prior": grouping by date and only ever summing groups already
    fully consumed keeps that guarantee regardless of the tie-break order within a group."""
    dated = sorted([(d, k, v) for (d, k, v) in items if d is not None], key=lambda x: (x[0], x[1]))
    i = 0
    while i < len(dated):
        j = i
        cur = dated[i][0]
        group = []
        while j < len(dated) and dated[j][0] == cur:
            group.append((dated[j][1], dated[j][2]))
            j += 1
        yield cur, group
        i = j


def vendor_prior_stats(contexts):
    """Per PO, this vendor's mean amount / PO count over that vendor's OTHER POs with
    date_order STRICTLY EARLIER than this PO's date_order (no future data, unlike vendor_stats()
    above which stays alltime/unchanged for the frozen R5 rule). Tie-break for POs sharing an
    identical date_order: ordered by po_number (string sort) — deterministic, but immaterial to
    the result, since same-date POs are grouped together and never count each other as prior
    regardless of order within the group. count/avg are both null when there is no strictly-
    earlier PO (including when this PO itself has no date_order, since "strictly earlier than"
    is then undefined)."""
    by_vendor = collections.defaultdict(list)
    for c in contexts:
        vid = c["po_identity"]["vendor"]["id"]
        d = parse_dt(c["po_identity"].get("date_order"))
        po_num = c["po_identity"]["po_number"]
        by_vendor[vid].append((d, po_num, num(c["po_identity"].get("amount_total"))))

    prior = {}
    for vid, items in by_vendor.items():
        for _, k, _ in items:
            prior.setdefault(k, {"count": None, "avg": None})  # undated default
        cum = []
        for _date, group in _dated_groups(items):
            if cum:
                prior_count, prior_avg = len(cum), round(sum(cum) / len(cum), 2)
            else:
                prior_count, prior_avg = None, None
            for po_num, _amt in group:
                prior[po_num] = {"count": prior_count, "avg": prior_avg}
            cum.extend(amt for _po_num, amt in group)
    return prior


def vendor_product_prior_stats(contexts):
    """S5 support: per (vendor, product), this vendor's average unit price for that exact
    product on STRICTLY EARLIER POs only — same point-in-time discipline and tie-break as
    vendor_prior_stats() above. When a PO has multiple lines for the same product, the PO's
    lines are averaged first so each earlier PO contributes one point, not one per line. Returns
    a lookup keyed by (vendor_id, product_id, po_number) -> prior avg unit price, or None."""
    per_key = collections.defaultdict(lambda: collections.defaultdict(list))
    po_date = {}
    for c in contexts:
        vid = c["po_identity"]["vendor"]["id"]
        po_num = c["po_identity"]["po_number"]
        po_date[po_num] = parse_dt(c["po_identity"].get("date_order"))
        for l in c.get("lines", []):
            pid = l.get("product_id")
            up = l.get("unit_price")
            if pid is None or up is None:
                continue
            per_key[(vid, pid)][po_num].append(num(up))

    lookup = {}
    for (vid, pid), by_po in per_key.items():
        items = [(po_date.get(n), n, sum(prices) / len(prices)) for n, prices in by_po.items()]
        for _, n, _ in items:
            lookup[(vid, pid, n)] = None  # undated default
        cum = []
        for _date, group in _dated_groups(items):
            avg = round(sum(cum) / len(cum), 4) if cum else None
            for n, _p in group:
                lookup[(vid, pid, n)] = avg
            cum.extend(p for _n, p in group)
    return lookup


# ---------- S1: cross-document facts assembled in Python from the already-loaded contexts ----------
def vendor_payment_index(contexts):
    """Vendor -> this vendor's payments across ALL its POs (date, amount, doc_number, and the
    destination bank account id — facts, no proximity verdict), for the vendor_audit grain to
    carry as payment context. Zero extra DB round-trips: contexts already hold `payments` per PO.
    destination_bank_account_id lets a detector match against bank_account_additions[].bank_ref
    to notice a payment landing on a recently-added, non-first account."""
    idx = collections.defaultdict(list)
    for c in contexts:
        vid = c["po_identity"]["vendor"]["id"]
        for pm in c.get("payments", []):
            idx[vid].append({
                "doc_number": pm.get("doc_number"),
                "amount": pm.get("amount"),
                "date": pm.get("date"),
                "destination_bank_account_id": pm.get("destination_bank_account_id"),
            })
    for vid in idx:
        idx[vid].sort(key=lambda p: (p["date"] or ""))
    return idx


def main():
    out_dir = rel(CFG["paths"]["out_dir"])
    po_dir, ven_dir, va_dir = out_dir / "po", out_dir / "vendor", out_dir / "vendor_audit"
    for d in (po_dir, ven_dir, va_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    sql = rel(CFG["paths"]["sql"]).read_text(encoding="utf-8")
    audit_sql = rel(CFG["paths"]["sql_vendor_audit"]).read_text(encoding="utf-8")
    conn = psycopg2.connect(**CFG["db"])
    conn.set_session(readonly=True, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
        cur.execute(audit_sql)          # scenario-3 audit trail (res.partner + res.partner.bank)
        audit_rows = cur.fetchall()
        # S4 support: vendor creation date, so a "brand-new vendor" is observable at po_identity.
        # A separate lightweight read on the same read-only connection — po_context.sql/
        # vendor_audit.sql are out of scope for this change, so this stays in Python.
        cur.execute("SELECT id, create_date FROM res_partner")
        vendor_create_dates = {vid: (str(cd) if cd is not None else None) for vid, cd in cur.fetchall()}
    conn.close()

    # psycopg2 parses json/jsonb to Python objects; fall back to json.loads if it's a str.
    raw = []
    for name, ctx in rows:
        raw.append((name, ctx if isinstance(ctx, dict) else json.loads(ctx)))

    vstats = vendor_stats([c for _, c in raw])
    vprior = vendor_prior_stats([c for _, c in raw])            
    vprod_prior = vendor_product_prior_stats([c for _, c in raw])  # S5

    po_validator = Draft202012Validator(json.loads(rel(CFG["paths"]["schema_po"]).read_text(encoding="utf-8")))
    ven_validator = Draft202012Validator(json.loads(rel(CFG["paths"]["schema_vendor"]).read_text(encoding="utf-8")))

    contexts, failures = [], []
    jsonl = open(out_dir / "po_context.jsonl", "w", encoding="utf-8")
    for name, ctx in raw:
        vid = ctx["po_identity"]["vendor"]["id"]
        po_num = ctx["po_identity"]["po_number"]
        ctx["po_identity"]["vendor"]["create_date"] = vendor_create_dates.get(vid)  # S4
        for l in ctx.get("lines", []):
            pid = l.get("product_id")
            l["vendor_prior_avg_unit_price"] = (
                vprod_prior.get((vid, pid, po_num)) if pid is not None else None)  # S5
        ctx["timeline"] = build_timeline(ctx)
        ctx["baselines"] = build_baselines(ctx, vstats, vprior)
        ctx["data_quality"] = build_data_quality(ctx)
        derive_approval_facts(ctx)  # neutral po_identity.self_approval / rfq_sent_skipped
        ctx["ground_truth"] = {"injected_anomaly": None}  # filled at scoring time from the label sidecars
        assert_no_pii(f"po {name}", ctx)
        errs = sorted(po_validator.iter_errors(ctx), key=lambda e: e.path)
        if errs:
            failures.append({"po": name, "errors": [e.message for e in errs[:5]]})
        (po_dir / f"{name.replace('/', '_')}.json").write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        jsonl.write(json.dumps({"po": name, **ctx}, ensure_ascii=False, default=str) + "\n")
        contexts.append(ctx)
    jsonl.close()

    rollups = build_vendor_rollups(contexts)
    ven_fail = []
    # vendor.jsonl feeds baseline.sql's \copy directly (one JSON object per line, matching each
    # out/vendor/*.json file exactly). Writing it here, in the same pass that writes those files,
    # means it can never go stale relative to them.
    ven_jsonl = open(out_dir / "vendor.jsonl", "w", encoding="utf-8")
    for r in rollups:
        assert_no_pii(f"vendor {r['vendor']['id']}", r)
        errs = sorted(ven_validator.iter_errors(r), key=lambda e: e.path)
        if errs:
            ven_fail.append({"vendor": r["vendor"]["id"], "errors": [e.message for e in errs[:5]]})
        (ven_dir / f"vendor_{r['vendor']['id']}.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        ven_jsonl.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    ven_jsonl.close()

    # ---- scenario-3 vendor master-data audit (off the FK spine; SQL-assembled per vendor) ----
    audit_validator = Draft202012Validator(json.loads(rel(CFG["paths"]["schema_vendor_audit"]).read_text(encoding="utf-8")))
    vpay = vendor_payment_index(contexts)  # this vendor's payments, keyed off already-loaded PO contexts
    audits, audit_fail = [], []
    # audit.jsonl: same reasoning as vendor.jsonl above — written in-pass so it cannot go stale.
    audit_jsonl = open(out_dir / "audit.jsonl", "w", encoding="utf-8")
    for vid, audit in audit_rows:
        a = audit if isinstance(audit, dict) else json.loads(audit)
        a["payments"] = vpay.get(vid, [])  # facts only — date/amount, no proximity verdict
        assert_no_pii(f"vendor_audit {vid}", a)
        errs = sorted(audit_validator.iter_errors(a), key=lambda e: e.path)
        if errs:
            audit_fail.append({"vendor_audit": vid, "errors": [e.message for e in errs[:5]]})
        (va_dir / f"vendor_{vid}.json").write_text(
            json.dumps(a, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        audit_jsonl.write(json.dumps(a, ensure_ascii=False, default=str) + "\n")
        audits.append(a)
    audit_jsonl.close()

    manifest = {
        "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "db": CFG["db"]["dbname"],
        "params": CFG["params"],
        "counts": {"po": len(contexts), "vendor": len(rollups), "vendor_audit": len(audits)},
        "validation": {
            "po_pass": len(contexts) - len(failures), "po_fail": len(failures),
            "vendor_pass": len(rollups) - len(ven_fail), "vendor_fail": len(ven_fail),
            "vendor_audit_pass": len(audits) - len(audit_fail), "vendor_audit_fail": len(audit_fail),
        },
        "failures": failures + ven_fail + audit_fail,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"POs: {len(contexts)} | vendors: {len(rollups)} | vendor_audit: {len(audits)} | "
          f"schema failures: PO={len(failures)} vendor={len(ven_fail)} audit={len(audit_fail)}")
    print(f"output -> {out_dir}")
    sys.exit(1 if (failures or ven_fail or audit_fail) else 0)


if __name__ == "__main__":
    main()
