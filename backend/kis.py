"""KIS 일반 모의투자 조회 CLI. Python 3.11+, 외부 패키지 없음."""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PAPER_URL = "https://openapivts.koreainvestment.com:29443"


class KisError(Exception):
    """비밀값을 포함하지 않는 사용자용 오류."""


@dataclass(repr=False)
class Settings:
    app_key: str
    app_secret: str
    account: str = ""
    product_code: str = "01"

    @classmethod
    def load(cls, path: Path):
        try:
            values = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raise KisError("config.local.toml을 만들고 모의투자 설정을 입력하세요.") from None
        except tomllib.TOMLDecodeError:
            raise KisError("설정 파일의 TOML 형식을 확인하세요.") from None
        settings = cls(**{key: values.get(key, default) for key, default in (
            ("app_key", ""), ("app_secret", ""), ("account", ""), ("product_code", "01")
        )})
        if not all(isinstance(value, str) for value in vars(settings).values()):
            raise KisError("설정값은 따옴표로 감싼 문자열이어야 합니다.")
        if not settings.app_key.strip() or not settings.app_secret.strip():
            raise KisError("app_key와 app_secret에 모의투자 키를 입력하세요.")
        return settings

    def validate_account(self):
        if not re.fullmatch(r"[0-9]{8}", self.account):
            raise KisError("account에 모의 계좌 앞 8자리를 입력하세요.")
        if not re.fullmatch(r"[0-9]{2}", self.product_code):
            raise KisError("product_code에 계좌 뒤 2자리를 입력하세요.")


@dataclass(repr=False)
class PaperClient:
    settings: Settings
    cache_path: Path = ROOT / ".local" / "kis-token.json"
    _next_request: float = field(default=0, init=False)

    def _request(self, path, *, headers=None, params=None, body=None):
        # 모의 서버로만 연결하며, 서버 응답 본문이나 인증 헤더를 오류에 출력하지 않는다.
        time.sleep(max(0, self._next_request - time.monotonic()))
        self._next_request = time.monotonic() + 1
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
        identity = hashlib.sha256(
            (self.settings.app_key + "\0" + self.settings.app_secret).encode()
        ).hexdigest()
        try:
            saved = json.loads(self.cache_path.read_text(encoding="utf-8"))
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
        if not re.fullmatch(r"[0-9]{6}", symbol):
            raise KisError("종목코드는 숫자 6자리로 입력하세요.")
        data, _ = self._get("/uapi/domestic-stock/v1/quotations/inquire-price",
                            "FHKST01010100", {
                                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
                            })
        output = data.get("output")
        if not isinstance(output, dict) or not output.get("stck_prpr"):
            raise KisError("현재가 응답이 없습니다. 종목과 서비스 지원 여부를 확인하세요.")
        return {"environment": "paper", "symbol": symbol, "market": "KRX",
                "price": output["stck_prpr"], "change_percent": output.get("prdy_ctrt")}

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
                "pdno", "prdt_name", "hldg_qty", "pchs_avg_pric", "prpr", "evlu_amt"
            )} for row in rows)
            if headers.get("tr_cont") not in ("M", "F"):
                return {"environment": "paper", "holdings": holdings,
                        "summary": {key: summary[0].get(key) for key in (
                            "dnca_tot_amt", "scts_evlu_amt", "tot_evlu_amt"
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="KIS 일반 모의투자 조회")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="로컬 설정 형식 확인")
    commands.add_parser("auth", help="인증 확인")
    commands.add_parser("quote", help="현재가 조회").add_argument("symbol")
    commands.add_parser("balance", help="잔고 조회")
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(ROOT / "config.local.toml")
        client = PaperClient(settings)
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
