"""KIS 공개 종목 마스터의 이름 캐시. 시세·계좌 API는 호출하지 않는다."""

import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.request import urlopen
from zipfile import ZipFile

MASTER_URL = "https://new.real.download.dws.co.kr/common/master/{}_code.mst.zip"
MASTERS = (("kospi", 288), ("kosdaq", 282))
MAX_DOWNLOAD = 4 * 1024 * 1024
MAX_MASTER = 8 * 1024 * 1024
MAX_CACHE = 4 * 1024 * 1024
CACHE_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 60 * 60


def _download(url):
    # 기본 HTTPS 컨텍스트의 인증서 검증을 유지한다.
    with urlopen(url, timeout=15) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("종목 마스터 다운로드 크기 초과")
    return data


def _valid_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and value == value.strip() and all(character.isprintable() for character in value))


def _parse_master(payload, board, row_size):
    if len(payload) > MAX_DOWNLOAD:
        raise ValueError("종목 마스터 다운로드 크기 초과")
    with ZipFile(io.BytesIO(payload)) as archive:
        entry = archive.getinfo(f"{board}_code.mst")
        if entry.file_size > MAX_MASTER:
            raise ValueError("종목 마스터 파일 크기 초과")
        with archive.open(entry) as stream:
            data = stream.read(MAX_MASTER + 1)
    if len(data) > MAX_MASTER:
        raise ValueError("종목 마스터 파일 크기 초과")
    names = {}
    for row in data.splitlines():
        # 공식 파일: 코드 9 + ISIN 12 + 한글명 40바이트 + 시장별 고정 필드.
        if len(row) != row_size:
            raise ValueError("종목 마스터 형식 변경")
        symbol = row[:9].decode("ascii").strip()
        name = row[21:61].decode("cp949").strip()
        if re.fullmatch(r"[0-9]{6}", symbol):
            if not _valid_name(name) or symbol in names:
                raise ValueError("종목 마스터 이름 오류")
            names[symbol] = name
    if not names:
        raise ValueError("종목 마스터가 비어 있음")
    return names


class SymbolNames:
    def __init__(self, path, *, fetcher=None, now=None):
        self.path = Path(path)
        self._fetcher = fetcher or _download
        self._now = now or time.time
        self._lock = threading.Lock()
        self._names = {}
        self._next_attempt = 0
        self._refreshing = False
        self._load()

    def _load(self):
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_CACHE + 1)
            if len(raw) > MAX_CACHE:
                return
            cached = json.loads(raw)
            names, updated = cached["names"], cached["updated_at"]
            if (not isinstance(names, dict) or not names
                    or any(not re.fullmatch(r"[0-9]{6}", code) or not _valid_name(name)
                           for code, name in names.items())
                    or type(updated) not in (int, float) or not math.isfinite(updated)):
                return
            self._names = names
            # 잘못된 미래 시각의 캐시도 이름은 보존하되 다시 갱신한다.
            self._next_attempt = updated + CACHE_SECONDS if updated <= self._now() + 300 else 0
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def lookup(self, symbols):
        """메모리 캐시를 즉시 반환하고, 필요한 갱신은 백그라운드에서 한다."""
        with self._lock:
            result = {symbol: self._names[symbol] for symbol in symbols if symbol in self._names}
            if not self._refreshing and self._now() >= self._next_attempt:
                self._refreshing = True
                threading.Thread(target=self._update, name="symbol-names", daemon=True).start()
        return result

    def refresh(self):
        """명시적 동기 갱신. 실패하면 기존 캐시를 유지하고 False를 반환한다."""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
        return self._update()

    def _update(self):
        succeeded = False
        temporary = None
        try:
            names = {}
            for board, row_size in MASTERS:
                current = _parse_master(self._fetcher(MASTER_URL.format(board)), board, row_size)
                if names.keys() & current.keys():
                    raise ValueError("중복 종목 코드")
                names.update(current)
            updated = self._now()
            content = json.dumps({"updated_at": updated, "names": names}, ensure_ascii=False).encode("utf-8")
            if len(content) > MAX_CACHE:
                raise ValueError("종목 이름 캐시 크기 초과")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=self.path.name + ".", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            with self._lock:
                self._names = names
                self._next_attempt = updated + CACHE_SECONDS
            succeeded = True
            return True
        except Exception:
            # 이름 다운로드/형식/디스크 오류는 시세 수집과 대시보드를 중단하지 않는다.
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            with self._lock:
                if not succeeded:
                    self._next_attempt = self._now() + RETRY_SECONDS
                self._refreshing = False
