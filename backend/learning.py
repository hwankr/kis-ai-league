"""Prospective policy research, independent of the order worker.

One frozen challenger is compared with its incumbent on new daily observations.
Development data never count toward promotion. No broker or order API is used.
"""
from copy import deepcopy
from datetime import datetime, time, timezone
import hashlib
import json
from pathlib import Path
import threading

from backend.experiment_store import encode
from backend.kis import KST
from backend.request_gate import file_lock

MIN_SESSIONS = 40
MIN_CLOSED_TRADES = 20
MAX_SESSIONS = 120
MIN_EXCESS_PP = 1.0
MAX_DRAWDOWN_PCT = 15.0


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class LearningService:
    def __init__(self, store, *, enabled, now, proposer=None, evaluator=None, llm_factory=None, development_reader=None):
        self.store, self.enabled, self.now = store, enabled, now
        self.proposer, self.evaluator, self.llm_factory = proposer, evaluator, llm_factory
        self.development_reader = development_reader
        self.worker = None
        self.stopping = False
        self.lock = threading.Lock()
        self.pending = None
        self.attempted = None
        self.version = hashlib.sha256(Path(__file__).read_bytes() +
                                      Path(__file__).with_name("learning_policy.py").read_bytes()).hexdigest()
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS learning_frames
                    (day TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_policies
                    (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_trials
                    (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    def _timestamp(self):
        return self.now().astimezone(timezone.utc).isoformat(timespec="microseconds")

    def state(self):
        return self.store.setting("learning", {})

    def champion(self):
        from backend.learning_policy import BASELINE_POLICY
        return deepcopy(self.state().get("champion", {}).get("policy", BASELINE_POLICY))

    def frames(self, limit=300):
        with self.store.connect() as db:
            values = db.execute("SELECT payload FROM learning_frames ORDER BY day DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in reversed(values)]

    def history(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT payload FROM learning_trials ORDER BY created_at DESC,id DESC").fetchall()
        history = [json.loads(row[0]) for row in rows]
        return [item if index < 30 else {"candidate_id": item["candidate_id"], "policy": item["policy"], "decision": item["decision"]}
                for index, item in enumerate(history)]

    def _research_history(self):
        from backend.learning_policy import policy_id
        history = self.history()
        fingerprint = (self.store.setting("policy") or {}).get("fingerprint")
        groups = {}
        for order in self.store.orders():
            if not fingerprint or order.get("fingerprint") != fingerprint:
                continue
            identity = order.get("policy_id")
            if not identity:
                from backend.learning_policy import BASELINE_POLICY
                identity = policy_id(BASELINE_POLICY)
            groups.setdefault(identity, []).append(order)
        def execution(identity):
            orders = groups.get(identity, [])
            terminal = [order for order in orders if order["status"] in {"filled", "cancelled", "rejected"}]
            accepted = [order for order in terminal if order["status"] != "rejected"]
            return {"terminal_orders": len(terminal), "unresolved_orders": len(orders) - len(terminal),
                    "rejection_rate": sum(order["status"] == "rejected" for order in terminal) / len(terminal) if terminal else None,
                    "fill_ratio": sum(order["filled_quantity"] / order["quantity"] for order in accepted) / len(accepted) if accepted else None}
        for item in history:
            item["execution"] = execution(item["candidate_id"])
        champion = self.champion()
        return [{"policy": champion, "candidate_id": policy_id(champion), "decision": "execution_feedback",
                 "execution": execution(policy_id(champion))}, *history]

    def snapshot(self):
        from backend.learning_policy import policy_id
        state, policy = self.state(), self.champion()
        trial = state.get("trial")
        return {"enabled": self.enabled,
                "status": "disabled" if not self.enabled else state.get("status", "waiting"),
                "champion": {"id": policy_id(policy), "name": policy["name"],
                             "adopted_at": state.get("champion", {}).get("adopted_at")},
                "challenger": {"id": trial["candidate_id"], "name": trial["policy"]["name"],
                               "started_at": trial["created_at"],
                               "sessions": trial.get("metrics", {}).get("challenger", {}).get("sessions", 0)} if trial else None,
                "last_evaluation": state.get("last_evaluation"), "last_change": state.get("last_change"),
                "error": state.get("error")}

    def enqueue(self, data, baseline_signals, limits, *, source_id):
        if not self.enabled or self.stopping:
            return
        # The input object belongs to an immutable completed analysis. Repeated
        # polling does not create another LLM request, evaluation or database row.
        with self.lock:
            if self.attempted == source_id:
                return
            self.pending = (data, baseline_signals, limits, source_id)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, daemon=True, name="policy-research")
                self.worker.start()

    def close(self):
        self.stopping = True
        if self.worker and self.worker.is_alive():
            self.worker.join(5)
        return self.worker is None or not self.worker.is_alive()

    def _work(self):
        while not self.stopping:
            with self.lock:
                work, self.pending = self.pending, None
                if work is None:
                    self.worker = None
                    return
                data, baseline, limits, self.attempted = work
            try:
                with file_lock(self.store.path.parent / "learning.lock", blocking=False):
                    self.process(data, baseline, limits)
            except BlockingIOError:
                return
            except Exception:
                # Research failure is visible but never changes order permission.
                state = self.state()
                state.update(status="error", error="개선 실험을 완료하지 못했습니다. 다음 완료 자료에서 재시도합니다.")
                self.store.save_setting("learning", state)

    def _save(self, state, trial=None):
        from backend.learning_policy import policy_id
        if self.stopping:
            return
        with self.store.connect() as db:
            db.execute("INSERT INTO settings VALUES ('learning',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                       "WHERE settings.value != excluded.value", (encode(state),))
            for policy in [state.get("champion", {}).get("policy"), (state.get("trial") or {}).get("policy")]:
                if policy:
                    db.execute("INSERT OR IGNORE INTO learning_policies VALUES (?,?)", (policy_id(policy), encode(policy)))
            for trial in (trial if isinstance(trial, list) else [trial]):
                if trial is None:
                    continue
                db.execute("INSERT INTO learning_trials VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload "
                           "WHERE learning_trials.payload != excluded.payload", (trial["id"], trial["created_at"], encode(trial)))

    @staticmethod
    def _cohort_error(frames, data):
        # Catch adjusted-history revisions without rewriting the original evidence.
        histories = {symbol: {str(day): bar for day, bar in bars.items()}
                     for symbol, bars in data.get("histories", {}).items()}
        for frame in frames:
            for row in frame["rows"]:
                bar = histories.get(row["symbol"], {}).get(frame["as_of"])
                if bar and any(float(bar[field]) != float(row[field]) for field in ("open", "high", "low", "close", "volume")):
                    return "평가 기간의 원본 가격이 수정되어 비교를 다시 시작합니다."
        for previous, following in zip(frames, frames[1:]):
            deadline = datetime.combine(datetime.fromisoformat(following["as_of"]).date(), time(9), KST)
            if datetime.fromisoformat(previous["captured_at"]) >= deadline:
                return "장전까지 완료되지 않은 관측이 있어 비교를 다시 시작합니다."
        return None

    @staticmethod
    def judge(incumbent, candidate):
        sessions = min(incumbent.get("sessions", 0), candidate.get("sessions", 0))
        if sessions < 2:
            return "waiting", "새 관측을 수집 중"
        if not incumbent.get("valid") or not candidate.get("valid"):
            return "invalid", "비교 자료 결측 또는 실행 조건 불일치"
        if sessions < MIN_SESSIONS:
            return "waiting", "새 관측을 수집 중"
        if candidate["closed_trades"] < MIN_CLOSED_TRADES:
            return ("keep", "완결 거래 부족") if sessions >= MAX_SESSIONS else ("waiting", "완결 거래를 수집 중")
        improvements = [c - b for b, c in zip(incumbent["half_returns"], candidate["half_returns"])]
        better = (candidate["net_return_pct"] >= incumbent["net_return_pct"] + MIN_EXCESS_PP
                  and candidate["stress_return_pct"] >= incumbent["stress_return_pct"] + MIN_EXCESS_PP
                  and candidate["net_return_pct"] > 0 and candidate["stress_return_pct"] > 0
                  and abs(candidate["max_drawdown_pct"]) <= min(MAX_DRAWDOWN_PCT, abs(incumbent["max_drawdown_pct"]) + 2)
                  and abs(candidate.get("stress_drawdown_pct", candidate["max_drawdown_pct"])) <= MAX_DRAWDOWN_PCT
                  and len(improvements) == 2 and all(value > 0 for value in improvements))
        return ("promote", "비용·높은 비용·기간별 수익 및 낙폭 기준 충족") if better else ("keep", "교체 기준 미달")

    def process(self, data, baseline_signals, limits):
        from backend.learning_policy import BASELINE_POLICY, build_frame, policy_id, portfolio_metrics, validate_policy
        from backend.learning_research import propose_candidate
        if not self.enabled or self.stopping:
            return
        limits = {key: str(limits[key]) for key in ("budget", "order_cap", "daily_buy_limit")}
        frame = build_frame(data, baseline_signals)
        frame["captured_at"] = self._timestamp()
        digest = _hash({key: value for key, value in frame.items() if key not in {"observed_at", "captured_at"}})
        state = self.state()
        with self.store.connect() as db:
            prior = db.execute("SELECT digest FROM learning_frames WHERE day=?", (frame["as_of"],)).fetchone()
            if prior is None:
                db.execute("INSERT INTO learning_frames VALUES (?,?,?)", (frame["as_of"], digest, encode(frame)))
        # A day is frozen once. Repeated analysis, restarts and late LLM success
        # cannot replace decisions after seeing the subsequent market move.
        if prior and prior[0] != digest:
            state.update(status="error", error="동일 거래일 관측이 변경됐습니다. 원본은 보존하고 다음 완료일을 기다립니다.")
            trial = state.pop("trial", None)
            if trial:
                trial.update(decision="invalid", reason=state["error"])
            self._save(state, trial)
            return
        if (state.get("processed_day") == frame["as_of"] and state.get("limits") == limits
                and state.get("engine_version") == self.version):
            return
        frames = self.frames()
        if not frames or frames[-1]["as_of"] != frame["as_of"]:
            return
        state.setdefault("champion", {"policy": deepcopy(BASELINE_POLICY), "adopted_at": None})
        state.update(error=None, limits=limits)
        if state.get("engine_version") not in {None, self.version}:
            # A deployment cannot reinterpret an in-progress comparison as new evidence.
            trial = state.pop("trial", None)
            if trial:
                trial.update(decision="invalid", reason="평가 코드 버전 변경")
                self._save(state, trial)
            if state.get("guard"):
                state["guard"].update(start_day=max(frame["as_of"], self.now().astimezone(KST).date().isoformat()),
                                      engine_version=self.version)
        state["engine_version"] = self.version
        self._check_fallback(state, frames, limits, data)
        trial = state.get("trial")
        if trial and trial["limits"] != limits:
            trial.update(decision="invalid", reason="운용 한도 변경")
            state.pop("trial", None)
            self._save(state, trial)
            trial = None
        if trial:
            cohort = [item for item in frames if item["as_of"] >= trial["start_day"]]
            error = self._cohort_error(cohort, data)
            evaluate = self.evaluator or portfolio_metrics
            a = evaluate(trial["incumbent"], cohort, limits)
            b = evaluate(trial["policy"], cohort, limits)
            decision, reason = ("invalid", error) if error else self.judge(a, b)
            state["last_evaluation"] = {"as_of": frame["as_of"], "sessions": min(a.get("sessions", 0), b.get("sessions", 0)),
                "required_sessions": MIN_SESSIONS, "champion_return_pct": a.get("net_return_pct"),
                "challenger_return_pct": b.get("net_return_pct"), "champion_drawdown_pct": a.get("max_drawdown_pct"),
                "challenger_drawdown_pct": b.get("max_drawdown_pct"), "closed_trades": b.get("closed_trades", 0),
                "decision": decision, "reason": reason}
            trial.update(metrics={"champion": a, "challenger": b}, decision=decision, reason=reason,
                         evaluated_through=frame["as_of"])
            if decision == "promote":
                before = state["champion"]
                state["previous_champion"] = before
                state["champion"] = {"policy": trial["policy"], "adopted_at": self._timestamp()}
                state["last_change"] = {"at": self._timestamp(), "from": policy_id(before["policy"]),
                                        "to": trial["candidate_id"], "reason": reason}
                state["guard"] = {"policy": before["policy"], "limits": limits,
                                  "engine_version": self.version,
                                  "start_day": max(frame["as_of"], self.now().astimezone(KST).date().isoformat())}
            if decision != "waiting":
                state.pop("trial", None)
            self._save(state, trial)
        if not state.get("trial"):
            state.update(status="researching")
            self._save(state)
            llm = self.llm_factory() if self.llm_factory else None
            development = frames
            if self.development_reader and len(frames) < 120:
                try:
                    historical = self.development_reader(data)
                    combined = {item["as_of"]: item for item in historical if item["as_of"] <= frame["as_of"]}
                    combined.update({item["as_of"]: item for item in frames})
                    development = [combined[day] for day in sorted(combined)][-300:]
                except Exception:
                    pass  # Unavailable development data cannot alter a prospective trial.
            proposal = (self.proposer or propose_candidate)(state["champion"]["policy"], development, limits, self._research_history(), llm=llm)
            policy = validate_policy(proposal["policy"])
            identity = policy_id(policy)
            if identity == policy_id(state["champion"]["policy"]):
                raise ValueError("duplicate_challenger")
            created = self._timestamp()
            trial = {"id": _hash([identity, created]), "candidate_id": identity, "created_at": created,
                     "start_day": max(frame["as_of"], self.now().astimezone(KST).date().isoformat()),
                     "policy": policy, "incumbent": deepcopy(state["champion"]["policy"]), "limits": limits,
                     "engine_version": self.version,
                     "decision": "waiting", "method": proposal["method"], "rationale": proposal["rationale"],
                     "development": proposal.get("development")}
            state["trial"] = trial
        state.update(status="evaluating", processed_day=frame["as_of"])
        self._save(state, state.get("trial"))

    def _check_fallback(self, state, frames, limits, data):
        """Compare the previous policy on new observations after an adoption."""
        from backend.learning_policy import policy_id, portfolio_metrics
        guard = state.get("guard")
        if not guard:
            return
        cohort = [frame for frame in frames if frame["as_of"] >= guard["start_day"]][-60:]
        if guard["limits"] != limits or self._cohort_error(cohort, data):
            guard.update(limits=limits, start_day=max(frames[-1]["as_of"], self.now().astimezone(KST).date().isoformat()))
            return
        if len(cohort) < 20:
            return
        evaluate = self.evaluator or portfolio_metrics
        active = evaluate(state["champion"]["policy"], cohort, limits)
        fallback = evaluate(guard["policy"], cohort, limits)
        if not active.get("valid") or not fallback.get("valid"):
            return
        deteriorated = (abs(active["max_drawdown_pct"]) > MAX_DRAWDOWN_PCT
                        or active["net_return_pct"] < fallback["net_return_pct"] - 2
                        and len(active["half_returns"]) == len(fallback["half_returns"]) == 2
                        and all(a < b for a, b in zip(active["half_returns"], fallback["half_returns"])))
        if not (deteriorated and fallback["stress_return_pct"] > active["stress_return_pct"]
                and abs(fallback["max_drawdown_pct"]) < abs(active["max_drawdown_pct"])):
            return
        before = state["champion"]["policy"]
        state["champion"] = {"policy": guard["policy"], "adopted_at": self._timestamp()}
        state["last_change"] = {"at": self._timestamp(), "from": policy_id(before), "to": policy_id(guard["policy"]),
                                "reason": "채택 후 새 관측에서 수익·낙폭 악화로 이전 전략 복귀"}
        state.pop("guard", None)
        trial = state.pop("trial", None)
        if trial:
            trial.update(decision="invalid", reason="비교 기준 전략 복귀")
        rollback = {"id": _hash(["rollback", policy_id(before), self._timestamp()]),
                    "candidate_id": policy_id(before), "created_at": self._timestamp(),
                    "start_day": cohort[0]["as_of"], "evaluated_through": cohort[-1]["as_of"],
                    "policy": before, "incumbent": guard["policy"], "limits": limits,
                    "engine_version": self.version, "method": "post_adoption_comparison",
                    "decision": "rollback", "reason": state["last_change"]["reason"],
                    "metrics": {"champion": fallback, "challenger": active}}
        state["last_change"]["trial_id"] = rollback["id"]
        self._save(state, [trial, rollback])
