"""로컬 전용 KIS 모의 계좌 대시보드. 실행: python -m backend.dashboard"""

import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

from backend.kis import KisError, PaperClient, ROOT, Settings, client_for_profile, load_profiles

KST = timezone(timedelta(hours=9))
FRONTEND = ROOT / "frontend"
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
    def __init__(self, client_factory=load_client, clock=time.monotonic, balance_reader=None):
        self.client_factory = client_factory
        self.clock = clock
        self.balance_reader = balance_reader or (lambda client: client.balance())
        self.client = None
        self.lock = threading.Lock()
        self.data = {"summary": {}, "holdings": []}
        self.updated_at = None
        self.next_attempt = 0
        self.error = None

    def snapshot(self):
        # 중복 브라우저 요청이 인증·잔고 연속조회를 겹쳐 실행하지 않도록 직렬화.
        with self.lock:
            if self.clock() >= self.next_attempt:
                try:
                    if self.client is None:
                        self.client = self.client_factory()
                    result = normalize_balance(self.balance_reader(self.client))
                    self.data = result
                    self.updated_at = datetime.now(KST).isoformat(timespec="seconds")
                    self.error = None
                except KisError as error:
                    self.error = str(error)
                except OSError:
                    self.error = "로컬 설정 또는 토큰 캐시 파일에 접근할 수 없습니다."
                except Exception:
                    # 원본 응답·예외에는 계좌 정보가 있을 수 있어 브라우저에 보내지 않는다.
                    self.error = "계좌 데이터를 처리하지 못했습니다. 잠시 후 다시 조회하세요."
                self.next_attempt = self.clock() + (10 if self.error else 5)
            return {
                "status": "error" if self.error else "ok",
                "environment": "paper", "updated_at": self.updated_at,
                "refresh_interval_seconds": REFRESH_SECONDS,
                "stale": bool(self.error and self.updated_at),
                "error": self.error, **self.data,
            }


class UnknownAccount(KisError):
    pass


class AccountDirectory:
    """탭별 선택을 요청에 담고, 계좌·설정별로 조회 상태를 격리한다."""

    def __init__(self, config_path=ROOT / "config.local.toml", client_factory=client_for_profile,
                 clock=time.monotonic, sleep=time.sleep):
        self.config_path = config_path
        self.client_factory = client_factory
        self.clock = clock
        self.sleep = sleep
        self.services = {}
        self.lock = threading.Lock()
        self.broker_lock = threading.Lock()
        self.next_broker_request = 0

    def list_accounts(self):
        profiles = load_profiles(self.config_path)
        return {
            "default_account": profiles.default_id,
            "accounts": [{"id": profile.id, "name": profile.name,
                          "configured": profile.configured}
                         for profile in profiles.profiles.values()],
        }

    def read_balance(self, client):
        # 동일 키를 쓰는 계좌 사이에도 모의 서버 요청이 겹치지 않게 한다.
        with self.broker_lock:
            self.sleep(max(0, self.next_broker_request - self.clock()))
            try:
                return client.balance()
            finally:
                self.next_broker_request = self.clock() + 1

    def snapshot(self, account_id=None):
        with self.lock:
            profiles = load_profiles(self.config_path)
            selected = profiles.default_id if account_id is None else account_id
            profile = profiles.profiles.get(selected)
            if profile is None:
                raise UnknownAccount("선택한 계좌가 설정에 없습니다. 계좌 목록을 새로고침하세요.")
            # ID를 재사용해 키나 계좌번호를 바꿔도 이전 잔고를 재사용하지 않는다.
            identity = hashlib.sha256("\0".join((profile.settings.app_key,
                profile.settings.app_secret, profile.settings.account,
                profile.settings.product_code)).encode()).hexdigest()
            for removed in self.services.keys() - profiles.profiles.keys():
                del self.services[removed]
            cached = self.services.get(selected)
            if cached is None or cached[0] != identity:
                service = AccountService(lambda: self.client_factory(profile), self.clock,
                                         self.read_balance)
                self.services[selected] = (identity, service)
            else:
                service = cached[1]
        if not profile.configured:
            # 미발급·미완성 계좌에는 네트워크 요청을 보내지 않는다.
            return {
                "status": "error", "environment": "paper", "stale": False,
                "updated_at": None, "refresh_interval_seconds": REFRESH_SECONDS,
                "summary": {}, "holdings": [],
                "error": "이 계좌의 API 키와 계좌번호 설정을 완료하세요.",
                "account": {"id": profile.id, "name": profile.name},
            }
        return {**service.snapshot(), "account": {"id": profile.id, "name": profile.name}}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=8765, service=None):
        self.service = service if service is not None else AccountDirectory()
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
        if path in ("/api/account", "/api/accounts"):
            origin = self.headers.get("Origin")
            if (self.headers.get("X-KIS-Dashboard") != "1"
                    or (origin is not None and origin not in self.server.origins)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.json_response(403, {"error": "대시보드에서 계좌를 조회하세요."})
                return
            params = parse_qs(url.query, keep_blank_values=True)
            if (set(params) - {"account"} or
                    (path == "/api/accounts" and params) or
                    ("account" in params and
                     (len(params["account"]) != 1 or not params["account"][0]))):
                self.json_response(400, {"status": "error", "error": "계좌 선택 요청이 올바르지 않습니다."})
                return
            try:
                if path == "/api/accounts":
                    self.json_response(200, self.server.service.list_accounts())
                    return
                selected = params.get("account", [None])[0]
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
            "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
            "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
            "/fonts/PretendardVariable.woff2": ("fonts/PretendardVariable.woff2", "font/woff2"),
        }
        if path not in assets:
            self.json_response(404, {"error": "페이지를 찾을 수 없습니다."})
            return
        filename, content_type = assets[path]
        try:
            body = (FRONTEND / filename).read_bytes()
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
