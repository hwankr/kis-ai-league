"""One click starts missing local services without changing order permissions."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
import webbrowser

from backend.kis import ROOT
from backend.request_gate import file_lock
from backend.supervisor import _hidden_options, _port_busy, _read, _write, health_probe


class LaunchError(Exception):
    """A short, safe message for the desktop shortcut."""


def lock_busy(path):
    try:
        with file_lock(path, blocking=False):
            return False
    except BlockingIOError:
        return True


def fresh(timestamp, seconds=60, *, after=None):
    try:
        value = datetime.fromisoformat(timestamp)
        return (value.tzinfo is not None
                and -5 <= (datetime.now(timezone.utc) - value).total_seconds() <= seconds
                and (after is None or value > datetime.fromisoformat(after)))
    except (TypeError, ValueError):
        return False


def order_controls(root):
    """Read an already initialized database; never bootstrap or enable orders."""
    path = Path(root) / ".local" / "experiments" / "experiments.sqlite3"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            values = {key: json.loads(value) for key, value in db.execute(
                "SELECT key,value FROM settings WHERE key IN ('enabled','user_paused','autonomous_initialized')")}
        if any(type(values.get(key)) is not bool for key in ("enabled", "user_paused")):
            raise ValueError
        # The dashboard's first-run initializer can enable autonomous orders.
        if values.get("autonomous_initialized") is not True and values["user_paused"] is not True:
            raise ValueError
        return {key: values[key] for key in ("enabled", "user_paused")}
    except (sqlite3.Error, OSError, ValueError):
        raise LaunchError("저장된 주문 설정을 확인하지 못했습니다. 대시보드 설정을 먼저 확인하세요.") from None


def set_mirror_desired(root, running):
    """Persist a process preference under launcher.lock, never an order permission."""
    if type(running) is not bool:
        raise ValueError("실행기 설정은 참 또는 거짓이어야 합니다.")
    _write(Path(root) / ".local" / "timefolio-mirror-control.json",
           {"version": 1, "desired_running": running, "failures": 0, "retry_at": None})


class Launcher:
    def __init__(self, root=ROOT, *, popen=None, probe=None, busy=None,
                 port_busy=None, monotonic=None, sleep=None, timeout=120):
        self.root = Path(root).resolve()
        self.local = self.root / ".local"
        self.popen = popen or subprocess.Popen
        self.probe = probe or health_probe
        self.busy = busy or lock_busy
        self.port_busy = port_busy or _port_busy
        self.monotonic, self.sleep = monotonic or time.monotonic, sleep or time.sleep
        self.timeout = timeout

    def _spawn(self, module, arguments, stdout, stderr=None, *, environment=None):
        python = self.root / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            raise LaunchError("프로젝트의 Python 설치를 찾지 못했습니다.")
        self.local.mkdir(parents=True, exist_ok=True)
        stdout.parent.mkdir(parents=True, exist_ok=True)
        with stdout.open("ab", buffering=0) as output:
            with (stderr or stdout).open("ab", buffering=0) as errors:
                return self.popen([str(python), "-u", "-X", "utf8", "-m", module, *arguments],
                                  cwd=self.root, env=environment or dict(os.environ),
                                  stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                  close_fds=True, **_hidden_options())

    def _dashboard_ready(self):
        state = _read(self.local / "supervisor" / "state.json")
        health = self.probe()
        return bool(state.get("instance") and type(state.get("child_pid")) is int
                    and fresh(state.get("last_heartbeat"))
                    and isinstance(health, dict) and health.get("status") == "ok"
                    and health.get("instance") == state["instance"]
                    and health.get("pid") == state["child_pid"] and fresh(health.get("updated_at"))
                    and self.busy(self.local / "supervisor" / "supervisor.lock"))

    def ensure_dashboard(self):
        lock = self.local / "supervisor" / "supervisor.lock"
        child = None
        if not self.busy(lock):
            if self.port_busy():
                raise LaunchError("8765 포트를 다른 서버가 사용 중입니다. 기존 서버를 확인하세요.")
            child = self._spawn("backend.supervisor", ["run"], self.local / "supervisor" / "supervisor.log")
        deadline = self.monotonic() + self.timeout
        while True:
            if self._dashboard_ready():
                return "started" if child else "reused"
            if child is not None and child.poll() is not None and not self.busy(lock):
                raise LaunchError("대시보드를 시작하지 못했습니다. .local/supervisor/ 로그를 확인하세요.")
            if self.monotonic() >= deadline:
                raise LaunchError("대시보드 준비를 확인하지 못했습니다. .local/supervisor/ 로그를 확인하세요.")
            self.sleep(1)

    def ensure_mirror(self):
        lock = self.local / "timefolio-mirror.lock"
        path = self.local / "timefolio-mirror-state.json"
        if self.busy(lock):
            # A lock can also belong to the login command or an older runner.
            observed = _read(path)
            deadline = self.monotonic() + self.timeout
            while (observed.get("instance") and type(observed.get("pid")) is int
                   and fresh(observed.get("updated_at"), 360)
                   and observed.get("status") not in {"error", "stopped"}):
                state = _read(path)
                if not self.busy(lock) or state.get("instance") != observed["instance"] or state.get("pid") != observed["pid"]:
                    break
                if (fresh(state.get("updated_at"), after=observed.get("updated_at"))
                        and state.get("status") in {"paused", "waiting_market", "idle", "working"}):
                    return {"action": "reused", "status": state["status"]}
                if state.get("status") in {"blocked", "error", "stopped"} or self.monotonic() >= deadline:
                    break
                self.sleep(1)
            return {"action": "reused", "status": "existing_unverified"}
        instance = uuid4().hex
        environment = dict(os.environ, KIS_MIRROR_INSTANCE=instance)
        arguments = ["run", "--headless"]
        chrome = self.local / "timefolio-chrome" / "chrome-win64" / "chrome.exe"
        if chrome.is_file():
            arguments += ["--browser-executable", str(chrome)]
        child = self._spawn("backend.timefolio_mirror", arguments,
                            self.local / "timefolio-mirror.stdout.log",
                            self.local / "timefolio-mirror.stderr.log", environment=environment)
        deadline = self.monotonic() + self.timeout
        while True:
            state = _read(path)
            if state.get("instance") == instance and fresh(state.get("updated_at")):
                status = state.get("status")
                if status in {"error", "stopped", "blocked"}:
                    raise LaunchError("타임폴리오 연결을 확인하지 못했습니다. .local/timefolio-mirror.stdout.log를 확인하세요.")
                if status in {"paused", "waiting_market", "idle", "working"} and self.busy(lock):
                    return {"action": "started", "status": status}
            if child.poll() is not None:
                raise LaunchError("타임폴리오 실행이 종료됐습니다. .local/timefolio-mirror.stderr.log를 확인하세요.")
            if self.monotonic() >= deadline:
                raise LaunchError("타임폴리오 준비를 확인하지 못했습니다. 실행 로그를 확인하세요.")
            self.sleep(1)

    def run(self):
        try:
            with file_lock(self.local / "launcher.lock", blocking=False):
                before = order_controls(self.root)
                dashboard = self.ensure_dashboard()
                self._check_controls(before)
                set_mirror_desired(self.root, True)
                mirror = self.ensure_mirror()
                self._check_controls(before)
                result = {"status": "needs_check" if mirror["status"] == "existing_unverified" else "ready",
                          "dashboard": dashboard, "mirror": mirror, "orders": before}
                if result["status"] == "needs_check":
                    result["message"] = "대시보드는 실행 중입니다. 타임폴리오 프로필이 사용 중이어서 실행기 상태를 확인하지 못했습니다."
                self.record(result)
                return result
        except BlockingIOError:
            return {"status": "already_starting"}

    def _check_controls(self, before):
        if order_controls(self.root) != before:
            raise LaunchError("시작 중 주문 설정이 변경됐습니다. 대시보드에서 현재 설정을 확인하세요.")

    def record(self, result):
        _write(self.local / "launcher-status.json", dict(result, updated_at=datetime.now(timezone.utc).isoformat()))


def notify_error(message):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "AI 투자 운용사 시작", 0x10)


def main(argv=None):
    parser = argparse.ArgumentParser(description="대시보드·연구·타임폴리오를 창 없이 시작")
    parser.add_argument("--no-ui", action="store_true", help="오류 안내 창 없이 결과만 출력")
    parser.add_argument("--open-dashboard", action="store_true", help="준비된 웹 운용실 열기")
    args = parser.parse_args(argv)
    launcher = Launcher()
    try:
        result = launcher.run()
    except (LaunchError, OSError, sqlite3.Error) as error:
        message = str(error) if isinstance(error, LaunchError) else "시작하지 못했습니다. 프로젝트 설치와 실행 로그를 확인하세요."
        result = {"status": "error", "message": message}
        try:
            launcher.record(result)
        except OSError:
            pass
    opened = False
    notice_shown = False
    if args.open_dashboard and not args.no_ui and launcher._dashboard_ready():
        try:
            opened = webbrowser.open("http://127.0.0.1:8765/#/experiments", new=0)
        except (OSError, webbrowser.Error):
            pass
        if not opened and result["status"] != "error":
            notify_error("운용실은 실행 중입니다. 브라우저에서 http://127.0.0.1:8765 를 여세요.")
            notice_shown = True
    if (not args.no_ui and (result["status"] == "error"
            or result["status"] == "needs_check" and not opened and not notice_shown)):
        notify_error(result["message"])
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] in {"error", "needs_check"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
