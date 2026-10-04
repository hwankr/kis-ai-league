"""검증된 일봉의 과거 창 캐시. 겹친 값이 바뀌면 이전 수정주가를 전부 폐기한다.

index의 개수 모드는 실제 응답 거래일만 모은다. stock의 날짜 목록 모드는
그 지수 달력 밖의 관측을 사용하지 않으며, 짧은 이력을 보간하지 않는다.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import tempfile

from backend.candidate_features import _number, _stock_row
from backend.kis import KisError, ROOT
from backend.request_gate import file_lock

MAX_BYTES = 3 * 1024 * 1024
MAX_ROWS = 1000
MAX_PAGES = 16
PAGE_ROWS = {"stock": 100, "index": 50}
STOCK_FIELDS = ("open", "high", "low", "close", "volume", "turnover")


def _request(kind, symbol):
    pattern = r"[0-9A-Z]{6}" if kind == "stock" else r"[0-9]{4}"
    if not isinstance(kind, str) or kind not in PAGE_ROWS or not isinstance(symbol, str) or not re.fullmatch(pattern, symbol):
        raise KisError("과거 일봉의 종류·종목코드가 올바르지 않습니다.")


def _parsed(rows, kind, *, keep=None):
    if not isinstance(rows, dict) or len(rows) > MAX_ROWS or any(type(day) is not date for day in rows):
        raise KisError("과거 일봉의 날짜·행 수가 올바르지 않습니다.")
    return {day: _stock_row(value) if kind == "stock" else _number(value, positive=True)
            for day, value in rows.items() if keep is None or keep(day)}


def _decimal(value):
    if (not isinstance(value, str) or len(value) > 128
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)):
        raise KisError("저장된 과거 일봉의 수치가 올바르지 않습니다.")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise KisError("저장된 과거 일봉의 수치가 올바르지 않습니다.") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("중복 캐시 키")
        result[key] = value
    return result


class HistoryCache:
    def __init__(self, directory=ROOT / ".local" / "candidate-history"):
        self.directory = Path(directory)

    def _path(self, kind, symbol):
        _request(kind, symbol)
        return self.directory / f"{kind}-{symbol}.json"

    def _read(self, path, kind, symbol):
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            payload = json.loads(raw, object_pairs_hook=_unique_object)
            if (not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1
                    or payload.get("kind") != kind or payload.get("symbol") != symbol
                    or not isinstance(payload.get("rows"), dict) or len(payload["rows"]) > MAX_ROWS):
                return None
            rows = {}
            for text, row in payload["rows"].items():
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
                    return None
                day = date.fromisoformat(text)
                if kind == "stock":
                    if not isinstance(row, dict) or set(row) != set(STOCK_FIELDS):
                        return None
                    rows[day] = {field: _decimal(row[field]) for field in STOCK_FIELDS}
                else:
                    rows[day] = _decimal(row)
            return _parsed(rows, kind)
        except (OSError, ValueError, TypeError, KisError):
            return None

    def _write(self, path, kind, symbol, rows):
        encoded = {day.isoformat(): ({field: format(row[field], "f") for field in STOCK_FIELDS}
                                    if kind == "stock" else format(row, "f"))
                   for day, row in sorted(rows.items())}
        # 캐시와 입력값 모두 동일한 양의 가격·비음수 거래량·대금 제한을 따른다.
        values = (value for row in encoded.values() for value in row.values()) if kind == "stock" else encoded.values()
        if any(len(value) > 128 for value in values):
            raise KisError("과거 일봉 수치의 저장 한도를 초과했습니다.")
        payload = json.dumps({"version": 1, "kind": kind, "symbol": symbol, "rows": encoded},
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise KisError("과거 일봉 캐시 크기 한도를 초과했습니다.")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix=path.name + ".", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def put(self, kind, symbol, rows):
        """출처에서 검증한 parsed 이력을 같은 검증·원자 저장 절차로 가져온다."""
        path = self._path(kind, symbol)
        parsed = _parsed(rows, kind)
        if not parsed:
            raise KisError("가져올 과거 일봉이 없습니다.")
        with file_lock(path.with_suffix(".lock")):
            self._write(path, kind, symbol, parsed)

    def extend(self, kind, symbol, latest, required_dates, fetch_before):
        """fetch_before(earliest)는 경계일을 포함한 이전 페이지를 반환해야 한다.

        required_dates는 stock/index의 검증된 날짜 목록, 또는 index의 필요 행 수다.
        전자의 미래 기준은 목록 끝, 후자의 기준은 최신 API 응답의 마지막 거래일이다.
        """
        path = self._path(kind, symbol)
        count = required_dates if type(required_dates) is int else None
        if count is not None:
            if kind != "index" or not 1 <= count <= MAX_ROWS:
                raise KisError("지수 과거 일봉 개수가 올바르지 않습니다.")
            initial = _parsed(latest, kind)
            if not initial:
                return {}
            as_of = max(initial)
            required = None
            keep = lambda day: day <= as_of
        else:
            if (not isinstance(required_dates, (list, tuple)) or len(required_dates) > MAX_ROWS
                    or any(type(day) is not date for day in required_dates)
                    or list(required_dates) != sorted(set(required_dates))):
                raise KisError("과거 일봉 조회의 거래일 구성이 올바르지 않습니다.")
            if not required_dates:
                return {}
            required = set(required_dates)
            as_of = required_dates[-1]
            keep = lambda day: day in required
            initial = _parsed(latest, kind, keep=keep)
            if not initial:
                return {}

        def complete(rows):
            return len(rows) >= count if count is not None else required.issubset(rows)

        with file_lock(path.with_suffix(".lock")):
            old = self._read(path, kind, symbol)
            old = {day: row for day, row in (old or {}).items() if keep(day)}
            overlap = old.keys() & initial.keys()
            # 한 겹친 관측이라도 바뀌면 배율을 추정하지 않고 현재 API에서 다시 받는다.
            merged = {**old, **initial} if overlap and all(old[day] == initial[day] for day in overlap) else dict(initial)
            for _ in range(MAX_PAGES):
                if complete(merged):
                    break
                boundary = min(merged)
                if required is not None and not any(day < boundary for day in required - merged.keys()):
                    break  # 중간 결측은 오래된 행으로 대체할 수 없다.
                raw = fetch_before(boundary)
                if not isinstance(raw, dict) or len(raw) > PAGE_ROWS[kind]:
                    raise KisError("과거 일봉 페이지 형식·행 수가 올바르지 않습니다.")
                if not raw:
                    break
                page = _parsed(raw, kind, keep=lambda day: keep(day) and day <= boundary)
                if boundary not in page:
                    raise KisError("과거 일봉 페이지에 비교 경계일이 없습니다.")
                common = merged.keys() & page.keys()
                if any(merged[day] != page[day] for day in common):
                    raise KisError("과거 일봉의 경계 값이 다릅니다. 수정주가·지수 이력을 다시 확인하세요.")
                additions = page.keys() - merged.keys()
                if not additions:
                    break
                merged.update(page)
                if len(merged) > MAX_ROWS:
                    raise KisError("과거 일봉 캐시 행 수 한도를 초과했습니다.")
            else:
                if not complete(merged):
                    raise KisError("과거 일봉 연속조회 한도를 초과했습니다.")
            if count is not None:
                merged = {day: merged[day] for day in sorted(merged)[-count:]}
            else:
                merged = {day: merged[day] for day in sorted(merged) if day <= as_of and day in required}
            self._write(path, kind, symbol, merged)
            return merged
