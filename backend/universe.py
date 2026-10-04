"""출처를 확인해 저장한 대회 대상 종목군. 현재 지수 구성종목으로 대체하지 않는다."""

from collections import Counter
from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

AS_OF = "2026-09-21"
RULES_URL = "https://vts3.koreainvestment.com/vts/#/univ/rule/1"
KRX_URL = "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC02010301"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "competition-universe.json"
MAX_BYTES = 256 * 1024
MAX_SYMBOLS = 500
INDEX_NAMES = {"KOSPI": "코스피 200", "KOSDAQ": "코스닥 150"}
SOURCE_HOSTS = {
    "official_competition_list": {"vts3.koreainvestment.com", "www.truefriend.com", "securities.koreainvestment.com"},
    "official_index_constituents": {"data.krx.co.kr", "index.krx.co.kr"},
}


def _unverified(error):
    return {
        "status": "unverified", "as_of": AS_OF, "checked_at": None,
        "source_url": RULES_URL, "source_kind": None,
        "count": 0, "rows": [], "error": error,
    }


def _source_url(value, kind):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("공식 원자료 주소가 필요합니다.")
    url = urlsplit(value)
    if (url.scheme != "https" or url.hostname not in SOURCE_HOSTS[kind]
            or url.username or url.password or url.port not in (None, 443)):
        raise ValueError("공식 HTTPS 원자료 주소를 확인하세요.")
    return value


def _index_sources(sources, counts):
    if not isinstance(sources, list) or len(sources) != 2:
        raise ValueError("KRX 두 지수 원본의 검증 기록이 필요합니다.")
    normalized, seen = [], set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("KRX 원본 검증 기록 형식이 잘못되었습니다.")
        board = source.get("board")
        if not isinstance(board, str) or board not in INDEX_NAMES or board in seen:
            raise ValueError("KRX 원본에 두 시장을 각각 기록해야 합니다.")
        if source.get("index_name") != INDEX_NAMES[board] or source.get("as_of") != AS_OF:
            raise ValueError("KRX 원본의 지수명·기준일을 확인하세요.")
        row_count, filename, digest = source.get("row_count"), source.get("filename"), source.get("sha256")
        if type(row_count) is not int or not 1 <= row_count <= MAX_SYMBOLS or row_count != counts[board]:
            raise ValueError("KRX 원본 행 수와 대상 종목 수가 일치하지 않습니다.")
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}\.csv", filename):
            raise ValueError("KRX 원본 CSV 파일명이 잘못되었습니다.")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("KRX 원본 SHA256 검증 기록이 필요합니다.")
        normalized.append({"board": board, "index_name": INDEX_NAMES[board], "as_of": AS_OF,
                           "filename": filename, "row_count": row_count, "sha256": digest,
                           "source_url": _source_url(source.get("source_url"), "official_index_constituents")})
        seen.add(board)
    return normalized


def _validate(data):
    if not isinstance(data, dict) or data.get("status") != "verified":
        raise ValueError("출처 확인을 마친 전체 목록이 필요합니다.")
    if data.get("as_of") != AS_OF:
        raise ValueError(f"대상 종목 기준일은 {AS_OF}이어야 합니다.")
    source_kind = data.get("source_kind")
    if not isinstance(source_kind, str) or source_kind not in SOURCE_HOSTS:
        raise ValueError("주최측 목록 또는 KRX 기준일 구성종목 출처가 필요합니다.")
    source_url = _source_url(data.get("source_url"), source_kind)
    checked_at = data.get("checked_at")
    if not isinstance(checked_at, str):
        raise ValueError("원자료 확인 시각이 필요합니다.")
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError as exc:
        raise ValueError("원자료 확인 시각 형식이 잘못되었습니다.") from exc
    if checked.tzinfo is None or checked.date().isoformat() < AS_OF:
        raise ValueError("원자료 확인 시각에는 기준일 이후 날짜와 시간대가 필요합니다.")
    rows = data.get("rows")
    if (not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SYMBOLS
            or type(data.get("count")) is not int or data["count"] != len(rows)):
        raise ValueError("전체 종목 수와 목록 길이가 일치하지 않습니다.")
    seen, normalized = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("종목 목록 형식이 잘못되었습니다.")
        symbol, name, board = row.get("symbol"), row.get("name"), row.get("board")
        if not isinstance(symbol, str) or not re.fullmatch(r"[0-9A-Z]{6}", symbol):
            raise ValueError("종목코드는 6자리 숫자·영문 대문자 문자열이어야 합니다.")
        if symbol in seen:
            raise ValueError("종목코드가 중복되었습니다.")
        if (not isinstance(name, str) or not 1 <= len(name) <= 128
                or name != name.strip() or not name.isprintable()):
            raise ValueError("종목명이 비어 있거나 잘못되었습니다.")
        if board not in ("KOSPI", "KOSDAQ"):
            raise ValueError("시장 구분은 KOSPI 또는 KOSDAQ이어야 합니다.")
        seen.add(symbol)
        normalized.append({"symbol": symbol, "name": name, "board": board})
    sources = None
    if source_kind == "official_index_constituents":
        sources = _index_sources(data.get("sources"), Counter(row["board"] for row in normalized))
        provenance = "2026-09-21 KRX 구성종목 기준 대상 종목군. 당일 거래 제한 여부는 별도입니다."
    else:
        provenance = "주최측 기준일 전체 목록. 당일 거래 제한 여부는 별도입니다."
    return {
        "status": "verified", "as_of": AS_OF, "checked_at": checked_at,
        "source_url": source_url, "source_kind": source_kind,
        "count": len(normalized), "rows": normalized, "error": None,
        "provenance": provenance,
        **({"sources": sources} if sources is not None else {}),
    }


def load_universe(path=None):
    """검증 기록이 있는 고정 스냅샷을 읽고 구조를 검사한다. 네트워크 조회는 하지 않는다.

    verified는 원자료 확인 기록이며 URL 형식 검사만으로 출처를 인증하지 않는다.
    주최측 전체 목록 또는 두 KRX 기준일 목록을 확인한 뒤에만 파일을 작성한다.
    """
    source = Path(path) if path is not None else DEFAULT_PATH
    try:
        with source.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return _unverified("대상 종목 파일 크기가 제한을 초과했습니다.")
        return _validate(json.loads(raw))
    except FileNotFoundError:
        return _unverified("2026-09-21 기준 대회 전체 종목 목록이 필요합니다.")
    except (OSError, ValueError, TypeError, RecursionError):
        # JSON/경로의 원문과 로컬 파일 위치는 API 응답에 노출하지 않는다.
        return _unverified("대상 종목 파일의 기준일·출처·전체 종목 수·형식을 확인하세요.")


def import_universe(source, destination=None):
    """확인한 JSON 원자료를 검증 후 저장한다. 잘못된 입력은 기존 목록을 보존한다."""
    result = load_universe(source)
    if result["status"] != "verified":
        raise ValueError(result["error"])
    target = Path(destination) if destination is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, target)
        pending = None
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="출처를 확인한 대회 대상 종목 JSON 가져오기")
    parser.add_argument("command", choices=["import"])
    parser.add_argument("source", help="확인한 전체 목록 JSON 파일")
    parser.add_argument("--output", help="저장할 JSON 파일 (기본: config/competition-universe.json)")
    arguments = parser.parse_args(argv)
    try:
        result = import_universe(arguments.source, arguments.output)
    except (OSError, ValueError):
        print("가져오기 실패: 원자료의 기준일·출처·전체 종목 수·형식 또는 저장 경로를 확인하세요.")
        return 2
    print(f"{result['as_of']} 대상 종목 {result['count']}개 저장 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
