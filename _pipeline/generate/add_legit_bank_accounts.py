#!/usr/bin/env python
"""add_legit_bank_accounts.py - post-generation step: give 8 clean vendors a second, legitimate
bank account (Scenario-3 hard negatives).

Each added account:
  - is created via XML-RPC with the setup session (base.group_system), then its create_date /
    write_date are backdated in Postgres to a fixed 30-day offset after the vendor's existing
    (ordinal-1) account, clamped into the generation period;
  - never receives a payment (account.payment / account.move are not touched);
  - goes only to a vendor whose labels_audit.json entry is null.

Before planning, a read-only check confirms that no payment of any candidate vendor has ever
pointed to a second (ordinal > 1) bank account (ordinal = row_number() OVER (PARTITION BY
partner_id ORDER BY create_date, id), as in _pipeline/sql/vendor_audit.sql). A vendor that fails
the check is excluded with a printed reason.

Account numbers use the same shape as plan_dataset.py's _random_bank_acc ("{prefix}-{mid}-{tail}")
from a fixed-seed random.Random, so the selection is reproducible. fix_timestamps.sql is not
re-run. Selections are recorded in gen_out/legit_bank_additions.json; vendors already listed
there are skipped. Dry run is the default (UPDATE statements are printed, not executed);
`--apply` writes. `--db` defaults to odoo.dbname in gen_config.json.

    python _pipeline/generate/add_legit_bank_accounts.py                # dry run
    python _pipeline/generate/add_legit_bank_accounts.py --apply        # write
"""
import argparse
import datetime
import json
import pathlib
import random
import sys

import psycopg2

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import odoo_client  # noqa: E402

GEN_OUT = HERE / "gen_out"

N_VENDORS = 8
SEED = 20260813

OUT_FILE = GEN_OUT / "legit_bank_additions.json"


# ---------------------------------------------------------------- loading

def _load(name):
    return json.loads((GEN_OUT / name).read_text(encoding="utf-8"))


def _random_bank_acc(rng):
    """Same shape as plan_dataset.py's _random_bank_acc(rng), reproduced with stdlib random so
    no numeric pattern separates this script's accounts from the generator's own."""
    prefix = rng.randint(100, 999)
    mid = rng.randint(1000000, 9999999)
    tail = rng.randint(0, 9)
    return f"{prefix}-{mid}-{tail}"


# ---------------------------------------------------------------- date selection

def pick_addition_date(existing_accounts, period_start, period_end):
    """Plain, simple date choice: 30 days after the vendor's earliest (ordinal-1) bank account's
    created_at, clamped into [period_start, period_end]. Does NOT need to sit far from any
    payment - see the module docstring's VERIFICATION section for what actually makes these valid
    hard negatives (a DB-level check that no payment was ever diverted to a second account, not
    timing)."""
    if existing_accounts:
        base = min(datetime.date.fromisoformat(str(a["created_at"])[:10]) for a in existing_accounts)
    else:
        base = period_start
    chosen = base + datetime.timedelta(days=30)
    return max(period_start, min(period_end, chosen))


# ---------------------------------------------------------------- verification

def check_never_diverted(conn, vendor_ids):
    """Read-only DB check (works against a live connection in dry-run mode too - no writes).

    For the given vendor ids, confirms that no account_payment.partner_bank_id has EVER (past or
    present) pointed to one of that vendor's second-or-later bank accounts (account_ordinal > 1,
    same row_number() OVER (PARTITION BY partner_id ORDER BY create_date, id) construction as
    _pipeline/sql/vendor_audit.sql). This is what actually makes a selected vendor a valid hard
    negative for the evasive bank_add anomaly: "legitimate" means the addition never received a
    payment, checked directly against the DB rather than inferred from how far its create_date
    sits from a payment date.

    Returns {vendor_id: (ok: bool, detail: str)}. A vendor with ok=False must be excluded, not
    silently included - see main()."""
    if not vendor_ids:
        return {}
    vids = list(vendor_ids)
    sql = """
        WITH ranked AS (
            SELECT id, partner_id,
                   row_number() OVER (PARTITION BY partner_id ORDER BY create_date, id) AS ordinal
            FROM res_partner_bank
            WHERE partner_id = ANY(%s)
        )
        SELECT r.partner_id, ap.partner_bank_id, r.ordinal, count(*) AS n_payments
        FROM account_payment ap
        JOIN ranked r ON r.id = ap.partner_bank_id
        WHERE r.ordinal > 1
        GROUP BY r.partner_id, ap.partner_bank_id, r.ordinal
        ORDER BY r.partner_id, r.ordinal
    """
    with conn.cursor() as cur:
        cur.execute(sql, (vids,))
        rows = cur.fetchall()
    diverted = {}
    for partner_id, bank_id, ordinal, n_payments in rows:
        diverted.setdefault(partner_id, []).append((bank_id, ordinal, n_payments))
    result = {}
    for vid in vendor_ids:
        if vid in diverted:
            detail = "; ".join(f"bank_id={b} ordinal={o} n_payments={n}"
                                for b, o, n in diverted[vid])
            result[vid] = (False, f"payment(s) diverted to a non-first account: {detail}")
        else:
            result[vid] = (True, "no payment ever pointed to an ordinal>1 account")
    return result


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Add a legitimate second bank account to 8 clean audit-trail vendors, so "
                    "'account added after onboarding' stops being a perfect label for the "
                    "evasive vendor_master_mod anomaly")
    ap.add_argument("--db", default=None,
                    help="target database (default: odoo.dbname in gen_config.json)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this the run only reports what it would do.")
    args = ap.parse_args()

    if args.db is None:
        args.db = json.loads((HERE / "gen_config.json").read_text(encoding="utf-8"))["odoo"]["dbname"]

    cfg = json.loads((HERE / "gen_config.json").read_text(encoding="utf-8"))
    odoo_cfg = dict(cfg["odoo"])
    odoo_cfg["dbname"] = args.db
    cluster = odoo_client.OdooCluster.from_gen_config(odoo_cfg)

    labels_audit = _load("labels_audit.json")
    plan = _load("plan.json")
    # plan.json's params_echo.setup_session is only populated by generate.py's live run
    # (generate.py:229) and is absent from this dataset's plan.json on disk. run_manifest.json's
    # preflight block records the same value independently (odoo_client.preflight() resolved it
    # for real against this exact database) and is the fallback. There is no "setup" alias in a
    # freshly built OdooCluster either - generate.py creates that via cluster.alias("setup", ...)
    # (generate.py:230), which this standalone script never calls - so the real session name is
    # used directly instead of relying on an alias that was never created.
    setup_session_name = (plan.get("params_echo") or {}).get("setup_session")
    if not setup_session_name:
        manifest_path = GEN_OUT / "run_manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            setup_session_name = (manifest.get("preflight") or {}).get("setup_session")
    if not setup_session_name:
        sys.exit("cannot resolve the setup session: absent from both plan.json's params_echo "
                 "and gen_out/run_manifest.json's preflight block")

    period_start = datetime.date.fromisoformat(str(plan["params_echo"]["generation_start"])[:10])
    period_end = datetime.date.fromisoformat(str(plan["params_echo"]["generation_end"])[:10])

    already = {}
    if OUT_FILE.exists():
        already = json.loads(OUT_FILE.read_text(encoding="utf-8"))

    clean_ids = sorted(int(k) for k, v in labels_audit.items() if v is None)
    vendor_audit_dir = ROOT / "_pipeline" / "out" / "vendor_audit"

    candidates = []
    for vid in clean_ids:
        vfile = vendor_audit_dir / f"vendor_{vid}.json"
        if not vfile.exists():
            continue
        candidates.append(vid)
        if len(candidates) >= N_VENDORS:
            break

    if len(candidates) < N_VENDORS:
        sys.exit(f"only found {len(candidates)} clean vendors with a vendor_audit file, need "
                 f"{N_VENDORS}")

    print(f"db={args.db}  mode={'APPLY' if args.apply else 'DRY RUN'}")
    print(f"period: {period_start} .. {period_end}")
    print(f"setup_session: {setup_session_name!r}"
          f"{'' if (plan.get('params_echo') or {}).get('setup_session') else '  (fallback, not in params_echo)'}")
    print(f"selected {len(candidates)} clean vendors (first {N_VENDORS} by numeric vendor id "
          f"with a vendor_audit file, all label=null): {candidates}")

    # ---- verify these are valid hard negatives: no payment has EVER been diverted to a second
    # bank account (read-only; this is what makes the "legitimate" label correct, not timing -
    # see VERIFICATION in the module docstring) ----
    db_cfg = json.loads((ROOT / "_pipeline" / "config.json").read_text(encoding="utf-8"))["db"]
    check_conn = psycopg2.connect(host=db_cfg["host"], port=db_cfg["port"], user=db_cfg["user"],
                                  password=db_cfg["password"], dbname=args.db)
    try:
        diversion_check = check_never_diverted(check_conn, candidates)
    finally:
        check_conn.close()

    print("\n--- verification: has any payment ever pointed to a second (ordinal>1) bank "
          "account for these vendors? ---")
    verified_candidates = []
    for vid in candidates:
        ok, detail = diversion_check[vid]
        print(f"  vendor {vid}: {'OK' if ok else 'REFUSED'} - {detail}")
        if ok:
            verified_candidates.append(vid)
        else:
            print(f"    -> excluding vendor {vid}: this vendor is NOT a valid hard negative "
                  f"(a payment was diverted to a non-first account), refusing to proceed for it")
    if len(verified_candidates) == len(candidates):
        print(f"confirmed: all {len(candidates)} selected vendors passed - none has ever had a "
              f"payment diverted to a second bank account.")
    else:
        print(f"\n{len(candidates) - len(verified_candidates)} of {len(candidates)} candidate(s) "
              f"refused by the verification check; proceeding with {len(verified_candidates)}.")
    candidates = verified_candidates

    # ---- read existing acc_number values in the DB, to guarantee no collision ----
    names = cluster.names()
    reader = cluster.as_(next((n for n in names if n.startswith("manager")), names[0]))
    existing_banks = reader.search_read("res.partner.bank", [], ["acc_number"])
    used_numbers = {b["acc_number"] for b in existing_banks if b.get("acc_number")}
    print(f"\nexisting res.partner.bank rows in DB: {len(existing_banks)}")

    rng = random.Random(SEED)

    plan_units = []  # (vendor_id, name, new_number, new_date, existing_accounts, pay_range)
    skipped = []
    for vid in candidates:
        if str(vid) in already:
            skipped.append(vid)
            continue

        vdata = json.loads((vendor_audit_dir / f"vendor_{vid}.json").read_text(encoding="utf-8"))
        vname = vdata["vendor"]["name"]
        assert labels_audit.get(str(vid)) is None, f"vendor {vid} is not clean, refusing"

        payments = vdata.get("payments") or []
        pay_dates = [datetime.date.fromisoformat(p["date"]) for p in payments]
        existing_accounts = vdata.get("bank_account_additions") or []
        chosen_date = pick_addition_date(existing_accounts, period_start, period_end)

        new_number = _random_bank_acc(rng)
        guard = 0
        while new_number in used_numbers:
            guard += 1
            if guard > 10000:
                sys.exit("exhausted random attempts finding a non-colliding acc_number")
            new_number = _random_bank_acc(rng)
        used_numbers.add(new_number)

        plan_units.append({
            "vendor_id": vid,
            "vendor_name": vname,
            "existing_accounts": existing_accounts,
            "n_payments": len(payments),
            "pay_min": min(pay_dates).isoformat() if pay_dates else None,
            "pay_max": max(pay_dates).isoformat() if pay_dates else None,
            "new_create_date": chosen_date.isoformat(),
            "diversion_check": diversion_check[vid][1],
            "new_acc_number": new_number,
        })

    if skipped:
        print(f"\nskipping {len(skipped)} vendor(s) already recorded in "
              f"{OUT_FILE.name}: {skipped}")

    print(f"\n--- planned additions ({len(plan_units)}) ---")
    for u in plan_units:
        print(f"\nvendor {u['vendor_id']}  {u['vendor_name']!r}")
        for acc in u["existing_accounts"]:
            print(f"    existing account: {acc.get('acc_number')}  "
                  f"created_at={acc.get('created_at')}  ordinal={acc.get('account_ordinal')}")
        print(f"    payments: n={u['n_payments']}  range=[{u['pay_min']}, {u['pay_max']}]")
        print(f"    new account create_date = {u['new_create_date']}  (plain 30-day offset from "
              f"the vendor's earliest account; validity comes from the diversion check below, "
              f"not from this date)")
        print(f"    diversion check: {u['diversion_check']}")
        print(f"    new acc_number = {u['new_acc_number']}")
        assert labels_audit.get(str(u["vendor_id"])) is None, "labelled vendor slipped through"
    print(f"\nconfirmed: none of the {len(plan_units)} selected vendors carries a "
          f"vendor_master_mod label, and all passed the never-diverted-a-payment check above.")

    if not plan_units:
        print("\nnothing to do - all selected vendors already recorded.")
        return

    if not args.apply:
        print("\n--- would run (Postgres, not executed in dry run) ---")
        ts_placeholder = "<new_bank_id>"
        for u in plan_units:
            ts = f"{u['new_create_date']} 09:00:00"
            print(f"UPDATE res_partner_bank SET create_date='{ts}', write_date='{ts}' "
                  f"WHERE id = {ts_placeholder};  -- vendor {u['vendor_id']}, "
                  f"acc_number={u['new_acc_number']}")
        print(f"\nDRY RUN - nothing was created, nothing was written to "
              f"{OUT_FILE.relative_to(ROOT)}. Re-run with --apply to perform the additions.")
        return

    # ---------------------------------------------------------------- apply
    setup = cluster.as_(setup_session_name)
    created = dict(already)
    new_bank_ids, ts_pairs = [], []
    for u in plan_units:
        new_bank_id = setup.create("res.partner.bank", {
            "partner_id": u["vendor_id"],
            "acc_number": u["new_acc_number"],
            "acc_holder_name": u["vendor_name"],
        })
        print(f"created res.partner.bank id={new_bank_id} for vendor {u['vendor_id']}")
        new_bank_ids.append(new_bank_id)
        ts_pairs.append((new_bank_id, u["new_create_date"]))
        created[str(u["vendor_id"])] = {
            "vendor_name": u["vendor_name"],
            "bank_id": new_bank_id,
            "acc_number": u["new_acc_number"],
            "create_date": u["new_create_date"],
            "diversion_check": u["diversion_check"],
        }

    db = json.loads((ROOT / "_pipeline" / "config.json").read_text(encoding="utf-8"))["db"]
    conn = psycopg2.connect(host=db["host"], port=db["port"], user=db["user"],
                            password=db["password"], dbname=args.db)
    try:
        with conn.cursor() as cur:
            for bank_id, date_str in ts_pairs:
                ts = f"{date_str} 09:00:00"
                cur.execute("UPDATE res_partner_bank SET create_date=%s, write_date=%s "
                            "WHERE id = %s", (ts, ts, bank_id))
        conn.commit()
        print(f"backdated {len(ts_pairs)} res.partner.bank row(s)")
    finally:
        conn.close()

    OUT_FILE.write_text(json.dumps(created, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
