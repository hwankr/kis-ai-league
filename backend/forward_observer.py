"""Append-only observation of an unadopted daily rule; never sends orders."""
from __future__ import annotations

import ast
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from backend.kis import KST, ROOT
from backend.request_gate import file_lock
from backend.universe import load_universe


FEES = {"buy_fee": Decimal("0.000140527"), "sell_fee": Decimal("0.000140527"),
        "sell_tax": Decimal("0.002")}
SLIPPAGES = (Decimal("0.001"), Decimal("0.002"))
MAX_BYTES = 32 * 1024 * 1024
MARKET_FIELDS = ("universe", "histories", "calendars", "indices")


def _observer_code_hash(source):
    """Keep the established research version across this storage-only upgrade.

    Price, timing and judgment implementations remain fingerprinted. A change
    to any of them falls back to the complete source hash and starts a version.
    The actual source is checked separately for edits during a running process.
    """
    module = ast.parse(source)
    names = {"FEES", "SLIPPAGES", "_json", "_encoded", "_hash", "_utc", "_bar", "_outcome",
             "_universe_identity", "_without_refresh_times"}
    methods = {"minimum_sessions", "_review_date", "_load", "observe", "_classification",
               "_materialize_days", "_adjudicate_timing", "_evaluate", "_comparison"}

    def selected(node, wanted):
        return (isinstance(node, ast.FunctionDef) and node.name in wanted
                or isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id in wanted for target in node.targets))

    nodes = [node for node in module.body if selected(node, names)]
    observer = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "ForwardObserver")
    nodes.extend(node for node in observer.body if selected(node, methods))
    semantic = hashlib.sha256(ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False).encode()).hexdigest()
    if semantic == "80926382d970b278e2525e355df68304c07a9fc9e4e47a6a4620b33aeeb1db39":
        return "17b750f3f7c099e75e0fc139c2c371543a349d0118e5bb386c318f0c6f0eeeee"
    return hashlib.sha256(source).hexdigest()


def _json(value):
    if isinstance(value, dict):
        return {key.isoformat() if isinstance(key, date) else str(key): _json(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _encoded(value):
    return json.dumps(_json(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _without_refresh_times(value):
    if isinstance(value, dict):
        return {key: _without_refresh_times(item) for key, item in value.items()
                if key not in {"checked_at", "master_observed_at", "status_observed_at", "observed_at"}}
    if isinstance(value, list):
        return [_without_refresh_times(item) for item in value]
    return value


def _read(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Observation file exceeds size limit")
    envelope = json.loads(raw)
    if set(envelope) != {"sha256", "payload"} or envelope["sha256"] != _hash(envelope["payload"]):
        raise ValueError("Observation checksum mismatch")
    return envelope["payload"]


def _append(path, payload):
    """Publish once; a different existing record is an error, never an overwrite."""
    path = Path(path)
    payload = _json(payload)
    if path.exists():
        if _read(path) != payload:
            raise ValueError("Immutable observation differs")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _encoded({"sha256": _hash(payload), "payload": payload})
    if len(raw) > MAX_BYTES:
        raise ValueError("Observation exceeds size limit")
    if path.suffix == ".gz":
        raw = gzip.compress(raw, mtime=0)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # Atomic and fails if destination already exists.
        except FileExistsError:
            if _read(path) != payload:
                raise ValueError("Concurrent immutable observation differs") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _universe_identity(universe):
    return _hash({key: universe.get(key) for key in ("as_of", "source_url", "rows")})


def _utc(now):
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _bar(row):
    if not isinstance(row, dict):
        return None
    try:
        values = {key: Decimal(row[key]) for key in ("open", "high", "low", "close", "volume")}
        if (any(not number.is_finite() for number in values.values()) or values["volume"] < 0
                or min(values[key] for key in ("open", "high", "low", "close")) <= 0
                or not values["low"] <= min(values["open"], values["close"])
                <= max(values["open"], values["close"]) <= values["high"]):
            return None
        return values
    except (KeyError, TypeError, ArithmeticError, ValueError):
        return None


def _outcome(status, reason=None):
    return {"status": status, "reason": reason, "entry_date": None, "entry_observed_at": None,
            "exit_date": None, "exit_observed_at": None, "holding_days": 0,
            "entry_raw": None, "exit_raw": None,
            "returns": [{"slippage": float(slip), "net_return": None} for slip in SLIPPAGES]}


class ForwardObserver:
    """Freeze inputs before collection and retain original signals and later outcomes."""
    minimum_sessions = 63

    def __init__(self, directory=ROOT / ".local" / "forward-observations", *, now=None,
                 evaluator=None, dependencies=None, universe_loader=load_universe):
        from backend.research_rules import RULE_ID, evaluate_pullback
        self.directory = Path(directory)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.evaluate = evaluator or evaluate_pullback
        self.rule_id = RULE_ID
        self.dependencies = dependencies
        self.runtime_dependencies = None
        self.universe_loader = universe_loader
        self.lock = threading.RLock()
        self.version = None
        self.path = None
        self.error = None
        self.next_refresh_at = None
        self.next_attempt = None
        self.next_tick = None
        self.collecting = False
        self.runtime_source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self._load_signature = None
        self._load_cache = None
        self._committed_files = set()
        self._completed_requests = set()
        self._completed_inputs = set()
        self._activate_safely()

    def _fingerprint(self):
        if self.dependencies is not None:
            return deepcopy(self.dependencies)
        paths = [ROOT / "backend/research_rules.py", ROOT / "backend/forward_observer.py",
                 ROOT / "backend/candidates.py", ROOT / "backend/candidate_selection.py",
                 ROOT / "backend/candidate_features.py", ROOT / "backend/candidate_history.py",
                 ROOT / "backend/eligibility.py", ROOT / "backend/kis.py", ROOT / "backend/universe.py",
                 ROOT / "backend/chart.py",
                 ROOT / "config/candidate-selection.json",
                 ROOT / "research/pullback-recovery/plan.json"]
        result = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        observer_path = ROOT / "backend" / "forward_observer.py"
        result[str(observer_path.relative_to(ROOT))] = _observer_code_hash(observer_path.read_bytes())
        universe = self.universe_loader()
        if universe.get("status") != "verified":
            raise ValueError("Verified universe required")
        result["universe"] = _universe_identity(universe)
        return result

    def _activate_safely(self):
        try:
            if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != self.runtime_source_hash:
                self.fail("관찰 코드·기준이 변경되었습니다. 새 버전을 사용하려면 서버를 다시 시작하세요.")
                return False
            dependencies = self._fingerprint()
            if self.runtime_dependencies is None:
                self.runtime_dependencies = deepcopy(dependencies)
            elif self.runtime_dependencies != dependencies:
                self.fail("관찰 코드·기준이 변경되었습니다. 새 버전을 사용하려면 서버를 다시 시작하세요.")
                return False
            version_id = _hash({"rule_id": self.rule_id, "dependencies": dependencies})
            path = self.directory / version_id
            with file_lock(self.directory / "observer.lock", blocking=False):
                if path.exists():
                    version = _read(path / "freeze.json")
                    if version["version_id"] != version_id or version["dependencies"] != dependencies:
                        raise ValueError("Frozen version mismatch")
                else:
                    path.mkdir(parents=True)
                    version = {"version_id": version_id, "rule_id": self.rule_id,
                               "frozen_at": _utc(self.now()), "dependencies": dependencies,
                               "research_status": "unadopted", "order_enabled": False,
                               "review_date": self._review_date()}
                    _append(path / "freeze.json", version)
            self.version, self.path = version, path
            return True
        except Exception:
            self.fail("관찰 기준·저장 파일을 확인하지 못했습니다. 다음 수집 때 다시 확인합니다.")
            return False

    def _review_date(self):
        if self.dependencies is not None:
            return "2026-11-20"
        plan = json.loads((ROOT / "research/pullback-recovery/plan.json").read_text(encoding="utf-8"))
        return plan["forward"]["review_date_kst"]

    def fail(self, message):
        with self.lock:
            self.error = message
            self.collecting = False

    def _inventory(self):
        paths = [self.path / "freeze.json"]
        for directory in ("inputs", "data", "records", "outcomes", "days", "timing", "receipts"):
            paths.extend(path for path in (self.path / directory).glob("*.json*") if path.is_file())
        return tuple((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                     for path in sorted(paths) for stat in (path.stat(),))

    def _load(self, *, mutable=False):
        if self.path is None:
            raise ValueError("No frozen observation version")
        signature = self._inventory()
        if signature == self._load_signature and self._load_cache is not None:
            return deepcopy(self._load_cache) if mutable else self._load_cache
        # A failed verification must never leave a formerly-valid result cached.
        self._load_signature = self._load_cache = None
        payloads, hashes = {}, {}

        def read_once(path):
            key = str(path)
            if key not in payloads:
                payloads[key] = _read(path)
                hashes[key] = _hash(payloads[key])
            return payloads[key]

        def verified_hash(path):
            read_once(path)
            return hashes[str(path)]

        read_once(self.path / "freeze.json")
        snapshots = [read_once(path) for path in sorted((self.path / "inputs").glob("*.json"))]
        for snapshot in snapshots:
            digest = snapshot.get("market_data_sha256")
            if digest is not None:
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(char not in "0123456789abcdef" for char in digest)):
                    raise ValueError("Invalid market data reference")
                path = self.path / "data" / f"{digest}.json.gz"
                market = read_once(path)
                if verified_hash(path) != digest or set(market) != set(MARKET_FIELDS):
                    raise ValueError("Observation market data mismatch")
                snapshot["data"] = {**snapshot["data"], **market}
        records = [read_once(path) for path in sorted((self.path / "records").glob("*.json"))]
        events = [read_once(path) for path in sorted((self.path / "outcomes").glob("*.json"))]
        days = [read_once(path) for path in sorted((self.path / "days").glob("*.json"))]
        timing = [read_once(path) for path in sorted((self.path / "timing").glob("*.json"))]
        receipts = [read_once(path) for path in sorted((self.path / "receipts").glob("*.json"))]
        inputs = {item["input_sha256"]: item for item in snapshots}
        record_map = {item["record_id"]: item for item in records}
        for day in days:
            if day["input_sha256"] not in inputs:
                raise ValueError("Sealed day input is missing")
        for receipt in receipts:
            if receipt["input_sha256"] not in inputs:
                raise ValueError("Collection receipt input is missing")
            day = next((day for day in days if day["as_of"] == receipt["as_of"]), None)
            if day is None or set(day["symbols"]) != {row["symbol"] for row in records
                                                      if row["signal_date"] == receipt["as_of"]}:
                raise ValueError("Completed collection records are missing")
            for relative, expected in receipt.get("files", {}).items():
                path = self.path / relative
                if (len(Path(relative).parts) != 2 or Path(relative).parts[0] not in {"records", "days", "timing", "outcomes"}
                        or not path.resolve().is_relative_to(self.path.resolve())
                        or verified_hash(path) != expected):
                    raise ValueError("Completed observation dependency changed or disappeared")
        for record in records:
            if record["input_sha256"] not in inputs:
                raise ValueError("Signal input is missing")
        for event in events:
            if event["record_id"] not in record_map or event["input_sha256"] not in inputs:
                raise ValueError("Outcome source is missing")
        latest = {}
        for event in sorted(events, key=lambda item: (item.get("sequence", 0), item["observed_at"])):
            latest[event["record_id"]] = event["outcome"]
        for event in timing:
            if event["record_id"] not in record_map or event["input_sha256"] not in inputs:
                raise ValueError("Timing source is missing")
            record = record_map[event["record_id"]]
            record["original_classification"] = record["classification"]
            record["classification"] = event["classification"]
            record["timing_observed_at"] = event["observed_at"]
        for day in days:
            found = {row["symbol"] for row in records if row["signal_date"] == day["as_of"]}
            for row in records:
                if row["signal_date"] == day["as_of"]:
                    row["day_complete"] = found == set(day["symbols"])
        self._committed_files = {relative for receipt in receipts for relative in receipt.get("files", {})}
        self._completed_requests = {receipt["requested_through"] for receipt in receipts}
        self._completed_inputs = {(receipt["input_sha256"], receipt["requested_through"]) for receipt in receipts}
        result = snapshots, records, inputs, latest
        if self._inventory() != signature:
            raise ValueError("Observation files changed during verification")
        self._load_signature, self._load_cache = signature, result
        return deepcopy(result) if mutable else result

    def _classification(self, as_of, generated_at):
        close = datetime.combine(date.fromisoformat(as_of), time(16), KST)
        frozen = datetime.fromisoformat(self.version["frozen_at"])
        generated = datetime.fromisoformat(generated_at)
        if close <= frozen:
            return "bootstrap"
        if close <= generated < datetime.combine(close.date() + timedelta(days=1), time(9), KST):
            return "prospective"
        return "timing_unverified"

    def observe(self, *, universe, result, histories, calendars, indices, errors, sources=None, known_ineligible=None):
        """Called inside candidate run.lock after a completed collection, including partial runs."""
        with self.lock:
            if not self._activate_safely():
                return
            with file_lock(self.directory / "observer.lock", blocking=False):
                snapshots, records, inputs, latest = self._load(mutable=True)
                expected = self.version["dependencies"].get("universe")
                if expected is not None and expected != _universe_identity(universe):
                    raise ValueError("Universe changed during collection")
                if result.get("status") != "complete":
                    raise ValueError("Collection is unfinished")
                normalized = _json({"as_of": result["as_of"], "requested_through": result["requested_through"],
                                    "universe": universe, "histories": histories, "calendars": calendars,
                                    "indices": indices, "errors": errors,
                                    "known_ineligible": known_ineligible or {},
                                    "rows": result["rows"], "screening": result.get("screening"),
                                    "sources": sources or {}})
                # Refresh timestamps are provenance, not fresh price evidence or new signal identifiers.
                hash_input = deepcopy(normalized)
                hash_input.pop("sources")
                input_hash = _hash(_without_refresh_times(hash_input))
                path = self.path / "inputs" / f"{result['as_of']}-{input_hash}.json"
                if path.exists():
                    snapshot = inputs[input_hash]
                else:
                    known = known_ineligible or {}
                    missing = [row["symbol"] for row in result["rows"]
                               if row.get("status") == "error" or row["symbol"] in errors
                               or row.get("selection", {}).get("status") == "unverified" and row["symbol"] not in known]
                    screening = result.get("screening", {})
                    ready = (not missing and bool(calendars) and screening.get("status") == "ready"
                             and all(len(days) >= self.minimum_sessions for days in calendars.values()))
                    snapshot = {"input_sha256": input_hash, "generated_at": _utc(self.now()),
                                "received_at": result["updated_at"], "quality": "ready" if ready else "partial",
                                "missing_symbols": missing, "data": normalized}
                    market = {key: normalized[key] for key in MARKET_FIELDS}
                    market_hash = _hash(market)
                    _append(self.path / "data" / f"{market_hash}.json.gz", market)
                    _append(path, {**snapshot, "market_data_sha256": market_hash,
                                   "data": {key: value for key, value in normalized.items() if key not in MARKET_FIELDS}})
                    snapshots.append(snapshot)
                    inputs[input_hash] = snapshot
                if snapshot["quality"] == "ready":
                    day_path = self.path / "days" / f"{result['as_of']}.json"
                    if day_path.exists():
                        day = _read(day_path)
                    else:
                        day = {"as_of": result["as_of"], "input_sha256": input_hash,
                               "sealed_at": _utc(self.now()),
                               "symbols": [row["symbol"] for row in snapshot["data"]["rows"]
                                           if row.get("selection", {}).get("status") == "selected"]}
                        _append(day_path, day)
                self._materialize_days(inputs, records)
                for record in records:
                    self._adjudicate_timing(record, snapshot)
                    if (record["classification"] != "prospective"
                            or not (record["signal"] or record["control"])):
                        continue
                    old = latest.get(record["record_id"])
                    if old and old.get("reason") == "price_revision":
                        continue
                    outcome = self._evaluate(record, inputs[record["input_sha256"]], snapshot, old)
                    if old == outcome:
                        continue
                    event = {"record_id": record["record_id"], "input_sha256": input_hash,
                             "observed_at": _utc(self.now()), "outcome": outcome,
                             "sequence": len(list((self.path / "outcomes").glob("*.json"))) + 1}
                    _append(self.path / "outcomes" / f"{record['record_id']}-{_hash(event)}.json", event)
                if snapshot["quality"] == "ready":
                    files = {str(path.relative_to(self.path)): _hash(_read(path))
                             for directory in ("records", "days", "timing", "outcomes")
                             for path in (self.path / directory).glob("*.json")
                             if str(path.relative_to(self.path)) not in self._committed_files}
                    receipt = self.path / "receipts" / f"{snapshot['data']['requested_through']}-{input_hash}-{_hash(files)}.json"
                    if files or (input_hash, snapshot["data"]["requested_through"]) not in self._completed_inputs:
                        _append(receipt, {"input_sha256": input_hash, "as_of": snapshot["data"]["as_of"],
                                          "requested_through": snapshot["data"]["requested_through"],
                                          "completed_at": _utc(self.now()), "files": files})
                self.error = None
                self.collecting = False

    def _materialize_days(self, inputs, records):
        for day_path in sorted((self.path / "days").glob("*.json")):
            day = _read(day_path)
            sealed = inputs[day["input_sha256"]]
            for row in sealed["data"]["rows"]:
                if row["symbol"] not in day["symbols"]:
                    continue
                symbol, board = row["symbol"], row["board"]
                record_id = _hash([self.version["version_id"], day["as_of"], symbol])
                record_path = self.path / "records" / f"{record_id}.json"
                if record_path.exists():
                    continue
                bars = {date.fromisoformat(key): {field: Decimal(value) for field, value in values.items()}
                        for key, values in sealed["data"]["histories"].get(symbol, {}).items()}
                calendar = [date.fromisoformat(key) for key in sealed["data"]["calendars"].get(board, [])]
                judgment = self.evaluate(bars, calendar)
                generated_at = _utc(self.now())
                record = {"record_id": record_id, "input_sha256": day["input_sha256"],
                          "symbol": symbol, "name": row["name"], "board": board,
                          "signal_date": day["as_of"], "generated_at": generated_at,
                          "received_at": sealed["received_at"],
                          "classification": self._classification(day["as_of"], generated_at),
                          "eligible": judgment["eligible"], "signal": judgment["signal"],
                          "trend": judgment["trend"], "reason": judgment["reason"],
                          "control": bool(judgment["eligible"] and judgment["trend"])}
                _append(record_path, record)
                records.append(record)

    def _adjudicate_timing(self, record, snapshot):
        if record["classification"] != "timing_unverified":
            return
        calendar = snapshot["data"]["calendars"].get(record["board"], [])
        if record["signal_date"] not in calendar:
            return
        following = [day for day in calendar if record["signal_date"] < day <= snapshot["data"]["as_of"]]
        if not following:
            return
        next_open = datetime.combine(date.fromisoformat(following[0]), time(9), KST)
        classification = "prospective" if datetime.fromisoformat(record["generated_at"]) < next_open else "late"
        event = {"record_id": record["record_id"], "input_sha256": snapshot["input_sha256"],
                 "classification": classification, "next_session": following[0], "observed_at": _utc(self.now())}
        _append(self.path / "timing" / f"{record['record_id']}.json", event)
        record["original_classification"] = record["classification"]
        record["classification"] = classification

    def _evaluate(self, record, original, current, old):
        as_of, signal = current["data"]["as_of"], record["signal_date"]
        if as_of < signal:
            return old or _outcome("pending_entry")
        symbol, board = record["symbol"], record["board"]
        before = original["data"]["histories"].get(symbol, {})
        after = current["data"]["histories"].get(symbol, {})
        basis = {**before, **(old or {}).get("price_basis", {})}
        for day in basis.keys() & after.keys():
            if any(Decimal(basis[day][key]) != Decimal(after[day][key]) for key in ("open", "high", "low", "close")):
                return _outcome("unknown", "price_revision")
        if old and old["status"] == "closed":
            return old
        calendar = current["data"]["calendars"].get(board, [])
        if signal not in calendar:
            return _outcome("unknown", "calendar_gap")
        future = [day for day in calendar if signal < day <= as_of]
        if not future:
            return _outcome("pending_entry")
        observed_at = current["data"].get("sources", {}).get("stock-" + symbol, {}).get("observed_at") or current["received_at"]
        result = _outcome("open")
        result["price_basis"] = {day: after[day] for day in future if day in after}
        result["entry_date"] = future[0]
        entry = _bar(after.get(future[0]))
        if entry is None:
            result.update(status="unknown", reason="missing_entry_bar")
            return result
        result["entry_observed_at"] = (old or {}).get("entry_observed_at") or observed_at
        if entry["volume"] == 0 or entry["high"] == entry["low"]:
            result.update(status="fill_unverifiable", reason="entry_fill_unverifiable")
            return result
        result["entry_raw"] = format(entry["open"], "f")
        result["holding_days"] = min(len(future), 5)
        for day in future[:5]:
            if _bar(after.get(day)) is None:
                result.update(status="unknown", reason="missing_holding_bar")
                return result
        if len(future) < 6:
            return result
        delayed = []
        sale = None
        for offset, day in enumerate(future[5:], start=5):
            sale = _bar(after.get(day))
            if sale is None:
                result.update(status="unknown", reason="missing_exit_bar", exit_delay_dates=delayed)
                return result
            if sale["volume"] == 0 or sale["high"] == sale["low"]:
                delayed.append(day)
                sale = None
                continue
            result.update(exit_date=day, exit_observed_at=observed_at, holding_days=offset)
            break
        result["exit_delay_dates"] = delayed
        if sale is None:
            result.update(status="fill_unverifiable", reason="exit_fill_unverifiable")
            return result
        result.update(status="closed", reason="time5_delayed" if delayed else "time5",
                      exit_raw=format(sale["open"], "f"), hypothetical=True)
        result["price_basis"] = {day: values for day, values in result["price_basis"].items()
                                 if day <= result["exit_date"]}
        result["returns"] = [{"slippage": float(slip),
                              "net_return": float(sale["open"] * (1 - slip)
                                  * (1 - FEES["sell_fee"] - FEES["sell_tax"])
                                  / (entry["open"] * (1 + slip) * (1 + FEES["buy_fee"])) - 1)}
                             for slip in SLIPPAGES]
        return result

    @staticmethod
    def _comparison(records, outcomes):
        groups = defaultdict(list)
        for record in records:
            if record["signal"] or record["control"]:
                groups[(record["signal_date"], record["board"])].append(record)
        by_date = defaultdict(list)
        signal_count = control_count = unresolved = paired_groups = 0
        for (day, _), rows in groups.items():
            signals = [row for row in rows if row["signal"] and row["classification"] == "prospective"]
            controls = [row for row in rows if row["control"]]
            if not signals:
                continue
            if (not controls or any(row["classification"] != "prospective" or not row.get("day_complete", True)
                                    or outcomes.get(row["record_id"], {}).get("status") != "closed" for row in rows)):
                unresolved += 1
                continue
            values = lambda selected: [outcomes[row["record_id"]]["returns"][0]["net_return"] for row in selected]
            by_date[day].append((len(signals), sum(values(signals)) / len(signals),
                                 sum(values(controls)) / len(controls)))
            signal_count += len(signals)
            control_count += len(controls)
            paired_groups += 1
        signal_means, control_means = [], []
        for groups_on_day in by_date.values():
            count = sum(item[0] for item in groups_on_day)
            signal_means.append(sum(item[0] * item[1] for item in groups_on_day) / count)
            control_means.append(sum(item[0] * item[2] for item in groups_on_day) / count)
        mean = lambda values: sum(values) / len(values) if values else None
        return {"pending": not paired_groups or bool(unresolved), "descriptive_only": True,
                "signal_count": signal_count, "control_count": control_count,
                "paired_days": len(by_date), "paired_groups": paired_groups,
                "unresolved_groups": unresolved, "signal_mean": mean(signal_means),
                "control_mean": mean(control_means),
                "edge": mean([a - b for a, b in zip(signal_means, control_means)]),
                "slippage": .001, "weighting": "signal_count_by_market_then_equal_date", "account_return": None}

    def snapshot(self):
        with self.lock:
            empty = {"status": "error" if self.error else "idle", "rule_id": self.rule_id,
                     "research_status": "unadopted", "version_id": None, "frozen_at": None,
                     "as_of": None, "observed_at": None, "error": self.error,
                     "order_enabled": False, "allocation": None, "next_refresh_at": self.next_refresh_at,
                     "counts": dict.fromkeys(("signals", "prospective", "bootstrap", "late", "timing_unverified", "closed", "open", "unknown"), 0),
                     "observations": [], "comparison": self._comparison([], {}),
                     "review_date": self.version.get("review_date") if self.version else None,
                     "descriptive_only": True}
            if self.version is not None:
                empty.update(version_id=self.version["version_id"], frozen_at=self.version["frozen_at"])
            try:
                snapshots, records, _, outcomes = self._load()
                latest = max(snapshots, key=lambda item: item["generated_at"]) if snapshots else None
                signals = [row for row in records if row["signal"]]
                counts = empty["counts"]
                counts["signals"] = len(signals)
                for classification in ("prospective", "bootstrap", "late", "timing_unverified"):
                    counts[classification] = sum(row["classification"] == classification for row in signals)
                for record in signals:
                    if record["classification"] != "prospective":
                        continue
                    status = outcomes.get(record["record_id"], {}).get("status", "pending_entry")
                    counts["closed" if status == "closed" else "unknown" if status in ("unknown", "fill_unverifiable") else "open"] += 1
                observations = []
                for record in sorted(signals, key=lambda row: (row["signal_date"], row["symbol"]), reverse=True)[:200]:
                    outcome = (outcomes.get(record["record_id"], _outcome("pending_entry"))
                               if record["signal"] and record["classification"] == "prospective"
                               else _outcome("excluded", record["classification"] if record["signal"] else "no_signal"))
                    observations.append({**record, "outcome": outcome})
                empty.update(status="error" if self.error else "collecting" if self.collecting else latest["quality"] if latest else "idle",
                             as_of=latest["data"]["as_of"] if latest else None,
                             observed_at=latest["generated_at"] if latest else None,
                             observations=observations, comparison=self._comparison(records, outcomes))
                if latest and latest["quality"] == "partial" and not empty["error"]:
                    empty["error"] = f"{len(latest['missing_symbols'])}종목 또는 거래일 달력 확인 필요. 새 신호 판정을 보류했습니다."
                empty["missing_count"] = len(latest["missing_symbols"]) if latest else 0
                empty["judgment_count"] = len(records)
                empty["no_signal_count"] = sum(not row["signal"] for row in records)
                return deepcopy(empty)
            except Exception:
                empty.update(status="error", error="관찰 기록·입력 파일을 확인하지 못했습니다. 원본 복구 후 다시 확인하세요.")
                return empty

    def tick(self, candidate_service):
        """A short server-loop callback; network work stays in CandidateService's worker."""
        now = self.now()
        if self.next_tick is not None and now < self.next_tick:
            return
        self.next_tick = now + timedelta(seconds=60)
        with self.lock:
            if not self._activate_safely():
                return
            try:
                snapshots, _, _, _ = self._load()
                local = now.astimezone(KST)
                target = local.date() if local.time() >= time(16) else local.date() - timedelta(days=1)
                completed = self._completed_requests
                if target.isoformat() in completed:
                    day = local.date() + timedelta(days=1) if local.time() >= time(16) else local.date()
                    self.next_refresh_at = _utc(datetime.combine(day, time(16), KST))
                    return
                if self.next_attempt is not None and now < self.next_attempt:
                    return
                state = candidate_service.snapshot()
                if state.get("status") == "running":
                    self.collecting = True
                    return
                self.next_attempt = now + timedelta(minutes=30)
                self.next_refresh_at = _utc(self.next_attempt)
                result = candidate_service.start()
                self.collecting = result.get("status") == "running"
                if result.get("status") == "error":
                    self.error = result.get("error") or "관찰용 일봉 수집을 시작하지 못했습니다."
            except Exception:
                self.fail("관찰 자료 수집·저장 상태를 확인하지 못했습니다. 30분 후 다시 확인합니다.")
                self.next_attempt = now + timedelta(minutes=30)
                self.next_refresh_at = _utc(self.next_attempt)
