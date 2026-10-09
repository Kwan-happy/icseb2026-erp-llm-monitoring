"""timestamps.py — intended-timestamp collector for the generator.

Odoo stamps most timestamp columns with `now()` at XML-RPC write time; only a few
fields accept an arbitrary date through the API (`purchase_order.date_order` /
`date_approve`, `account_move.invoice_date` / `date`, `account_payment.date`).
Everything else (create_date/write_date on every model, stock_picking.date_done,
mail_message.date, mail_tracking_value timestamps, ...) is written as "now" by
Odoo regardless of what the generator sends, and must be repaired afterwards with
plain SQL — see fix_timestamps.sql in this directory.

This module does NOT talk to Odoo or Postgres. The generator calls a `TimestampMap`
instance as it creates records, recording "this record/column was CONCEPTUALLY
created at time T even though Odoo will actually stamp it with today's date". At
the end of a generation run the generator calls `flush()`, which writes two CSVs
into gen_out/. fix_timestamps.sql loads those CSVs and rewrites the real rows.

Run from the repo root:
    python _pipeline/generate/timestamps.py
(that just runs the self-test below; the real caller is the generator script.)

---------------------------------------------------------------------------------
ORDERING ASSUMPTION for record_message_sequence() / message_timestamps.csv
---------------------------------------------------------------------------------
A single Odoo record (e.g. one purchase.order, one res.partner) can carry MULTIPLE
chatter messages that must land at DIFFERENT intended times — e.g. the scenario-3
change-then-revert (two mail.message rows: the tampering edit, then the restore),
or a PO's "RFQ Sent" transition followed later by "approved". At generation time we
know the (model, res_id) the messages belong to and the ORDER we created them in,
but not their eventual mail_message.id (Odoo assigns that at write time).

So record_message_sequence(model, res_id, [ts1, ts2, ...]) records an ORDERED list
of intended timestamps for that (model, res_id) pair. The repair script resolves
this by matching mail_message rows for that (model, res_id) IN mail_message.id
ORDER (= Odoo's own creation order, since ids are assigned sequentially) against
ts1, ts2, ... in list order. This assumes the generator emits chatter events for a
given (model, res_id) in the SAME order it lists them here, and that no unrelated
chatter message for that same (model, res_id) is interleaved by Odoo itself between
them. Both hold for this generator: each (model, res_id) group's messages are all
generator-injected state/audit transitions, created back-to-back.

record_message_sequence(..., [ts]) (a single-element list) is equivalent to
record_messages_for(model, res_id, ts) and is provided as the general form; use
record_messages_for() as the convenience shorthand when there is exactly one
intended timestamp for every message on that record (the common case).
"""
from __future__ import annotations

import csv
import datetime as _dt
import pathlib
from dataclasses import dataclass, field
from typing import Union

DateLike = Union[str, _dt.date, _dt.datetime]

OUT_DIR = pathlib.Path(__file__).resolve().parent / "gen_out"
TIMESTAMP_MAP_CSV = "timestamp_map.csv"
MESSAGE_TIMESTAMPS_CSV = "message_timestamps.csv"


def _iso(ts: DateLike) -> str:
    """Normalize to ISO-8601, naive (no timezone suffix, no 'Z') — Odoo stores
    naive UTC timestamps in Postgres `timestamp without time zone` columns."""
    if isinstance(ts, str):
        dt = _dt.datetime.fromisoformat(ts)
    elif isinstance(ts, _dt.datetime):
        dt = ts
    elif isinstance(ts, _dt.date):
        dt = _dt.datetime.combine(ts, _dt.time.min)
    else:
        raise TypeError(f"unsupported timestamp type: {type(ts)!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return dt.isoformat(sep=" ")


@dataclass
class TimestampMap:
    """Accumulates intended timestamps during generation; flush() writes the CSVs
    that fix_timestamps.sql consumes. Pure in-memory bookkeeping — no I/O until
    flush()."""

    # (table, record_id, column) -> intended_ts (ISO string). A dict de-dupes: if
    # the generator calls record() twice for the same cell, the LAST call wins
    # (mirrors normal "the intent changed" semantics; avoids silent duplicate rows).
    _cells: dict[tuple[str, int, str], str] = field(default_factory=dict)

    # (model, res_id) -> ordered list of intended_ts (ISO strings), in the order
    # the generator wants them matched to mail_message.id ascending for that group.
    _message_seqs: dict[tuple[str, int], list[str]] = field(default_factory=dict)

    # ---------------- generic entry point ----------------
    def record(self, table: str, record_id: int, column: str, intended_ts: DateLike) -> None:
        """Record that `table.column` for row `record_id` should end up at
        `intended_ts` once fix_timestamps.sql runs."""
        if record_id is None:
            raise ValueError(f"record_id is None for {table}.{column}")
        self._cells[(table, record_id, column)] = _iso(intended_ts)

    # ---------------- per-table convenience wrappers ----------------
    def record_po(self, po_id: int, create_date: DateLike = None, write_date: DateLike = None) -> None:
        """purchase_order.create_date / write_date. (date_order / date_approve are
        NOT recorded here — those two fields accept the intended date directly via
        XML-RPC at creation time, so they never need SQL repair.)"""
        if create_date is not None:
            self.record("purchase_order", po_id, "create_date", create_date)
        if write_date is not None:
            self.record("purchase_order", po_id, "write_date", write_date)

    def record_po_line(self, line_id: int, create_date: DateLike = None, write_date: DateLike = None) -> None:
        if create_date is not None:
            self.record("purchase_order_line", line_id, "create_date", create_date)
        if write_date is not None:
            self.record("purchase_order_line", line_id, "write_date", write_date)

    def record_picking(self, picking_id: int, ts: DateLike, *, scheduled: bool = True) -> None:
        """stock_picking: create_date, write_date, date_done, and (unless
        scheduled=False) scheduled_date all set to the same intended ts — a
        generated receipt is a single-instant event in this dataset."""
        self.record("stock_picking", picking_id, "create_date", ts)
        self.record("stock_picking", picking_id, "write_date", ts)
        self.record("stock_picking", picking_id, "date_done", ts)
        if scheduled:
            self.record("stock_picking", picking_id, "scheduled_date", ts)

    def record_move(self, move_id: int, ts: DateLike) -> None:
        """stock_move: create_date, write_date, date — set to the same instant as
        the owning picking's ts. Not to be confused with account_move (see
        record_account_move)."""
        self.record("stock_move", move_id, "create_date", ts)
        self.record("stock_move", move_id, "write_date", ts)
        self.record("stock_move", move_id, "date", ts)

    def record_account_move(self, move_id: int, create_date: DateLike = None, write_date: DateLike = None) -> None:
        """account_move (vendor bill). `invoice_date`/`date` are backdatable
        directly via XML-RPC and are NOT recorded here; only create_date/write_date
        need SQL repair."""
        if create_date is not None:
            self.record("account_move", move_id, "create_date", create_date)
        if write_date is not None:
            self.record("account_move", move_id, "write_date", write_date)

    def record_payment(self, payment_id: int, create_date: DateLike = None, write_date: DateLike = None) -> None:
        """account_payment. `date` is backdatable directly via XML-RPC."""
        if create_date is not None:
            self.record("account_payment", payment_id, "create_date", create_date)
        if write_date is not None:
            self.record("account_payment", payment_id, "write_date", write_date)

    def record_partner(self, partner_id: int, create_date: DateLike = None, write_date: DateLike = None) -> None:
        """res_partner (vendor master record)."""
        if create_date is not None:
            self.record("res_partner", partner_id, "create_date", create_date)
        if write_date is not None:
            self.record("res_partner", partner_id, "write_date", write_date)

    def record_partner_bank(self, bank_id: int, create_date: DateLike = None, write_date: DateLike = None) -> None:
        """res_partner_bank (vendor bank account row — scenario-3 target)."""
        if create_date is not None:
            self.record("res_partner_bank", bank_id, "create_date", create_date)
        if write_date is not None:
            self.record("res_partner_bank", bank_id, "write_date", write_date)

    def record_message(self, message_id: int, ts: DateLike) -> None:
        """Use when the generator already knows the mail_message id (e.g. it just
        read it back from an XML-RPC create() return value). Sets create_date,
        write_date, and the chatter-visible `date` column, all to `ts`."""
        self.record("mail_message", message_id, "create_date", ts)
        self.record("mail_message", message_id, "write_date", ts)
        self.record("mail_message", message_id, "date", ts)

    # ---------------- id-less chatter forms ----------------
    def record_messages_for(self, model: str, res_id: int, ts: DateLike) -> None:
        """Use when the generator knows 'every chatter message on this
        (model, res_id) belongs at time T' but does not know / does not want to
        track individual mail_message ids. Shorthand for a single-timestamp
        record_message_sequence() call — see the module docstring for the
        mail_message.id-ordering assumption this relies on.

        One CSV row covers ALL of that record's messages: fix_timestamps.sql matches
        seq to message rank and carries the LAST recorded timestamp forward to every
        higher rank, so messages Odoo adds on its own (creation logs, automatic
        notes) land at T too instead of keeping the wall-clock generation date."""
        self.record_message_sequence(model, res_id, [ts])

    def record_message_sequence(self, model: str, res_id: int, timestamps: list[DateLike]) -> None:
        """Ordered form: `timestamps` is applied to the (model, res_id)'s chatter
        messages IN mail_message.id ORDER (== creation order). Required whenever a
        record has multiple messages that must land at DIFFERENT times — the
        scenario-3 change-then-revert (2 messages: tamper, then restore) and the
        RFQ-sent-then-approved state-log pair. See the module docstring."""
        if not timestamps:
            raise ValueError(f"empty timestamp sequence for {model}#{res_id}")
        key = (model, res_id)
        # APPEND, never replace. Several stages write chatter to the SAME record at different
        # points in the run: build_master creates a vendor/bank (one creation message), then
        # inject_anomalies edits it later (one message per tracked change). Replacing here
        # would drop the earlier entries, shifting every rank by one so that the repair assigns
        # the creation message the first EDIT's timestamp and both edits collapse onto the last
        # one — observed on the first smoke run, where a change-then-revert pair ended up
        # sharing a single date and the pattern stopped being a "change, then revert".
        self._message_seqs.setdefault(key, []).extend(_iso(t) for t in timestamps)

    # ---------------- output ----------------
    def flush(self, out_dir: pathlib.Path | str = OUT_DIR) -> tuple[pathlib.Path, pathlib.Path]:
        """Writes timestamp_map.csv and message_timestamps.csv, sorted
        deterministically, with a header row. Returns the two file paths."""
        out_dir = pathlib.Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        tsmap_path = out_dir / TIMESTAMP_MAP_CSV
        with open(tsmap_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["table", "record_id", "column", "intended_ts"])
            for (table, record_id, column), ts in sorted(self._cells.items()):
                w.writerow([table, record_id, column, ts])

        msg_path = out_dir / MESSAGE_TIMESTAMPS_CSV
        with open(msg_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["model", "res_id", "seq", "intended_ts"])
            for (model, res_id), ts_list in sorted(self._message_seqs.items()):
                for seq, ts in enumerate(ts_list, start=1):
                    w.writerow([model, res_id, seq, ts])

        return tsmap_path, msg_path


# NOTE: no __main__ self-test block here on purpose — see _selftest_timestamps.py
# alongside this file (not part of the frozen module, a throwaway verification
# script) for a script that exercises every method and flushes to the scratchpad.
