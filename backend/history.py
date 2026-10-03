"""정규화된 계좌 관측값을 로컬 SQLite에 보관한다."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4


MAX_HISTORY_POINTS = 2000


@dataclass(frozen=True)
class HistorySeries:
    profile_id: str
    environment: str
    fingerprint: str


def series_for_profile(profile, environment="paper"):
    settings = profile.settings
    private_identity = json.dumps([
        settings.app_key, settings.app_secret, settings.account, settings.product_code,
    ], ensure_ascii=False, separators=(",", ":"))
    return HistorySeries(profile.id, environment,
                         hashlib.sha256(private_identity.encode("utf-8")).hexdigest())


class AccountHistory:
    def __init__(self, path):
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._initialized = False

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with self._schema_lock:
                if not self._initialized:
                    with connection:
                        connection.executescript("""
                            CREATE TABLE IF NOT EXISTS observations (
                                id INTEGER PRIMARY KEY,
                                observation_id TEXT NOT NULL UNIQUE,
                                profile_id TEXT NOT NULL,
                                environment TEXT NOT NULL,
                                fingerprint TEXT NOT NULL,
                                observed_at TEXT NOT NULL,
                                total_value TEXT,
                                cash TEXT,
                                snapshot_json TEXT NOT NULL
                            );
                            CREATE INDEX IF NOT EXISTS observations_series_time
                            ON observations(profile_id, environment, fingerprint,
                                            observed_at, id);
                        """)
                    self._initialized = True
            return connection
        except Exception:
            connection.close()
            raise

    def record(self, series, observed_at, snapshot, observation_id=None):
        timestamp = datetime.fromisoformat(observed_at)
        if timestamp.utcoffset() is None:
            raise ValueError("Observation timestamp must include its timezone")
        observed_at = timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
        # 호출자는 원본 KIS 응답 대신 정규화된 수치·보유 종목만 전달한다.
        payload = json.dumps({"summary": snapshot["summary"], "holdings": snapshot["holdings"]},
                             ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with closing(self._connect()) as connection, connection:
            connection.execute("""
                INSERT INTO observations
                    (observation_id, profile_id, environment, fingerprint, observed_at,
                     total_value, cash, snapshot_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO NOTHING
            """, (observation_id or uuid4().hex, series.profile_id, series.environment,
                  series.fingerprint, observed_at, snapshot["summary"].get("total_value"),
                  snapshot["summary"].get("cash"), payload))

    def read(self, series):
        scope = (series.profile_id, series.environment, series.fingerprint)
        with closing(self._connect()) as connection, connection:
            # 총수와 점 목록이 같은 SQLite 읽기 스냅샷에 속하도록 한다.
            connection.execute("BEGIN")
            total_count = connection.execute("""
                SELECT COUNT(*) FROM observations
                WHERE profile_id = ? AND environment = ? AND fingerprint = ?
            """, scope).fetchone()[0]
            rows = connection.execute("""
                SELECT observed_at, total_value, cash FROM observations
                WHERE profile_id = ? AND environment = ? AND fingerprint = ?
                ORDER BY observed_at DESC, id DESC LIMIT ?
            """, (*scope, MAX_HISTORY_POINTS)).fetchall()
        return {"points": [dict(zip(("observed_at", "total_value", "cash"), row))
                           for row in reversed(rows)], "total_count": total_count}
