"""Headless mirror entry point; never enables KIS orders."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from backend.kis import ROOT
from backend.supervisor import _read, _write


@contextmanager
def process_lock(path):
    """An OS lock survives stale files and is released automatically on exit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        if not path.stat().st_size:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise ValueError("이미 타임폴리오 연동 프로그램이 실행 중입니다.") from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def local_status(db_path):
    """Read-only: status never creates the DB, opens a browser, or contacts KIS."""
    db_path = Path(db_path)
    if not db_path.exists():
        return {"status": "not_initialized", "jobs": []}
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mirror_runtime'").fetchone()
        if not exists:
            return {"status": "not_initialized", "jobs": []}
        row = db.execute("SELECT payload FROM mirror_runtime WHERE id=1").fetchone()
        binding = json.loads(row[0]) if row else None
        jobs = [json.loads(row[0]) for row in db.execute("SELECT payload FROM mirror_jobs ORDER BY rowid DESC LIMIT 30")]
    return {"binding": binding, "jobs": jobs}


def display_status(value):
    # Keep local account hashes and full proposals out of routine console output.
    jobs = value.get("jobs", [])
    result = {key: value[key] for key in ("status", "reason", "cursor") if key in value}
    if "binding" in value:
        binding = value["binding"] or {}
        result.update(contest=binding.get("contest"), cursor=binding.get("cursor"))
    if jobs:
        result["jobs"] = [{"symbol": j["symbol"], "status": j["status"], "reason": j.get("reason"),
                           "order_id": j.get("receipt", {}).get("order_id"),
                           "filled_quantity": j.get("receipt", {}).get("filled_quantity")}
                          for j in jobs]
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _wait_for_stop(seconds, requested):
    for _ in range(seconds):
        if requested():
            return True
        time.sleep(1)
    return requested()


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS AI 체결 → 타임폴리오 자동 주문 (직접 실행용)")
    parser.add_argument("command", nargs="?", choices=("status", "login", "run"), default="status")
    parser.add_argument("--dashboard", default="http://127.0.0.1:8765")
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.toml")
    parser.add_argument("--interval", type=int, default=30, help="회차 종료 후 대기 초 (10~300)")
    parser.add_argument("--browser-executable", type=Path, help="사용할 Chrome 실행 파일")
    parser.add_argument("--headless", action="store_true", help="창 없이 저장된 로그인 사용")
    args = parser.parse_args(argv)
    if not 10 <= args.interval <= 300:
        parser.error("--interval은 10~300초입니다.")
    directory = args.config.resolve().parent / ".local"
    db_path = directory / "experiments" / "experiments.sqlite3"
    if args.command == "status":
        try:
            print(display_status(local_status(db_path)))
            return 0
        except (OSError, ValueError, sqlite3.DatabaseError):
            print("저장된 연동 상태를 읽지 못했습니다.")
            return 1
    # Lazy import: status and module imports do not require Playwright.
    from backend.experiment_store import ExperimentStore
    from backend.mirror_runtime import MirrorBlocked, MirrorRuntime
    from backend.mirror_source import TimefolioMirrorSource
    from backend.timefolio_browser import TimefolioBrowser
    browser = TimefolioBrowser(directory / "timefolio-browser",
                              executable_path=args.browser_executable, headless=args.headless)
    state = None

    def publish(result):
        nonlocal state
        if state is None:
            return
        status = result.get("status")
        if status not in {"starting", "initialized", "paused", "waiting_market", "source_changed",
                          "working", "idle", "blocked", "stopping", "stopped", "error"}:
            status = "error"
        state = {**state, "status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
        state.pop("reason", None)
        if result.get("reason") is not None:
            reason = result["reason"]
            state["reason"] = reason if isinstance(reason, str) and re.fullmatch(
                r"[a-z][a-z0-9_]{0,120}", reason) else "mirror_error"
        if type(result.get("cursor")) is int and 0 <= result["cursor"] <= 2**63 - 1:
            state["cursor"] = result["cursor"]
        _write(directory / "timefolio-mirror-state.json", state)

    def stop_requested():
        request = _read(directory / "timefolio-mirror-stop.json")
        return bool(state and request.get("action") == "stop"
                    and type(request.get("pid")) is int
                    and all(request.get(key) == state[key] for key in ("instance", "pid", "started_at")))

    try:
        with process_lock(directory / "timefolio-mirror.lock"):
            if args.command == "run":
                instance = os.environ.get("KIS_MIRROR_INSTANCE", "")
                state = {"instance": instance if re.fullmatch(r"[0-9a-fA-F]{32}", instance) else uuid4().hex,
                         "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat(),
                         "control_version": 1}
            terminal = {"status": "stopped"}
            try:
                publish({"status": "starting"})
                if args.command == "run" and not db_path.exists():
                    raise ValueError("먼저 기존 프로젝트 대시보드를 실행하세요.")
                browser.open()
                if not args.headless:
                    print("별도 Chrome 창에서 타임폴리오에 로그인하세요. 기존 Chrome 로그인은 복사하지 않습니다.", flush=True)
                    input("로그인을 마쳤으면 Enter (종료 Ctrl+C): ")
                browser.identity()
                if args.command == "login":
                    print("로그인 확인 완료. 주문 연동은 실행하지 않았습니다.")
                    return 0
                source = TimefolioMirrorSource(args.dashboard, args.config)
                runtime = MirrorRuntime(ExperimentStore(db_path), source, browser)
                # Each snapshot must include receipts learned earlier in this
                # same tick, not only the jobs present when the loop started.
                browser.jobs_reader = lambda: runtime.snapshot()["jobs"]
                print("AI 체결 자동 연동 실행. KIS 주문 중지 상태를 따릅니다. 종료 Ctrl+C.", flush=True)
                previous = None
                while not stop_requested():
                    result = runtime.tick()
                    # Runtime turns adapter errors into blocked jobs. A lost
                    # browser must exit so supervision can reconnect it; the
                    # durable job state still prevents uncertain resubmission.
                    if not browser.is_alive():
                        raise RuntimeError("mirror_browser_disconnected")
                    publish(result)
                    # Print only changes, including terminal/rejected results.
                    current = display_status({**result, "jobs": runtime.snapshot()["jobs"][-10:]})
                    if current != previous:
                        print(current, flush=True)
                        previous = current
                    if _wait_for_stop(args.interval, stop_requested):
                        break
                publish({"status": "stopping"})
                return 0
            except BaseException as error:
                if not isinstance(error, KeyboardInterrupt):
                    terminal = {"status": "error", "reason": "mirror_run_failed"}
                    if isinstance(error, MirrorBlocked) and str(error) == "timefolio_login_required":
                        terminal["reason"] = "timefolio_login_required"
                raise
            finally:
                # Keep the process lock until this browser has released its
                # persistent profile, even after Ctrl+C or startup failure.
                try:
                    browser.close()
                except BaseException:
                    terminal = {"status": "error", "reason": "browser_close_failed"}
                    raise
                finally:
                    publish(terminal)
    except KeyboardInterrupt:
        print("연동 종료. 이미 접수된 주문은 사이트에 남습니다.")
        return 0
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("playwright"):
            print("Playwright 설치 필요: .\\.venv\\Scripts\\python.exe -m pip install -r requirements-mirror.txt")
            return 1
        raise
    except ValueError as error:
        print(str(error))
        return 1
    except Exception as error:
        # Browser errors can contain page contents and personal account details.
        print("연동 중단: " + type(error).__name__ + ". status 명령으로 마지막 주문 상태를 확인하세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
