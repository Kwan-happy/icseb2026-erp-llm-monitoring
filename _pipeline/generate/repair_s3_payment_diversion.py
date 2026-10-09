#!/usr/bin/env python
"""repair_s3_payment_diversion.py - post-generation step: divert one payment per evasive
Scenario-3 vendor to the bank account added by the injection.

The evasive vendor_master_mod variant (anomaly_spec.md) is a NEW res.partner.bank row for the
vendor plus a payment made to it. inject_anomalies.py creates the bank row; this step creates the
payment side. For each evasive Scenario-3 vendor it takes the vendor's SECOND res.partner.bank row
(ranked by (create_date, id)) and the vendor's FIRST account.payment dated on or after that row's
create_date, and re-points both the payment and its linked account.move:

    UPDATE account_payment SET partner_bank_id = <new_id> WHERE id = <payment_id>;
    UPDATE account_move    SET partner_bank_id = <new_id> WHERE id = <move_id>;

A read-only check (`check_consistency`) first confirms that account_payment.partner_bank_id and
account_move.partner_bank_id agree for every existing payment of these vendors, so the two stay
in sync after the update. The new bank id found in the DB must match the `kind == 'bank_add'`
entry in gen_out/injected.json; otherwise the step stops.

The update is a direct Postgres write because Odoo's ORM refuses to change partner_bank_id on a
paid payment or posted move. Each repair is recorded in gen_out/s3_payment_diversions.json; a
payment already pointing at the new account is skipped, so the step is idempotent. Dry run is
the default; `--apply` writes. `--db` defaults to odoo.dbname in gen_config.json.

    python _pipeline/generate/repair_s3_payment_diversion.py                # dry run
    python _pipeline/generate/repair_s3_payment_diversion.py --apply        # write
"""
import argparse
import json
import pathlib
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]

GEN_OUT = HERE / "gen_out"

# The six evasive Scenario-3 vendors (anomaly_spec.md's vendor_master_mod, bank_add variant).
# Cross-checked against gen_out/injected.json's own bank_add records before any write - see
# main(). Hardcoded rather than re-derived every run.
VENDOR_IDS = [82, 87, 92, 93, 94, 95]

OUT_FILE = GEN_OUT / "s3_payment_diversions.json"


# ---------------------------------------------------------------- SQL

CONSISTENCY_SQL = """
    SELECT p.id AS payment_id, p.partner_id AS vendor_id,
           p.partner_bank_id AS payment_bank_id,
           m.id AS move_id, m.partner_bank_id AS move_bank_id
    FROM account_payment p
    JOIN account_move m ON m.id = p.move_id
    WHERE p.partner_id = ANY(%(vendor_ids)s)
    ORDER BY p.partner_id, p.date, p.id;
"""

# For each vendor: rank their res.partner.bank rows by (create_date, id) and take the second
# (the bank_add row); then find the first account_payment dated on or after that account's
# create_date. LEFT JOIN LATERAL so a vendor with no such payment still appears in the result
# (payment_id NULL) instead of being silently dropped.
SURVEY_SQL = """
    WITH ranked_banks AS (
        SELECT id, partner_id, create_date,
               row_number() OVER (PARTITION BY partner_id ORDER BY create_date, id) AS ordinal
        FROM res_partner_bank
        WHERE partner_id = ANY(%(vendor_ids)s)
    ),
    new_accounts AS (
        SELECT id AS new_bank_id, partner_id AS vendor_id, create_date
        FROM ranked_banks
        WHERE ordinal = 2
    )
    SELECT
        na.vendor_id, na.new_bank_id, na.create_date AS new_bank_created,
        p.id AS payment_id, p.date AS payment_date, p.amount, p.state AS payment_state,
        p.partner_bank_id AS payment_old_bank_id, p.move_id,
        m.partner_bank_id AS move_old_bank_id, m.state AS move_state
    FROM new_accounts na
    LEFT JOIN LATERAL (
        SELECT pay.id, pay.date, pay.amount, pay.state, pay.partner_bank_id, pay.move_id
        FROM account_payment pay
        WHERE pay.partner_id = na.vendor_id
          AND pay.date >= na.create_date::date
        ORDER BY pay.date, pay.id
        LIMIT 1
    ) p ON true
    LEFT JOIN account_move m ON m.id = p.move_id
    ORDER BY na.vendor_id;
"""


# ---------------------------------------------------------------- loading / connecting

def _load_injected_bank_adds():
    """vendor_id -> new bank_id, from injected.json's audit records (the ground truth for which
    bank_add edits _apply_s3 actually made). Used only as a cross-check against what the live DB
    says - the repair itself reads and writes the database, not this file."""
    injected = json.loads((GEN_OUT / "injected.json").read_text(encoding="utf-8"))
    out = {}
    for entry in injected.get("audit", {}).values():
        vendor_id = entry["vendor_id"]
        for edit in entry.get("edits", []):
            if edit.get("kind") == "bank_add":
                out[vendor_id] = edit["bank_id"]
    return out


def _connect(dbname):
    db = json.loads((ROOT / "_pipeline" / "config.json").read_text(encoding="utf-8"))["db"]
    return psycopg2.connect(
        host=db["host"], port=db["port"], user=db["user"], password=db["password"],
        dbname=dbname, cursor_factory=RealDictCursor)


# ---------------------------------------------------------------- inspection

def check_consistency(conn, vendor_ids):
    """Read-only: for every existing payment of these vendors, does account_payment.partner_bank_id
    agree with its linked account_move.partner_bank_id? This is the fact that justifies writing
    BOTH tables in the repair - if they ever disagreed already, updating only one would not be the
    conservative move it looks like."""
    with conn.cursor() as cur:
        cur.execute(CONSISTENCY_SQL, {"vendor_ids": vendor_ids})
        rows = cur.fetchall()
    mismatches = [r for r in rows if r["payment_bank_id"] != r["move_bank_id"]]
    return rows, mismatches


def survey(conn, vendor_ids):
    with conn.cursor() as cur:
        cur.execute(SURVEY_SQL, {"vendor_ids": vendor_ids})
        return cur.fetchall()


def _load_done():
    if OUT_FILE.exists():
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    return []


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Divert one payment per evasive Scenario-3 vendor to its new bank account, "
                    "so the bank_add anomaly has an actual diverted payment behind it")
    ap.add_argument("--db", default=None,
                    help="target database (default: odoo.dbname in gen_config.json)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this the run only reports what it would do.")
    args = ap.parse_args()

    if args.db is None:
        args.db = json.loads((HERE / "gen_config.json").read_text(encoding="utf-8"))["odoo"]["dbname"]

    print(f"db={args.db}  mode={'APPLY' if args.apply else 'DRY RUN'}")

    injected_bank_adds = _load_injected_bank_adds()
    if set(injected_bank_adds) != set(VENDOR_IDS):
        sys.exit(f"vendor mismatch: injected.json's bank_add edits name "
                 f"{sorted(injected_bank_adds)}, this script expects {sorted(VENDOR_IDS)} - "
                 f"stopping rather than guessing which vendors to repair")

    conn = _connect(args.db)
    try:
        # ---- read-only consistency check (justifies updating both tables) ----
        print("\n--- consistency check: account_payment.partner_bank_id vs "
              "account_move.partner_bank_id, ALL existing payments of these 6 vendors ---")
        pay_rows, mismatches = check_consistency(conn, VENDOR_IDS)
        print(f"checked {len(pay_rows)} payment(s) across vendors {VENDOR_IDS}")
        if mismatches:
            print(f"MISMATCH: {len(mismatches)} payment(s) already disagree with their move:")
            for r in mismatches:
                print(f"  payment {r['payment_id']} (vendor {r['vendor_id']}): "
                      f"payment.partner_bank_id={r['payment_bank_id']} != "
                      f"move.partner_bank_id={r['move_bank_id']} (move {r['move_id']})")
        else:
            print("OK - every payment's partner_bank_id agrees with its move's partner_bank_id.")

        # ---- survey: the one payment per vendor this repair would divert ----
        print("\n--- survey: payment to divert per vendor ---")
        rows = survey(conn, VENDOR_IDS)
        by_vendor = {r["vendor_id"]: r for r in rows}

        done = _load_done()

        plan = []
        missing = []
        for vid in VENDOR_IDS:
            r = by_vendor.get(vid)
            print(f"\nvendor {vid}:")
            if r is None or r["payment_id"] is None:
                created = r["new_bank_created"] if r else "?"
                new_bank = r["new_bank_id"] if r else "?"
                print(f"  new bank account: id={new_bank}  created={created}")
                print(f"  NO PAYMENT dated on/after {created} - this vendor CANNOT be repaired "
                      f"this way and needs a different fix")
                missing.append(vid)
                continue

            expected_bank_add = injected_bank_adds.get(vid)
            if expected_bank_add != r["new_bank_id"]:
                sys.exit(f"vendor {vid}: DB's second bank account (id={r['new_bank_id']}) does "
                         f"not match injected.json's bank_add record (id={expected_bank_add}) - "
                         f"stopping")

            print(f"  new bank account: id={r['new_bank_id']}  created={r['new_bank_created']}")
            print(f"  payment to divert: id={r['payment_id']}  date={r['payment_date']}  "
                  f"amount={r['amount']}  state={r['payment_state']}")
            print(f"    current account_payment.partner_bank_id = {r['payment_old_bank_id']}")
            print(f"    current account_move({r['move_id']}, state={r['move_state']})"
                  f".partner_bank_id = {r['move_old_bank_id']}")

            if r["payment_old_bank_id"] == r["new_bank_id"]:
                print(f"  ALREADY DIVERTED (partner_bank_id is already {r['new_bank_id']}) - "
                      f"nothing to do")
                continue

            print(f"  would SET account_payment.partner_bank_id = {r['new_bank_id']} "
                  f"(payment {r['payment_id']})")
            print(f"  would SET account_move.partner_bank_id    = {r['new_bank_id']} "
                  f"(move {r['move_id']})")
            plan.append({
                "vendor_id": vid, "payment_id": r["payment_id"], "move_id": r["move_id"],
                "old_bank_id": r["payment_old_bank_id"], "new_bank_id": r["new_bank_id"],
            })

        print("\n--- summary ---")
        print(f"{len(VENDOR_IDS)} vendors checked, {len(missing)} unrepairable this way, "
              f"{len(plan)} payment(s) to divert this run")
        if missing:
            print(f"UNREPAIRABLE vendors (no payment on/after account creation): {missing}")
        print(f"\nall 6 vendors have at least one payment on/after their account's create_date: "
              f"{not missing}")

        if not args.apply:
            print("\nDRY RUN - nothing was written. Re-run with --apply to perform the repair.")
            return

        if missing:
            print(f"\nWARNING: proceeding with the {len(plan)} vendor(s) that can be repaired; "
                 f"{missing} still need a different fix and are left untouched.")

        with conn.cursor() as cur:
            for p in plan:
                cur.execute("UPDATE account_payment SET partner_bank_id = %s WHERE id = %s",
                            (p["new_bank_id"], p["payment_id"]))
                cur.execute("UPDATE account_move SET partner_bank_id = %s WHERE id = %s",
                            (p["new_bank_id"], p["move_id"]))
        conn.commit()
        print(f"\nwrote {len(plan)} payment/move pair(s).")

        if plan:
            done.extend(plan)
            GEN_OUT.mkdir(exist_ok=True)
            OUT_FILE.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"recorded {len(plan)} new repair(s) in "
                  f"{OUT_FILE.relative_to(ROOT)} (total recorded: {len(done)})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
