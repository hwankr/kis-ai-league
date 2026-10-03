"""독립 시세 수집기. python -m backend.collector [--once | --stop | --status]"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from backend.history import series_for_profile
from backend.kis import KisError, ROOT, client_for_profile, load_profiles
from backend.market import MARKET_DATABASE, MarketSettings, load_market_settings
from backend.market_history import MarketHistory
from backend.request_gate import file_lock

HEARTBEAT_SECONDS = 5


def utc_now():
    return datetime.now(timezone.utc)


class Collector:
    def __init__(self, config_path=ROOT / "config.local.toml", database=MARKET_DATABASE,
                 client_factory=client_for_profile, store=None, now=utc_now):
        self.config_path, self.database = Path(config_path), Path(database)
        self.store = store if store is not None else MarketHistory(database)
        self.client_factory, self.now = client_factory, now
        self.lock_path = self.database.with_suffix(".collector.lock")
        self.control_path = self.database.with_suffix(".collector-control.lock")
        self.stop_path = self.database.with_suffix(".stop")
        self.collector_id = uuid4().hex
        self.stop_event = threading.Event()
        self.monitor_done = threading.Event()
        self.state_lock = threading.Lock()
        self.settings = MarketSettings()
        self.next_run_at = None
        self.error = None
        self.client = None
        self.identity = None

    def publish(self, state="running"):
        with self.state_lock:
            self.store.set_collector(state, self.now().isoformat(timespec="microseconds"),
                                     self.next_run_at, collector_id=self.collector_id,
                                     interval_seconds=self.settings.interval_seconds,
                                     symbols=list(self.settings.symbols), error=self.error)

    def monitor(self):
        # 네트워크 응답을 기다릴 때도 상태를 갱신한다. 브라우저와 무관한 스레드다.
        while not self.monitor_done.wait(1):
            try:
                request = json.loads(self.stop_path.read_text(encoding="utf-8"))
                collector_id = request.get("collector_id") if isinstance(request, dict) else None
                if not isinstance(collector_id, str) or not re.fullmatch(r"[0-9a-f]{32}", collector_id):
                    raise ValueError
                if collector_id == self.collector_id:
                    self.stop_event.set()
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError, ValueError):
                with self.state_lock:
                    self.error = "수집기 종료 요청 파일을 읽지 못했습니다."
            # 짧은 주기 덕분에 중지 요청에도 신속히 반응한다.
            if time.monotonic() - self.last_heartbeat >= HEARTBEAT_SECONDS:
                try:
                    self.publish()
                except Exception:
                    with self.state_lock:
                        self.error = "시세 수집 상태를 저장하지 못했습니다."
                self.last_heartbeat = time.monotonic()

    def collect_cycle(self):
        settings = load_market_settings(self.config_path)
        with self.state_lock:
            self.settings, self.next_run_at, self.error = settings, None, None
        self.publish()
        profile = load_profiles(self.config_path).select(settings.account)
        profile.settings.validate_credentials()
        identity = series_for_profile(profile)
        if self.client is None or identity != self.identity:
            self.client = self.client_factory(profile)
            self.identity = identity
        successful = True
        for symbol in settings.symbols:
            if self.stop_event.is_set():
                break
            observation_id = uuid4().hex
            try:
                quote = self.client.quote(symbol)
                if (quote.get("symbol") != symbol or quote.get("environment") != "paper"
                        or quote.get("market") != "KRX"):
                    raise KisError("요청 종목과 시세 응답이 일치하지 않습니다.")
            except KisError as error:
                message = str(error)
            except Exception:
                message = "시세를 조회하지 못했습니다. 다음 수집 때 다시 시도합니다."
            else:
                if self.stop_event.is_set():
                    break
                try:
                    self.store.record_quote(quote, self.now().isoformat(timespec="microseconds"), observation_id)
                except ValueError:
                    message = "시세 응답의 수치가 올바르지 않습니다. 다음 수집 때 다시 시도합니다."
                else:
                    continue
            successful = False
            if not self.stop_event.is_set():
                self.store.record_attempt(symbol, self.now().isoformat(timespec="microseconds"), message)
        return successful

    def run(self, once=False):
        with ExitStack() as ownership:
            # 시작과 종료 요청의 짧은 제어 구간을 직렬화한다. byte 0의 소유
            # 잠금은 전체 실행 동안 유지하고, 뒤에는 현재 실행 토큰만 기록한다.
            with file_lock(self.control_path):
                handle = ownership.enter_context(file_lock(self.lock_path, blocking=False))
                handle.seek(1)
                handle.write(self.collector_id.encode("ascii"))
                handle.truncate()
                handle.flush()
                self.stop_path.unlink(missing_ok=True)
            return self._run_owned(once)

    def _run_owned(self, once):
        self.last_heartbeat = time.monotonic()
        monitor = threading.Thread(target=self.monitor, daemon=True, name="market-heartbeat")
        monitor.start()
        result = 0
        try:
            while not self.stop_event.is_set():
                try:
                    successful = self.collect_cycle()
                    result = 0 if successful else 1
                except KisError as error:
                    with self.state_lock:
                        self.error = str(error)
                    result = 1
                except Exception:
                    with self.state_lock:
                        self.error = "시세 기록 또는 수집 설정을 처리하지 못했습니다."
                    result = 1
                if once:
                    break
                with self.state_lock:
                    delay = self.settings.interval_seconds
                    self.next_run_at = (self.now() + timedelta(seconds=delay)).isoformat(timespec="microseconds")
                try:
                    self.publish()
                except Exception:
                    pass  # 저장소 장애 중에는 HTTP 조회도 오류를 반환한다. 다음 회차에서 재시도한다.
                self.stop_event.wait(delay)
        finally:
            self.monitor_done.set()
            monitor.join(timeout=2)
            with self.state_lock:
                self.next_run_at = None
            try:
                self.publish("stopped")
            except Exception:
                pass
        return result


def request_stop(database):
    database = Path(database)
    lock_path = database.with_suffix(".collector.lock")
    with file_lock(database.with_suffix(".collector-control.lock")):
        try:
            with file_lock(lock_path, blocking=False):
                return False
        except BlockingIOError:
            # DB 초기화 전에도 실행 토큰을 읽는다. Windows의 잠긴 byte 0을
            # 읽지 않도록 버퍼 없이 byte 1부터 읽으며 시작 측은 control 잠금을 공유한다.
            with lock_path.open("rb", buffering=0) as handle:
                handle.seek(1)
                try:
                    collector_id = handle.read(33).decode("ascii")
                except UnicodeError:
                    raise KisError("수집기 실행 정보를 읽지 못했습니다. 잠시 후 다시 종료하세요.") from None
            if not re.fullmatch(r"[0-9a-f]{32}", collector_id):
                raise KisError("수집기 실행 정보를 읽지 못했습니다. 잠시 후 다시 종료하세요.")
            stop_path = database.with_suffix(".stop")
            temporary = stop_path.with_name(stop_path.name + "." + uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps({"collector_id": collector_id}), encoding="utf-8")
                temporary.replace(stop_path)
            finally:
                temporary.unlink(missing_ok=True)
            return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS 모의 현재가 수집·저장")
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.toml")
    parser.add_argument("--database", type=Path, default=MARKET_DATABASE)
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--once", action="store_true", help="한 회차만 수집")
    command.add_argument("--stop", action="store_true", help="실행 중인 수집기에 종료 요청")
    command.add_argument("--status", action="store_true", help="저장된 수집 상태 확인")
    args = parser.parse_args(argv)
    try:
        if args.stop:
            print("수집기 종료를 요청했습니다." if request_stop(args.database) else "실행 중인 수집기가 없습니다.")
            return 0
        if args.status:
            state = MarketHistory(args.database).get_collector()
            state.pop("collector_id", None)
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        collector = Collector(args.config, args.database)
        print("KIS 시세 수집기를 시작합니다.", flush=True)
        result = collector.run(once=args.once)
        if args.once:
            print("시세 수집 1회 완료." if result == 0 else "시세 수집 실패. --status와 대시보드의 오류를 확인하세요.")
        return result
    except BlockingIOError:
        print("이 저장소의 시세 수집기가 이미 실행 중입니다.")
        return 1
    except KeyboardInterrupt:
        return 0
    except KisError as error:
        print(str(error))
        return 1
    except Exception:
        print("시세 수집기를 실행하지 못했습니다. 설정과 로컬 저장 파일을 확인하세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
