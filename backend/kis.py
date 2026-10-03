"""KIS 모의투자 조회 CLI. Python 3.11+, 외부 패키지 없음."""

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import tomllib
from typing import Callable, ContextManager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.request_gate import file_lock, request_spacing

ROOT = Path(__file__).resolve().parents[1]
PAPER_URL = "https://openapivts.koreainvestment.com:29443"
KST = timezone(timedelta(hours=9))
EXECUTION_MAX_DAYS = 90
TOKEN_LOCK = threading.Lock()


class KisError(Exception):
    """비밀값을 포함하지 않는 사용자용 오류."""


def _execution_today():
    return datetime.now(KST).date()


def validate_execution_range(start_date, end_date):
    """조회 부담을 제한하는 앱 정책: 양 끝을 포함해 최대 90일."""
    try:
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
                   for value in (start_date, end_date)):
            raise ValueError
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        raise KisError("거래 조회 날짜는 YYYY-MM-DD 형식으로 입력하세요.") from None
    if start > end:
        raise KisError("거래 조회 시작일은 종료일보다 늦을 수 없습니다.")
    if end > _execution_today():
        raise KisError("거래 내역은 오늘까지 조회할 수 있습니다.")
    if (end - start).days >= EXECUTION_MAX_DAYS:
        raise KisError("거래 내역은 한 번에 최대 90일간 조회할 수 있습니다.")
    return start, end


def _execution_number(value):
    if (not isinstance(value, str) or len(value) > 40
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value.strip())):
        raise KisError("체결 응답에 올바른 수량·금액이 없습니다.")
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise KisError("체결 응답에 올바른 수량·금액이 없습니다.") from None
    return format(number, "f").rstrip("0").rstrip(".") if "." in str(number) else str(number)


def _quote_number(value, *, signed=False, integer=False, positive=False):
    pattern = (r"[+-]?" if signed else "") + (r"[0-9]+" if integer else r"[0-9]+(?:\.[0-9]+)?")
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(pattern, value.strip()):
        raise KisError("시세 응답에 올바른 가격·거래량이 없습니다.")
    number = Decimal(value.strip())
    if positive and number <= 0:
        raise KisError("시세 응답에 유효한 현재가가 없습니다.")
    normalized = format(number, "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _execution_row(row, start, end):
    if not isinstance(row, dict):
        raise KisError("체결 응답 형식이 예상과 다릅니다.")
    quantity = _execution_number(row.get("tot_ccld_qty"))
    if Decimal(quantity) == 0:
        return None
    order_date = row.get("ord_dt")
    try:
        if not isinstance(order_date, str) or not re.fullmatch(r"[0-9]{8}", order_date):
            raise ValueError
        order_date = datetime.strptime(order_date, "%Y%m%d").date()
    except ValueError:
        raise KisError("체결 응답의 주문일자가 올바르지 않습니다.") from None
    if not start <= order_date <= end:
        raise KisError("체결 응답에 조회 기간 밖의 주문이 포함되어 있습니다.")
    order_id, branch_id, symbol = (row.get(key) for key in ("odno", "ord_gno_brno", "pdno"))
    if (not isinstance(order_id, str) or not re.fullmatch(r"[0-9]{1,20}", order_id)
            or not isinstance(branch_id, str) or not re.fullmatch(r"[0-9]{1,10}", branch_id)
            or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6,12}", symbol)):
        raise KisError("체결 응답의 주문 식별값이 올바르지 않습니다.")
    side_code = row.get("sll_buy_dvsn_cd")
    side = {"01": "sell", "02": "buy"}.get(side_code) if isinstance(side_code, str) else None
    if side is None:
        raise KisError("체결 응답의 매수·매도 구분이 올바르지 않습니다.")
    name = row.get("prdt_name")
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
            or any(ord(character) < 32 or ord(character) == 127 for character in name)):
        raise KisError("체결 응답의 종목명이 올바르지 않습니다.")
    price, amount = _execution_number(row.get("avg_prvs")), _execution_number(row.get("tot_ccld_amt"))
    if Decimal(price) <= 0 or Decimal(amount) <= 0:
        raise KisError("체결 응답에 유효한 체결 금액이 없습니다.")
    order_time = row.get("ord_tmd")
    if order_time in (None, ""):
        order_time = None
    else:
        try:
            if not isinstance(order_time, str) or not re.fullmatch(r"[0-9]{6}", order_time):
                raise ValueError
            order_time = datetime.strptime(order_time, "%H%M%S").strftime("%H:%M:%S")
        except ValueError:
            raise KisError("체결 응답의 주문시각이 올바르지 않습니다.") from None
    # 일별주문체결 응답은 개별 체결 틱이 아닌 주문별 누적 수량·평균가다.
    return {"order_date": order_date.isoformat(), "order_id": order_id, "branch_id": branch_id,
            "symbol": symbol, "name": name.strip(), "side": side, "quantity": quantity,
            "price": price, "amount": amount, "order_time": order_time}


@dataclass(repr=False)
class Settings:
    app_key: str
    app_secret: str
    account: str = ""
    product_code: str = "01"

    @classmethod
    def load(cls, path: Path, profile_id: str | None = None):
        catalog = load_profiles(path)
        profile = catalog.select(profile_id)
        profile.settings.validate_credentials()
        return profile.settings

    def validate_credentials(self):
        if not self.app_key.strip() or not self.app_secret.strip():
            raise KisError("app_key와 app_secret에 모의투자 키를 입력하세요.")

    def validate_account(self):
        if not re.fullmatch(r"[0-9]{8}", self.account):
            raise KisError("account에 모의 계좌 앞 8자리를 입력하세요.")
        if not re.fullmatch(r"[0-9]{2}", self.product_code):
            raise KisError("product_code에 계좌 뒤 2자리를 입력하세요.")


@dataclass(repr=False)
class AccountProfile:
    id: str
    name: str
    settings: Settings

    @property
    def configured(self):
        try:
            self.settings.validate_credentials()
            self.settings.validate_account()
        except KisError:
            return False
        return True

    def public_metadata(self):
        return {"id": self.id, "name": self.name, "configured": self.configured}


@dataclass(repr=False)
class AccountProfiles:
    default_id: str
    profiles: dict[str, AccountProfile]

    def select(self, profile_id: str | None = None):
        selected = self.default_id if profile_id is None else profile_id
        if selected not in self.profiles:
            raise KisError("등록되지 않은 계좌입니다. 계좌 설정을 확인하세요.")
        return self.profiles[selected]


def load_profiles(path: Path):
    try:
        values = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise KisError("config.local.toml을 만들고 모의투자 설정을 입력하세요.") from None
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise KisError("설정 파일의 TOML 형식을 확인하세요.") from None

    defaults = {"app_key": "", "app_secret": "", "account": "", "product_code": "01"}
    accounts = values.get("accounts", {})
    if not isinstance(accounts, dict):
        raise KisError("accounts는 계좌별 TOML 테이블이어야 합니다.")
    accounts = dict(accounts)
    legacy = any(key in values for key in defaults)
    if legacy and "paper" in accounts:
        raise KisError("기존 계좌 설정과 accounts.paper를 동시에 사용할 수 없습니다.")
    if legacy or "accounts" not in values:
        accounts = {"paper": {"name": "일반 모의투자", **{
            key: values.get(key, default) for key, default in defaults.items()
        }}, **accounts}
    if not accounts:
        raise KisError("계좌 설정을 하나 이상 추가하세요.")

    profiles = {}
    for profile_id, account in accounts.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", profile_id):
            raise KisError("계좌 ID는 영문 소문자로 시작하는 1~32자의 소문자·숫자·밑줄·하이픈이어야 합니다.")
        if not isinstance(account, dict):
            raise KisError("각 계좌 설정은 TOML 테이블이어야 합니다.")
        name = account.get("name", "일반 모의투자" if profile_id == "paper" else profile_id)
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 40
                or any(ord(character) < 32 or ord(character) == 127 for character in name)):
            raise KisError("계좌 이름은 줄바꿈 없는 1~40자의 문자열이어야 합니다.")
        fields = {key: account.get(key, default) for key, default in defaults.items()}
        if not all(isinstance(value, str) for value in fields.values()):
            raise KisError("계좌 설정값은 따옴표로 감싼 문자열이어야 합니다.")
        profiles[profile_id] = AccountProfile(profile_id, name.strip(), Settings(**fields))

    default_id = values.get("default_account", "paper" if "paper" in profiles else next(iter(profiles)))
    if not isinstance(default_id, str) or default_id not in profiles:
        raise KisError("default_account에 등록된 계좌 ID를 입력하세요.")
    return AccountProfiles(default_id, profiles)


def _credential_identity(settings: Settings):
    return hashlib.sha256((settings.app_key + "\0" + settings.app_secret).encode()).hexdigest()


def client_for_profile(profile: AccountProfile, cache_dir: Path | None = None):
    profile.settings.validate_credentials()
    directory = ROOT / ".local" if cache_dir is None else cache_dir
    return PaperClient(profile.settings, directory / f"token-{_credential_identity(profile.settings)}.json",
                       legacy_cache_path=directory / "kis-token.json")


@dataclass(repr=False)
class PaperClient:
    settings: Settings
    cache_path: Path = ROOT / ".local" / "kis-token.json"
    legacy_cache_path: Path | None = None
    request_guard: Callable[[], ContextManager] | None = None

    def _gate_path(self, purpose):
        return self.cache_path.parent / f"{purpose}-{_credential_identity(self.settings)}.lock"

    def _request(self, path, *, headers=None, params=None, body=None):
        # 계좌 간 제한을 공유하되, 연속조회의 페이지 사이에는 잔고 조회가 진행된다.
        with self.request_guard() if self.request_guard is not None else nullcontext():
            return self._perform_request(path, headers=headers, params=params, body=body)

    def _perform_request(self, path, *, headers=None, params=None, body=None):
        # 대시보드와 독립 수집기가 같은 키를 사용해도 요청 간격을 공유한다.
        try:
            with request_spacing(self._gate_path("request")):
                return self._http_request(path, headers=headers, params=params, body=body)
        except (OSError, TimeoutError):
            raise KisError("로컬 API 요청 잠금에 접근하지 못했습니다. 잠시 후 다시 조회하세요.") from None

    def _http_request(self, path, *, headers=None, params=None, body=None):
        # 모의 서버로만 연결하며, 서버 응답 본문이나 인증 헤더를 오류에 출력하지 않는다.
        url = PAPER_URL + path
        if params:
            url += "?" + urlencode(params)
        request = Request(
            url, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "kis-ai-league/0.1", **(headers or {})},
        )
        try:
            with urlopen(request, timeout=15) as response:
                data = json.load(response)
                response_headers = dict(response.headers.items())
        except HTTPError as error:
            raise KisError(f"KIS 요청 실패 (HTTP {error.code}). 설정과 서비스 상태를 확인하세요.") from None
        except (URLError, TimeoutError, OSError):
            raise KisError("KIS 연결 실패. 네트워크와 서비스 상태를 확인하세요.") from None
        except (ValueError, UnicodeError):
            raise KisError("KIS 응답을 읽을 수 없습니다.") from None
        if not isinstance(data, dict):
            raise KisError("KIS 응답 형식이 예상과 다릅니다.")
        if "rt_cd" in data and data["rt_cd"] != "0":
            code = str(data.get("msg_cd", ""))
            suffix = f" ({code})" if re.fullmatch(r"[A-Z]{3}[0-9]{5}", code) else ""
            raise KisError("KIS 업무 응답 오류" + suffix)
        return data, {key.lower(): value for key, value in response_headers.items()}

    def token(self):
        # 잔고·체결 클라이언트가 동시에 시작해도 캐시 확인부터 발급·저장까지
        # 직렬화해 동일 키로 토큰을 연달아 발급하지 않는다.
        with TOKEN_LOCK:
            try:
                with file_lock(self._gate_path("token")):
                    return self._cached_or_issue_token()
            except (OSError, TimeoutError):
                raise KisError("로컬 인증 캐시·잠금에 접근하지 못했습니다. 잠시 후 다시 조회하세요.") from None

    def _cached_or_issue_token(self):
        identity = _credential_identity(self.settings)
        for path in (self.cache_path, self.legacy_cache_path):
            if path is None:
                continue
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if (saved["identity"] == identity and saved["expires_at"] > time.time() + 60
                        and isinstance(saved["token"], str) and saved["token"]):
                    return saved["token"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        issued_at = time.time()
        data, _ = self._request("/oauth2/tokenP", body={
            "grant_type": "client_credentials", "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
        })
        token = data.get("access_token")
        try:
            lifetime = int(data["expires_in"])
        except (KeyError, ValueError, TypeError):
            raise KisError("인증 응답에 유효한 만료 시간이 없습니다.") from None
        if not isinstance(token, str) or not token or lifetime <= 60:
            raise KisError("인증 응답에 유효한 토큰이 없습니다.")
        expires_at = issued_at + lifetime
        if data.get("access_token_token_expired"):
            try:
                absolute = datetime.strptime(data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
                expires_at = min(expires_at, absolute.replace(
                    tzinfo=timezone(timedelta(hours=9))).timestamp())
            except (ValueError, TypeError):
                raise KisError("인증 응답의 만료 일시를 확인할 수 없습니다.") from None
        if expires_at <= time.time() + 60:
            raise KisError("발급된 토큰이 만료됐거나 만료가 임박했습니다.")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                         dir=self.cache_path.parent) as handle:
            json.dump({"identity": identity, "token": token,
                       "expires_at": expires_at}, handle)
            temporary = Path(handle.name)
        try:
            temporary.replace(self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)
        return token

    def _get(self, path, tr_id, params, continuation=""):
        data, headers = self._request(path, params=params, headers={
            "authorization": "Bearer " + self.token(),
            "appkey": self.settings.app_key, "appsecret": self.settings.app_secret,
            "tr_id": tr_id, "custtype": "P", "tr_cont": continuation,
        })
        if data.get("rt_cd") != "0":
            raise KisError("KIS 응답에 성공 코드가 없습니다.")
        return data, headers

    def quote(self, symbol):
        if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
            raise KisError("종목코드는 숫자 6자리로 입력하세요.")
        data, _ = self._get("/uapi/domestic-stock/v1/quotations/inquire-price",
                            "FHKST01010100", {
                                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
                            })
        output = data.get("output")
        if not isinstance(output, dict) or not output:
            raise KisError("현재가 응답이 없습니다. 종목과 서비스 지원 여부를 확인하세요.")
        if "stck_shrn_iscd" in output and output["stck_shrn_iscd"] != symbol:
            raise KisError("시세 응답의 종목코드가 요청과 다릅니다.")
        # 현재가 API의 누적 거래량·거래대금이다. 조회 시각은 수집기가 별도로
        # 기록하며, 응답에 없는 거래일·체결시각·종목명을 추정하지 않는다.
        return {"environment": "paper", "symbol": symbol, "market": "KRX",
                "price": _quote_number(output.get("stck_prpr"), positive=True),
                "change_percent": _quote_number(output.get("prdy_ctrt"), signed=True),
                "volume": _quote_number(output.get("acml_vol"), integer=True),
                "cumulative_turnover": _quote_number(output.get("acml_tr_pbmn"))}

    def chart_daily(self, symbol, start_date, end_date):
        """최대 100개의 수정 일봉 원본. 날짜·가격 검증은 차트 서비스가 담당한다."""
        if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
            raise KisError("종목코드는 숫자 6자리로 입력하세요.")
        data, _ = self._get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                            "FHKST03010100", {
                                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
                                "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
                                "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
                                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
                            })
        return data

    def chart_minutes(self, symbol, hour):
        """당일 분봉 최대 30행. hour 이전 방향으로 차트 서비스가 페이지를 조회한다."""
        if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
            raise KisError("종목코드는 숫자 6자리로 입력하세요.")
        if not isinstance(hour, str) or not re.fullmatch(r"[0-9]{6}", hour):
            raise KisError("분봉 조회 시각이 올바르지 않습니다.")
        try:
            datetime.strptime(hour, "%H%M%S")
        except ValueError:
            raise KisError("분봉 조회 시각이 올바르지 않습니다.") from None
        data, _ = self._get("/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                            "FHKST03010200", {
                                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
                                "FID_INPUT_HOUR_1": hour, "FID_PW_DATA_INCU_YN": "Y",
                                "FID_ETC_CLS_CODE": "",
                            })
        return data

    def balance(self):
        self.settings.validate_account()
        params = {
            "CANO": self.settings.account, "ACNT_PRDT_CD": self.settings.product_code,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        holdings, seen = [], set()
        for page in range(100):
            data, headers = self._get("/uapi/domestic-stock/v1/trading/inquire-balance",
                                      "VTTC8434R", params, "N" if page else "")
            rows, summary = data.get("output1"), data.get("output2")
            if (not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
                    or not isinstance(summary, list) or not summary
                    or not isinstance(summary[0], dict)):
                raise KisError("잔고 응답 형식이 예상과 다릅니다.")
            holdings.extend({key: row.get(key) for key in (
                "pdno", "prdt_name", "hldg_qty", "pchs_avg_pric", "pchs_amt",
                "prpr", "evlu_amt", "evlu_pfls_amt", "evlu_pfls_rt"
            )} for row in rows)
            if headers.get("tr_cont") not in ("M", "F"):
                return {"environment": "paper", "holdings": holdings,
                        "summary": {key: summary[0].get(key) for key in (
                            "dnca_tot_amt", "scts_evlu_amt", "tot_evlu_amt",
                            "pchs_amt_smtl_amt", "evlu_pfls_smtl_amt"
                        )}}
            cursor = (data.get("ctx_area_fk100", ""), data.get("ctx_area_nk100", ""))
            if not all(isinstance(value, str) for value in cursor):
                raise KisError("잔고 연속조회 키가 올바르지 않습니다.")
            cursor = tuple(value.strip() for value in cursor)
            if not any(cursor) or cursor in seen:
                raise KisError("잔고 연속조회가 진행되지 않습니다. 부분 결과를 반환하지 않습니다.")
            seen.add(cursor)
            params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
        raise KisError("잔고 연속조회 한도를 초과했습니다.")

    def executions(self, start_date, end_date):
        """전 페이지를 검증한 주문별 누적 체결. 오류 시 부분 결과를 반환하지 않는다."""
        self.settings.validate_account()
        start, end = validate_execution_range(start_date, end_date)
        today = _execution_today()
        # 공식 legacy 예제의 월 단위 경계: 4월 25일이면 1월 1일부터 최근 TR.
        month_index = today.year * 12 + today.month - 1 - 3
        cutoff = date(month_index // 12, month_index % 12 + 1, 1)
        periods = []
        if start < cutoff:
            periods.append((start, min(end, cutoff - timedelta(days=1)), "VTSC9215R"))
        if end >= cutoff:
            periods.append((max(start, cutoff), end, "VTTC0081R"))
        executions = {}
        for period_start, period_end, tr_id in periods:
            params = {
                "CANO": self.settings.account, "ACNT_PRDT_CD": self.settings.product_code,
                "INQR_STRT_DT": period_start.strftime("%Y%m%d"),
                "INQR_END_DT": period_end.strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "",
                # 전체 주문에서 실제 체결량을 검사해 미완료 부분체결도 포함한다.
                "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "",
                "INQR_DVSN_1": "", "INQR_DVSN_3": "00", "EXCG_ID_DVSN_CD": "ALL",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            }
            seen = set()
            for page in range(1000):
                data, headers = self._get("/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                                          tr_id, params, "N" if page else "")
                rows = data.get("output1")
                if not isinstance(rows, list) or not isinstance(data.get("output2"), dict):
                    raise KisError("체결 응답 형식이 예상과 다릅니다.")
                for raw in rows:
                    row = _execution_row(raw, period_start, period_end)
                    if row is None:
                        continue
                    key = (row["order_date"], row["branch_id"], row["order_id"])
                    previous = executions.get(key)
                    if previous is not None:
                        if (any(previous[field] != row[field] for field in ("symbol", "side"))
                                or Decimal(row["quantity"]) < Decimal(previous["quantity"])):
                            raise KisError("중복 주문의 체결 응답이 일치하지 않습니다. 다시 조회하세요.")
                    executions[key] = row
                continuation = headers.get("tr_cont", "").strip()
                if continuation in ("", "D", "E"):
                    break
                if continuation not in ("M", "F"):
                    raise KisError("체결 연속조회 상태를 확인할 수 없습니다.")
                cursor = (data.get("ctx_area_fk100"), data.get("ctx_area_nk100"))
                if not all(isinstance(value, str) for value in cursor):
                    raise KisError("체결 연속조회 키가 올바르지 않습니다.")
                cursor = tuple(value.strip() for value in cursor)
                if not any(cursor) or cursor in seen:
                    raise KisError("체결 연속조회가 진행되지 않습니다. 부분 결과를 반환하지 않습니다.")
                seen.add(cursor)
                params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
            else:
                raise KisError("체결 연속조회 한도를 초과했습니다. 조회 기간을 줄이세요.")
        return {"environment": "paper", "executions": sorted(executions.values(), key=lambda row: (
            row["order_date"], row["order_time"] or "", row["branch_id"], row["order_id"]), reverse=True)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS 모의투자 조회")
    parser.add_argument("--account", help="config.local.toml에 등록한 계좌 ID")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="로컬 설정 형식 확인")
    commands.add_parser("auth", help="인증 확인")
    commands.add_parser("quote", help="현재가 조회").add_argument("symbol")
    commands.add_parser("balance", help="잔고 조회")
    args = parser.parse_args(argv)
    try:
        profile = load_profiles(ROOT / "config.local.toml").select(args.account)
        settings = profile.settings
        client = client_for_profile(profile)
        if args.command == "check":
            if settings.account:
                settings.validate_account()
            result = {"config": "ok", "account_configured": bool(settings.account)}
        elif args.command == "auth":
            client.token()
            result = {"environment": "paper", "authentication": "ok"}
        elif args.command == "quote":
            result = client.quote(args.symbol)
        else:
            result = client.balance()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KisError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("로컬 설정 또는 토큰 캐시 파일 접근에 실패했습니다.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
