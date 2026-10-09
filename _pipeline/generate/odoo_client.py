#!/usr/bin/env python
"""odoo_client.py - thin XML-RPC session wrapper for Odoo 19 (dataset generation).

Stdlib only (xmlrpc.client). Every write must go through an explicit named session
(buyer / manager / ...) because Odoo stamps mail_message.create_uid with the acting
user, and the scenario-4 self-approval signal depends on that actor being
correct at write time (not fixed up after the fact).

Usage:
    cluster = OdooCluster.from_gen_config(cfg["odoo"])
    ids = cluster.as_("manager").create("res.partner", {"name": "Acme"})
"""
import copy
import socket
import time
import xmlrpc.client as xc


class OdooCallError(RuntimeError):
    """A model/method call failed at the Odoo (business-logic) level."""

    def __init__(self, model, method, args, fault):
        summary = args[0] if args else None
        # keep the summary short - args[0] can be a big vals dict/list
        if isinstance(summary, (list, dict)) and len(repr(summary)) > 300:
            summary = repr(summary)[:300] + "...(truncated)"
        msg = f"{model}.{method}({summary!r}, ...) failed: {fault}"
        super().__init__(msg)
        self.model = model
        self.method = method
        self.fault = fault


# Transport-level errors worth a couple of quick retries (server hiccup, not a
# business-logic failure). xmlrpc.client.Fault must NEVER be retried here - it means
# Odoo validated the call and rejected it, retrying just repeats the same rejection.
_TRANSPORT_RETRY_EXC = (xc.ProtocolError, OSError, socket.error, ConnectionError, TimeoutError)


class OdooSession:
    """One authenticated XML-RPC session, acting as a single Odoo user."""

    def __init__(self, url, db, login, password, _context=None, _stats=None, _uid=None,
                 _models=None, _common=None):
        self.url = url.rstrip("/")
        self.db = db
        self.login = login
        self.password = password
        self._context = dict(_context or {})
        # stats dict is SHARED across with_context() copies of the same logical user,
        # so .stats() on any of them reflects the whole session's call history.
        self._stats = _stats if _stats is not None else {}

        if _common is not None and _models is not None and _uid is not None:
            # cheap copy path (used by with_context) - reuse the existing connection
            self._common, self._models, self.uid = _common, _models, _uid
            return

        self._common = xc.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xc.ServerProxy(f"{self.url}/xmlrpc/2/object")
        try:
            uid = self._common.authenticate(self.db, self.login, self.password, {})
        except _TRANSPORT_RETRY_EXC as e:
            raise OdooCallError("common", "authenticate", (self.login,), str(e))
        if not uid:
            raise OdooCallError(
                "common", "authenticate", (self.login,),
                "authentication returned False - wrong login/password or db name",
            )
        self.uid = uid

    # ---------- low-level ----------
    def execute(self, model, method, args, kwargs=None):
        """Thin execute_kw wrapper: args is a list of positional args (e.g. [domain] or
        [vals] or [ids, vals]); kwargs is an optional dict (e.g. {'context': {...}})."""
        kwargs = dict(kwargs or {})
        if self._context:
            kwargs["context"] = {**self._context, **kwargs.get("context", {})}

        call = (self.db, self.uid, self.password, model, method, args, kwargs)
        attempts = 3  # 1 try + 2 retries, transport errors only
        for attempt in range(attempts):
            t0 = time.time()
            try:
                result = self._models.execute_kw(*call)
            except xc.Fault as f:
                self._record_stat(model, method, t0)
                raise OdooCallError(model, method, args, f.faultString)
            except _TRANSPORT_RETRY_EXC as e:
                self._record_stat(model, method, t0)
                if attempt == attempts - 1:
                    raise OdooCallError(model, method, args, f"transport error after retries: {e}")
                time.sleep(0.5 * (attempt + 1))  # small linear backoff
                continue
            else:
                self._record_stat(model, method, t0)
                return result

    def _record_stat(self, model, method, t0):
        key = (model, method)
        n, secs = self._stats.get(key, (0, 0.0))
        self._stats[key] = (n + 1, secs + (time.time() - t0))

    def stats(self):
        """{(model, method): {"calls": n, "seconds": s}} cumulative for this session."""
        return {f"{m}.{meth}": {"calls": n, "seconds": round(s, 3)}
                for (m, meth), (n, s) in self._stats.items()}

    # ---------- convenience CRUD ----------
    def create(self, model, vals):
        """Create ONE record; returns its id."""
        return self.execute(model, "create", [[vals]])[0]

    def create_many(self, model, vals_list):
        """Create MANY records in a single round-trip (Odoo's create() accepts a list of
        vals dicts and returns a list of ids in the same order) - always prefer this over
        looping .create() when creating more than a handful of records."""
        if not vals_list:
            return []
        return self.execute(model, "create", [vals_list])

    def write(self, model, ids, vals):
        ids = ids if isinstance(ids, (list, tuple)) else [ids]
        return self.execute(model, "write", [ids, vals])

    def search(self, model, domain):
        return self.execute(model, "search", [domain])

    def search_read(self, model, domain, fields, **kw):
        # kw forwards e.g. limit=, offset=, order= as execute_kw kwargs
        return self.execute(model, "search_read", [domain, fields], kw)

    # A fault whose message matches this is not a business-logic failure: Odoo's own XML-RPC
    # controller (xmlrpc.py's dumps()) raised it AFTER the button method already ran and
    # committed, while trying to serialize a bare Python None return value with allow_none=False.
    # Verified empirically on a smoke run: purchase.order.button_cancel([1061])
    # raised exactly this fault, and a direct DB read immediately after showed state='cancel' -
    # the cancellation had already committed. button_confirm/button_approve never hit this in
    # ~2,000 calls, so it appears specific to button_cancel's return value in
    # this Odoo 19 build, not a general RPC problem.
    _NONE_MARSHAL_FAULT = "cannot marshal None unless allow_none is enabled"

    def call_button(self, model, ids, method):
        """Invoke a business-logic method as if a UI button were clicked (e.g. button_confirm,
        button_approve, button_cancel). ids may be a single id or a list.

        Swallows ONLY the exact None-marshalling transport fault documented above, and only for
        this method - never for execute()/write()/create(), where a masked failure could corrupt
        data silently. Every caller that relies on this must independently verify the expected
        state change with a fresh read (build_normal.py's cancel step does) - this method itself
        makes no claim beyond "the RPC round-trip's response could not be decoded", not "the
        button's business logic necessarily succeeded"."""
        ids = ids if isinstance(ids, (list, tuple)) else [ids]
        try:
            return self.execute(model, method, [ids])
        except OdooCallError as e:
            if self._NONE_MARSHAL_FAULT in e.fault:
                return None
            raise

    def with_context(self, ctx):
        """Return a shallow copy of this session whose subsequent calls merge `ctx` into the
        Odoo call context (needed for wizards / tz / lang / tracking-disable flags etc.).
        Shares the underlying connection and .stats() with the original."""
        merged = {**self._context, **ctx}
        return OdooSession(
            self.url, self.db, self.login, self.password,
            _context=merged, _stats=self._stats, _uid=self.uid,
            _models=self._models, _common=self._common,
        )


class OdooCluster:
    """A named set of OdooSession objects sharing one db, built from gen_config.json's
    {"odoo": {"url", "dbname", "users": {name: {"login","password"}}}} block."""

    def __init__(self, sessions):
        self._sessions = sessions  # {name: OdooSession}

    @classmethod
    def from_gen_config(cls, odoo_cfg):
        url = odoo_cfg["url"]
        dbname = odoo_cfg["dbname"]
        sessions = {}
        for name, creds in odoo_cfg["users"].items():
            sessions[name] = OdooSession(url, dbname, creds["login"], creds["password"])
        return cls(sessions)

    def as_(self, name):
        try:
            return self._sessions[name]
        except KeyError:
            raise KeyError(f"no session named {name!r}; have {list(self._sessions)}")

    def alias(self, name, target):
        """Bind a symbolic session name to an existing one. The plan is built before any
        connection exists, so it cannot know WHICH manager holds the elevated rights; it emits
        the symbolic actor "setup" for master data and the scenario-3 bank-account edits, and
        preflight resolves it here once the real rights are known."""
        self._sessions[name] = self.as_(target)
        return self._sessions[name]

    def names(self):
        return list(self._sessions)

    def stats(self):
        """Merged stats across every session in the cluster, keyed by user then call."""
        return {name: s.stats() for name, s in self._sessions.items()}


# ---------- preflight ----------
def preflight(cluster, expected):
    """Verify the target Odoo DB is safe to generate into. FAILS FAST (raises RuntimeError
    with an actionable message) rather than trying to fix anything - the author sets up
    the DB/users/settings by hand in the Odoo UI.

    expected: {"approval_threshold": 20000} (only key currently checked; kept as a dict
    so the controller can extend it without changing this function's signature).

    Returns a dict summary of everything it found, on success.
    """
    problems = []
    summary = {}

    # 1. every login already succeeded in OdooCluster construction (authenticate() raises
    #    OdooCallError otherwise) - if we get here, record who authenticated as what uid.
    #    Session naming is a contract: 'buyer*' = non-manager purchasing users POs are
    #    attributed to, 'manager*' = approvers. Several of each is fine (and more realistic
    #    than a single buyer); at least one of each is required.
    buyers = sorted(n for n in cluster.names() if n.startswith("buyer"))
    managers = sorted(n for n in cluster.names() if n.startswith("manager"))
    if not buyers:
        problems.append("gen_config.json defines no 'buyer*' user session")
    if not managers:
        problems.append("gen_config.json defines no 'manager*' user session")
    summary["logins"] = {n: cluster.as_(n).uid for n in cluster.names()}
    summary["roles"] = {"buyers": buyers, "managers": managers}

    if problems:  # can't check groups/settings without at least one of each
        raise RuntimeError("preflight failed:\n- " + "\n- ".join(problems))

    # 2. group membership - read via res.users.has_group (each user checking themselves,
    #    since group membership visibility follows the acting user's own access).
    groups = {}
    for name in buyers:
        s = cluster.as_(name)
        is_mgr = s.execute("res.users", "has_group", [[s.uid], "purchase.group_purchase_manager"])
        is_usr = s.execute("res.users", "has_group", [[s.uid], "purchase.group_purchase_user"])
        groups[name] = {"login": s.login, "purchase_user": is_usr, "purchase_manager": is_mgr}
        if is_mgr:
            problems.append(
                f"buyer session {name!r} ({s.login}) has purchase.group_purchase_manager - it "
                "must NOT. A manager who confirms an above-threshold PO self-approves it, so "
                "such a user can never produce the normal 'routed to To Approve' path that the "
                "scenario-4 signal is defined against. Set Purchase = User (not "
                "Administrator) for this user in Settings > Users, or move it to a 'manager*' "
                "key in gen_config.json."
            )
        if not is_usr:
            problems.append(
                f"buyer session {name!r} ({s.login}) lacks purchase.group_purchase_user - "
                "grant Purchase = User in Settings > Users.")
    for name in managers:
        s = cluster.as_(name)
        is_mgr = s.execute("res.users", "has_group", [[s.uid], "purchase.group_purchase_manager"])
        groups[name] = {"login": s.login, "purchase_manager": is_mgr}
        if not is_mgr:
            problems.append(
                f"manager session {name!r} ({s.login}) lacks purchase.group_purchase_manager - "
                "grant Purchase = Administrator in Settings > Users.")
    summary["groups"] = groups

    manager = cluster.as_(managers[0])

    # 3. two-step approval + threshold, read off the manager's company.
    company_id = manager.execute("res.users", "read", [[manager.uid], ["company_id"]])[0]["company_id"][0]
    comp = manager.execute("res.company", "read",
                            [[company_id], ["po_double_validation", "po_double_validation_amount"]])[0]
    summary["company"] = {
        "id": company_id,
        "po_double_validation": comp.get("po_double_validation"),
        "po_double_validation_amount": comp.get("po_double_validation_amount"),
    }
    if comp.get("po_double_validation") != "two_step":
        problems.append(
            f"po_double_validation is {comp.get('po_double_validation')!r}, expected 'two_step' "
            "(Purchase > Settings > 'Purchase Order Approval' must be set to 'two levels'). "
            "Without two-step approval, above-threshold POs never route to a separate approver "
            "and the scenario-4 signal cannot exist."
        )
    want_threshold = expected.get("approval_threshold", 20000)
    if comp.get("po_double_validation_amount") != want_threshold:
        problems.append(
            f"po_double_validation_amount is {comp.get('po_double_validation_amount')!r}, "
            f"expected {want_threshold} (must match _pipeline/config.json's approval_threshold "
            "and params.json's fixed_constants.approval_threshold exactly)."
        )

    # 3b. Master-data rights. Creating product.product and creating/writing res.partner.bank
    #     are gated by product.group_product_manager / base.group_partner_manager, and in a
    #     stock Odoo 19 DB those are implied ONLY by base.group_system ("Administration:
    #     Settings"). A plain purchase manager can create VENDORS but not products or bank
    #     accounts.  At least one manager session must carry
    #     both, since it performs master-data setup and the scenario-3 bank-account edits.
    setup = None
    for name in managers:
        s = cluster.as_(name)
        has_partner = s.execute("res.users", "has_group", [[s.uid], "base.group_partner_manager"])
        has_product = s.execute("res.users", "has_group", [[s.uid], "product.group_product_manager"])
        groups[name]["partner_manager"] = has_partner
        groups[name]["product_manager"] = has_product
        if has_partner and has_product and setup is None:
            setup = name
    summary["setup_session"] = setup
    if setup is None:
        problems.append(
            "no 'manager*' session can create products and bank accounts. Grant ONE manager "
            "user the 'Administration: Settings' access right in Settings > Users (it implies "
            "both base.group_partner_manager and product.group_product_manager). Without it, "
            "master-data setup and the scenario-3 bank-account tampering both fail mid-run."
        )

    # 4. DB must be FRESH - no existing purchase orders.
    po_count = manager.execute("purchase.order", "search_count", [[]])
    summary["existing_purchase_order_count"] = po_count
    if po_count:
        problems.append(
            f"target DB already has {po_count} purchase.order record(s) - generation requires "
            "a FRESH, empty DB. Create a new database and re-point gen_config.json at it."
        )

    if problems:
        raise RuntimeError("preflight failed:\n- " + "\n- ".join(problems))
    return summary
