"""Explicit user-run entry point; no daemon, autostart, or implicit order enable."""
import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from backend.kis import ROOT


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
    from backend.mirror_runtime import MirrorRuntime
    from backend.mirror_source import TimefolioMirrorSource
    from backend.timefolio_browser import TimefolioBrowser
    browser = TimefolioBrowser(directory / "timefolio-browser",
                              executable_path=args.browser_executable, headless=args.headless)
    try:
        with process_lock(directory / "timefolio-mirror.lock"):
            try:
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
                while True:
                    result = runtime.tick()
                    # Print only changes, including terminal/rejected results.
                    current = display_status({**result, "jobs": runtime.snapshot()["jobs"][-10:]})
                    if current != previous:
                        print(current, flush=True)
                        previous = current
                    time.sleep(args.interval)
            finally:
                # Keep the process lock until this browser has released its
                # persistent profile, even after Ctrl+C or startup failure.
                browser.close()
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
