"""Local dashboard process supervision; never sends brokerage requests or orders."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from backend.kis import ROOT
from backend.request_gate import file_lock


PORT = 8765
STATE_LIMIT = 64 * 1024
STARTUP_GRACE = 90
HEALTH_GRACE = 180
STABLE_RESET = 300
MAX_BACKOFF = 60


class SupervisorError(Exception):
    """Public local supervisor error, without raw commands or credentials."""


def _utc():
    return datetime.now(timezone.utc)


def _read(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(STATE_LIMIT + 1)
        if len(raw) > STATE_LIMIT:
            raise ValueError
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _hidden_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}


def runtime_python():
    """Windows venv launchers spawn a second process; retain the real interpreter's PID."""
    executable = Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()
    if not executable.is_file():
        raise SupervisorError("실제 Python 실행 파일을 찾을 수 없습니다.")
    return str(executable)


def _port_busy():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def health_probe():
    """Read only the bounded local health response; no account or order endpoint."""
    connection = HTTPConnection("127.0.0.1", PORT, timeout=3)
    try:
        connection.request("GET", "/api/health", headers={"X-KIS-Dashboard": "1"})
        response = connection.getresponse()
        if response.status != 200:
            return None
        raw = response.read(STATE_LIMIT + 1)
        if len(raw) > STATE_LIMIT:
            return None
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except (OSError, HTTPException, ValueError, UnicodeError):
        return None
    finally:
        connection.close()


class Supervisor:
    def __init__(self, root=ROOT, *, directory=None, popen=None, probe=None, port_busy=None,
                 now=None, monotonic=None, interval=5):
        self.root = Path(root).resolve()
        self.directory = Path(directory).resolve() if directory else self.root / ".local" / "supervisor"
        self.popen, self.probe, self.port_busy = popen or subprocess.Popen, probe or health_probe, port_busy or _port_busy
        self.now, self.monotonic = now or _utc, monotonic or time.monotonic
        self.interval = interval
        self.instance = uuid4().hex
        self.child = None
        self.logs = []
        self.stop_event = threading.Event()
        self.attempt = 0
        self.next_retry = 0
        self.launched_at = None
        self.last_healthy = None
        self.healthy_since = None
        self.state = {"version": 1, "instance": self.instance, "supervisor_pid": os.getpid(),
                      "child_pid": None, "status": "starting", "started_at": self.now().isoformat(),
                      "last_heartbeat": None, "child_healthy_at": None, "attempt": 0,
                      "next_retry": None, "error": None, "last_exit_code": None}

    def _publish(self, **changes):
        self.state.update(changes, last_heartbeat=self.now().isoformat(), attempt=self.attempt,
                          child_pid=self.child.pid if self.child is not None else None)
        _write(self.directory / "state.json", self.state)

    def _close_logs(self):
        for stream in self.logs:
            stream.close()
        self.logs = []

    def _log(self, name):
        path = self.directory / name
        if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
            path.replace(path.with_suffix(path.suffix + ".previous"))
        stream = path.open("ab", buffering=0)
        self.logs.append(stream)
        return stream

    def _launch(self):
        if self.port_busy():
            self._publish(status="conflict", next_retry=None, error="8765 포트를 다른 서버가 사용 중입니다. 기존 프로세스는 종료하지 않았습니다.")
            return False
        self.directory.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment.update(KIS_SUPERVISOR_INSTANCE=self.instance,
                           KIS_SUPERVISOR_STOP_FILE=str(self.directory / "child-stop.json"))
        try:
            self.child = self.popen([runtime_python(), "-X", "utf8", "-m", "backend.dashboard", "--port", str(PORT)],
                cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                stdout=self._log("dashboard.stdout.log"), stderr=self._log("dashboard.stderr.log"),
                close_fds=True, **_hidden_options())
        except Exception:
            self._close_logs()
            self._retry("대시보드 프로세스를 생성하지 못했습니다.")
            return True
        self.launched_at = self.monotonic()
        self.last_healthy = self.healthy_since = None
        self._publish(status="starting", next_retry=None, error=None, child_healthy_at=None)
        return True

    def _retry(self, reason, *, exit_code=None):
        self.attempt += 1
        delay = min(MAX_BACKOFF, 2 ** min(self.attempt - 1, 16))
        self.next_retry = self.monotonic() + delay
        self._publish(status="backoff", error=reason, last_exit_code=exit_code,
                      next_retry=(self.now() + timedelta(seconds=delay)).isoformat())

    def _stop_child(self):
        child = self.child
        if child is None:
            return
        # The retained Popen handle is the only authority to stop a process. Never open a saved PID.
        try:
            if child.poll() is None:
                _write(self.directory / "child-stop.json", {"instance": self.instance, "child_pid": child.pid})
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
        finally:
            self.child = None
            self._close_logs()

    def _healthy(self, payload):
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return False
        if payload.get("pid") != self.child.pid:
            return False
        if payload.get("instance") != self.instance:
            return False
        try:
            updated = datetime.fromisoformat(payload["updated_at"])
            if updated.utcoffset() is None or not -5 <= (self.now() - updated).total_seconds() <= 60:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def step(self):
        """One bounded monitor iteration; production calls it only under the lifetime owner lock."""
        request = _read(self.directory / "stop.json")
        if self.stop_event.is_set() or request.get("instance") == self.instance:
            self._publish(status="stopping", next_retry=None)
            self._stop_child()
            self._publish(status="stopped", next_retry=None, error=None)
            return False
        if self.child is not None:
            code = self.child.poll()
            if code is not None:
                self.child = None
                self._close_logs()
                self._retry(f"대시보드가 종료됐습니다 (종료 코드 {code}).", exit_code=code)
                return True
            now = self.monotonic()
            if self._healthy(self.probe()):
                if self.healthy_since is None:
                    self.healthy_since = now
                self.last_healthy = now
                if now - self.healthy_since >= STABLE_RESET:
                    self.attempt = 0
                self._publish(status="running", error=None, next_retry=None, child_healthy_at=self.now().isoformat())
            else:
                self.healthy_since = None
                anchor = self.last_healthy if self.last_healthy is not None else self.launched_at
                grace = HEALTH_GRACE if self.last_healthy is not None else STARTUP_GRACE
                if now - anchor >= grace:
                    self._publish(status="restarting", error="대시보드 상태 응답이 제한 시간 안에 확인되지 않았습니다.")
                    self._stop_child()
                    self._retry("대시보드 상태 응답 지연으로 소유 프로세스를 재시작합니다.")
                else:
                    self._publish(error="대시보드 상태 응답 확인 중")
            return True
        if self.monotonic() >= self.next_retry:
            return self._launch()
        self._publish()
        return True

    def run(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            with file_lock(self.directory / "supervisor.lock", blocking=False):
                self._publish()
                try:
                    while self.step():
                        self.stop_event.wait(self.interval)
                except BaseException:
                    self._publish(status="stopping", error="감시자가 중단되어 소유 대시보드를 종료합니다.")
                    self._stop_child()
                    self._publish(status="stopped", next_retry=None)
                    raise
                return 0 if self.state["status"] == "stopped" else 1
        except BlockingIOError:
            raise SupervisorError("이 프로젝트의 감시자가 이미 실행 중입니다.") from None


def request_stop(directory):
    directory = Path(directory)
    state = _read(directory / "state.json")
    instance = state.get("instance")
    if not isinstance(instance, str) or len(instance) != 32 or state.get("status") in ("stopped", "conflict"):
        raise SupervisorError("중지할 감시자 실행 기록이 없습니다.")
    _write(directory / "stop.json", {"instance": instance, "requested_at": _utc().isoformat()})
    return {"status": "stop_requested", "instance": instance}


def startup_file(root=ROOT, *, startup_directory=None):
    root = Path(root).resolve()
    if startup_directory is None:
        if os.name != "nt" or not os.environ.get("APPDATA"):
            raise SupervisorError("사용자 자동 시작 등록은 Windows에서 지원합니다.")
        startup_directory = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    name = "KIS-AI-League-" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12] + ".vbs"
    return Path(startup_directory).resolve() / name


def _startup_content(root):
    root = Path(root).resolve()
    def literal(value):
        return '"' + str(value).replace('"', '""') + '"'
    command = subprocess.list2cmdline([runtime_python(), "-X", "utf8", "-m", "backend.supervisor", "run"])
    marker = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return (f"' KIS-AI-League supervisor managed {marker}\r\n"
            'Set kisShell = CreateObject("WScript.Shell")\r\n'
            f"kisShell.CurrentDirectory = {literal(root)}\r\n"
            f"kisShell.Run {literal(command)}, 0, False\r\n")


def install_startup(root=ROOT, *, startup_directory=None, remove=False):
    path = startup_file(root, startup_directory=startup_directory)
    expected = _startup_content(root)
    if path.exists():
        try:
            existing = path.read_bytes().decode("utf-16")
        except (OSError, UnicodeError):
            raise SupervisorError("동일 이름의 자동 시작 파일이 있어 변경하지 않았습니다.") from None
        if existing != expected:
            raise SupervisorError("기존 자동 시작 파일이 이 실행 환경과 달라 변경하지 않았습니다.")
        if remove:
            path.unlink()
            return {"status": "removed", "path": str(path)}
        return {"status": "installed", "path": str(path)}
    if remove:
        return {"status": "absent", "path": str(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(expected.encode("utf-16"))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise SupervisorError("자동 시작 파일이 이미 생성되어 덮어쓰지 않았습니다.") from None
    return {"status": "installed", "path": str(path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS 대시보드 로컬 감시자")
    parser.add_argument("command", nargs="?", choices=("run", "start", "stop", "status"), default="run")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install-startup", action="store_true")
    group.add_argument("--remove-startup", action="store_true")
    args = parser.parse_args(argv)
    directory = ROOT / ".local" / "supervisor"
    try:
        if args.install_startup or args.remove_startup:
            result = install_startup(remove=args.remove_startup)
        elif args.command == "status":
            result = _read(directory / "state.json") or {"status": "not_started"}
        elif args.command == "stop":
            result = request_stop(directory)
        elif args.command == "start":
            directory.mkdir(parents=True, exist_ok=True)
            with file_lock(directory / "supervisor.lock", blocking=False):
                if _port_busy():
                    raise SupervisorError("8765 포트를 사용 중인 기존 서버가 있습니다.")
            with (directory / "supervisor.log").open("ab") as log:
                process = subprocess.Popen([runtime_python(), "-X", "utf8", "-m", "backend.supervisor", "run"],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **_hidden_options())
            result = {"status": "starting", "supervisor_pid": process.pid}
        else:
            supervisor = Supervisor()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: supervisor.stop_event.set())
            return supervisor.run()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except BlockingIOError:
        print("이 프로젝트의 감시자가 이미 실행 중입니다.", file=sys.stderr)
        return 1
    except (SupervisorError, OSError) as error:
        print(str(error) if isinstance(error, SupervisorError) else "감시자 파일·프로세스 작업에 실패했습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
