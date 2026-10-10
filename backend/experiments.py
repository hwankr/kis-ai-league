"""Local experiment runner: analysis, paper execution and durable reconciliation."""
from datetime import date, datetime, time as daytime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import threading
import time
import tomllib
from uuid import uuid4

from backend.candidate_history import HistoryCache
from backend.eligibility import EligibilityService, assess_master
from backend.experiment_store import ExperimentStore, encode
from backend.history import series_for_profile
from backend.kis import KST, ROOT, KisError, load_profiles
from backend.request_gate import file_lock
from backend.universe import load_universe

ACTIVE = {"submitting", "unknown", "submitted", "partial", "cancel_pending"}
FEE = Decimal("0.000140527")
TAX = Decimal("0.002")
POLICY_KEYS = {"account_id", "budget", "order_cap", "daily_buy_limit", "execution_strategy"}
ALL_STRATEGIES = "all-strategies-v1"


class OperationalBlock(KisError):
    def __init__(self, message, code="record_mismatch", question=None):
        super().__init__(message)
        self.code = code
        self.question = question or "증권사 주문 내역과 잔고에서 표시된 불일치 원인을 확인해 주세요."


class SymbolUnavailable(KisError):
    """A verified local trading restriction; other symbols may continue."""


def decimal(value, *, positive=False):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or positive and result <= 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise KisError("금액·수량이 올바르지 않습니다.") from None


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise KisError("기록에 시간대가 없습니다.")
    return result


def public_policy(policy):
    return {key: policy.get(key) for key in POLICY_KEYS}


class ExperimentObserver:
    """Fan out the validated collection without altering the frozen observer."""
    def __init__(self, research, experiments):
        self.research, self.experiments = research, experiments
        self.minimum_sessions = max(63, research.minimum_sessions if research else 63)

    def observe(self, **payload):
        if self.research:
            self.research.observe(**payload)
        self.experiments.collection_ready(payload["result"])

    def fail(self, message):
        if self.research:
            self.research.fail(message)


class ExperimentService:
    def __init__(self, config_path=ROOT / "config.local.toml", *, directory=None,
                 candidate_service=None, now=None, broker_factory=None,
                 input_reader=None, analyzer=None, llm_factory=None, master_reader=None):
        self.config_path = Path(config_path)
        self.directory = Path(directory or self.config_path.parent / ".local" / "experiments")
        self.store = ExperimentStore(self.directory / "experiments.sqlite3")
        self.now = now or (lambda: datetime.now(KST))
        self.candidate_service = candidate_service
        self.broker_factory = broker_factory
        self.input_reader = input_reader or self._read_input
        self.analyzer, self.llm_factory = analyzer, llm_factory
        self.master_reader = master_reader or EligibilityService().master_snapshot
        self.lock = threading.RLock()
        self.worker = None
        self.worker_started_at = None
        self.next_tick = 0
        self.error = None
        self.updated_at = None
        self._llm = None
        self._shadow_sources = None
        self.stopping = False
        try:
            settings = tomllib.loads(self.config_path.read_text(encoding="utf-8")).get("experiments", {})
            self.autonomous = settings.get("autonomous") is True
        except (OSError, ValueError, AttributeError):
            self.autonomous = False
            settings = {}
        from backend.learning import LearningService
        self.learning = LearningService(self.store,
            enabled=settings.get("learning", {}).get("enabled", self.autonomous) is True,
            now=self.now, llm_factory=self._get_llm, development_reader=self._development_inputs)
        # Never resubmit a mutation interrupted between durable intent and response.
        try:
            with file_lock(self.directory / "runner.lock", blocking=False):
                for order in self.store.orders():
                    if order["status"] == "submitting":
                        order.update(status="unknown", error="서버 중단으로 주문 접수 여부 확인 필요")
                        self.store.save_order(order)
                # Enabled is durable user intent, not process liveness. Every
                # cycle reconciles before it may send a new order.
                if not self.store.setting("autonomous_initialized", False) and self.autonomous:
                    prior_pause = self.store.setting("pause_reason") or ""
                    if "사용자" in prior_pause:
                        self.store.save_setting("user_paused", True)
                    if not self.store.setting("user_paused", False):
                        self.store.save_setting("enabled", True)
                        self.store.save_setting("pause_reason", None)
                    self.store.save_setting("autonomous_initialized", True)
                for order in self.store.orders():
                    if order["status"] in {"unknown", "submitting", "cancel_pending"}:
                        self._order_issue(order)
                self._health(recovering=True)
        except BlockingIOError:
            raise KisError("다른 서버에서 실험 주문 작업이 진행 중입니다.") from None

    def _timestamp(self):
        return self.now().astimezone(timezone.utc).isoformat(timespec="microseconds")

    def close(self):
        self.stopping = True
        learning_closed = self.learning.close()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(20)
        return learning_closed and (self.worker is None or not self.worker.is_alive())

    def _event(self, kind, message):
        self.store.event(kind, message, self._timestamp())

    def _pause(self, reason):
        self.store.save_setting("enabled", False)
        self.store.save_setting("pause_reason", reason)
        self.store.save_setting("user_paused", True)

    def _health(self, **changes):
        value = self.store.setting("autonomy_status", {})
        value.update(changes)
        self.store.save_setting("autonomy_status", value)

    def _issue(self, code, message, *, key=None, question=None, blocking=True):
        key = key or code
        items = self.store.setting("issues", [])
        previous = next((item for item in items if item["id"] == key), None)
        value = {"id": key, "code": code, "message": message,
                 "first_seen": previous["first_seen"] if previous and previous["state"] == "open" else self._timestamp(),
                 "last_seen": self._timestamp(), "state": "open", "question": question,
                 "blocking": blocking}
        self.store.save_setting("issues", [item for item in items if item["id"] != key] + [value])

    def _resolve_issue(self, key):
        items = self.store.setting("issues", [])
        changed = False
        for item in items:
            if item["id"] == key and item["state"] == "open":
                item.update(state="resolved", last_seen=self._timestamp())
                changed = True
        if changed:
            self.store.save_setting("issues", items)

    def _order_issue(self, order):
        question = (f"{order['symbol']} {order['order_date']} 주문의 증권사 주문번호·지점번호를 확인해 주세요."
                    if not order.get("order_id") else None)
        requested = order.get("cancel_requested_at")
        if requested and (self.now() - stamp(requested)).total_seconds() >= 600:
            question = f"{order['symbol']} 주문의 취소가 확인되지 않습니다. 증권사 주문·취소 내역을 확인해 주세요."
        self._issue("order_unknown", "주문 접수·취소 결과를 대조하고 있습니다.", key="order:" + order["id"],
                    question=question)

    def _blocked(self):
        return any(item["state"] == "open" and item["blocking"] for item in self.store.setting("issues", []))

    def _retry_due(self, setting="autonomy_status"):
        value = self.store.setting(setting, {}).get("next_retry_at")
        return not value or self.now() >= stamp(value)

    def _failure(self, error, operation):
        self.error = str(error) if isinstance(error, KisError) else "실험 조회·처리를 완료하지 못했습니다. 자동 재시도합니다."
        health = self.store.setting("autonomy_status", {})
        failures = health.get("consecutive_failures", 0) + 1
        delay = min(900, 30 * 2 ** min(failures - 1, 5))
        self._health(last_error_at=self._timestamp(), consecutive_failures=failures,
                     next_retry_at=(self.now() + timedelta(seconds=delay)).isoformat(), last_error=self.error)
        if isinstance(error, OperationalBlock):
            self._issue(error.code, self.error, question=error.question)
        else:
            previous = next((item for item in self.store.setting("issues", [])
                             if item["id"] == "temporary_failure" and item["state"] == "open"), None)
            question = ("이 PC의 네트워크와 KIS 모의 API 연결 상태를 확인해 주세요."
                        if previous and (self.now() - stamp(previous["first_seen"])).total_seconds() >= 1800 else None)
            self._issue("temporary_failure", self.error, blocking=False, question=question)
        self._event("error", self.error)

    def _initialize_policy(self):
        policy = self._policy()
        if policy.get("account_id") or not self.autonomous:
            return policy
        from backend.paper_broker import PaperBroker
        try:
            profile = load_profiles(self.config_path).select()
            profile.settings.validate_credentials()
            profile.settings.validate_account()
        except KisError as error:
            raise OperationalBlock(str(error), "account_configuration",
                                   "기본 모의계좌의 앱 키·시크릿·계좌번호를 설정해 주세요.") from None
        broker = (self.broker_factory or PaperBroker)(profile)
        account = broker.snapshot()
        budget = decimal(account["total_value"], positive=True).to_integral_value(rounding="ROUND_FLOOR")
        daily = (budget / 6).to_integral_value(rounding="ROUND_FLOOR")
        if daily < 1:
            raise OperationalBlock("모의계좌 실험 예산을 계산할 수 없습니다.", "account_configuration",
                                   "모의계좌의 초기 자산·잔고를 확인해 주세요.")
        policy = {"account_id": profile.id, "budget": format(budget, "f"), "daily_buy_limit": format(daily, "f"),
                  "order_cap": format((daily / 4).to_integral_value(rounding="ROUND_FLOOR"), "f"), "execution_strategy": ALL_STRATEGIES,
                  "fingerprint": series_for_profile(profile).fingerprint,
                  "allocation_mode": "equal_strategies_signal_count", "initialized_at": self._timestamp()}
        self.store.save_setting("policy", policy)
        self._resolve_issue("account_configuration")
        self._event("configure", "초기 모의 평가액으로 4전략 균등 실험 예산 설정")
        return policy

    def _specs(self):
        from backend.experiment_analysis import strategy_specs
        from backend.learning_policy import policy_id
        specs = strategy_specs()
        policy = self.learning.champion()
        for item in policy["strategies"]:
            specs.append({"id": item["id"], "label": item["name"],
                          "description": f"채택 전략 · {item['holding_sessions']}거래일 · 배분 {item['weight'] * 100:g}%",
                          "version": policy_id(policy), "selectable": False})
        return specs

    def _adopted_policy(self):
        if self.learning.enabled and self._policy().get("execution_strategy") == ALL_STRATEGIES:
            return self.learning.champion()
        return None

    def _development_inputs(self, data):
        from backend.learning_history import development_frames
        return development_frames(self.config_path.parent, data)

    def _llm_meta(self):
        from backend.experiment_analysis import llm_metadata
        value = llm_metadata(self.config_path)
        latest = self.store.runs(1, details=False)
        state = latest[0].get("llm_status") if latest else None
        if isinstance(state, dict) and state.get("status") == "error":
            value["error"] = state.get("error")
        return value

    def _get_llm(self):
        from backend.experiment_analysis import make_llm
        return (self.llm_factory or make_llm)(self.config_path)

    def _policy(self):
        return self.store.setting("policy", {key: None for key in POLICY_KEYS})

    def _profile(self, policy):
        if not policy.get("account_id"):
            raise KisError("실험 계좌를 설정하세요.")
        try:
            profile = load_profiles(self.config_path).select(policy["account_id"])
            profile.settings.validate_credentials()
            profile.settings.validate_account()
        except KisError as error:
            raise OperationalBlock(str(error), "account_configuration",
                                   "설정한 모의계좌의 앱 키·시크릿·계좌번호를 확인해 주세요.") from None
        if policy.get("fingerprint") != series_for_profile(profile).fingerprint:
            raise OperationalBlock("계좌 연결이 변경되었습니다. 원래 계좌 연결을 복구하거나 실험 설정을 다시 저장하세요.",
                                   "account_configuration", "계좌 키·계좌번호를 의도적으로 변경했는지 확인해 주세요.")
        self._resolve_issue("account_configuration")
        return profile

    def _broker(self, policy):
        from backend.paper_broker import PaperBroker
        return (self.broker_factory or PaperBroker)(self._profile(policy))

    def _owned_orders(self, policy=None):
        policy = policy or self._policy()
        return [order for order in self.store.orders() if order.get("fingerprint") == policy.get("fingerprint")]

    def _portfolio(self, orders):
        """Derive positions from cumulative broker fills, never from submitted qty."""
        positions, metrics = {}, {}
        for order in orders:
            strategy = order["strategy_id"]
            metric = metrics.setdefault(strategy, {"strategy_id": strategy, "closed_trades": 0,
                                                   "realized_pnl": Decimal(0), "open_positions": 0})
            quantity = order["filled_quantity"]
            if not quantity:
                continue
            amount = decimal(order["filled_amount"], positive=True)
            key = (strategy, order["symbol"])
            position = positions.setdefault(key, {"strategy_id": strategy, "symbol": order["symbol"],
                "name": order["name"], "quantity": 0, "cost": Decimal(0),
                "entry_date": order["order_date"], "entry_id": order["id"]})
            if order["side"] == "buy":
                if position["quantity"] == 0:
                    position.update(entry_date=order["order_date"], entry_id=order["id"],
                                    policy_id=order.get("policy_id"), exit_policy=order.get("exit_policy"),
                                    learning_engine_version=order.get("learning_engine_version"),
                                    cancel_after_minutes=order.get("cancel_after_minutes", 10))
                position["quantity"] += quantity
                position["cost"] += amount * (1 + FEE)
            else:
                if quantity > position["quantity"]:
                    raise OperationalBlock("전략 보유수량보다 매도 체결이 많습니다. 체결 기록 확인 필요")
                cost = position["cost"] * quantity / position["quantity"]
                position["quantity"] -= quantity
                position["cost"] -= cost
                metric["realized_pnl"] += amount * (1 - FEE - TAX) - cost
                if position["quantity"] == 0:
                    metric["closed_trades"] += 1
        result = []
        for position in positions.values():
            if position["quantity"]:
                metrics[position["strategy_id"]]["open_positions"] += 1
                result.append({**position, "average_price": str(position["cost"] / position["quantity"]),
                               "cost": str(position["cost"])})
        return result, [{**item, "realized_pnl": str(item["realized_pnl"])} for item in metrics.values()]

    def snapshot(self):
        with self.lock:
            policy = self._policy()
            orders = self._owned_orders(policy)
            positions, metrics = self._portfolio(orders)
            actual = {item["strategy_id"]: item for item in metrics}
            shadow = {item["strategy_id"]: item for item in self.store.setting("shadow_metrics", [])}
            metrics = [{"strategy_id": spec["id"], "closed_trades": 0, "realized_pnl": "0", "open_positions": 0,
                        "shadow_closed": 0, "shadow_open": 0, "shadow_unknown": 0, "shadow_excluded": 0,
                        "shadow_pending": 0, "shadow_version_count": 0,
                        "shadow_net_pct": None, "shadow_stress_pct": None,
                        **actual.get(spec["id"], {}), **shadow.get(spec["id"], {})} for spec in self._specs()]
            enabled = self.store.setting("enabled", False)
            busy = self.worker is not None and self.worker.is_alive()
            return {"status": "running" if busy else "error" if self.error else "idle",
                    "busy": busy, "environment": "paper", "updated_at": self.updated_at,
                    "error": self.error, "llm": self._llm_meta(), "policy": public_policy(policy),
                    "automation": {"enabled": enabled, "state": "running" if enabled else "paused",
                                   "pause_reason": self.store.setting("pause_reason")},
                    "autonomy": {**self.store.setting("autonomy_status", {}), "configured": self.autonomous,
                                 "user_paused": self.store.setting("user_paused", False),
                                 "blocked": self._blocked(), "issues": self.store.setting("issues", [])},
                    "strategies": self._specs(), "runs": self.store.runs(30, details=False),
                    "orders": list(reversed(orders[-200:])), "positions": positions,
                    "metrics": metrics, "events": self.store.events(), "learning": self.learning.snapshot()}

    def mirror_snapshot(self, *, after=0, limit=100):
        """Read verified fill observations without fetching data or enabling orders."""
        try:
            return self.store.mirror_snapshot(after=after, limit=limit)
        except ValueError:
            raise KisError("연동 체결 조회 조건 또는 원장 상태를 확인하세요.") from None

    def command(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            raise KisError("실험 요청이 올바르지 않습니다.")
        action = body["action"]
        allowed = {"configure": {"action", "policy"}, "cancel": {"action", "order_id"},
                   "resolve": {"action", "id", "broker_order_id", "branch_id"}}
        if action not in {"analyze", "start", "pause", "reconcile", *allowed} or set(body) != allowed.get(action, {"action"}):
            raise KisError("실험 요청 항목이 올바르지 않습니다.")
        if action == "pause":
            self._pause("사용자가 주문을 중지했습니다.")
            self._event("pause", "새 주문 중지")
            return self.snapshot()
        if action == "configure":
            with self.lock, file_lock(self.directory / "runner.lock", blocking=False):
                self._configure(body["policy"])
            return self.snapshot()
        if action == "start":
            with self.lock:
                if self.worker is not None and self.worker.is_alive():
                    raise KisError("실험 작업이 진행 중입니다.")
                policy = self._policy()
                self._profile(policy)
                for key in ("budget", "order_cap", "daily_buy_limit"):
                    decimal(policy[key], positive=True)
                if any(order["status"] in {"unknown", "submitting", "cancel_pending"} for order in self._owned_orders(policy)):
                    raise KisError("미확정 주문을 먼저 확인하세요.")
                self.store.save_setting("enabled", True)
                self.store.save_setting("user_paused", False)
                self.store.save_setting("pause_reason", None)
                self._health(next_retry_at=None)
                retry = self.store.setting("llm_retry", {})
                if retry:
                    self.store.save_setting("llm_retry", {**retry, "next_retry_at": None})
                self._event("start", "모의계좌 자동 주문 시작 요청")
                self._launch("cycle")
        else:
            self._launch(action, body)
        return self.snapshot()

    def _configure(self, policy):
        if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
            raise KisError("계좌·전략·실험 예산·주문 한도를 입력하세요.")
        if self.store.setting("enabled", False):
            raise KisError("주문 중지 후 설정을 변경하세요.")
        old = self._policy()
        current = self._owned_orders(old)
        positions, _ = self._portfolio(current)
        if positions or any(item["status"] in ACTIVE for item in current):
            raise KisError("미체결 주문·전략 보유수량을 정리한 뒤 설정을 변경하세요.")
        strategy_ids = {item["id"] for item in self._specs() if item.get("selectable", True)} | {ALL_STRATEGIES}
        if policy["execution_strategy"] not in strategy_ids or not isinstance(policy["account_id"], str):
            raise KisError("등록된 계좌와 전략을 선택하세요.")
        values = {key: decimal(policy[key], positive=True) for key in ("budget", "order_cap", "daily_buy_limit")}
        if any(value != value.to_integral_value() or value > 1_000_000_000 for value in values.values()):
            raise KisError("실험 금액은 1~10억 원의 정수로 입력하세요.")
        if values["order_cap"] > values["daily_buy_limit"] or values["daily_buy_limit"] > values["budget"]:
            raise KisError("건당 한도 ≤ 일일 매수 한도 ≤ 실험 예산으로 입력하세요.")
        profile = load_profiles(self.config_path).select(policy["account_id"])
        profile.settings.validate_credentials()
        profile.settings.validate_account()
        policy = {**policy, **{key: str(value) for key, value in values.items()},
                  "fingerprint": series_for_profile(profile).fingerprint}
        self.store.save_setting("policy", policy)
        self._event("configure", "모의계좌·실행 전략·실험 한도 저장")

    def _launch(self, operation, body=None):
        with self.lock:
            if self.stopping:
                return
            if self.worker is not None and self.worker.is_alive():
                raise KisError("실험 작업이 진행 중입니다.")
            self.worker = threading.Thread(target=self._work, args=(operation, body or {}), daemon=True)
            self.worker_started_at = time.monotonic()
            self.worker.start()

    def _work(self, operation, body):
        try:
            with file_lock(self.directory / "runner.lock", blocking=False):
                if operation == "analyze":
                    self._analyze(retry_llm=True)
                elif operation == "automatic":
                    self._automatic()
                elif operation == "cycle":
                    self._cycle()
                elif operation == "reconcile":
                    self._reconcile(self._policy())
                elif operation == "cancel":
                    self._cancel(body["order_id"])
                elif operation == "resolve":
                    self._resolve(body)
                self.error = None
                self.updated_at = self._timestamp()
                self._health(last_success_at=self.updated_at, consecutive_failures=0,
                             next_retry_at=None, last_error=None, recovering=False)
                self._resolve_issue("temporary_failure")
        except (KisError, ValueError) as error:
            self._failure(error, operation)
        except BlockingIOError:
            self.error = "다른 서버에서 실험 작업이 진행 중입니다."
        except Exception:
            self._failure(KisError("실험 작업을 완료하지 못했습니다. 저장된 기록으로 자동 재시도합니다."), operation)
        finally:
            self.worker_started_at = None

    def collection_ready(self, result):
        # Scheduler checks the durable collection after the collector releases its lock.
        self.next_tick = 0

    def tick(self):
        if self.stopping:
            return
        if time.monotonic() < self.next_tick:
            return
        self.next_tick = time.monotonic() + 30
        if self.worker is not None and self.worker.is_alive():
            return
        if self._retry_due():
            self._launch("automatic")

    def _automatic(self):
        failure = None
        try:
            policy = self._initialize_policy()
            # Reconciliation continues during a user pause, without changing intent.
            if policy.get("account_id"):
                if self.store.setting("enabled", False):
                    self._cycle()
                elif any(item["status"] in ACTIVE for item in self._owned_orders(policy)):
                    self._reconcile(policy)
        except Exception as error:
            failure = error
        try:
            candidate = self.candidate_service.snapshot() if self.candidate_service else None
            if not self.stopping and (candidate is None or candidate["status"] == "complete" and not candidate.get("stale")):
                self._analyze(automatic_retry=True)
        except Exception:
            if failure is None:
                raise
        if failure is not None:
            raise failure

    def _read_input(self):
        candidates = self.config_path.parent / ".local" / "candidates"
        payload = json.loads((candidates / "latest.json").read_text(encoding="utf-8"))
        result = payload["state"]
        if result["status"] != "complete" or not result.get("as_of"):
            raise KisError("후보 전체 조회를 완료하세요.")
        cache = HistoryCache(candidates / "history")
        indices = {board: cache._read(cache._path("index", code), "index", code)
                   for board, code in (("KOSPI", "0001"), ("KOSDAQ", "1001"))}
        if any(not value for value in indices.values()):
            raise KisError("지수 과거 자료가 없습니다. 후보 전체 조회가 필요합니다.")
        as_of = date.fromisoformat(result["as_of"])
        indices = {board: {day: value for day, value in bars.items() if day <= as_of}
                   for board, bars in indices.items()}
        rows = result["rows"]
        # The learned screener sees the full observed universe. Regulatory and
        # current trading restrictions remain outside the tunable strategy.
        if self.learning.enabled:
            master = self.master_reader()
            rows = [{**row, "learning_eligible": row.get("status") == "ok" and decimal(row.get("close") or 0) >= 1000
                     and master.get("status") == "ok" and not master.get("stale")
                     and assess_master(master.get("rows", {}).get(row["symbol"]), row["board"])["status"] == "pass"}
                    for row in rows]
        else:
            rows = [row for row in rows if row.get("selection", {}).get("status") == "selected"]
        histories = {}
        for row in rows:
            bars = cache._read(cache._path("stock", row["symbol"]), "stock", row["symbol"])
            if bars:
                histories[row["symbol"]] = {day: bar for day, bar in bars.items() if day <= as_of}
        return {"as_of": result["as_of"], "observed_at": self._timestamp(), "rows": rows,
                "histories": histories, "calendars": {board: sorted(bars) for board, bars in indices.items()},
                "benchmarks": indices}

    def _analyze(self, *, retry_llm=False, automatic_retry=False):
        from backend.experiment_analysis import analyze_experiments, analysis_version, build_snapshot
        from backend.learning_policy import build_frame, policy_id, signals
        data = self.input_reader()
        # Do not reroll LLM recommendations from the same frozen daily input.
        normalized = build_snapshot(**data)
        digestable = {key: value for key, value in normalized.items() if key != "observed_at"}
        digestable["analysis_version"] = analysis_version(self.config_path)
        base_key = hashlib.sha256(encode(digestable).encode()).hexdigest()
        adopted = self._adopted_policy()
        learned_frame = build_frame(data) if adopted and adopted["kind"] == "rules" else None
        key = (hashlib.sha256(encode([base_key, policy_id(adopted),
                self.learning.version,
                {key: value for key, value in learned_frame.items() if key != "observed_at"}]).encode()).hexdigest()
               if learned_frame else base_key)
        previous = self.store.find_run(key, exact=True)
        if previous is not None:
            state = previous.get("llm_status")
            failed = isinstance(state, dict) and state.get("status") in {"error", "unconfigured"}
            retry = retry_llm or automatic_retry and self._retry_due("llm_retry")
            if not retry or not failed:
                self._refresh_shadow(data)
                self._learn(data, previous)
                return previous
        # An interrupted analysis has no executable result. Keep the attempt
        # separate from immutable completed runs and retry after process recovery.
        self.store.save_setting("analysis_attempt", {"collection_hash": key, "state": "running",
                                                     "started_at": self._timestamp()})
        baseline = self.store.find_run(base_key)
        if baseline and not retry_llm and not (isinstance(baseline.get("llm_status"), dict)
                                               and baseline["llm_status"].get("status") in {"error", "unconfigured"}):
            result = {"decisions": [item for item in baseline["signals"] if not item.get("policy_id")],
                      "input_hash": baseline["input_hash"], "version_id": baseline["version_id"],
                      "llm_status": baseline.get("llm_status"), "input": normalized}
        else:
            result = (self.analyzer or analyze_experiments)(**data, llm=self._get_llm())
        decisions = result["decisions"] + (signals(adopted, learned_frame) if learned_frame else [])
        result["decisions"] = decisions
        run = {"id": uuid4().hex, "as_of": str(data["as_of"]), "created_at": self._timestamp(),
               "status": "ready", "input_hash": result["input_hash"], "version_id": result["version_id"],
               "collection_hash": key, "signals": decisions,
               "error": (result.get("llm_status") or {}).get("error") if isinstance(result.get("llm_status"), dict) else None,
               "llm_status": result.get("llm_status"), "analysis": result,
               "learning_policy": adopted, "learning_engine_version": self.learning.version if adopted else None}
        self.store.add_run(run)
        self.store.save_setting("analysis_attempt", {"collection_hash": key, "state": "complete",
                                                     "completed_at": self._timestamp(), "run_id": run["id"]})
        self._llm_retry_state(run)
        self._refresh_shadow(data)
        self._event("analysis", f"{run['as_of']} 전략 분석 {len(decisions)}건 저장")
        self._learn(data, run)
        return run

    def _learn(self, data, run):
        policy = self._policy()
        if self.learning.enabled and policy.get("execution_strategy") == ALL_STRATEGIES:
            baseline = [item for item in run["signals"] if not item.get("policy_id")]
            self.learning.enqueue(data, baseline, policy, source_id=run["collection_hash"])

    def _llm_retry_state(self, run):
        state = run.get("llm_status")
        if not isinstance(state, dict) or state.get("status") not in {"error", "unconfigured"}:
            self.store.save_setting("llm_retry", {})
            self._resolve_issue("llm_failure")
            return
        previous = self.store.setting("llm_retry", {})
        attempts = previous.get("attempts", 0) + 1 if previous.get("collection_hash") == run["collection_hash"] else 1
        code = state.get("error_code") or state.get("status")
        authentication = code in {"codex_authentication", "llm_authentication", "unconfigured"}
        quota = code in {"codex_usage_limit", "llm_usage_limit"}
        delay = 21600 if authentication else min(21600, 1800 * 2 ** min(attempts - 1, 4)) if quota else min(900, 60 * 2 ** min(attempts - 1, 4))
        self.store.save_setting("llm_retry", {"collection_hash": run["collection_hash"], "attempts": attempts,
            "code": code, "last_error_at": self._timestamp(),
            "next_retry_at": (self.now() + timedelta(seconds=delay)).isoformat()})
        question = "Codex 로그인 상태를 확인해 주세요." if authentication else None
        self._issue("llm_failure", state.get("error") or "LLM 분석을 완료하지 못했습니다.",
                    question=question, blocking=False)

    def _refresh_shadow(self, data):
        from backend.experiment_shadow import input_key, update_shadow
        revision = self.store.run_revision()
        if self._shadow_sources is None or self._shadow_sources[0] != revision:
            summaries = self.store.runs(10000, details=False)
            symbols = {item["symbol"] for run in summaries for item in run["signals"]
                       if item.get("action") == "buy" and not item.get("policy_id")}
            # Old finalized records still need revision checks after their run rolls out of the limit.
            symbols.update(record["symbol"] for record in self.store.setting("shadow", {}).get("records", {}).values())
            self._shadow_sources = revision, symbols
        symbols = self._shadow_sources[1]
        histories = dict(data["histories"])
        cache = HistoryCache(self.config_path.parent / ".local" / "candidates" / "history")
        for symbol in symbols - histories.keys():
            bars = cache._read(cache._path("stock", symbol), "stock", symbol)
            if bars:
                histories[symbol] = {day: bar for day, bar in bars.items() if str(day) <= str(data["as_of"])}
        refresh_key = input_key(revision, histories, data["calendars"], data["as_of"])
        if self.store.setting("shadow_refresh_key") == refresh_key:
            return
        runs = self.store.runs(10000)
        metrics = update_shadow(self.store, runs, histories, data["calendars"], data["as_of"])
        for row in metrics:
            for key in ("shadow_net_pct", "shadow_stress_pct"):
                if row[key] is not None:
                    row[key] = format(Decimal(str(row[key])), "f")
        self.store.save_settings({"shadow_metrics": metrics, "shadow_refresh_key": refresh_key})

    def _reconcile(self, policy, broker=None):
        broker = broker or self._broker(policy)
        orders = self._owned_orders(policy)
        pending = [item for item in orders if item["status"] in ACTIVE]
        if not pending:
            self._resolve_issue("record_mismatch")
            self._refresh_rejection_issues()
            return broker
        start = min(item["order_date"] for item in pending)
        today = self.now().astimezone(KST).date().isoformat()
        remote = broker.orders(start, today)
        lookup = {(item["order_date"], item["order_id"], item["branch_id"]): item for item in remote}
        for order in pending:
            if not order.get("order_id"):
                self._order_issue(order)
                continue
            row = lookup.get((order["order_date"], order["order_id"], order["branch_id"]))
            if row is None:
                self._issue("order_missing", "접수된 주문이 체결 조회에서 확인되지 않습니다.",
                            key="order:" + order["id"], question="증권사 주문 내역에서 해당 주문의 상태를 확인해 주세요.")
                order["error"] = "브로커 주문 조회 미확인"
                self.store.save_order(order)
                continue
            self._apply_remote(order, row)
            if order["status"] in {"unknown", "submitting", "cancel_pending"}:
                self._order_issue(order)
            elif order["status"] in {"submitted", "partial"} and order["order_date"] < today:
                self._issue("order_expiry_unverified", "이전 거래일 주문의 잔량 종료를 아직 확인하지 못했습니다.",
                            key="order:" + order["id"],
                            question=f"{order['order_date']} {order['symbol']} 주문의 증권사 체결·취소·잔량 종료 상태를 확인해 주세요.")
            else:
                self._resolve_issue("order:" + order["id"])
        self._resolve_issue("record_mismatch")
        self._refresh_rejection_issues()
        return broker

    def _apply_remote(self, order, row):
        if (row["symbol"] != order["symbol"] or row["side"] != order["side"]
                or row["quantity"] != order["quantity"]
                or decimal(row["limit_price"]) != decimal(order["limit_price"])
                or not order["filled_quantity"] <= row["filled_quantity"] <= order["quantity"]):
            raise OperationalBlock("주문 원본과 브로커 응답이 다릅니다. 확인 필요")
        status = {"open": "submitted", "partial": "partial", "filled": "filled",
                  "cancelled": "cancelled", "rejected": "rejected"}.get(row["status"])
        if status is None:
            raise OperationalBlock("브로커 주문 상태를 확인할 수 없습니다.")
        if order["status"] == "cancel_pending" and status in {"submitted", "partial"}:
            status = "cancel_pending"
        if row["filled_quantity"]:
            decimal(row["filled_amount"], positive=True)
            decimal(row["average_price"], positive=True)
        if (decimal(row["filled_amount"]) < decimal(order["filled_amount"])
                or row["filled_quantity"] == order["filled_quantity"] and
                (decimal(row["filled_amount"]) != decimal(order["filled_amount"])
                 or row["filled_quantity"] > 0 and decimal(row["average_price"]) != decimal(order["average_price"]))):
            raise OperationalBlock("누적 체결금액·평균가가 이전 체결 기록과 충돌합니다.")
        order.update(status=status, filled_quantity=row["filled_quantity"],
                     average_price=row["average_price"] if row["filled_quantity"] else None,
                     filled_amount=row["filled_amount"], remaining_quantity=row["remaining_quantity"],
                     error=None, reconciled_at=self._timestamp())
        self.store.save_order(order, capture_fill=True)

    def _resolve(self, body):
        policy = self._policy()
        order = next((item for item in self._owned_orders(policy) if item["id"] == body["id"]), None)
        if order is None or order["status"] != "unknown" or order.get("order_id"):
            raise KisError("접수 미확정 주문을 선택하세요.")
        key = (body["broker_order_id"], body["branch_id"])
        if not all(isinstance(value, str) and value.isdigit() and 1 <= len(value) <= 20 for value in key):
            raise KisError("브로커 주문번호·지점번호를 확인하세요.")
        if any(item.get("order_id") == key[0] and item.get("branch_id") == key[1]
               and item["order_date"] == order["order_date"] for item in self._owned_orders(policy)):
            raise KisError("이미 연결된 브로커 주문입니다.")
        rows = self._broker(policy).orders(order["order_date"], order["order_date"])
        row = next((item for item in rows if (item["order_id"], item["branch_id"]) == key), None)
        if row is None:
            raise KisError("해당 날짜의 브로커 주문을 찾을 수 없습니다.")
        order.update(order_id=key[0], branch_id=key[1])
        self._apply_remote(order, row)
        self._resolve_issue("order:" + order["id"])
        self._event("resolve", "미확정 주문과 브로커 원본 연결")

    def _cancel(self, local_id, broker=None):
        policy = self._policy()
        broker = self._reconcile(policy, broker)
        order = next((item for item in self._owned_orders(policy) if item["id"] == local_id), None)
        if order is None or order["status"] not in {"submitted", "partial"} or not order.get("order_id"):
            raise KisError("취소 가능한 시스템 주문이 없습니다.")
        from backend.paper_broker import BrokerRejected
        order.update(status="cancel_pending", error=None, cancel_requested_at=self._timestamp())
        self.store.save_order(order)
        try:
            broker.cancel(order["order_id"], order["branch_id"], order["symbol"], order["remaining_quantity"])
        except BrokerRejected as error:
            order.update(status="partial" if order["filled_quantity"] else "submitted", error=str(error))
            self.store.save_order(order)
            raise
        except Exception:
            self._order_issue(order)
            raise KisError("취소 접수 여부 확인 필요") from None
        self._event("cancel", f"{order['symbol']} 미체결 취소 요청")
        self._reconcile(policy, broker)

    def _market_open(self):
        now = self.now().astimezone(KST)
        return now.weekday() < 5 and daytime(9, 5) <= now.time() < daytime(15, 15)

    def _live_quote(self, broker, symbol):
        quote = broker.quote(symbol)
        if not quote["eligible"]:
            raise SymbolUnavailable("현재 거래 제한 종목입니다.")
        session = broker.market_session(symbol)
        now = self.now().astimezone(KST)
        if (session["session_date"] != now.date().isoformat()
                or not 0 <= (now - stamp(session["last_trade_at"])).total_seconds() <= 180
                or session["volume"] <= 0):
            raise SymbolUnavailable("당일 최근 체결이 확인되지 않습니다.")
        price = decimal(quote["price"], positive=True)
        if price < 1000 or price != price.to_integral_value():
            raise SymbolUnavailable("거래 가능한 원화 가격이 아닙니다.")
        return price

    def _cycle(self):
        policy = self._policy()
        broker = self._reconcile(policy)
        if not self.store.setting("enabled", False) or self.stopping:
            return
        current = self.now().astimezone(KST)
        today = current.date()
        orders = self._owned_orders(policy)
        # Stop entries at 15:15, but maintain accepted orders through the paper
        # market's 16:00 fill window. Never send a prior-day ID to today's cancel API.
        if current.weekday() < 5 and daytime(9) <= current.time() < daytime(16):
            for order in orders:
                if (order["order_date"] == today.isoformat() and order["status"] in {"submitted", "partial"}
                        and (self.now() - stamp(order["created_at"])).total_seconds() >= 60 * order.get("cancel_after_minutes", 10)):
                    self._cancel(order["id"], broker)
        if not self._market_open():
            return
        orders = self._owned_orders(policy)
        for item in orders:
            if item["status"] in {"unknown", "submitting", "cancel_pending"}:
                self._order_issue(item)
        positions, _ = self._portfolio(orders)
        start = min([today - timedelta(days=20), *[date.fromisoformat(item["entry_date"]) for item in positions]])
        days = broker.session_days(start.isoformat(), today.isoformat())
        if today.isoformat() not in days:
            return
        prior_days = [day for day in days if day < today.isoformat()]
        if not prior_days:
            return
        account = broker.snapshot()
        owned = {}
        for position in positions:
            owned[position["symbol"]] = owned.get(position["symbol"], 0) + position["quantity"]
        if any(account["holdings"].get(symbol, {}).get("quantity", 0) < quantity for symbol, quantity in owned.items()):
            raise OperationalBlock("전략 보유수량과 실제 계좌 잔고가 다릅니다. 체결 대조 필요", "balance_mismatch",
                                   "수동 매도·계좌 초기화·기업행사로 전략 보유수량이 변경됐는지 확인해 주세요.")
        self._resolve_issue("balance_mismatch")
        # Process exits before considering new exposure. Only system-owned shares can be sold.
        for position in positions:
            if not self._exit_due(position, days, today):
                continue
            if any(item["symbol"] == position["symbol"] and item["status"] in ACTIVE for item in orders):
                continue
            holding = account["holdings"].get(position["symbol"], {})
            if holding.get("sellable_quantity", 0) < position["quantity"]:
                self._issue("sellable_quantity", "전략 보유수량보다 매도가능수량이 적습니다.",
                            key="sellable:" + position["symbol"],
                            question=f"{position['symbol']}에 수동 미체결 매도 주문이 있는지 확인해 주세요.")
                continue
            self._resolve_issue("sellable:" + position["symbol"])
            price = self._symbol_quote(broker, position["symbol"])
            if price is None:
                continue
            self._submit(policy, broker, position, "sell", position["quantity"], price,
                         f"exit:{position['entry_id']}:{today.isoformat()}", run_id=None)
        # The latest completed analysis must have existed before this session opened.
        runs = [run for run in self.store.runs(100, details=False) if run["as_of"] == prior_days[-1]
                and stamp(run["created_at"]) < datetime.combine(today, daytime(9), KST)]
        if not runs:
            self._issue("analysis_missing", "직전 거래일의 장전 완료 분석이 없어 신규 진입을 기다립니다.", blocking=False)
            return
        self._resolve_issue("analysis_missing")
        if not self.store.setting("enabled", False) or self._blocked():
            return
        run = runs[0]
        adopted = self._adopted_policy()
        if adopted:
            from backend.learning_policy import BASELINE_POLICY, policy_id
            if policy_id(run.get("learning_policy") or BASELINE_POLICY) != policy_id(adopted):
                return  # New policy needs its own completed pre-open signals.
            if adopted["kind"] == "rules" and run.get("learning_engine_version") != self.learning.version:
                return
        execution = run.get("learning_policy") if policy["execution_strategy"] == ALL_STRATEGIES else None
        learned = execution and execution["kind"] == "rules" and self.learning.enabled
        from backend.experiment_analysis import analysis_version
        if run.get("version_id") != analysis_version(self.config_path):
            self._issue("analysis_version", "새 분석 버전의 완료 결과를 기다립니다.", blocking=False)
            return
        self._resolve_issue("analysis_version")
        universe = load_universe()
        if universe.get("status") != "verified":
            raise KisError("대회 종목군 확인 필요")
        allowed = {row["symbol"]: row for row in universe["rows"]}
        master = self.master_reader()
        if master.get("status") != "ok" or master.get("stale"):
            raise KisError("현재 종목 상태 목록 확인 필요")
        decisions, caps = self._entry_plan(policy, run, today)
        for decision in decisions:
            if not self.store.setting("enabled", False) or self.stopping or self._blocked():
                break
            symbol = decision["symbol"]
            if symbol not in allowed or assess_master(master["rows"].get(symbol), allowed[symbol]["board"])["status"] != "pass":
                continue
            orders = self._owned_orders(policy)
            positions, portfolio_results = self._portfolio(orders)
            if any(item["symbol"] == symbol for item in positions) or any(item["symbol"] == symbol and item["status"] in ACTIVE for item in orders):
                continue
            if learned:
                reserved_symbols = {item["symbol"] for item in orders if item["side"] == "buy" and item["status"] in ACTIVE}
                if len({item["symbol"] for item in positions} | reserved_symbols) >= execution["max_positions"]:
                    continue
            account = broker.snapshot()
            if account["holdings"].get(symbol, {}).get("quantity", 0):
                continue  # Do not mix manually-held shares with experimental positions.
            price = self._symbol_quote(broker, symbol)
            if price is None:
                continue
            if learned and price > decimal(decision["reference_close"], positive=True) * (1 + Decimal(str(execution["entry_slippage_bps"])) / 10000):
                continue
            reserved = sum(decimal(item["limit_price"]) * item.get("remaining_quantity", item["quantity"])
                           * (1 + FEE) for item in orders if item["side"] == "buy" and item["status"] in ACTIVE)
            used = sum(decimal(item["cost"]) for item in positions) + reserved
            daily = sum((decimal(item["filled_amount"]) + decimal(item["limit_price"]) *
                         (item.get("remaining_quantity", 0) if item["status"] in ACTIVE else 0)) * (1 + FEE)
                        for item in orders if item["side"] == "buy" and item["order_date"] == today.isoformat())
            buying = broker.buyability(symbol, str(price))
            strategy_used = sum(decimal(item["cost"]) for item in positions if item["strategy_id"] == decision["strategy_id"])
            strategy_reserved = sum(decimal(item["limit_price"]) * item.get("remaining_quantity", item["quantity"]) * (1 + FEE)
                                    for item in orders if item["side"] == "buy" and item["status"] in ACTIVE
                                    and item["strategy_id"] == decision["strategy_id"])
            strategy_daily = sum((decimal(item["filled_amount"]) + decimal(item["limit_price"]) *
                                 (item.get("remaining_quantity", 0) if item["status"] in ACTIVE else 0)) * (1 + FEE)
                                for item in orders if item["side"] == "buy" and item["order_date"] == today.isoformat()
                                and item["strategy_id"] == decision["strategy_id"])
            weight = (Decimal(str(next(item["weight"] for item in execution["strategies"] if item["id"] == decision["strategy_id"])))
                      if learned else Decimal(1) / (4 if policy["execution_strategy"] == ALL_STRATEGIES else 1))
            exposure = Decimal(1) - Decimal(str(execution["cash_reserve"])) if learned else Decimal(1)
            cash_floor = decimal(policy["budget"]) * (1 - exposure)
            owned_cash = (decimal(policy["budget"]) + sum(Decimal(item["realized_pnl"]) for item in portfolio_results) - used
                          if learned else decimal(account["cash"]) - reserved)
            available = min(caps[decision["strategy_id"]], decimal(policy["budget"]) * exposure - used,
                            decimal(policy["daily_buy_limit"]) - daily,
                            decimal(account["cash"]) - reserved - cash_floor, decimal(buying["cash"]),
                            owned_cash - cash_floor,
                            decimal(policy["budget"]) * weight - strategy_used - strategy_reserved,
                            decimal(policy["daily_buy_limit"]) * weight - strategy_daily)
            quantity = min(max(0, int(available / (price * (1 + FEE)))), buying["quantity"])
            if quantity <= 0:
                continue
            self._submit(policy, broker, decision, "buy", quantity, price,
                         f"entry:{policy['fingerprint']}:{today.isoformat()}:{symbol}", run_id=run["id"])

    def _symbol_quote(self, broker, symbol):
        try:
            result = self._live_quote(broker, symbol)
        except SymbolUnavailable as error:
            self._issue("symbol_unavailable", str(error), key="symbol:" + symbol, blocking=False)
            return None
        self._resolve_issue("symbol:" + symbol)
        return result

    def _exit_due(self, position, days, today):
        rule = position.get("exit_policy") or {"holding_sessions": 5}
        if sum(position["entry_date"] <= day < today.isoformat() for day in days) >= rule["holding_sessions"]:
            return True
        if not rule.get("stop_loss_pct") and not rule.get("take_profit_pct"):
            return False
        prior = [day for day in days if day < today.isoformat()]
        if not prior or position["entry_date"] > prior[-1]:
            return False
        # Close-based stops are executed on the following session. Reading the
        # cached bar keeps research and LLM work out of this order path.
        cache = HistoryCache(self.config_path.parent / ".local" / "candidates" / "history")
        bars = cache._read(cache._path("stock", position["symbol"]), "stock", position["symbol"])
        bar = (bars or {}).get(date.fromisoformat(prior[-1]))
        if not bar:
            return False
        entry = decimal(position["average_price"], positive=True) / (1 + FEE)
        change = (decimal(bar["close"], positive=True) / entry - 1) * 100
        return (bool(rule.get("stop_loss_pct")) and change <= -Decimal(str(rule["stop_loss_pct"]))) or (
                bool(rule.get("take_profit_pct")) and change >= Decimal(str(rule["take_profit_pct"])))

    def _entry_plan(self, policy, run, today):
        from backend.experiment_analysis import strategy_specs
        execution = run.get("learning_policy")
        if (self.learning.enabled and policy["execution_strategy"] == ALL_STRATEGIES
                and execution and execution["kind"] == "rules"):
            from backend.learning_policy import policy_id
            weights = {item["id"]: Decimal(str(item["weight"])) for item in execution["strategies"]}
            groups = {key: [dict(item, cancel_after_minutes=execution["cancel_after_minutes"]) for item in run["signals"]
                           if item["strategy_id"] == key and item.get("policy_id") == policy_id(execution)
                           and item["action"] == "buy" and item.get("status", "ready") == "ready"] for key in weights}
            caps = {key: min(decimal(policy["order_cap"]), decimal(policy["daily_buy_limit"]) * weights[key] / max(1, len(items)))
                    for key, items in groups.items()}
            ordered = [dict(item, cancel_after_minutes=execution["cancel_after_minutes"],
                            learning_engine_version=run.get("learning_engine_version")) for item in run["signals"]
                       if item["strategy_id"] in groups and item.get("policy_id") == policy_id(execution)
                       and item["action"] == "buy" and item.get("status", "ready") == "ready"]
            return ordered, caps
        strategies = [item["id"] for item in strategy_specs()] if policy["execution_strategy"] == ALL_STRATEGIES else [policy["execution_strategy"]]
        groups = {key: [item for item in run["signals"] if item["strategy_id"] == key
                       and item["action"] == "buy" and item.get("status", "ready") == "ready"] for key in strategies}
        if policy["execution_strategy"] == ALL_STRATEGIES:
            groups = {key: sorted(items, key=lambda item: item["symbol"]) for key, items in groups.items()}
        caps = {key: (min(decimal(policy["order_cap"]), decimal(policy["daily_buy_limit"]) / len(strategies) / max(1, len(items)))
                      if policy["execution_strategy"] == ALL_STRATEGIES else decimal(policy["order_cap"]))
                for key, items in groups.items()}
        offset = today.toordinal() % len(strategies)
        strategies = strategies[offset:] + strategies[:offset]
        # Round-robin priority rotates daily; durable account/day/symbol intents
        # stop a cancelled order from moving to another strategy on the same day.
        ordered = [groups[key][index] for index in range(max((len(items) for items in groups.values()), default=0))
                   for key in strategies if index < len(groups[key])]
        return ordered, caps

    def _submit(self, policy, broker, decision, side, quantity, price, intent_key, *, run_id):
        if self.stopping or not self.store.setting("enabled", False) or not self._market_open():
            return
        # Recheck credentials immediately before crossing the broker boundary.
        self._profile(policy)
        order = {"id": uuid4().hex, "strategy_id": decision["strategy_id"], "symbol": decision["symbol"],
                 "name": decision["name"], "side": side, "quantity": quantity, "limit_price": str(price),
                 "filled_quantity": 0, "filled_amount": "0", "average_price": None,
                 "remaining_quantity": quantity, "status": "submitting", "order_id": None, "branch_id": None,
                 "order_date": self.now().astimezone(KST).date().isoformat(), "created_at": self._timestamp(),
                 "error": None, "fingerprint": policy["fingerprint"], "run_id": run_id}
        if decision.get("policy_id"):
            order.update(policy_id=decision["policy_id"], exit_policy=decision.get("exit_policy"),
                         cancel_after_minutes=decision.get("cancel_after_minutes", 10),
                         learning_engine_version=decision.get("learning_engine_version"),
                         reference_close=decision.get("reference_close"))
        if not self.store.reserve_order(order, intent_key):
            return
        from backend.paper_broker import BrokerRejected
        try:
            ack = broker.submit(order["symbol"], side, quantity, str(price))
        except BrokerRejected as error:
            order.update(status="rejected", remaining_quantity=0, error=str(error), broker_error_code=error.code)
            self.store.save_order(order)
            self._event("rejected", f"{order['symbol']} 주문 거절")
            self._rejected_issue(order)
        except Exception:
            order.update(status="unknown", error="주문 접수 여부 확인 필요. 재전송하지 않습니다.")
            self.store.save_order(order)
            self._order_issue(order)
            self._event("unknown", f"{order['symbol']} 주문 접수 미확정")
        else:
            order.update(status="submitted", order_id=ack["order_id"], branch_id=ack["branch_id"])
            self.store.save_order(order)
            self._resolve_issue("submission_rejections")
            self._refresh_rejection_issues()
            self._event("submitted", f"{order['symbol']} {'매수' if side == 'buy' else '매도'} {quantity}주 접수")

    def _rejected_issue(self, order):
        code = order.get("broker_error_code")
        detail = f" ({code})" if code else ""
        message = f"{order['symbol']} {'매수' if order['side'] == 'buy' else '매도'} 주문 거절{detail}: {order['error']}"
        authentication = "인증" in order["error"]
        question = ("KIS 모의계좌 앱 키·시크릿과 인증 상태를 확인해 주세요." if authentication else
                    f"{order['symbol']} 보유분의 청산 주문이 거절됐습니다. 증권사 매도가능수량과 주문 제한을 확인해 주세요."
                    if order["side"] == "sell" else None)
        self._issue("order_rejected", message, key="rejected:" + order["id"], question=question, blocking=False)
        today = [item for item in self._owned_orders() if item["order_date"] == order["order_date"]]
        if len({item["symbol"] for item in today}) >= 3 and all(item["status"] == "rejected" for item in today):
            self._issue("submission_rejections", "당일 서로 다른 3종목 이상의 주문이 모두 거절됐습니다.", blocking=False,
                        question="KIS 모의계좌 주문 연결·주문가능 상태를 확인해 주세요. 거절 주문의 오류 코드가 함께 기록돼 있습니다.")

    def _refresh_rejection_issues(self):
        orders = self._owned_orders()
        indexed = {order["id"]: order for order in orders}
        accepted = [order for order in orders if order.get("order_id") and order["status"] != "rejected"]
        positions, _ = self._portfolio(orders)
        for issue in self.store.setting("issues", []):
            if issue["state"] != "open" or issue["code"] != "order_rejected":
                continue
            rejected = indexed.get(issue["id"].removeprefix("rejected:"))
            if rejected is None:
                continue
            later = [order for order in accepted if stamp(order["created_at"]) > stamp(rejected["created_at"])]
            resolved = ("인증" in rejected["error"] and bool(later)
                        or rejected["side"] == "buy" and any(order["order_date"] > rejected["order_date"] for order in later)
                        or rejected["side"] == "sell" and not any(position["symbol"] == rejected["symbol"]
                            and position["strategy_id"] == rejected["strategy_id"] for position in positions))
            if resolved:
                self._resolve_issue(issue["id"])
