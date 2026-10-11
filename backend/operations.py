"""Local mirror process controls; order settings are read, never changed."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import threading
import time

from backend.kis import ROOT
from backend.launcher import Launcher, fresh, lock_busy, order_controls, set_mirror_desired
from backend.request_gate import file_lock
from backend.supervisor import _read, _write


STATUSES = {"stopped", "starting", "initialized", "paused", "waiting_market", "idle",
            "working", "blocked", "source_changed", "error", "stopping", "unverified"}
REASONS = {"timefolio_login_required": "타임폴리오 로그인을 확인하세요.",
           "competition_ended": "타임폴리오 대회가 종료되어 새 주문을 중지했습니다.",
           "mirror_run_failed": "연동 실행 중 오류가 발생했습니다.",
           "browser_close_failed": "브라우저 종료를 확인하지 못했습니다.",
           "unknown_destination_submission": "접수 결과를 확인해야 합니다."}
HEALTHY = {"paused", "waiting_market", "idle", "working"}
RETRY_MIN = 15
RETRY_MAX = 300
STABLE_RESET = 300
TRANSIENT_BLOCKS = {"source_dashboard_unavailable", "adapter_or_storage_error",
                    "source_snapshot_stale_or_invalid", "source_execution_prerequisite_changed"}


def _identity(state):
    instance, pid, started = (state.get(key) for key in ("instance", "pid", "started_at"))
    if (isinstance(instance, str) and re.fullmatch(r"[0-9a-fA-F]{32}", instance)
            and type(pid) is int and 0 < pid < 2**32 and _timestamp(started)):
        return instance, pid, started
    return None


def _timestamp(value):
    try:
        return value if (isinstance(value, str) and len(value) <= 64
                         and datetime.fromisoformat(value).utcoffset() is not None) else None
    except ValueError:
        return None


class OperationsService:
    def __init__(self, root=ROOT, *, now=None, monotonic=None):
        self.root = Path(root).resolve()
        self.local = self.root / ".local"
        self._launcher = Launcher(self.root)
        self._guard = threading.RLock()
        self._observed = None
        self._verified = False
        self._busy = False
        self._error = None
        self._worker = None
        self._timeout = 120
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._next_tick = 0
        self._healthy_since = None
        self._closed = False

    def _control(self):
        value = _read(self.local / "timefolio-mirror-control.json")
        if (type(value.get("version")) is not int or value["version"] != 1
                or type(value.get("desired_running")) is not bool
                or type(value.get("failures")) is not int or not 0 <= value["failures"] <= 1000
                or value.get("retry_at") is not None and not _timestamp(value["retry_at"])):
            return None
        return value

    def _due(self, control):
        return (control.get("retry_at") is None
                or datetime.fromisoformat(control["retry_at"]) <= self._now())

    @staticmethod
    def _needs_attention(state):
        # A login or submission ambiguity is not a crashed process to retry.
        reason = state.get("reason")
        return (state.get("status") == "blocked"
                and (not isinstance(reason, str) or reason not in TRANSIENT_BLOCKS)
                or isinstance(reason, str) and reason in {"timefolio_login_required", "unknown_destination_submission",
                                           "browser_close_failed"})

    def _retry(self, control, *, advance):
        failures = min(1000, control["failures"] + int(advance))
        delay = min(RETRY_MAX, RETRY_MIN * 2 ** min(max(failures - 1, 0), 8))
        value = {"version": 1, "desired_running": True, "failures": failures,
                 "retry_at": (self._now() + timedelta(seconds=delay)).isoformat()}
        _write(self.local / "timefolio-mirror-control.json", value)
        return value

    def tick(self):
        """Bounded dashboard-loop hook. The worker alone may start a missing runner."""
        with self._guard:
            now = self._monotonic()
            if self._closed or self._busy or now < self._next_tick:
                return
            self._next_tick = now + 5
            try:
                control = self._control()
                if not control or not control["desired_running"]:
                    self._healthy_since = None
                    return
                state, locked, verified = self._observe()
                if locked:
                    if verified and state.get("status") in HEALTHY:
                        if self._healthy_since is None:
                            self._healthy_since = now
                        if (control["failures"] and now - self._healthy_since >= STABLE_RESET
                                and fresh(state.get("updated_at"))):
                            with file_lock(self.local / "launcher.lock", blocking=False):
                                current = self._control()
                                if current and current["desired_running"]:
                                    set_mirror_desired(self.root, True)
                    else:
                        self._healthy_since = None
                    # A held runner/profile lock is never grounds to spawn or kill,
                    # including when its heartbeat cannot be verified.
                    return
                self._healthy_since = None
                if (not self._needs_attention(state) and self._due(control)
                        and not self._locked("launcher.lock")):
                    self._queue("mirror_start", None, automatic=True)
            except BlockingIOError:
                pass
            except Exception:
                self._error = "연동 자동 복구 설정을 확인하지 못했습니다."

    def close(self):
        """Stop supervising on dashboard shutdown; leave the mirror and preference intact."""
        with self._guard:
            self._closed = True
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(1)
        return worker is None or not worker.is_alive()

    def _observe(self):
        state = _read(self.local / "timefolio-mirror-state.json")
        locked = self._locked("timefolio-mirror.lock")
        identity = _identity(state)
        stamp = state.get("updated_at")
        valid = (locked and identity and fresh(stamp, 360) and isinstance(state.get("status"), str)
                 and state.get("status") in STATUSES)
        if not valid:
            self._observed, self._verified = None, False
        elif self._observed is None or self._observed[0] != identity:
            self._observed, self._verified = (identity, stamp), False
        else:
            previous = self._observed[1]
            if fresh(stamp, 360, after=previous):
                self._observed, self._verified = (identity, stamp), True
            elif stamp != previous:
                self._observed, self._verified = (identity, stamp), False
        return state, locked, bool(valid and self._verified)

    def _locked(self, name):
        path = self.local / name
        try:
            # GET must not create or initialize lock files.
            return path.stat().st_size == 0 or lock_busy(path)
        except FileNotFoundError:
            return False

    def _stop_pending(self, state):
        request = _read(self.local / "timefolio-mirror-stop.json")
        return bool(_identity(state) and request.get("action") == "stop"
                    and type(request.get("pid")) is int
                    and all(request.get(key) == state.get(key) for key in ("instance", "pid", "started_at")))

    def snapshot(self):
        with self._guard:
            state, locked, verified = self._observe()
            control = self._control()
            busy = self._busy or self._locked("launcher.lock")
            pending = locked and self._stop_pending(state)
            status = state.get("status")
            reason = None
            if not locked:
                status = "error" if status == "error" else "stopped"
            elif not verified:
                status, reason = "unverified", "실행 상태 갱신을 확인하지 못했습니다."
            elif pending:
                status, reason = "stopping", "현재 회차가 끝나면 종료합니다."
            if reason is None and (state.get("reason") or status in {"blocked", "error"}):
                code = state.get("reason")
                reason = REASONS.get(code, "연동 상태를 확인하세요.") if isinstance(code, str) else "연동 상태를 확인하세요."
            if not locked and reason is None:
                if control and control["desired_running"] and not self._due(control):
                    reason = "자동 복구 대기 중입니다."
            controllable = (verified and type(state.get("control_version")) is int
                            and state["control_version"] == 1)
            return {"mirror": {"status": status, "updated_at": _timestamp(state.get("updated_at")),
                               "reason": reason, "can_start": not locked and not busy,
                               "can_stop": bool(not busy and (
                                   not locked and control and control["desired_running"]
                                   or controllable and not pending
                                   and status not in {"stopped", "error", "stopping"}))},
                    "busy": bool(busy), "error": self._error}

    def command(self, body):
        if (type(body) is not dict or set(body) != {"action"} or not isinstance(body["action"], str)
                or body["action"] not in {"mirror_start", "mirror_stop"}):
            raise ValueError("지원하지 않는 실행기 요청입니다.")
        with self._guard:
            if self._closed:
                raise ValueError("실행기 감시가 종료 중입니다.")
            snapshot = self.snapshot()
            action = body["action"]
            allowed = snapshot["mirror"]["can_start" if action == "mirror_start" else "can_stop"]
            if not allowed:
                raise ValueError("현재 실행 상태에서는 이 요청을 처리할 수 없습니다.")
            target = self._observed[0] if action == "mirror_stop" and self._observed else None
            self._queue(action, target)
            return self.snapshot()

    def _queue(self, action, target, *, automatic=False):
        self._busy, self._error = True, None
        self._worker = threading.Thread(target=self._execute, args=(action, target),
                                        kwargs={"automatic": automatic}, daemon=True)
        try:
            self._worker.start()
        except RuntimeError:
            self._busy = False
            self._error = "실행기 요청을 시작하지 못했습니다."

    def _execute(self, action, target, *, automatic=False):
        error = None
        retry = None
        try:
            with file_lock(self.local / "launcher.lock", blocking=False):
                if action == "mirror_start":
                    if automatic:
                        control = self._control()
                        if (self._closed or not control or not control["desired_running"]
                                or not self._due(control) or self._locked("timefolio-mirror.lock")
                                or self._needs_attention(_read(self.local / "timefolio-mirror-state.json"))):
                            return
                    else:
                        set_mirror_desired(self.root, True)
                        control = self._control()
                    retry = self._retry(control, advance=True)
                    before = order_controls(self.root)
                    try:
                        result = self._launcher.ensure_mirror()
                        if result.get("status") == "existing_unverified":
                            error = "연동 실행 상태를 확인하지 못했습니다."
                    finally:
                        self._retry(retry, advance=False)
                        if order_controls(self.root) != before:
                            error = "요청 중 주문 설정이 변경됐습니다. 현재 설정을 확인하세요."
                else:
                    # Persist before requesting shutdown, even if cleanup times out.
                    set_mirror_desired(self.root, False)
                    if target is not None:
                        self._stop(target)
                    elif self._locked("timefolio-mirror.lock"):
                        raise ValueError("새로 시작한 실행기의 상태를 확인하세요.")
        except BlockingIOError:
            error = "다른 실행기 요청이 진행 중입니다."
        except TimeoutError:
            error = error or "종료를 아직 확인하지 못했습니다. 실행 상태를 확인하세요."
        except Exception:
            error = error or "실행기 요청을 완료하지 못했습니다. 실행 상태를 확인하세요."
        finally:
            with self._guard:
                self._error, self._busy = error, False

    def _stop(self, target):
        with self._guard:
            state, locked, verified = self._observe()
            if (not locked or not verified or _identity(state) != target
                    or type(state.get("control_version")) is not int or state["control_version"] != 1
                    or state.get("status") in {"stopped", "error", "stopping"}
                    or self._stop_pending(state)):
                raise ValueError("실행 인스턴스를 확인하지 못했습니다.")
            _write(self.local / "timefolio-mirror-stop.json",
                   {"action": "stop", "instance": target[0], "pid": target[1], "started_at": target[2],
                    "requested_at": datetime.now(timezone.utc).isoformat()})
        deadline = time.monotonic() + self._timeout
        while True:
            state = _read(self.local / "timefolio-mirror-state.json")
            locked = self._locked("timefolio-mirror.lock")
            if _identity(state) != target:
                raise ValueError("실행 인스턴스가 변경됐습니다.")
            if not locked:
                if state.get("status") == "stopped" and fresh(state.get("updated_at")):
                    return
                raise ValueError("정상 종료를 확인하지 못했습니다.")
            if time.monotonic() >= deadline:
                raise TimeoutError
            time.sleep(1)
