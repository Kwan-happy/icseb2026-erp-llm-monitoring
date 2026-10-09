#!/usr/bin/env python
"""build_master.py - generation, APPLY stage 1: master data (vendors, banks, products).

Carries out `plan["master"]` — it does NO sampling of its own. Every name, price, category and
bank account number was decided in plan_dataset.py from the single seeded RNG, so a dry run
shows exactly what this stage will create and a re-run with the same seed reproduces it.

Created as the MANAGER session: master-data setup is a manager action, and it keeps the buyer
users' chatter free of setup noise.

Two Odoo 19 specifics verified against a live DB:
  * `product_template.type` no longer has a 'product' value — storable goods are
    `type='consu'` + `is_storable=True`.
  * `product_product.standard_price` is a per-company jsonb; `po_context.sql` reads it as
    `pp.standard_price->>company_id`, so it must be set for the company the POs belong to.

Everything created here is stamped "now" by Odoo, so its create_date/write_date (and the
chatter Odoo writes for tracked fields) are recorded into the TimestampMap and realigned to
the start of the generation period by fix_timestamps.sql — master data must pre-date the
orders that reference it.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]          # repo root
GEN_DIR = ROOT / "_pipeline" / "generate"


def build_master(cluster, plan, cfg, tsmap):
    """Returns
        {"vendors":  [{"idx","id","bank_id","name"}],
         "products": [{"idx","id","default_code","standard_price"}],
         "vendor_by_idx": {idx: id}, "product_by_idx": {idx: id},
         "company_id": int}
    and writes it to gen_out/master.json so a later stage can resume without re-querying.
    """
    # The SETUP session, not just any manager: creating product.product and res.partner.bank
    # needs product.group_product_manager / base.group_partner_manager, which a plain purchase
    # manager lacks (preflight resolves which manager holds them and fails fast if none does).
    mgr = cluster.as_(plan["params_echo"].get("setup_session") or "manager")
    m = plan["master"]

    # Master data exists BEFORE the first order. The generation period starts 2025-01-01, so
    # anchor it there: early enough to precede every PO, still inside the window that
    # fix_timestamps.sql verifies (a 2024 date would show up as "outside the period").
    born = "2025-01-01 00:00:00"

    company_id = mgr.execute("res.users", "read", [[mgr.uid], ["company_id"]])[0]["company_id"][0]

    # ---------- vendors ----------
    vendor_vals = [{
        "name": v["name"],
        "company_type": "company",
        "is_company": True,
        "supplier_rank": 1,
        "street": v["street"],
        "city": v["city"],
        "phone": v["phone"],
        "vat": v["vat"],
    } for v in m["vendors"]]
    vendor_ids = mgr.create_many("res.partner", vendor_vals)

    # One bank account per vendor: scenario 3 tampers with res.partner.bank.acc_number, and
    # res.partner.bank_ids is NOT tracked on res.partner itself, so the row must exist as its
    # own record for vendor_audit.sql to find the change.
    bank_ids = mgr.create_many("res.partner.bank", [{
        "partner_id": vid,
        "acc_number": v["bank_acc_number"],
        "acc_holder_name": v["name"],
    } for vid, v in zip(vendor_ids, m["vendors"])])

    for vid, bid in zip(vendor_ids, bank_ids):
        tsmap.record_partner(vid, create_date=born, write_date=born)
        tsmap.record_partner_bank(bid, create_date=born, write_date=born)
        # Odoo writes chatter for tracked fields at creation; park it at the same instant so
        # no vendor trail starts with a 2026 date. Scenario-3 edits later append to these
        # records and are re-recorded by inject_anomalies with their own sequence.
        tsmap.record_messages_for("res.partner", vid, born)
        tsmap.record_messages_for("res.partner.bank", bid, born)

    # ---------- product categories ----------
    category_names = m["category_names"]
    existing = {c["name"]: c["id"] for c in mgr.search_read(
        "product.category", [("name", "in", category_names)], ["id", "name"])}
    missing = [n for n in category_names if n not in existing]
    if missing:
        existing.update(dict(zip(missing, mgr.create_many(
            "product.category", [{"name": n} for n in missing]))))
    categ_ids = [existing[n] for n in category_names]

    # ---------- products ----------
    product_vals = [{
        "name": p["name"],
        "default_code": p["default_code"],
        "type": "consu",
        "is_storable": True,
        "purchase_ok": True,
        "sale_ok": False,
        "categ_id": categ_ids[p["category_idx"] % len(categ_ids)],
        "list_price": round(p["standard_price"] * 1.2, 2),
        "standard_price": p["standard_price"],
        # strip any default vendor tax: the dataset is tax-free so amount_untaxed == amount_total
        "supplier_taxes_id": [(6, 0, [])],
    } for p in m["products"]]
    product_ids = mgr.create_many("product.product", product_vals)

    vendors_out = [{"idx": v["idx"], "id": vid, "bank_id": bid, "name": v["name"]}
                   for v, vid, bid in zip(m["vendors"], vendor_ids, bank_ids)]
    products_out = [{"idx": p["idx"], "id": pid, "default_code": p["default_code"],
                     "standard_price": p["standard_price"]}
                    for p, pid in zip(m["products"], product_ids)]

    result = {
        "company_id": company_id,
        "vendors": vendors_out,
        "products": products_out,
        "vendor_by_idx": {v["idx"]: v["id"] for v in vendors_out},
        "product_by_idx": {p["idx"]: p["id"] for p in products_out},
    }

    out_dir = GEN_DIR / "gen_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "master.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  master: {len(vendors_out)} vendors (+banks), {len(products_out)} products")
    return result


# build_normal.py refers to this stage as apply_master(); same function, clearer verb.
apply_master = build_master
