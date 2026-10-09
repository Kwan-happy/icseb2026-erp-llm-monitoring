#!/usr/bin/env python
"""repair_cross_po.py - post-generation step: build the cross-PO duplicate-payment pairs.

For each evasive cross-PO duplicate_payment pair in plan.json (resolved by `pair_id` and
`pair_role`, with a vendor-match guard), this step:
  - removes any duplicate bill and payment that is not attached to the correct anchor,
  - clears a shared invoice reference from any bill outside the pair,
  - calls inject_anomalies._apply_s1_cross_po with the correct anchor, so the rows are built by
    the same construction, spine linkage and payment wizard as the generator,
  - backdates the new rows in Postgres (Odoo stamps them with the current time).

Labels are not changed. A pair whose `injected.json` entry already points at the correct anchor
is reported and skipped, so the step is re-runnable. Dry run is the default; `--apply` writes.
`--db` defaults to odoo.dbname in gen_config.json; `--pair N` limits the run to one pair.

    python _pipeline/generate/repair_cross_po.py                    # dry run, all pairs
    python _pipeline/generate/repair_cross_po.py --pair 5 --apply   # one pair
    python _pipeline/generate/repair_cross_po.py --apply            # all pairs
"""
import argparse
import datetime
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import odoo_client                      # noqa: E402
import timestamps                       # noqa: E402
import build_normal as _bn              # noqa: E402
import inject_anomalies as _inj         # noqa: E402

GEN_OUT = HERE / "gen_out"


# ---------------------------------------------------------------- loading

def _load(name):
    return json.loads((GEN_OUT / name).read_text(encoding="utf-8"))


def resolve_pairs(plan):
    """Every cross-PO pair, resolved by `pair_id` and `pair_role`. The `anchor_po_key`
    string in plan.json is not used."""
    pairs = {}
    for po in plan["pos"]:
        a = po.get("anomaly") or {}
        if a.get("type") != "duplicate_payment" or a.get("variant") != "cross_po":
            continue
        pairs.setdefault(a["pair_id"], {})[a["pair_role"]] = po
    out = []
    for pair_id, members in sorted(pairs.items()):
        dup, anchor = members.get("duplicate"), members.get("anchor")
        if dup is None or anchor is None:
            raise KeyError(f"pair_id={pair_id!r} is incomplete: {sorted(members)}")
        if dup["vendor_idx"] != anchor["vendor_idx"]:
            raise ValueError(
                f"pair_id={pair_id!r} spans two vendors ({dup['vendor_idx']} vs "
                f"{anchor['vendor_idx']}) - the plan itself is wrong, not just the injection")
        out.append((pair_id, anchor, dup))
    return out


# ---------------------------------------------------------------- inspection

def survey(session, pair_id, anchor, dup, applied, injected):
    """Read-only: what is in the DB now, and what the repair would change."""
    dup_key, anchor_key = dup["po_key"], anchor["po_key"]
    shared_ref = (dup.get("anomaly") or {}).get("shared_ref")
    inj = (injected.get("pos") or {}).get(dup_key) or {}

    anchor_applied = _inj._applied_po(applied, anchor_key)
    dup_applied = _inj._applied_po(applied, dup_key)
    anchor_bill_id = (anchor_applied.get("bill_ids") or [None])[0]

    wrong_bill_id = inj.get("duplicate_bill_id")
    wrong_pay_id = inj.get("duplicate_payment_id")

    wrong_bill = session.search_read(
        "account.move", [("id", "=", wrong_bill_id)],
        ["name", "ref", "amount_total", "invoice_origin", "state"]) if wrong_bill_id else []
    anchor_bill = session.search_read(
        "account.move", [("id", "=", anchor_bill_id)],
        ["name", "ref", "amount_total", "invoice_origin", "invoice_date", "state"])

    # The bill that wrongly received this pair's shared_ref: carries the ref but is neither the
    # wrong duplicate bill nor the true anchor's bill.
    strays = session.search_read(
        "account.move",
        [("ref", "=", shared_ref), ("move_type", "=", "in_invoice"),
         ("id", "not in", [i for i in (wrong_bill_id, anchor_bill_id) if i])],
        ["name", "ref", "amount_total", "invoice_origin"])

    return {
        "pair_id": pair_id, "shared_ref": shared_ref,
        "dup_po": dup_applied["po_number"], "dup_key": dup_key,
        "anchor_po": anchor_applied["po_number"], "anchor_key": anchor_key,
        "vendor_idx": dup["vendor_idx"],
        "wrong_bill": wrong_bill[0] if wrong_bill else None,
        "wrong_bill_id": wrong_bill_id, "wrong_pay_id": wrong_pay_id,
        "anchor_bill": anchor_bill[0] if anchor_bill else None,
        "anchor_bill_id": anchor_bill_id,
        "strays": strays,
        # A pair counts as done when its injected.json record already names the TRUE anchor.
        # Testing "does the recorded bill still exist" instead would be wrong: after a repair
        # the record names the NEW bill, which does exist, and a second run would delete the
        # good bill and rebuild it. Harmless but wasteful, and it churns document numbers.
        "already_repaired": inj.get("anchor_po_number") == anchor_applied["po_number"],
    }


def print_survey(s):
    print(f"\n--- pair {s['pair_id']}  ref={s['shared_ref']}  vendor_idx={s['vendor_idx']}")
    if s["already_repaired"]:
        print(f"    ALREADY REPAIRED - already paired with the correct anchor "
              f"{s['anchor_po']}, skipping")
        return
    wb, ab = s["wrong_bill"], s["anchor_bill"]
    print(f"    duplicate PO {s['dup_po']}: bill {s['wrong_bill_id']} "
          f"{wb['name'] if wb else '?'} amount={wb['amount_total'] if wb else '?'} "
          f"ref={wb['ref'] if wb else '?'}  <- DELETE (payment {s['wrong_pay_id']} too)")
    print(f"    anchor PO    {s['anchor_po']}: bill {s['anchor_bill_id']} "
          f"{ab['name'] if ab else '?'} amount={ab['amount_total'] if ab else '?'} "
          f"ref={ab['ref'] if ab else '(none)'}  <- keep, set ref={s['shared_ref']}")
    print(f"    NEW duplicate bill on {s['dup_po']} will carry amount="
          f"{ab['amount_total'] if ab else '?'} ref={s['shared_ref']}")
    for st in s["strays"]:
        print(f"    stray ref on {st['invoice_origin']} (bill {st['id']} {st['name']}, "
              f"amount={st['amount_total']})  <- CLEAR ref")


# ---------------------------------------------------------------- repair

def _release_sequence(session, move_ids):
    """Give a draft move its sequence number back before deletion.

    Odoo refuses to unlink a move that has consumed a sequence number and is not the last one in
    its chain ("You cannot delete this entry, as it has already consumed a sequence number...").
    Setting `name` back to '/' while the move is in draft releases that slot, which is Odoo's own
    mechanism for the same situation."""
    for mid in move_ids:
        if mid:
            session.write("account.move", mid, {"name": "/"})


def delete_wrong_artifacts(cluster, dup, s):
    """Remove the wrong duplicate bill and its payment. Posted accounting documents are
    immutable in Odoo, so each is reset to draft and has its sequence number released first.
    The payment goes first because it is reconciled against the bill."""
    session = cluster.as_(dup["buyer_session"])

    if s["wrong_pay_id"]:
        pay = session.search_read("account.payment", [("id", "=", s["wrong_pay_id"])],
                                  ["state", "move_id"])
        if pay:
            session.call_button("account.payment", s["wrong_pay_id"], "action_draft")
            move_id = pay[0]["move_id"][0] if pay[0].get("move_id") else None
            _release_sequence(session, [move_id])
            session.execute("account.payment", "unlink", [[s["wrong_pay_id"]]])
            if session.search_read("account.payment", [("id", "=", s["wrong_pay_id"])], ["id"]):
                raise RuntimeError(f"payment {s['wrong_pay_id']} survived unlink")
            print(f"    deleted payment {s['wrong_pay_id']} (and its move {move_id})")

    if s["wrong_bill_id"]:
        if session.search_read("account.move", [("id", "=", s["wrong_bill_id"])], ["state"]):
            session.call_button("account.move", s["wrong_bill_id"], "button_draft")
            _release_sequence(session, [s["wrong_bill_id"]])
            session.execute("account.move", "unlink", [[s["wrong_bill_id"]]])
            if session.search_read("account.move", [("id", "=", s["wrong_bill_id"])], ["id"]):
                raise RuntimeError(f"bill {s['wrong_bill_id']} survived unlink")
            print(f"    deleted bill {s['wrong_bill_id']}")


def clear_stray_refs(cluster, dup, s):
    """Take this pair's shared_ref back off any uninvolved bill that carries it."""
    session = cluster.as_(dup["buyer_session"])
    for st in s["strays"]:
        session.write("account.move", st["id"], {"ref": False})
        print(f"    cleared ref from bill {st['id']} ({st['invoice_origin']})")


def backdate(conn, move_ids, payment_ids, when):
    """Realign the rows Odoo stamped with wall-clock now. fix_timestamps.sql replays CSVs
    captured during generation and knows nothing about rows created afterwards, so the repair
    does its own targeted update over exactly the ids it created."""
    ts = f"{when} 09:00:00"
    with conn.cursor() as cur:
        if move_ids:
            cur.execute("UPDATE account_move SET create_date=%s, write_date=%s "
                        "WHERE id = ANY(%s)", (ts, ts, move_ids))
            cur.execute("UPDATE account_move_line SET create_date=%s, write_date=%s "
                        "WHERE move_id = ANY(%s)", (ts, ts, move_ids))
            cur.execute("UPDATE mail_message SET date=%s "
                        "WHERE model='account.move' AND res_id = ANY(%s)", (ts, move_ids))
        if payment_ids:
            cur.execute("UPDATE account_payment SET create_date=%s, write_date=%s "
                        "WHERE id = ANY(%s)", (ts, ts, payment_ids))
    conn.commit()


def repair_pair(cluster, conn, pair_id, anchor, dup, master, applied, injected, s):
    print(f"    repairing pair {pair_id} ...")
    delete_wrong_artifacts(cluster, dup, s)
    clear_stray_refs(cluster, dup, s)

    # Rebuild with the CORRECT anchor, through the generator's own code path.
    tsmap = timestamps.TimestampMap()
    record = _inj._apply_s1_cross_po(cluster, anchor, dup, master, applied, tsmap)
    print(f"    created bill {record['duplicate_bill_id']} amount={record['target_total']} "
          f"payment {record['duplicate_payment_id']}  anchor={record['anchor_po_number']}")

    session = cluster.as_(dup["buyer_session"])
    new_bill = session.search_read(
        "account.move", [("id", "=", record["duplicate_bill_id"])], ["invoice_date"])[0]
    pay_move = session.search_read(
        "account.payment", [("id", "=", record["duplicate_payment_id"])], ["move_id", "date"])[0]
    move_ids = [record["duplicate_bill_id"]]
    if pay_move.get("move_id"):
        move_ids.append(pay_move["move_id"][0])
    backdate(conn, move_ids, [record["duplicate_payment_id"]], new_bill["invoice_date"])
    print(f"    backdated {len(move_ids)} move(s) and 1 payment to {new_bill['invoice_date']}")

    injected.setdefault("pos", {})[dup["po_key"]] = record
    return record


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Build the cross-PO duplicate-payment pairs")
    ap.add_argument("--db", default=None,
                    help="target database (default: odoo.dbname in gen_config.json)")
    ap.add_argument("--pair", type=int, default=None, help="repair only this pair_id")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this the run only reports what it would do.")
    args = ap.parse_args()

    if args.db is None:
        args.db = json.loads((HERE / "gen_config.json").read_text(encoding="utf-8"))["odoo"]["dbname"]

    cfg = json.loads((HERE / "gen_config.json").read_text(encoding="utf-8"))
    odoo_cfg = dict(cfg["odoo"])
    odoo_cfg["dbname"] = args.db
    cluster = odoo_client.OdooCluster.from_gen_config(odoo_cfg)

    plan = _load("plan.json")
    master = _load("master.json")
    applied = _load("applied.json")
    injected = _load("injected.json")

    # build_normal's period clamp is normally initialised by apply_normal(); set it here so the
    # payment dates this repair draws land inside the dataset period like every other one.
    _bn._PERIOD_END = datetime.date.fromisoformat(
        str(plan["params_echo"]["generation_end"])[:10])

    pairs = resolve_pairs(plan)
    if args.pair is not None:
        pairs = [p for p in pairs if p[0] == args.pair]
        if not pairs:
            sys.exit(f"no pair with pair_id={args.pair}")

    print(f"db={args.db}  pairs={[p[0] for p in pairs]}  mode={'APPLY' if args.apply else 'DRY RUN'}")
    probe = cluster.as_(pairs[0][2]["buyer_session"])

    surveys = []
    for pair_id, anchor, dup in pairs:
        s = survey(probe, pair_id, anchor, dup, applied, injected)
        surveys.append((pair_id, anchor, dup, s))
        print_survey(s)

    if not args.apply:
        print("\nDRY RUN - nothing was written. Re-run with --apply to perform the repair.")
        return

    conn = None
    try:
        import psycopg2
        # Credentials come from the extraction pipeline's config, the one place they are already
        # kept; only the database name is overridden.
        db = json.loads((ROOT / "_pipeline" / "config.json").read_text(encoding="utf-8"))["db"]
        conn = psycopg2.connect(
            host=db["host"], port=db["port"], user=db["user"],
            password=db["password"], dbname=args.db)
    except Exception as e:
        sys.exit(f"cannot open a Postgres connection for the timestamp repair: {e}")

    done = []
    try:
        for pair_id, anchor, dup, s in surveys:
            if s["already_repaired"]:
                continue
            done.append(repair_pair(cluster, conn, pair_id, anchor, dup,
                                    master, applied, injected, s))
    finally:
        conn.close()
        if done:
            (GEN_OUT / "injected.json").write_text(
                json.dumps(injected, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\nrewrote gen_out/injected.json for {len(done)} pair(s)")

    print(f"\nrepaired {len(done)} pair(s). Re-run _pipeline/extract.py against {args.db} next.")


if __name__ == "__main__":
    main()
