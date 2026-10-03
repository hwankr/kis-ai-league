"""동일 로컬 저장소의 KIS 클라이언트 간 요청·토큰 발급 직렬화."""

from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import threading
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_locks = {}
_locks_guard = threading.Lock()


def _thread_lock(path):
    key = str(path.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


@contextmanager
def file_lock(path: Path, *, blocking=True, timeout=60):
    """커널 잠금은 프로세스 종료 시 해제된다. 잠금 파일은 삭제하지 않는다."""
    path = Path(path)
    deadline = time.monotonic() + timeout
    local = _thread_lock(path)
    acquired = local.acquire(timeout=timeout) if blocking else local.acquire(blocking=False)
    if not acquired:
        if not blocking:
            raise BlockingIOError("Local request lock is busy")
        raise TimeoutError("Local request lock timed out")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # a+b는 초기 생성에만 사용한다. 이후 r+b로 고정 위치를 갱신한다.
        with path.open("ab") as initialize:
            if initialize.tell() == 0:
                initialize.write(b"\0")
        with path.open("r+b") as handle:
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        raise BlockingIOError("Local request lock is busy") from None
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Local request lock timed out") from None
                    time.sleep(0.05)
            try:
                yield handle
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        local.release()


@contextmanager
def request_spacing(path: Path, interval=1.0):
    """요청 완료 뒤 최소 간격. 실패한 요청도 같은 제한을 적용한다."""
    with file_lock(path) as handle:
        handle.seek(1)
        try:
            saved = handle.read(80).decode("ascii")
            next_request = float(saved) if saved else 0.0
            if not math.isfinite(next_request):
                raise ValueError
        except (ValueError, UnicodeError):
            # 손상된 상태에서도 한 간격 대기해 제한을 건너뛰지 않는다.
            next_request = time.time() + interval
        delay = max(0.0, min(interval, next_request - time.time()))
        if delay:
            time.sleep(delay)
        try:
            yield
        finally:
            handle.seek(1)
            handle.write(str(time.time() + interval).encode("ascii"))
            handle.truncate()
            handle.flush()
