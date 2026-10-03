"""계좌 지문별 차트 캐시. 정상 결과만 원자적으로 저장하고 최대 32개를 유지한다."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile

from backend.request_gate import file_lock

MAX_FILES = 32
MAX_BYTES = 256 * 1024
VERSION = 1


class DiskChartCache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("Invalid chart cache identity")
        return self.directory / f"{identity}.json"

    def read(self, identity):
        try:
            path = self._path(identity)
            if path.stat().st_size > MAX_BYTES:
                return None
            with path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("version") != VERSION
                    or value.get("identity") != identity):
                return None
            return value
        except (OSError, ValueError, UnicodeError):
            return None

    def write(self, identity, data):
        value = {**data, "version": VERSION, "identity": identity}
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("Chart cache is too large")
        destination = self._path(identity)
        self.directory.mkdir(parents=True, exist_ok=True)
        with file_lock(self.directory / "cache.lock"):
            previous = self.read(identity)
            if previous:
                try:
                    # 별도 대시보드 프로세스의 더 최근 관측값을 덮어쓰지 않는다.
                    if datetime.fromisoformat(previous["observed_at"]) > datetime.fromisoformat(data["observed_at"]):
                        return
                except (KeyError, ValueError, TypeError):
                    pass
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".chart-", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            files = [path for path in self.directory.glob("*.json")
                     if re.fullmatch(r"[0-9a-f]{64}\.json", path.name)]
            files.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
            for path in files[MAX_FILES:]:
                path.unlink(missing_ok=True)
