"""로컬 전용 KIS 모의 계좌 대시보드. 실행: python -m backend.dashboard"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from backend.history import AccountHistory, series_for_profile
from backend.kis import (KisError, PaperClient, ROOT, Settings, client_for_profile,
                         load_profiles, validate_execution_range)
from backend.trades import ExecutionConflict, ExecutionHistory
from backend.market import MarketService
from backend.chart import ChartService

FRONTEND = ROOT / "frontend" / "dist"
REFRESH_SECONDS = 30


def number(value):
    """누락·비정상 수치를 0으로 바꾸지 않고 JSON용 십진 문자열로 보존한다."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return format(parsed, "f") if parsed.is_finite() else None


def normalize_balance(balance):
    summary = balance["summary"]
    purchase = number(summary.get("pchs_amt_smtl_amt"))
    pnl = number(summary.get("evlu_pfls_smtl_amt"))
    rate = None
    if purchase is not None and Decimal(purchase) > 0 and pnl is not None:
        rate = format(Decimal(pnl) / Decimal(purchase) * 100, ".4f")
    holdings = []
    for row in balance["holdings"]:
        quantity = number(row.get("hldg_qty"))
        # 전량 매도한 행이 API에 남아 있어도 현재 보유 종목으로 세지 않는다.
        if quantity is not None and Decimal(quantity) == 0:
            continue
        holdings.append({
            "symbol": str(row.get("pdno") or ""),
            "name": str(row.get("prdt_name") or ""),
            "quantity": quantity,
            **{alias: number(row.get(key)) for alias, key in (
                ("avg_price", "pchs_avg_pric"), ("price", "prpr"),
                ("market_value", "evlu_amt"), ("purchase_amount", "pchs_amt"),
                ("pnl", "evlu_pfls_amt"), ("return_pct", "evlu_pfls_rt"),
            )},
        })
    return {
        "summary": {
            "cash": number(summary.get("dnca_tot_amt")),
            "securities_value": number(summary.get("scts_evlu_amt")),
            "total_value": number(summary.get("tot_evlu_amt")),
            "purchase_amount": purchase, "unrealized_pnl": pnl,
            "unrealized_return_pct": rate,
        },
        "holdings": holdings,
    }


def load_client():
    return PaperClient(Settings.load(ROOT / "config.local.toml"))


class AccountService:
    def __init__(self, client_factory=load_client, clock=time.monotonic, balance_reader=None,
                 history_store=None, series=None):
        self.client_factory = client_factory
        self.clock = clock
        self.balance_reader = balance_reader or (lambda client: client.balance())
        self.client = None
        self.lock = threading.Lock()
        self.data = {"summary": {}, "holdings": []}
        self.updated_at = None
        self.next_attempt = 0
        self.error = None
        self.history_store = history_store
        self.series = series
        self.history_write_error = None

    def history(self):
        result = {"points": [], "total_count": 0, "error": self.history_write_error}
        if self.history_store is not None:
            try:
                result.update(self.history_store.read(self.series))
            except Exception:
                result["error"] = "계좌 이력을 읽지 못했습니다. 로컬 저장 파일을 확인하세요."
        return result

    def snapshot(self):
        # 중복 브라우저 요청이 인증·잔고 연속조회를 겹쳐 실행하지 않도록 직렬화.
        with self.lock:
            if self.clock() >= self.next_attempt:
                try:
                    if self.client is None:
                        self.client = self.client_factory()
                    result = normalize_balance(self.balance_reader(self.client))
                    self.data = result
                    self.updated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                    self.error = None
                except KisError as error:
                    self.error = str(error)
                except OSError:
                    self.error = "로컬 설정 또는 토큰 캐시 파일에 접근할 수 없습니다."
                except Exception:
                    # 원본 응답·예외에는 계좌 정보가 있을 수 있어 브라우저에 보내지 않는다.
                    self.error = "계좌 데이터를 처리하지 못했습니다. 잠시 후 다시 조회하세요."
                else:
                    # 같은 잔고라도 새 조회는 관측값이다. 캐시·실패 경로에서는 기록하지 않는다.
                    if self.history_store is not None:
                        try:
                            self.history_store.record(self.series, self.updated_at, self.data)
                            self.history_write_error = None
                        except Exception:
                            self.history_write_error = "계좌 이력을 저장하지 못했습니다. 로컬 저장 파일을 확인하세요."
                self.next_attempt = self.clock() + (10 if self.error else 5)
            return {
                "status": "error" if self.error else "ok",
                "environment": "paper", "updated_at": self.updated_at,
                "refresh_interval_seconds": REFRESH_SECONDS,
                "stale": bool(self.error and self.updated_at),
                "error": self.error, **self.data, "history": self.history(),
            }


class UnknownAccount(KisError):
    pass


class TradeService:
    """모든 페이지 조회·저장이 성공한 기간만 정상 갱신으로 표시한다."""

    def __init__(self, client_factory, reader, store, series, clock=time.monotonic):
        self.client_factory, self.reader = client_factory, reader
        self.store, self.series, self.clock = store, series, clock
        self.client = None
        self.lock = threading.Lock()
        self.attempts = {}

    def snapshot(self, start, end):
        with self.lock:
            key = (start, end)
            next_attempt, error = self.attempts.get(key, (0, None))
            if self.clock() >= next_attempt:
                try:
                    if self.client is None:
                        self.client = self.client_factory()
                    rows = self.reader(self.client, start, end)["executions"]
                except KisError as failure:
                    error = str(failure)
                except Exception:
                    error = "체결 내역을 조회하지 못했습니다. 잠시 후 다시 조회하세요."
                else:
                    try:
                        self.store.record(self.series, start, end,
                                          datetime.now(timezone.utc).isoformat(timespec="microseconds"), rows)
                        error = None
                    except ExecutionConflict:
                        error = "체결 누적값이 기존 기록과 다릅니다. 기존 내역을 유지합니다. 다시 조회하세요."
                    except Exception:
                        error = "체결 내역을 저장하지 못했습니다. 로컬 저장 파일을 확인하세요."
                self.attempts[key] = (self.clock() + (10 if error else 5), error)
                # 기간을 계속 바꿔도 서버 메모리를 무한히 사용하지 않는다.
                if len(self.attempts) > 64:
                    del self.attempts[next(iter(self.attempts))]
            saved = {"trades": [], "total_count": 0, "updated_at": None}
            try:
                saved = self.store.read(self.series, start, end)
            except Exception:
                error = "체결 내역을 읽지 못했습니다. 로컬 저장 파일을 확인하세요."
            return {"status": "error" if error else "ok", "environment": "paper",
                    "start_date": start, "end_date": end, **saved,
                    "stale": bool(error and (saved["updated_at"] or saved["trades"])),
                    "error": error}


class AccountDirectory:
    """탭별 선택을 요청에 담고, 계좌·설정별로 조회 상태를 격리한다."""

    def __init__(self, config_path=ROOT / "config.local.toml", client_factory=client_for_profile,
                 clock=time.monotonic, sleep=time.sleep, history_store=None,
                 history_path=ROOT / ".local" / "account-history.sqlite3", execution_store=None):
        self.config_path = config_path
        self.client_factory = client_factory
        self.clock = clock
        self.sleep = sleep
        self.services = {}
        self.trade_services = {}
        self.lock = threading.Lock()
        self.broker_lock = threading.Lock()
        self.next_broker_request = 0
        self.history_store = history_store if history_store is not None else AccountHistory(history_path)
        self.execution_store = execution_store if execution_store is not None else ExecutionHistory(history_path)

    def list_accounts(self):
        profiles = load_profiles(self.config_path)
        return {
            "default_account": profiles.default_id,
            "accounts": [{"id": profile.id, "name": profile.name,
                          "configured": profile.configured}
                         for profile in profiles.profiles.values()],
        }

    def read_balance(self, client):
        if isinstance(client, PaperClient):
            return client.balance()
        return self.broker_read(client.balance)

    def read_executions(self, client, start, end):
        if isinstance(client, PaperClient):
            return client.executions(start, end)
        return self.broker_read(lambda: client.executions(start, end))

    @contextmanager
    def broker_guard(self):
        # 동일 키를 쓰는 계좌 사이에도 모의 서버 요청이 겹치지 않게 한다.
        # 페이지마다 잠금을 풀어 긴 체결 조회 중에도 잔고 요청을 처리한다.
        with self.broker_lock:
            self.sleep(max(0, self.next_broker_request - self.clock()))
            try:
                yield
            finally:
                self.next_broker_request = self.clock() + 1

    def broker_read(self, read):
        with self.broker_guard():
            return read()

    def make_client(self, profile):
        client = self.client_factory(profile)
        if isinstance(client, PaperClient):
            client.request_guard = self.broker_guard
        return client

    def snapshot(self, account_id=None):
        return self._query(account_id)

    def trades(self, account_id, start, end):
        validate_execution_range(start, end)
        return self._query(account_id, (start, end))

    def _query(self, account_id, trade_range=None):
        services = self.trade_services if trade_range else self.services
        for _ in range(3):
            with self.lock:
                profiles = load_profiles(self.config_path)
                selected = profiles.default_id if account_id is None else account_id
                profile = profiles.profiles.get(selected)
                if profile is None:
                    raise UnknownAccount("선택한 계좌가 설정에 없습니다. 계좌 목록을 새로고침하세요.")
                series = series_for_profile(profile)
                for removed in self.services.keys() - profiles.profiles.keys():
                    del self.services[removed]
                for removed in self.trade_services.keys() - profiles.profiles.keys():
                    del self.trade_services[removed]
                if not profile.configured:
                    self.services.pop(selected, None)
                    self.trade_services.pop(selected, None)
                    # 미발급·미완성 계좌는 네트워크와 이력 저장소를 조회하지 않는다.
                    if trade_range:
                        return {"status": "error", "environment": "paper", "stale": False,
                                "updated_at": None, "start_date": trade_range[0], "end_date": trade_range[1],
                                "trades": [], "total_count": 0,
                                "error": "이 계좌의 API 키와 계좌번호 설정을 완료하세요.",
                                "account": {"id": profile.id, "name": profile.name}}
                    return {
                        "status": "error", "environment": "paper", "stale": False,
                        "updated_at": None, "refresh_interval_seconds": REFRESH_SECONDS,
                        "summary": {}, "holdings": [],
                        "error": "이 계좌의 API 키와 계좌번호 설정을 완료하세요.",
                        "account": {"id": profile.id, "name": profile.name},
                        "history": {"points": [], "total_count": 0, "error": None},
                    }
                cached = services.get(selected)
                if cached is None or cached[0] != series:
                    factory = lambda profile=profile: self.make_client(profile)
                    if trade_range:
                        service = TradeService(factory, self.read_executions, self.execution_store,
                                               series, self.clock)
                    else:
                        service = AccountService(factory, self.clock, self.read_balance,
                                                 self.history_store, series)
                    services[selected] = (series, service)
                else:
                    service = cached[1]
            result = service.snapshot(*trade_range) if trade_range else service.snapshot()
            # 조회 중 키·계좌번호·기본 계좌가 바뀌었으면 새 설정으로 다시 선택한다.
            with self.lock:
                current = load_profiles(self.config_path)
                current_id = current.default_id if account_id is None else account_id
                current_profile = current.profiles.get(current_id)
                if current_profile is None:
                    raise UnknownAccount("선택한 계좌가 설정에 없습니다. 계좌 목록을 새로고침하세요.")
                if series_for_profile(current_profile) != series:
                    continue
                return {**result, "account": {"id": current_profile.id, "name": current_profile.name}}
        raise KisError("계좌 설정이 변경되었습니다. 잠시 후 다시 조회하세요.")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=8765, service=None, frontend_path=FRONTEND, market_service=None,
                 chart_service=None):
        self.service = service if service is not None else AccountDirectory()
        self.market_service = market_service if market_service is not None else MarketService()
        self.chart_service = chart_service if chart_service is not None else ChartService()
        self.frontend_path = frontend_path.resolve()
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.origins = {"http://" + host for host in self.hosts}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "KISDashboard"
    sys_version = ""

    def log_message(self, *args):
        pass

    def send_content(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; "
                         "script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
                         "frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_response(self, status, payload):
        self.send_content(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")

    def do_GET(self):
        if self.headers.get("Host") not in self.server.hosts:
            self.json_response(403, {"error": "로컬 접속만 허용됩니다."})
            return
        url = urlsplit(self.path)
        path = url.path
        if path in ("/api/account", "/api/accounts", "/api/trades", "/api/market", "/api/chart"):
            origin = self.headers.get("Origin")
            if (self.headers.get("X-KIS-Dashboard") != "1"
                    or (origin is not None and origin not in self.server.origins)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.json_response(403, {"error": "대시보드에서 계좌를 조회하세요."})
                return
            params = parse_qs(url.query, keep_blank_values=True)
            if path == "/api/chart":
                if (set(params) - {"symbol", "interval", "refresh"}
                        or len(params.get("symbol", [])) != 1
                        or not re.fullmatch(r"[0-9]{6}", params["symbol"][0])
                        or len(params.get("interval", ["day"])) != 1
                        or params.get("interval", ["day"])[0] not in {"day", "5m", "15m"}
                        or ("refresh" in params and params["refresh"] != ["1"])):
                    self.json_response(400, {"status": "error", "error": "종목코드 6자리와 차트 주기를 확인하세요."})
                    return
                try:
                    payload = self.server.chart_service.snapshot(
                        params["symbol"][0], params.get("interval", ["day"])[0],
                        **({"force": True} if "refresh" in params else {}))
                    self.json_response(200 if payload["status"] == "ok" else 503, payload)
                except Exception:
                    self.json_response(503, {"status": "error", "error": "종목 차트를 불러오지 못했습니다."})
                return
            if path == "/api/market":
                if params:
                    self.json_response(400, {"status": "error", "error": "시세 상태 조회에는 추가 조건을 지정할 수 없습니다."})
                    return
                try:
                    payload = self.server.market_service.snapshot()
                    self.json_response(200 if payload["status"] == "ok" else 503, payload)
                except Exception:
                    self.json_response(503, {"status": "error", "error": "시세 수집 상태를 읽지 못했습니다."})
                return
            allowed = {"account", "start", "end"} if path == "/api/trades" else {"account"}
            if (set(params) - allowed or
                    (path == "/api/accounts" and params) or
                    ("account" in params and
                     (len(params["account"]) != 1 or not params["account"][0]))):
                self.json_response(400, {"status": "error", "error": "계좌 선택 요청이 올바르지 않습니다."})
                return
            if path == "/api/trades":
                if any(len(params.get(key, [])) != 1 or not params[key][0] for key in ("start", "end")):
                    self.json_response(400, {"status": "error", "error": "체결 조회 시작일과 종료일을 입력하세요."})
                    return
                try:
                    validate_execution_range(params["start"][0], params["end"][0])
                except KisError as error:
                    self.json_response(400, {"status": "error", "error": str(error)})
                    return
            try:
                if path == "/api/accounts":
                    self.json_response(200, self.server.service.list_accounts())
                    return
                selected = params.get("account", [None])[0]
                if path == "/api/trades":
                    payload = self.server.service.trades(selected, params["start"][0], params["end"][0])
                else:
                    payload = (self.server.service.snapshot(selected) if selected is not None
                               else self.server.service.snapshot())
            except UnknownAccount as error:
                self.json_response(404, {"status": "error", "error": str(error)})
                return
            except KisError as error:
                self.json_response(503, {"status": "error", "error": str(error)})
                return
            except (OSError, UnicodeError):
                self.json_response(503, {"status": "error", "error": "로컬 계좌 설정을 읽을 수 없습니다."})
                return
            except Exception:
                self.json_response(503, {"status": "error", "error": "계좌 설정을 처리하지 못했습니다."})
                return
            self.json_response(502 if payload["status"] == "error" else 200, payload)
            return
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/fonts/PretendardVariable.woff2": ("fonts/PretendardVariable.woff2", "font/woff2"),
            "/fonts/OFL.txt": ("fonts/OFL.txt", "text/plain; charset=utf-8"),
        }
        if path in assets:
            filename, content_type = assets[path]
        elif re.fullmatch(r"/assets/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(js|css|woff2)", path):
            filename = path.lstrip("/")
            content_type = {"js": "text/javascript; charset=utf-8",
                            "css": "text/css; charset=utf-8", "woff2": "font/woff2"}[
                                path.rsplit(".", 1)[1]]
        else:
            self.json_response(404, {"error": "페이지를 찾을 수 없습니다."})
            return
        resolved = (self.server.frontend_path / filename).resolve()
        if not resolved.is_relative_to(self.server.frontend_path):
            self.json_response(404, {"error": "페이지를 찾을 수 없습니다."})
            return
        try:
            body = resolved.read_bytes()
        except FileNotFoundError:
            if filename == "index.html":
                self.json_response(503, {"error": "화면 빌드가 없습니다. npm ci 후 npm run build를 실행하세요."})
            else:
                self.json_response(404, {"error": "페이지를 찾을 수 없습니다."})
            return
        except OSError:
            self.json_response(503, {"error": "대시보드 화면 파일을 읽을 수 없습니다."})
            return
        self.send_content(200, body, content_type)


def main(argv=None):
    parser = argparse.ArgumentParser(description="로컬 KIS 모의 계좌 대시보드")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("포트는 1~65535 사이여야 합니다.")
    try:
        server = DashboardServer(args.port)
    except OSError:
        print("서버를 시작하지 못했습니다. 사용 중인 포트인지 확인하세요.")
        return 1
    print(f"KIS 모의 계좌 대시보드: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
