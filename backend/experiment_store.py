"""Durable experiment records and order intents, separate from frozen research."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _aware_timestamp(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


class ExperimentStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, created_at TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS runs_created_at ON runs(created_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS run_index (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    collection_hash TEXT, input_key TEXT, summary TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_index_created_at ON run_index(created_at DESC,id DESC);
                CREATE INDEX IF NOT EXISTS run_index_collection ON run_index(collection_hash);
                CREATE INDEX IF NOT EXISTS run_index_input ON run_index(input_key);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, intent_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mirror_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, from_qty INTEGER NOT NULL, to_qty INTEGER NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(order_id,to_qty));
                CREATE INDEX IF NOT EXISTS mirror_fills_account ON mirror_fills(fingerprint,id);
            """)
            # Backfill only derived metadata; frozen run payloads stay byte-for-byte intact.
            for (payload,) in db.execute("SELECT r.payload FROM runs r LEFT JOIN run_index i ON i.id=r.id "
                                         "WHERE i.id IS NULL").fetchall():
                self._index_run(db, json.loads(payload))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def save_setting(self, key, value):
        self.save_settings({key: value})

    def save_settings(self, values):
        with self.connect() as db:
            db.executemany("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                           "WHERE settings.value != excluded.value",
                           [(key, encode(value)) for key, value in values.items()])

    @staticmethod
    def _index_run(db, run):
        original = (run.get("analysis") or {}).get("input")
        input_key = None
        if original and run.get("version_id"):
            normalized = {key: value for key, value in original.items() if key != "observed_at"}
            normalized["analysis_version"] = run["version_id"]
            input_key = hashlib.sha256(encode(normalized).encode()).hexdigest()
        summary = {key: value for key, value in run.items() if key != "analysis"}
        db.execute("INSERT OR IGNORE INTO run_index VALUES (?,?,?,?,?)",
                   (run["id"], run["created_at"], run.get("collection_hash"), input_key, encode(summary)))

    def add_run(self, run):
        stored = dict(run)
        analysis = run.get("analysis")
        if isinstance(analysis, dict) and analysis.get("decisions") == run.get("signals"):
            stored["analysis"] = {key: value for key, value in analysis.items() if key != "decisions"}
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run["id"], run["as_of"], run["created_at"], encode(stored)))
            self._index_run(db, run)

    def runs(self, limit=100, *, details=True):
        with self.connect() as db:
            query = ("SELECT payload FROM runs" if details else "SELECT summary FROM run_index")
            runs = [json.loads(row[0]) for row in db.execute(query + " ORDER BY created_at DESC,id DESC LIMIT ?", (limit,))]
        for run in runs:
            if isinstance(run.get("analysis"), dict) and "decisions" not in run["analysis"] and "signals" in run:
                run["analysis"]["decisions"] = run["signals"]
        return runs

    def find_run(self, collection_hash, *, exact=False):
        with self.connect() as db:
            condition = "collection_hash=?" if exact else "collection_hash=? OR input_key=?"
            parameters = (collection_hash,) if exact else (collection_hash, collection_hash)
            row = db.execute("SELECT summary FROM run_index WHERE " + condition +
                             " ORDER BY created_at DESC,id DESC LIMIT 1", parameters).fetchone()
        return json.loads(row[0]) if row else None

    def run_revision(self):
        with self.connect() as db:
            return tuple(db.execute("SELECT COUNT(*),MAX(rowid) FROM runs").fetchone())

    def reserve_order(self, order, key):
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?,?)",
                                (order["id"], key, order["created_at"], encode(order)))
            return cursor.rowcount == 1

    @staticmethod
    def _fill_change(previous, order):
        """A confirmed cumulative observation, never an execution acknowledgement."""
        from backend.mirror_plan import MirrorInputError, _orders
        fields = ("id", "fingerprint", "order_date", "symbol", "side", "quantity")
        if any(previous.get(field) != order.get(field) for field in fields):
            raise MirrorInputError("source_order_identity_conflict")
        fingerprint = order.get("fingerprint")
        old = _orders([previous], fingerprint)
        new = _orders([order], fingerprint)
        if old and old.keys() != new.keys():
            raise MirrorInputError("source_order_identity_conflict")
        before, after = previous["filled_quantity"], order["filled_quantity"]
        old_amount, amount = Decimal(str(previous["filled_amount"])), Decimal(str(order["filled_amount"]))
        if after < before or amount < old_amount:
            raise MirrorInputError("source_fill_decreased")
        if after == before and amount != old_amount or after > before and amount <= old_amount:
            raise MirrorInputError("source_fill_amount_conflict")
        if after == before:
            return None
        observed_at = order.get("reconciled_at")
        if not _aware_timestamp(observed_at):
            raise MirrorInputError("verified_observation_timestamp_required") from None
        return {"source_key": next(iter(new)), "source_fingerprint": fingerprint,
                "symbol": order["symbol"], "side": order["side"], "quantity": after - before,
                "amount": str(amount - old_amount), "from_quantity": before, "to_quantity": after,
                "source_order_id": order["id"], "observed_at": observed_at}

    def save_order(self, order, *, capture_fill=False):
        with self.connect() as db:
            if capture_fill:
                # Updating the order and observing its new fill are one commit.
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT payload FROM orders WHERE id=?", (order["id"],)).fetchone()
                if row is None:
                    raise ValueError("주문 원본이 없습니다.")
                change = self._fill_change(json.loads(row[0]), order)
                payload = encode(order)
                if payload != row[0]:
                    db.execute("UPDATE orders SET payload=? WHERE id=?", (payload, order["id"]))
                if change is not None:
                    db.execute("INSERT INTO mirror_fills(order_id,fingerprint,from_qty,to_qty,payload) VALUES (?,?,?,?,?)",
                               (order["id"], change["source_fingerprint"], change["from_quantity"],
                                change["to_quantity"], encode(change)))
                return
            cursor = db.execute("UPDATE orders SET payload=? WHERE id=?", (encode(order), order["id"]))
            if cursor.rowcount != 1:
                raise ValueError("주문 원본이 없습니다.")

    def orders(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM orders ORDER BY created_at,id")]

    def mirror_snapshot(self, *, after=0, limit=100):
        """Read one account-scoped observation feed and full ledger snapshot.

        This does not prove broker freshness, reconcile external/manual trades,
        or acknowledge any destination submission. Existing fills are not replayed.
        """
        from backend.mirror_plan import detect_fill_changes
        if type(after) is not int or after < 0 or after > 2**63 - 1:
            raise ValueError("invalid_mirror_cursor")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_mirror_limit")
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT value FROM settings WHERE key='policy'").fetchone()
            policy = json.loads(row[0]) if row else {}
            account = policy.get("account_id") if isinstance(policy, dict) else None
            fingerprint = policy.get("fingerprint") if isinstance(policy, dict) else None
            configured = all(isinstance(value, str) and value.strip() for value in (account, fingerprint))
            controls = {key: json.loads(value) for key, value in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('enabled','user_paused')")}
            if after:
                cursor = db.execute("SELECT fingerprint FROM mirror_fills WHERE id=?", (after,)).fetchone()
                if not configured or cursor is None or cursor[0] != fingerprint:
                    raise ValueError("mirror_cursor_account_mismatch_or_missing")
            result = {"source_account": account if configured else None,
                      "source_fingerprint": fingerprint if configured else None,
                      "status": "observing" if configured else "unconfigured", "submission_enabled": False,
                      "orders_enabled": controls.get("enabled") is True,
                      "user_paused": controls.get("user_paused") is True,
                      "events": [], "next_cursor": after, "has_more": False,
                      "owned_quantities": {}, "source_issues": []}
            if not configured:
                return result
            rows = db.execute("SELECT id,payload FROM mirror_fills WHERE fingerprint=? AND id>? ORDER BY id LIMIT ?",
                              (fingerprint, after, limit + 1)).fetchall()
            orders = [json.loads(row[0]) for row in db.execute("SELECT payload FROM orders ORDER BY created_at,id")]
            result["owned_quantities"] = detect_fill_changes(
                orders, source_fingerprint=fingerprint, initialize="baseline")["owned_quantities"]
            result["events"] = [{**json.loads(payload), "id": identity} for identity, payload in rows[:limit]]
            result["next_cursor"] = result["events"][-1]["id"] if result["events"] else after
            result["has_more"] = len(rows) > limit
            for order in orders:
                if order.get("fingerprint") != fingerprint:
                    continue
                status = order.get("status")
                reason = None
                if status in {"submitting", "unknown", "cancel_pending"}:
                    reason = "source_order_unconfirmed"
                elif order.get("error"):
                    reason = "source_order_error"
                elif status != "rejected" and (not order.get("order_id") or not order.get("branch_id")):
                    reason = "source_broker_identity_missing"
                elif status != "rejected" and not _aware_timestamp(order.get("reconciled_at")):
                    reason = "source_order_not_reconciled"
                if reason:
                    result["source_issues"].append({"id": order.get("id"), "status": status, "reason": reason})
            return result

    def event(self, kind, message, at=None):
        at = at or datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO events(at,kind,message) VALUES (?,?,?)", (at, kind, message))

    def events(self, limit=40):
        with self.connect() as db:
            return [{"at": row[0], "kind": row[1], "message": row[2]} for row in db.execute(
                "SELECT at,kind,message FROM events ORDER BY id DESC LIMIT ?", (limit,))]
