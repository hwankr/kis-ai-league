"""KIS 마스터의 거래 제한 상태. 경고·주의환기 제외는 앱의 보수적 선별 정책이다.

마스터의 다운로드 시각은 상태의 효력 거래일을 증명하지 않는다. 감리와 대회
제한의 완전한 대응은 미확인이고, 현재가·장중 상태는 별도 조회가 필요하다.
"""

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from zipfile import ZipFile

from backend.kis import KST, ROOT
from backend.request_gate import file_lock
from backend.symbol_names import MASTER_URL, MAX_DOWNLOAD, MAX_MASTER, _download

MASTER_SCHEMA_URLS = (
    "https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/종목마스터정보(코스피).h",
    "https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/종목마스터정보(코스닥).h",
)
SOURCE_URLS = [MASTER_URL.format(board) for board in ("kospi", "kosdaq")]
CACHE_AGE = timedelta(hours=6)
MAX_CACHE = 12 * 1024 * 1024
WARNING_CODES = ("00", "01", "02", "03")
PREFERRED_CODES = ("0", "1", "2")
BOOL_FIELDS = ("halted", "liquidation", "managed", "low_liquidity", "investment_caution")
# 공식 구조체의 0부터 시작하는 바이트 위치. 이름은 CP949 40바이트다.
LAYOUTS = {
    "kospi": (288, {"low_liquidity": (77, 1), "halted": (121, 1),
                    "liquidation": (122, 1), "managed": (123, 1),
                    "warning_code": (124, 2), "preferred_code": (219, 1)}),
    "kosdaq": (282, {"low_liquidity": (77, 1), "halted": (116, 1),
                     "liquidation": (117, 1), "managed": (118, 1),
                     "warning_code": (119, 2), "preferred_code": (214, 1),
                     "investment_caution": (91, 1)}),
}


def _parse_master(payload, board):
    if not isinstance(payload, bytes) or len(payload) > MAX_DOWNLOAD:
        raise ValueError("종목 마스터 크기 오류")
    size, layout = LAYOUTS[board]
    with ZipFile(io.BytesIO(payload)) as archive:
        expected = f"{board}_code.mst"
        if archive.namelist() != [expected]:
            raise ValueError("종목 마스터 압축 구조 변경")
        entry = archive.getinfo(expected)
        if entry.file_size > MAX_MASTER:
            raise ValueError("종목 마스터 크기 초과")
        with archive.open(entry) as stream:
            data = stream.read(MAX_MASTER + 1)
    if len(data) > MAX_MASTER:
        raise ValueError("종목 마스터 크기 초과")
    result = {}
    for raw in data.splitlines():
        if len(raw) != size or any(byte < 32 or byte > 126 for byte in raw[:21] + raw[61:]):
            raise ValueError("종목 마스터 형식 변경")
        symbol = raw[:9].decode("ascii").strip()
        standard = raw[9:21].decode("ascii")
        name = raw[21:61].decode("cp949").strip()
        # ETN 등 주식 외 공개 마스터 코드도 구조 검증 후 보존한다.
        if (not re.fullmatch(r"[0-9A-Z]{6,9}", symbol)
                or not re.fullmatch(r"[0-9A-Z]{12}", standard)
                or not name or not name.isprintable() or symbol in result):
            raise ValueError("종목 마스터 식별값 오류")
        row = {"board": board.upper(), **dict.fromkeys(BOOL_FIELDS),
               "warning_code": None, "preferred_code": None}
        for field, (offset, width) in layout.items():
            value = raw[offset:offset + width].decode("ascii").strip()
            row[field] = {"Y": True, "N": False}.get(value) if field in BOOL_FIELDS else value or None
        result[symbol] = row
    if not result:
        raise ValueError("종목 마스터가 비어 있음")
    return result


def assess_master(row, expected_board):
    """미정의 코드는 통과시키지 않는다. 확인된 제외 사유가 불확실성보다 우선한다."""
    if not isinstance(row, dict):
        return {"status": "unknown", "reasons": ["master_row_missing"]}
    if expected_board not in ("KOSPI", "KOSDAQ") or row.get("board") != expected_board:
        return {"status": "unknown", "reasons": ["board_mismatch"]}
    excluded, unknown = [], []
    for field in BOOL_FIELDS:
        if field == "investment_caution" and expected_board == "KOSPI":
            continue  # 이 필드는 코스닥 마스터에만 있다.
        value = row.get(field)
        if value is True:
            excluded.append(field)
        elif value is not False:
            unknown.append("unknown_" + field)
    warning = row.get("warning_code")
    if warning not in WARNING_CODES:
        unknown.append("unknown_warning_code")
    elif warning != "00":
        excluded.append("market_warning")
    preferred = row.get("preferred_code")
    if preferred not in PREFERRED_CODES:
        unknown.append("unknown_preferred_code")
    elif preferred != "0":
        excluded.append("preferred_share")
    return {"status": "excluded" if excluded else "unknown" if unknown else "pass",
            "reasons": excluded + unknown}


class EligibilityService:
    def __init__(self, directory=ROOT / ".local" / "eligibility", *, now=None, fetcher=None):
        self.directory = Path(directory)
        self.now = now or (lambda: datetime.now(KST))
        self.fetcher = fetcher or _download
        self.lock = threading.Lock()
        self.previous = None

    def _parse_payload(self, payload):
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or set(payload.get("masters", {})) != set(LAYOUTS)):
            raise ValueError("저장된 종목 마스터 형식 오류")
        observed = datetime.fromisoformat(payload["observed_at"])
        if observed.tzinfo is None or observed > self.now():
            raise ValueError("저장된 종목 마스터 시각 오류")
        rows = {}
        for board in LAYOUTS:
            raw = base64.b64decode(payload["masters"][board], validate=True)
            current = _parse_master(raw, board)
            if rows.keys() & current.keys():
                raise ValueError("시장 간 종목 코드 중복")
            rows.update(current)
        return {"status": "ok", "observed_at": observed.isoformat(),
                "source_urls": list(SOURCE_URLS), "rows": rows, "error": None, "stale": False}

    def _load(self):
        try:
            with (self.directory / "masters.json").open("rb") as stream:
                raw = stream.read(MAX_CACHE + 1)
            if len(raw) > MAX_CACHE:
                return None
            return self._parse_payload(json.loads(raw))
        except Exception:
            return None

    def _fresh(self, snapshot):
        if snapshot is None:
            return False
        age = self.now() - datetime.fromisoformat(snapshot["observed_at"])
        return timedelta(0) <= age < CACHE_AGE

    def _save(self, payload):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix="masters.", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.directory / "masters.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def master_snapshot(self):
        """명시 호출에서만 갱신한다. 두 시장이 모두 검증되어야 새 결과를 게시한다."""
        with self.lock:
            previous = self._load() or self.previous
            if self._fresh(previous):
                self.previous = previous
                return deepcopy(previous)
            try:
                with file_lock(self.directory / "master.lock"):
                    saved = self._load()
                    if self._fresh(saved):
                        self.previous = saved
                        return deepcopy(saved)
                    if saved is not None:
                        previous = saved
                    masters = {board: base64.b64encode(self.fetcher(MASTER_URL.format(board))).decode("ascii")
                               for board in LAYOUTS}
                    payload = {"version": 1, "observed_at": self.now().astimezone(timezone.utc).isoformat(),
                               "masters": masters}
                    result = self._parse_payload(payload)
                    self._save(payload)
                    self.previous = result
                    return deepcopy(result)
            except Exception:
                result = deepcopy(previous) if previous else {"observed_at": None,
                    "source_urls": list(SOURCE_URLS), "rows": {}}
                result.update(status="error", stale=bool(result["rows"]),
                              error="거래 제한 종목 마스터를 갱신하지 못했습니다.")
                return result
