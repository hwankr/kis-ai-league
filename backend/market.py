"""시세 수집 설정과 대시보드용 로컬 저장값 조회."""

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from backend.kis import KisError, ROOT
from backend.market_history import MarketHistory
from backend.symbol_names import SymbolNames

MARKET_DATABASE = ROOT / ".local" / "market-history.sqlite3"


@dataclass(frozen=True)
class MarketSettings:
    symbols: tuple[str, ...] = ("005930",)
    interval_seconds: int = 60
    account: str | None = None


def load_market_settings(path):
    try:
        values = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise KisError("시세 수집 설정을 읽지 못했습니다. config.local.toml을 확인하세요.") from None
    settings = values.get("market_data", {})
    if not isinstance(settings, dict) or set(settings) - {"symbols", "interval_seconds", "account"}:
        raise KisError("market_data의 symbols·interval_seconds·account 설정을 확인하세요.")
    symbols = settings.get("symbols", ["005930"])
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 20
            or any(not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol) for symbol in symbols)
            or len(set(symbols)) != len(symbols)):
        raise KisError("수집 종목은 중복 없는 숫자 6자리 코드 1~20개로 입력하세요.")
    interval = settings.get("interval_seconds", 60)
    if type(interval) is not int or not 10 <= interval <= 3600:
        raise KisError("수집 주기는 10~3600초의 정수로 입력하세요.")
    account = settings.get("account")
    if account is not None and (not isinstance(account, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", account)):
        raise KisError("시세 수집 account에는 등록된 계좌 ID를 입력하세요.")
    return MarketSettings(tuple(symbols), interval, account)


class MarketService:
    def __init__(self, config_path=ROOT / "config.local.toml", store=None, database=MARKET_DATABASE,
                 symbol_names=None):
        self.config_path = config_path
        self.store = store if store is not None else MarketHistory(database)
        self.symbol_names = symbol_names if symbol_names is not None else SymbolNames(
            Path(database).parent / "stock-names.json")

    def snapshot(self):
        empty = {"status": "error", "error": None, "symbols": [], "quotes": [],
                 "collector": {"state": "not_started", "heartbeat_at": None,
                               "next_run_at": None, "interval_seconds": 60, "error": None}}
        try:
            settings = load_market_settings(self.config_path)
        except KisError as error:
            return {**empty, "error": str(error)}
        try:
            saved = self.store.read(settings.symbols)
            collector = self.store.get_collector()
            try:
                names = self.symbol_names.lookup(settings.symbols)
            except Exception:
                names = {}  # 이름 목록 장애가 저장된 시세 조회를 막지 않는다.
            quotes = [{**quote, **({"name": names[quote["symbol"]]} if quote["symbol"] in names else {})}
                      for quote in saved["quotes"]]
            # 프로세스 ID·제어 토큰은 HTTP 응답에 포함하지 않는다.
            public = {key: collector.get(key) for key in (
                "state", "heartbeat_at", "next_run_at", "interval_seconds", "error")}
            public["interval_seconds"] = settings.interval_seconds
            if (collector.get("state") == "running" and
                    (tuple(collector.get("symbols", [])) != settings.symbols
                     or collector.get("interval_seconds") != settings.interval_seconds)):
                public["error"] = "변경된 수집 설정 적용 대기 중"
            return {"status": "ok", "error": None, "symbols": list(settings.symbols),
                    "quotes": quotes, "collector": public}
        except Exception:
            return {**empty, "symbols": list(settings.symbols),
                    "error": "시세 기록을 읽지 못했습니다. 로컬 저장 파일을 확인하세요."}
