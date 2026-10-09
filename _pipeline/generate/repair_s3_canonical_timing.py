#!/usr/bin/env python
"""repair_s3_canonical_timing.py - post-generation step: move the tamper/revert window of 9
canonical Scenario-3 vendors so that it brackets a real payment.

The canonical vendor_master_mod recipe (anomaly_spec.md) is a bank-account-number edit shortly
before a payment, restored after the payment goes out. For each vendor in PLAN below, this step
keeps the edit/revert tracking rows unchanged (same field, actor, old/new values) and moves only
the `date` of the two existing mail_message rows so the window brackets one existing
account.payment of that vendor: new_edit = payment_date - 2 days, new_revert = payment_date + 3
days. The chosen payment is the one closest to the original window midpoint. No payment is
changed and nothing is created.

PLAN (vendor ids, message ids, payment ids) is hardcoded: it was computed read-only from the
generated database, so it applies to a database generated with the same seed and steps only.
mail_message.date is written directly in Postgres, as in fix_timestamps.sql. Results are recorded
in gen_out/s3_canonical_timing.json; a vendor whose edit date already equals the planned date is
skipped, so the step is idempotent. Dry run is the default; `--apply` writes. `--db` defaults to
odoo.dbname in gen_config.json.

    python _pipeline/generate/repair_s3_canonical_timing.py                # dry run
    python _pipeline/generate/repair_s3_canonical_timing.py --apply        # write
"""
import argparse
import datetime
import json
import pathlib
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GEN_OUT = HERE / "gen_out"

OUT_FILE = GEN_OUT / "s3_canonical_timing.json"

# Computed once, read-only, against the generated database. Each vendor's real payment,
# chosen as the one closest to that vendor's original tamper-window midpoint, plus the new
# edit/revert dates that bracket it (payment - 2 days .. payment + 3 days). message ids are the
# SAME two mail_message rows that already carry the edit/revert tracking values - only their
# `date` moves.
PLAN = [
    {"vendor_id": 53, "edit_msg_id": 28689, "revert_msg_id": 28691, "payment_id": 738,
     "payment_date": "2025-05-28", "new_edit_date": "2025-05-26", "new_revert_date": "2025-05-31"},
    {"vendor_id": 60, "edit_msg_id": 28701, "revert_msg_id": 28703, "payment_id": 1315,
     "payment_date": "2025-05-10", "new_edit_date": "2025-05-08", "new_revert_date": "2025-05-13"},
    {"vendor_id": 65, "edit_msg_id": 28705, "revert_msg_id": 28707, "payment_id": 1208,
     "payment_date": "2025-10-25", "new_edit_date": "2025-10-23", "new_revert_date": "2025-10-28"},
    {"vendor_id": 67, "edit_msg_id": 28709, "revert_msg_id": 28711, "payment_id": 478,
     "payment_date": "2025-02-26", "new_edit_date": "2025-02-24", "new_revert_date": "2025-03-01"},
    {"vendor_id": 69, "edit_msg_id": 28713, "revert_msg_id": 28715, "payment_id": 1333,
     "payment_date": "2025-08-25", "new_edit_date": "2025-08-23", "new_revert_date": "2025-08-28"},
    {"vendor_id": 76, "edit_msg_id": 28717, "revert_msg_id": 28719, "payment_id": 1120,
     "payment_date": "2025-09-19", "new_edit_date": "2025-09-17", "new_revert_date": "2025-09-22"},
    {"vendor_id": 78, "edit_msg_id": 28721, "revert_msg_id": 28723, "payment_id": 640,
     "payment_date": "2025-04-12", "new_edit_date": "2025-04-10", "new_revert_date": "2025-04-15"},
    {"vendor_id": 79, "edit_msg_id": 28725, "revert_msg_id": 28727, "payment_id": 1188,
     "payment_date": "2025-10-15", "new_edit_date": "2025-10-13", "new_revert_date": "2025-10-18"},
    {"vendor_id": 80, "edit_msg_id": 28729, "revert_msg_id": 28731, "payment_id": 667,
     "payment_date": "2025-04-23", "new_edit_date": "2025-04-21", "new_revert_date": "2025-04-26"},
]


def _connect(dbname):
    db = json.loads((ROOT / "_pipeline" / "config.json").read_text(encoding="utf-8"))["db"]
    return psycopg2.connect(
        host=db["host"], port=db["port"], user=db["user"], password=db["password"],
        dbname=dbname, cursor_factory=RealDictCursor)


def _load_done():
    if OUT_FILE.exists():
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    return []


def survey(conn, plan):
    """Read-only: current mail_message.date for the edit/revert ids, and confirm the target
    payment still exists with the same date, before touching anything."""
    with conn.cursor() as cur:
        for row in plan:
            cur.execute("SELECT id, date FROM mail_message WHERE id IN (%s, %s)",
                       (row["edit_msg_id"], row["revert_msg_id"]))
            msgs = {r["id"]: r["date"] for r in cur.fetchall()}
            cur.execute("SELECT id, date, amount FROM account_payment WHERE id = %s",
                       (row["payment_id"],))
            pay = cur.fetchone()
            yield row, msgs, pay


def main():
    ap = argparse.ArgumentParser(
        description="Shift the edit/revert window for 9 canonical Scenario-3 vendors so a real "
                    "payment falls inside it, without touching the payment or the edit itself")
    ap.add_argument("--db", default=None,
                    help="target database (default: odoo.dbname in gen_config.json)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this the run only reports what it would do.")
    args = ap.parse_args()

    if args.db is None:
        args.db = json.loads((HERE / "gen_config.json").read_text(encoding="utf-8"))["odoo"]["dbname"]

    print(f"db={args.db}  mode={'APPLY' if args.apply else 'DRY RUN'}")

    done = {d["vendor_id"] for d in _load_done()}

    conn = _connect(args.db)
    try:
        to_apply = []
        for row, msgs, pay in survey(conn, PLAN):
            vid = row["vendor_id"]
            print(f"\nvendor {vid}:")
            if pay is None:
                sys.exit(f"vendor {vid}: payment {row['payment_id']} no longer exists in "
                         f"{args.db} - stopping rather than guessing")
            if str(pay["date"]) != row["payment_date"]:
                sys.exit(f"vendor {vid}: payment {row['payment_id']} is now dated "
                         f"{pay['date']}, expected {row['payment_date']} - stopping")

            new_edit = datetime.date.fromisoformat(row["new_edit_date"])
            new_revert = datetime.date.fromisoformat(row["new_revert_date"])
            edit_now = msgs[row["edit_msg_id"]]
            revert_now = msgs[row["revert_msg_id"]]

            print(f"  payment {row['payment_id']} on {pay['date']} (${pay['amount']}) confirmed")
            print(f"  edit message {row['edit_msg_id']}: currently {edit_now}")
            print(f"  revert message {row['revert_msg_id']}: currently {revert_now}")

            if vid in done and str(edit_now.date()) == row["new_edit_date"]:
                print(f"  ALREADY REPAIRED - nothing to do")
                continue

            print(f"  would SET message {row['edit_msg_id']}.date   = {new_edit} 00:00:00")
            print(f"  would SET message {row['revert_msg_id']}.date = {new_revert} 00:00:00")
            to_apply.append((row, new_edit, new_revert))

        print(f"\n--- summary ---")
        print(f"{len(PLAN)} vendors checked, {len(to_apply)} window(s) to shift this run")

        if not args.apply:
            print("\nDRY RUN - nothing was written. Re-run with --apply to perform the repair.")
            return

        results = list(_load_done())
        with conn.cursor() as cur:
            for row, new_edit, new_revert in to_apply:
                cur.execute("UPDATE mail_message SET date = %s WHERE id = %s",
                           (f"{new_edit} 00:00:00", row["edit_msg_id"]))
                cur.execute("UPDATE mail_message SET date = %s WHERE id = %s",
                           (f"{new_revert} 00:00:00", row["revert_msg_id"]))
                results.append({**row, "applied_new_edit_date": str(new_edit),
                               "applied_new_revert_date": str(new_revert)})
        conn.commit()
        print(f"\nwrote {len(to_apply)} window(s).")

        GEN_OUT.mkdir(exist_ok=True)
        OUT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"recorded in {OUT_FILE.relative_to(ROOT)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
