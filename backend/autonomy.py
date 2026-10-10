"""Browser-independent collection, account observations and actionable questions."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import threading
import time

from backend.history import series_for_profile
from backend.kis import KST, KisError, load_profiles


def amount(value):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise KisError("계좌 평가금액을 확인할 수 없습니다.") from None


class AutonomousMonitor:
    def __init__(self, experiments, accounts, candidates, *, now=None, clock=time.monotonic):
        self.experiments, self.accounts, self.candidates = experiments, accounts, candidates
        self.store = experiments.store
        self.now = now or (lambda: datetime.now(KST))
        self.clock = clock
        self.worker = None
        self.worker_started_at = None
        self.collection_progress = None
        self.collection_progress_at = None
        self.collection_stalled = False
        self.next_tick = 0
        self.closed = False
        self.lock = threading.RLock()
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS autonomous_equity (
                    fingerprint TEXT NOT NULL, bucket TEXT NOT NULL,
                    observed_at TEXT NOT NULL, total_value TEXT NOT NULL, cash TEXT NOT NULL,
                    PRIMARY KEY(fingerprint,bucket));
                CREATE TABLE IF NOT EXISTS autonomous_reports (
                    fingerprint TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(fingerprint,day));
            """)

    def _stamp(self):
        return self.now().astimezone(timezone.utc).isoformat(timespec="microseconds")

    def tick(self):
        if self.closed or self.clock() < self.next_tick:
            return
        self.next_tick = self.clock() + 30
        self.store.save_setting("monitor_heartbeat", self._stamp())
        if self.stalled():
            self._issue("worker_stalled", "자동 계좌 점검 응답이 지연되어 서버 복구를 기다립니다.")
        if self.worker is None or not self.worker.is_alive():
            self._issue("worker_stalled")
            self.worker_started_at = self.clock()
            self.worker = threading.Thread(target=self._work, name="autonomous-monitor", daemon=True)
            self.worker.start()

    def stalled(self):
        return self.collection_stalled or (self.worker is not None and self.worker.is_alive() and self.worker_started_at is not None
                                          and self.clock() - self.worker_started_at > 180)

    def close(self):
        self.closed = True
        if self.worker is not None:
            self.worker.join(20)
        return self.worker is None or not self.worker.is_alive()

    def _issue(self, code, message=None, *, question=None, blocking=False):
        with self.lock:
            items = self.store.setting("monitor_issues", [])
            item = next((entry for entry in items if entry["code"] == code), None)
            if message:
                if item is None or item["state"] == "resolved":
                    item = {"id": f"monitor:{code}:{self._stamp()}", "code": code,
                            "first_seen": self._stamp()}
                    items = [entry for entry in items if entry["code"] != code] + [item]
                item.update(message=message, question=question, blocking=blocking,
                            state="open", last_seen=self._stamp())
                if not question and self.now() - datetime.fromisoformat(item["first_seen"]) >= timedelta(minutes=30):
                    item["question"] = "30분 이상 복구되지 않았습니다. 연결·설정 확인 후 다시 시도할까요?"
            elif item is not None and item["state"] != "resolved":
                item.update(state="resolved", last_seen=self._stamp())
            else:
                return
            self.store.save_setting("monitor_issues", items)

    def _work(self):
        # Independent jobs: collection failures must not suppress account observations.
        for operation, code in ((self._collect, "collection"), (self._account, "account")):
            try:
                operation()
            except Exception:
                self._issue(code, "자료 수집 재시도 중" if code == "collection" else "계좌 조회 재시도 중")
        self.store.save_setting("monitor_completed_at", self._stamp())

    def _collect(self):
        state = self.candidates.snapshot()
        if state.get("status") == "running":
            progress = json.dumps(state.get("progress"), sort_keys=True)
            if self.collection_progress != progress or self.collection_progress_at is None:
                self.collection_progress, self.collection_progress_at = progress, self.clock()
            self.collection_stalled = self.clock() - self.collection_progress_at > 600
            if self.collection_stalled:
                self._issue("collection_stalled", "후보 수집 진행이 10분 동안 멈춰 서버 복구를 기다립니다.")
            else:
                self._issue("collection_stalled")
            return
        self.collection_progress = self.collection_progress_at = None
        self.collection_stalled = False
        self._issue("collection_stalled")
        if state.get("status") == "complete" and not state.get("stale"):
            self._issue("collection")
            return
        if state.get("status") == "error" or state.get("error"):
            self._issue("collection", state.get("error") or "후보 자료 자동 수집 재시도 중")
        next_at = self.store.setting("collection_retry_at")
        if next_at and self.now() < datetime.fromisoformat(next_at):
            return
        self.store.save_setting("collection_retry_at", (self.now() + timedelta(minutes=30)).isoformat())
        result = self.candidates.start()
        if result.get("status") == "error":
            self._issue("collection", result.get("error") or "후보 자료 자동 수집 재시도 중")

    def _account(self):
        now = self.now()
        next_at = self.store.setting("account_retry_at")
        if next_at and now < datetime.fromisoformat(next_at):
            return
        policy = self.store.setting("policy", {})
        if not policy.get("account_id"):
            return  # The execution engine first binds a single verified paper account.
        profile = load_profiles(self.experiments.config_path).select(policy["account_id"])
        fingerprint = series_for_profile(profile).fingerprint
        if fingerprint != policy.get("fingerprint"):
            raise KisError("운영 계좌 연결이 변경되었습니다.")
        self.store.save_setting("account_retry_at", (now + timedelta(minutes=1)).isoformat())
        result = self.accounts.snapshot(policy["account_id"])
        now = self.now()  # The observation is stamped after the broker request finishes.
        if result.get("status") != "ok" or result.get("stale"):
            self._issue("account", result.get("error") or "계좌 조회 재시도 중")
            return
        if (result.get("history") or {}).get("error"):
            self._issue("account_history", "계좌 이력 저장 재시도 중")
        else:
            self._issue("account_history")
        summary = result["summary"]
        total, cash = amount(summary["total_value"]), amount(summary["cash"])
        observed_at = result["updated_at"]
        observed = datetime.fromisoformat(observed_at)
        if observed.utcoffset() is None or not 0 <= (now - observed).total_seconds() <= 120:
            raise KisError("계좌 조회 시각이 오래되었습니다.")
        # First observation per five-minute bucket survives restarts and duplicate polls.
        bucket = observed.astimezone(timezone.utc).replace(
            minute=(observed.minute // 5) * 5, second=0, microsecond=0).isoformat()
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO autonomous_equity VALUES (?,?,?,?,?)",
                       (fingerprint, bucket, observed_at, str(total), str(cash)))
        self.store.save_setting("monitor_account_at", observed_at)
        self._issue("account")
        local = now.astimezone(KST)
        interval = 5 if local.weekday() < 5 and 8 <= local.hour < 17 else 30
        self.store.save_setting("account_retry_at", (now + timedelta(minutes=interval)).isoformat())
        if local.hour >= 17:
            self._report(fingerprint, local.date().isoformat())

    def performance(self, fingerprint):
        empty = {"as_of": None, "baseline": None, "total_value": None, "cash": None,
                 "return_pct": None, "max_drawdown_pct": None, "observations": 0}
        if not fingerprint:
            return empty
        with self.store.connect() as db:
            rows = db.execute("SELECT observed_at,total_value,cash FROM autonomous_equity "
                              "WHERE fingerprint=? ORDER BY observed_at,bucket", (fingerprint,)).fetchall()
        if not rows:
            return empty
        baseline = amount(rows[0][1])
        peak, drawdown = baseline, Decimal(0)
        for row in rows:
            value = amount(row[1])
            peak = max(peak, value)
            if peak:
                drawdown = min(drawdown, (value / peak - 1) * 100)
        latest = rows[-1]
        return {"as_of": latest[0], "baseline": format(baseline, "f"), "total_value": latest[1],
                "cash": latest[2], "return_pct": format((amount(latest[1]) / baseline - 1) * 100, "f") if baseline else None,
                "max_drawdown_pct": format(drawdown, "f"), "observations": len(rows)}

    def issues(self):
        items = self.store.setting("issues", []) + self.store.setting("monitor_issues", [])
        answers = self.store.setting("issue_answers", {})
        # A pause acknowledgement applies only while that user pause remains in effect.
        # Resuming must expose every unresolved cause, even when its first_seen is unchanged.
        user_pause_active = (not self.store.setting("enabled", False)
                             and self.store.setting("user_paused", False))
        return [{**item, "state": "resolved" if user_pause_active
                 and answers.get(item["id"], {}).get("answer") == "keep_paused"
                 and answers.get(item["id"], {}).get("first_seen") == item["first_seen"]
                 else item["state"]} for item in items]

    def _report(self, fingerprint, day):
        performance = self.performance(fingerprint)
        orders = [order for order in self.store.orders()
                  if order.get("fingerprint") == fingerprint and order["order_date"] == day]
        value = {"date": day, "created_at": self._stamp(), "total_value": performance["total_value"],
                 "return_pct": performance["return_pct"], "orders": len(orders),
                 "filled_orders": sum(order["filled_quantity"] > 0 for order in orders),
                 "issues": sum(item["state"] == "open" for item in self.issues())}
        with self.store.connect() as db:
            db.execute("INSERT INTO autonomous_reports VALUES (?,?,?) ON CONFLICT(fingerprint,day) "
                       "DO UPDATE SET payload=excluded.payload", (fingerprint, day, json.dumps(value, ensure_ascii=False)))

    def snapshot(self, *, include_history=True):
        policy = self.store.setting("policy", {})
        fingerprint = policy.get("fingerprint")
        issues = self.issues()
        opened = [item for item in issues if item["state"] == "open"]
        enabled = self.store.setting("enabled", False)
        status = "healthy" if enabled else "paused"
        if not fingerprint:
            status = "starting"
        if opened:
            status = "attention" if any(item.get("blocking") or item.get("question") for item in opened) else "degraded"
        if self.stalled():
            status = "degraded"
        engine = self.store.setting("autonomy_status", {})
        reports = []
        if include_history:
            with self.store.connect() as db:
                reports = [json.loads(row[0]) for row in db.execute(
                    "SELECT payload FROM autonomous_reports WHERE fingerprint=? ORDER BY day DESC LIMIT 30", (fingerprint,))]
        return {"status": status, "last_heartbeat_at": self.store.setting("monitor_heartbeat"),
                "last_account_at": self.store.setting("monitor_account_at"),
                "next_retry_at": engine.get("next_retry_at"),
                "error": opened[0]["message"] if opened else None, "issues": issues,
                "performance": self.performance(fingerprint) if include_history else None, "daily_reports": reports,
                "last_cycle_at": engine.get("last_success_at"),
                "last_monitor_at": self.store.setting("monitor_completed_at")}

    def answer(self, body):
        if (set(body) != {"action", "id", "answer"} or body["answer"] not in {"retry", "keep_paused"}
                or not isinstance(body["id"], str)):
            raise KisError("질문 답변 형식이 올바르지 않습니다.")
        item = next((item for item in self.issues() if item["id"] == body["id"] and item["state"] == "open"), None)
        if item is None:
            raise KisError("이미 해결됐거나 존재하지 않는 질문입니다.")
        if body["answer"] == "keep_paused":
            self.experiments.command({"action": "pause"})
        elif item.get("blocking"):
            # start validates unresolved orders and the account; never overrides those gates.
            self.experiments.command({"action": "start"})
        else:
            self.store.save_setting("collection_retry_at", None)
            self.store.save_setting("account_retry_at", None)
            if item.get("code") == "llm_failure":
                retry = self.store.setting("llm_retry", {})
                self.store.save_setting("llm_retry", {**retry, "next_retry_at": None})
            health = self.store.setting("autonomy_status", {})
            self.store.save_setting("autonomy_status", {**health, "next_retry_at": None})
            self.experiments.next_tick = 0
        with self.lock:
            answers = self.store.setting("issue_answers", {})
            answers[body["id"]] = {"answer": body["answer"], "at": self._stamp(), "first_seen": item["first_seen"]}
            self.store.save_setting("issue_answers", answers)
        self.next_tick = 0
