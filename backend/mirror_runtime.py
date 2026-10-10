"""Durable source-event consumer. Adapters own all reads and browser actions.

Construction only creates local tables and quarantines interrupted submissions.
tick() may submit through the supplied destination; the CLI owns the process lock.
Source checkpoints are observations, separate from destination order receipts.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from uuid import uuid4

from backend.experiment_store import encode
from backend.mirror_plan import plan_weight_changes


class MirrorBlocked(ValueError):
    """Verified prerequisites are missing or inconsistent; no submission occurred."""


class MirrorUnknown(RuntimeError):
    """A click may have occurred. Reconcile; never blindly repeat it."""


class MirrorRejected(RuntimeError):
    """The destination explicitly rejected the submission."""


ACTIVE = {"pending", "blocked", "submitting", "unknown", "working"}
TERMINAL = {"filled", "completed", "cancelled", "rejected", "unchanged"}


def _text(value, reason):
    if not isinstance(value, str) or not value.strip():
        raise MirrorBlocked(reason)
    return value


def _decimal(value):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise MirrorBlocked("invalid_weight") from None


class MirrorRuntime:
    def __init__(self, store, source, destination, *, now=None, max_age_seconds=180):
        self.store, self.source, self.destination = store, source, destination
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_age_seconds = max_age_seconds
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mirror_runtime (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mirror_jobs (id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            for (payload,) in db.execute("SELECT payload FROM mirror_jobs WHERE status='submitting'").fetchall():
                job = json.loads(payload)
                job.update(status="unknown", reason="interrupted_submission")
                self._write_job(db, job)

    def _stamp(self):
        return self.now().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _write_job(db, job):
        db.execute("INSERT INTO mirror_jobs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "status=excluded.status,payload=excluded.payload WHERE payload!=excluded.payload",
                   (job["id"], job["symbol"], job["status"], encode(job)))

    def _save_job(self, job):
        with self.store.connect() as db:
            self._write_job(db, job)

    def _state(self):
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM mirror_runtime WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def _jobs(self, *, active_only=False):
        with self.store.connect() as db:
            query = "SELECT payload FROM mirror_jobs"
            if active_only:
                query += " WHERE status IN ('pending','blocked','submitting','unknown','working')"
            return [json.loads(row[0]) for row in db.execute(query + " ORDER BY rowid")]

    def snapshot(self):
        return {"binding": self._state(), "jobs": self._jobs()}

    def _validate_source(self, source, cursor):
        if not isinstance(source, dict) or source.get("has_more") is not False:
            raise MirrorBlocked("complete_source_page_required")
        for field in ("source_account", "source_fingerprint"):
            _text(source.get(field), "source_unconfigured")
        try:
            stamp = datetime.fromisoformat(source["as_of"])
            age = (self.now() - stamp).total_seconds()
            if stamp.utcoffset() is None or not -5 <= age <= self.max_age_seconds:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise MirrorBlocked("source_snapshot_stale_or_invalid") from None
        if type(source.get("market_open")) is not bool or type(source.get("orders_enabled")) is not bool:
            raise MirrorBlocked("source_execution_state_unverified")
        if not isinstance(source.get("source_issues"), list) or not isinstance(source.get("events"), list):
            raise MirrorBlocked("source_observations_unverified")
        if not isinstance(source.get("owned_quantities"), dict) or not isinstance(source.get("prices"), dict):
            raise MirrorBlocked("source_portfolio_unverified")
        if any(type(quantity) is not int or quantity < 0 for quantity in source["owned_quantities"].values()):
            raise MirrorBlocked("source_quantity_invalid")
        events, last = [], cursor
        seen = set()
        for event in source["events"]:
            if (not isinstance(event, dict) or type(event.get("id")) is not int or event["id"] <= 0
                    or event.get("source_fingerprint") != source["source_fingerprint"]
                    or type(event.get("quantity")) is not int or event["quantity"] <= 0
                    or event.get("side") not in {"buy", "sell"}):
                raise MirrorBlocked("source_event_invalid")
            _text(event.get("symbol"), "source_symbol_invalid")
            if event["id"] in seen:
                raise MirrorBlocked("duplicate_source_event")
            seen.add(event["id"])
            if event["id"] <= cursor:
                continue
            if event["id"] <= last:
                raise MirrorBlocked("source_event_order_invalid")
            last = event["id"]
            events.append(event)
        if type(source.get("next_cursor")) is not int or source["next_cursor"] != last:
            raise MirrorBlocked("source_cursor_inconsistent")
        return events

    def _validate_destination(self, destination, state=None, *, require_today=False):
        if not isinstance(destination, dict) or destination.get("verified") is not True:
            raise MirrorBlocked("destination_snapshot_unverified")
        for field in ("account_key", "contest"):
            _text(destination.get(field), "destination_identity_missing")
        today = self.now().astimezone(timezone(timedelta(hours=9))).date().isoformat()
        try:
            day = datetime.fromisoformat(destination["session_date"]).date().isoformat()
            if day != destination["session_date"] or day > today:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise MirrorBlocked("destination_session_date_invalid") from None
        if require_today and day != today:
            raise MirrorBlocked("destination_session_date_mismatch")
        if (not isinstance(destination.get("positions"), dict) or not isinstance(destination.get("pending_orders"), list)
                or not isinstance(destination.get("orders"), list)):
            raise MirrorBlocked("destination_portfolio_incomplete")
        if state and (state["account_key"] != destination["account_key"] or state["contest"] != destination["contest"]):
            raise MirrorBlocked("destination_binding_changed")
        identities = set()
        for order in destination["orders"]:
            identity = _text(order.get("order_id") if isinstance(order, dict) else None, "destination_order_id_missing")
            if identity in identities:
                raise MirrorBlocked("destination_duplicate_order_id")
            identities.add(identity)
        return identities

    def _bind(self, source, destination):
        if any(source["owned_quantities"].values()):
            raise MirrorBlocked("initial_source_must_be_empty")
        if (destination["pending_orders"] or destination.get("unresolved_symbols")
                or any(_decimal(row.get("weight")) for row in destination["positions"].values())):
            raise MirrorBlocked("initial_destination_must_be_empty")
        state = {"source_account": source["source_account"], "source_fingerprint": source["source_fingerprint"],
                 "account_key": destination["account_key"], "contest": destination["contest"],
                 "cursor": source["next_cursor"], "baseline_order_ids": sorted(row["order_id"] for row in destination["orders"]),
                 "initialized_at": self._stamp()}
        with self.store.connect() as db:
            db.execute("INSERT INTO mirror_runtime VALUES (1,?)", (encode(state),))
        return state

    def _ingest(self, state, events, next_cursor):
        if not events:
            return state
        grouped = {}
        for event in events:
            grouped[event["symbol"]] = grouped.get(event["symbol"], 0) + event["quantity"] * (1 if event["side"] == "buy" else -1)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actual = json.loads(db.execute("SELECT payload FROM mirror_runtime WHERE id=1").fetchone()[0])
            if actual != state:
                raise MirrorBlocked("concurrent_runner_state_changed")
            for symbol, net in sorted(grouped.items()):
                rows = db.execute("SELECT payload FROM mirror_jobs WHERE symbol=? AND status IN ('pending','blocked') ORDER BY rowid", (symbol,)).fetchall()
                if len(rows) > 1:
                    raise MirrorBlocked("duplicate_pending_symbol")
                job = json.loads(rows[0][0]) if rows else {"id": uuid4().hex, "symbol": symbol,
                       "net_quantity": 0, "created_at": self._stamp()}
                job.update(status="pending", net_quantity=job["net_quantity"] + net,
                           source_cursor=next_cursor, reason=None)
                self._write_job(db, job)
            state = {**state, "cursor": next_cursor}
            db.execute("UPDATE mirror_runtime SET payload=? WHERE id=1", (encode(state),))
        return state

    def _receipt(self, job, receipt):
        proposal = job["proposal"]
        if (not isinstance(receipt, dict) or receipt.get("verified") is not True
                or receipt.get("status") not in {"working", "filled", "completed", "rejected", "cancelled", "unknown"}):
            raise MirrorUnknown("destination_receipt_unverified")
        if receipt["status"] == "unknown":
            raise MirrorUnknown("destination_receipt_unknown")
        identity = _text(receipt.get("order_id"), "destination_receipt_id_missing")
        if (receipt.get("symbol") != proposal["symbol"] or receipt.get("side") != proposal["side"]
                or _decimal(receipt.get("weight")) != _decimal(proposal["weight"])):
            raise MirrorUnknown("destination_receipt_conflict")
        if identity in proposal.get("prior_order_ids", []):
            raise MirrorUnknown("destination_receipt_predates_submission")
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM mirror_jobs WHERE id!=? AND json_extract(payload,'$.receipt.order_id')=? LIMIT 1",
                          (job["id"], identity)).fetchone():
                raise MirrorUnknown("destination_receipt_already_owned")
        filled_weight, filled_quantity = receipt.get("filled_weight"), receipt.get("filled_quantity")
        if filled_weight is not None and _decimal(filled_weight) > _decimal(proposal["weight"]):
            raise MirrorUnknown("destination_receipt_fill_exceeds_order")
        if receipt["status"] == "filled" and (filled_weight is None or _decimal(filled_weight) != _decimal(proposal["weight"])):
            raise MirrorUnknown("destination_full_fill_unverified")
        if filled_quantity is not None and (type(filled_quantity) is not int or filled_quantity < 0):
            raise MirrorUnknown("destination_receipt_quantity_invalid")
        previous = job.get("receipt")
        if previous:
            if previous["order_id"] != identity:
                raise MirrorUnknown("destination_receipt_decreased_or_changed")
            for field in ("filled_quantity", "filled_weight"):
                if (previous.get(field) is not None and receipt.get(field) is not None
                        and _decimal(receipt[field]) < _decimal(previous[field])):
                    raise MirrorUnknown("destination_receipt_decreased_or_changed")
        clean = {key: receipt[key] for key in ("order_id", "status", "symbol", "side", "weight")}
        clean.update(filled_weight=filled_weight, filled_quantity=filled_quantity)
        clean["verified"] = True
        reason = receipt.get("reason") if receipt["status"] == "rejected" else None
        if job.get("receipt") != clean or job["status"] != receipt["status"] or job.get("reason") != reason:
            job.update(status=receipt["status"], receipt=clean, reason=reason, updated_at=self._stamp())
            self._save_job(job)

    def _reconcile(self, destination, jobs):
        for job in jobs:
            if job["status"] not in {"unknown", "working"}:
                continue
            try:
                self._receipt(job, self.destination.lookup(job, snapshot=destination))
            except Exception:
                # An acknowledged order remains acknowledged if a later read
                # fails. Both working and unknown still serialize this symbol.
                job.update(reason="destination_lookup_unresolved")
                self._save_job(job)

    def _manual_symbols(self, destination, state):
        known = set(state["baseline_order_ids"])
        with self.store.connect() as db:
            known.update(row[0] for row in db.execute(
                "SELECT json_extract(payload,'$.receipt.order_id') FROM mirror_jobs WHERE json_extract(payload,'$.receipt.order_id') IS NOT NULL"))
        return {_text(row.get("symbol"), "unowned_destination_order_symbol_missing")
                for row in destination["orders"] if row["order_id"] not in known}

    def _plan(self, job, source, destination):
        net = job["net_quantity"]
        if not net:
            return {"symbol": job["symbol"], "status": "unchanged", "reason": "net_fills_zero"}
        changes = [{"source_fingerprint": source["source_fingerprint"], "symbol": job["symbol"],
                    "side": "buy" if net > 0 else "sell", "quantity": abs(net)}]
        return plan_weight_changes(changes, source_fingerprint=source["source_fingerprint"],
                   owned_quantities=source["owned_quantities"], source_prices=source["prices"], source_equity=source["equity"],
                   destination_positions=destination["positions"], pending_orders=destination["pending_orders"],
                   destination_verified=True)[0]

    def tick(self):
        """One bounded cycle. No automatic retry after an uncertain submission."""
        try:
            state = self._state()
            source = self.source.read(state["cursor"] if state else 0)
            events = self._validate_source(source, state["cursor"] if state else 0)
            if state and any(state[key] != source[key] for key in ("source_account", "source_fingerprint")):
                raise MirrorBlocked("source_binding_changed")
            if state is None:
                destination = self.destination.snapshot([])
                self._validate_destination(destination)
                state = self._bind(source, destination)
                return {"status": "initialized", "cursor": state["cursor"]}
            state = self._ingest(state, events, source["next_cursor"])
            jobs = self._jobs(active_only=True)
            for job in jobs:
                if job["status"] in {"pending", "blocked"} and not job["net_quantity"]:
                    job.update(status="unchanged", reason="net_fills_zero")
                    self._save_job(job)
            executable = source["orders_enabled"] and source["market_open"]
            candidates = [job for job in jobs if job["status"] in {"pending", "blocked"}]
            observing = any(job["status"] in {"unknown", "working"} for job in jobs)
            destination = None
            if executable and candidates:
                destination = self.destination.snapshot(sorted({job["symbol"] for job in jobs}))
                self._validate_destination(destination, state, require_today=True)
            elif observing:
                destination = self.destination.receipt_snapshot()
                self._validate_destination(destination, state)
            if destination is not None:
                self._reconcile(destination, jobs)
            if not source["orders_enabled"]:
                return {"status": "paused", "cursor": state["cursor"]}
            if not source["market_open"]:
                return {"status": "waiting_market", "cursor": state["cursor"]}
            manual = self._manual_symbols(destination, state) if candidates else set()
            unresolved_symbols = set(destination.get("unresolved_symbols", [])) if destination else set()
            ready = []
            for job in jobs:
                if job["status"] not in {"pending", "blocked"}:
                    continue
                unresolved = next((other for other in jobs if other["symbol"] == job["symbol"]
                                   and other["status"] in {"unknown", "working"}), None)
                reason = ("destination_symbol_order_" + unresolved["status"] if unresolved
                          else "unowned_destination_order_detected" if job["symbol"] in manual
                          else "destination_symbol_reservation_unresolved" if job["symbol"] in unresolved_symbols else None)
                if reason:
                    job.update(status="blocked", reason=reason)
                    self._save_job(job)
                    continue
                ready.append(job)
            # One valuation for this batch. KIS already made the investment
            # decision; market-price movement while filling a form is not a veto.
            symbols = sorted({job["symbol"] for job in ready if job["net_quantity"]})
            valued = self.source.value(source, symbols) if symbols else source
            valuation_issues = {row["symbol"]: row["reason"] for row in valued.get("valuation_issues", [])}
            changed_symbols = set()
            for job in ready:
                if job["symbol"] in changed_symbols:
                    continue
                if job["symbol"] in valuation_issues:
                    job.update(status="blocked", reason=valuation_issues[job["symbol"]])
                    self._save_job(job)
                    continue
                proposal = self._plan(job, valued, destination)
                if proposal["status"] != "proposal":
                    job.update(status=proposal["status"], reason=proposal.get("reason"), proposal=proposal)
                    self._save_job(job)
                    continue
                proposal.update(account_key=state["account_key"], contest=state["contest"],
                                session_date=destination["session_date"], job_id=job["id"],
                                prior_order_ids=sorted(row["order_id"] for row in destination["orders"]
                                    if row.get("symbol") == proposal["symbol"] and row.get("side") == proposal["side"]
                                    and _decimal(row.get("weight")) == _decimal(proposal["weight"])))
                try:
                    prepared = self.destination.prepare(proposal)
                except MirrorRejected as error:
                    job.update(status="rejected", reason=str(error), proposal=proposal, updated_at=self._stamp())
                    self._save_job(job)
                    continue
                fields = ("symbol", "side", "account_key", "contest", "session_date")
                if (not isinstance(prepared, dict) or prepared.get("verified") is not True
                        or any(prepared.get(key) != proposal[key] for key in fields)
                        or _decimal(prepared.get("weight")) != _decimal(proposal["weight"])):
                    raise MirrorBlocked("prepared_form_mismatch")
                confirmed = self.source.read(state["cursor"])
                newer = self._validate_source(confirmed, state["cursor"])
                if any(state[key] != confirmed[key] for key in ("source_account", "source_fingerprint")):
                    raise MirrorBlocked("source_binding_changed")
                if newer:
                    state = self._ingest(state, newer, confirmed["next_cursor"])
                    changed_symbols.update(event["symbol"] for event in newer)
                if not confirmed["orders_enabled"] or not confirmed["market_open"]:
                    raise MirrorBlocked("source_execution_prerequisite_changed")
                if job["symbol"] in changed_symbols:
                    continue
                job.update(status="submitting", proposal=proposal, reason=None, submitted_at=self._stamp())
                self._save_job(job)  # Must commit BEFORE the irreversible click.
                try:
                    receipt = self.destination.submit(proposal)
                except MirrorBlocked as error:
                    # Adapter guarantees this failure happened before clicking.
                    job.update(status="blocked", reason=str(error))
                    self._save_job(job)
                except MirrorRejected as error:
                    job.update(status="rejected", reason=str(error))
                    self._save_job(job)
                except Exception:
                    job.update(status="unknown", reason="submission_outcome_unknown")
                    self._save_job(job)
                else:
                    try:
                        self._receipt(job, receipt)
                    except Exception:
                        # Receipt validation follows the click; even a numeric
                        # validation error here must never allow another click.
                        job.update(status="unknown", reason="submission_outcome_unknown")
                        self._save_job(job)
            jobs = self._jobs(active_only=True)
            if any(job["status"] == "unknown" for job in jobs):
                return {"status": "blocked", "reason": "unknown_destination_submission", "cursor": state["cursor"]}
            if changed_symbols:
                return {"status": "source_changed", "cursor": state["cursor"]}
            return {"status": "blocked" if any(job["status"] == "blocked" for job in jobs)
                    else "working" if any(job["status"] == "working" for job in jobs) else "idle",
                    "cursor": state["cursor"]}
        except MirrorBlocked as error:
            return {"status": "blocked", "reason": str(error)}
        except ValueError as error:
            reason = str(error)
            return {"status": "blocked", "reason": reason if re.fullmatch(r"[a-z][a-z0-9_]{0,120}", reason) else "adapter_input_invalid"}
        except Exception:
            return {"status": "blocked", "reason": "adapter_or_storage_error"}
