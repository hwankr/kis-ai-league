"""계좌와 무관한 시세 관측값과 수집 상태를 로컬 SQLite에 보관한다."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
import threading
from uuid import uuid4


QUOTE_FIELDS = ("symbol", "price", "change_percent", "volume", "cumulative_turnover")


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (TypeError, ValueError):
        raise ValueError("Market timestamp must include its timezone") from None


def _symbol(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{6}", value) is None:
        raise ValueError("Market symbol must contain six digits")
    return value


def _source(environment, market):
    if environment not in ("paper", "live") or market not in ("KRX", "NXT"):
        raise ValueError("Unsupported market data source")
    return environment, market


def _error(value):
    # 호출자는 키·응답 본문 등이 없는 사용자용 오류만 전달한다.
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 300):
        raise ValueError("Market error must be a short sanitized message")
    return value


def _quote_values(quote):
    if not isinstance(quote, dict):
        raise ValueError("Quote must be normalized")
    _symbol(quote.get("symbol"))
    for field in ("price", "change_percent", "volume", "cumulative_turnover"):
        value = quote.get(field)
        try:
            if not isinstance(value, str) or len(value) > 64 or re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value) is None:
                raise ValueError
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError
            if field != "change_percent" and number < 0:
                raise ValueError
            if field == "price" and number == 0:
                raise ValueError
            if field == "volume" and number != number.to_integral_value():
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("Quote numeric values must be finite decimal strings") from None
    # 응답 전체·인증정보·추가 필드는 저장하지 않는다.
    return tuple(quote.get(field) for field in QUOTE_FIELDS)


class MarketHistory:
    def __init__(self, path):
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._initialized = False

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with self._schema_lock:
                if not self._initialized:
                    with connection:
                        connection.executescript("""
                            CREATE TABLE IF NOT EXISTS market_observations (
                                id INTEGER PRIMARY KEY,
                                observation_id TEXT NOT NULL,
                                environment TEXT NOT NULL,
                                market TEXT NOT NULL,
                                observed_at TEXT NOT NULL,
                                symbol TEXT NOT NULL,
                                price TEXT NOT NULL,
                                change_percent TEXT NOT NULL,
                                volume TEXT NOT NULL,
                                cumulative_turnover TEXT NOT NULL,
                                UNIQUE (environment, market, symbol, observation_id)
                            );
                            CREATE INDEX IF NOT EXISTS market_observations_source_time
                            ON market_observations(environment, market, symbol, observed_at, id);
                            CREATE TABLE IF NOT EXISTS market_attempts (
                                environment TEXT NOT NULL,
                                market TEXT NOT NULL,
                                symbol TEXT NOT NULL,
                                attempted_at TEXT NOT NULL,
                                error TEXT,
                                PRIMARY KEY (environment, market, symbol)
                            );
                            CREATE TABLE IF NOT EXISTS market_collector (
                                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                                collector_id TEXT NOT NULL,
                                state TEXT NOT NULL,
                                heartbeat_at TEXT NOT NULL,
                                next_run_at TEXT,
                                interval_seconds INTEGER NOT NULL,
                                symbols_json TEXT NOT NULL,
                                error TEXT
                            );
                        """)
                    self._initialized = True
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _save_attempt(connection, environment, market, symbol, attempted_at, error):
        connection.execute("""
            INSERT INTO market_attempts (environment, market, symbol, attempted_at, error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(environment, market, symbol) DO UPDATE SET
                attempted_at = excluded.attempted_at, error = excluded.error
            WHERE excluded.attempted_at >= market_attempts.attempted_at
        """, (environment, market, symbol, attempted_at, error))

    def record_quote(self, quote, observed_at, observation_id=None, *, environment=None, market=None):
        values = _quote_values(quote)
        environment, market = _source(environment or quote.get("environment", "paper"),
                                      market or quote.get("market", "KRX"))
        observed_at = _timestamp(observed_at)
        if observation_id is None:
            observation_id = uuid4().hex
        if not isinstance(observation_id, str) or not observation_id or len(observation_id) > 128:
            raise ValueError("Observation ID must be a short nonempty string")
        scope = (environment, market, values[0], observation_id)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("""
                SELECT observed_at, symbol, price, change_percent, volume, cumulative_turnover
                FROM market_observations
                WHERE environment = ? AND market = ? AND symbol = ? AND observation_id = ?
            """, scope).fetchone()
            if existing is not None:
                if existing != (observed_at, *values):
                    raise ValueError("Observation ID conflicts with a saved quote")
                return
            connection.execute("""
                INSERT INTO market_observations
                    (observation_id, environment, market, observed_at, symbol, price,
                     change_percent, volume, cumulative_turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (observation_id, environment, market, observed_at, *values))
            self._save_attempt(connection, environment, market, values[0], observed_at, None)

    def record_attempt(self, symbol, attempted_at, error, *, environment="paper", market="KRX"):
        environment, market = _source(environment, market)
        symbol, attempted_at, error = _symbol(symbol), _timestamp(attempted_at), _error(error)
        if error is None:
            raise ValueError("Successful collection must save a quote")
        with closing(self._connect()) as connection, connection:
            self._save_attempt(connection, environment, market, symbol, attempted_at, error)

    def read(self, symbols, *, environment="paper", market="KRX"):
        environment, market = _source(environment, market)
        symbols = list(dict.fromkeys(_symbol(symbol) for symbol in symbols))
        quotes = []
        with closing(self._connect()) as connection, connection:
            # 건수·마지막 관측·시도 상태를 같은 읽기 스냅샷으로 조회한다.
            connection.execute("BEGIN")
            for symbol in symbols:
                scope = (environment, market, symbol)
                count = connection.execute("""
                    SELECT COUNT(*) FROM market_observations
                    WHERE environment = ? AND market = ? AND symbol = ?
                """, scope).fetchone()[0]
                last = connection.execute("""
                    SELECT symbol, price, change_percent, volume, cumulative_turnover, observed_at
                    FROM market_observations
                    WHERE environment = ? AND market = ? AND symbol = ?
                    ORDER BY observed_at DESC, id DESC LIMIT 1
                """, scope).fetchone()
                attempt = connection.execute("""
                    SELECT attempted_at, error FROM market_attempts
                    WHERE environment = ? AND market = ? AND symbol = ?
                """, scope).fetchone()
                entry = dict(zip((*QUOTE_FIELDS, "observed_at"), last or
                                 (symbol, None, None, None, None, None)))
                entry.update(environment=environment, market=market, total_count=count,
                             last_attempt_at=attempt[0] if attempt else None,
                             error=attempt[1] if attempt else None)
                quotes.append(entry)
        return {"quotes": quotes, "total_count": sum(row["total_count"] for row in quotes)}

    def set_collector(self, state, heartbeat_at, next_run_at=None, *, collector_id,
                      interval_seconds, symbols, error=None):
        if state not in ("running", "stopped"):
            raise ValueError("Collector state must be running or stopped")
        if not isinstance(collector_id, str) or not collector_id or len(collector_id) > 128:
            raise ValueError("Collector ID must be a short string")
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 1:
            raise ValueError("Collector interval must be a positive integer")
        heartbeat_at = _timestamp(heartbeat_at)
        next_run_at = _timestamp(next_run_at) if next_run_at is not None else None
        symbols = list(dict.fromkeys(_symbol(symbol) for symbol in symbols))
        error = _error(error)
        with closing(self._connect()) as connection, connection:
            connection.execute("""
                INSERT INTO market_collector
                    (singleton, collector_id, state, heartbeat_at, next_run_at,
                     interval_seconds, symbols_json, error)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    collector_id = excluded.collector_id, state = excluded.state,
                    heartbeat_at = excluded.heartbeat_at, next_run_at = excluded.next_run_at,
                    interval_seconds = excluded.interval_seconds, symbols_json = excluded.symbols_json,
                    error = excluded.error
                WHERE excluded.heartbeat_at >= market_collector.heartbeat_at
            """, (collector_id, state, heartbeat_at, next_run_at, interval_seconds,
                  json.dumps(symbols, separators=(",", ":")), error))

    def get_collector(self, now=None):
        now = _timestamp(now) if now is not None else datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with closing(self._connect()) as connection:
            row = connection.execute("""
                SELECT state, heartbeat_at, next_run_at, interval_seconds, symbols_json, error, collector_id
                FROM market_collector WHERE singleton = 1
            """).fetchone()
        if row is None:
            return {"state": "not_started", "heartbeat_at": None, "next_run_at": None,
                    "interval_seconds": None, "symbols": [], "error": None, "collector_id": None}
        state, heartbeat_at, next_run_at, interval_seconds, symbols_json, error, collector_id = row
        # 수집기는 대기·요청 중에도 5초마다 heartbeat를 남긴다.
        age = (datetime.fromisoformat(now) - datetime.fromisoformat(heartbeat_at)).total_seconds()
        if state == "running" and (age > 20 or age < -5):
            state, next_run_at = "stale", None
        return {"state": state, "heartbeat_at": heartbeat_at,
                "next_run_at": next_run_at if state == "running" else None,
                "interval_seconds": interval_seconds, "symbols": json.loads(symbols_json),
                "error": error, "collector_id": collector_id}
