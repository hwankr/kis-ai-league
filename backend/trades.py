"""계좌별 주문 단위의 누적 체결과 조회 성공 시각을 보관한다."""

from contextlib import closing
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import threading


TRADE_FIELDS = ("order_date", "order_id", "branch_id", "symbol", "name", "side",
                "quantity", "price", "amount", "order_time")


class ExecutionConflict(ValueError):
    """조회된 누적 체결이 기존 주문 기록과 충돌한다."""


def _date(value):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Trade dates must use YYYY-MM-DD") from None
    return value


def _range(start, end):
    start, end = _date(start), _date(end)
    if start > end:
        raise ValueError("Trade date range is reversed")
    return start, end


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (TypeError, ValueError):
        raise ValueError("Trade sync timestamp must include its timezone") from None


def _row_values(row, start, end):
    if not isinstance(row, dict):
        raise ValueError("Trade row must be normalized")
    order_date = _date(row.get("order_date"))
    if not start <= order_date <= end:
        raise ValueError("Trade date is outside the requested range")
    for field in ("order_id", "branch_id", "symbol", "name"):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Trade identity and name must be nonempty strings")
    if row.get("side") not in ("buy", "sell"):
        raise ValueError("Trade side must be buy or sell")
    for field in ("quantity", "price", "amount"):
        value = row.get(field)
        try:
            if not isinstance(value, str) or not value.strip():
                raise ValueError
            number = Decimal(value)
            if not number.is_finite() or number < 0 or (field == "quantity" and number == 0):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("Trade numeric values must be finite nonnegative strings with positive quantity") from None
    order_time = row.get("order_time")
    if order_time is not None:
        try:
            if not isinstance(order_time, str) or time.fromisoformat(order_time).isoformat() != order_time:
                raise ValueError
            if len(order_time) != 8:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Trade order time must use HH:MM:SS or null") from None
    # 원본 응답이나 추가 필드는 보관하지 않는다.
    return tuple(row.get(field) for field in TRADE_FIELDS)


class ExecutionHistory:
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
                            CREATE TABLE IF NOT EXISTS executions (
                                profile_id TEXT NOT NULL,
                                environment TEXT NOT NULL,
                                fingerprint TEXT NOT NULL,
                                order_date TEXT NOT NULL,
                                order_id TEXT NOT NULL,
                                branch_id TEXT NOT NULL,
                                symbol TEXT NOT NULL,
                                name TEXT NOT NULL,
                                side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                                quantity TEXT NOT NULL,
                                price TEXT NOT NULL,
                                amount TEXT NOT NULL,
                                order_time TEXT,
                                PRIMARY KEY (profile_id, environment, fingerprint,
                                             order_date, branch_id, order_id)
                            );
                            CREATE INDEX IF NOT EXISTS executions_series_time
                            ON executions(profile_id, environment, fingerprint,
                                          order_date DESC, order_time DESC);
                            CREATE TABLE IF NOT EXISTS execution_syncs (
                                profile_id TEXT NOT NULL,
                                environment TEXT NOT NULL,
                                fingerprint TEXT NOT NULL,
                                start_date TEXT NOT NULL,
                                end_date TEXT NOT NULL,
                                updated_at TEXT NOT NULL,
                                PRIMARY KEY (profile_id, environment, fingerprint,
                                             start_date, end_date)
                            );
                        """)
                    self._initialized = True
            return connection
        except Exception:
            connection.close()
            raise

    def record(self, series, start, end, updated_at, rows):
        start, end = _range(start, end)
        updated_at = _timestamp(updated_at)
        scope = (series.profile_id, series.environment, series.fingerprint)
        values = [(*scope, *_row_values(row, start, end)) for row in rows]
        with closing(self._connect()) as connection, connection:
            # 여러 서버/저장소 인스턴스가 있어도 검증부터 저장까지 한 번에 잠근다.
            connection.execute("BEGIN IMMEDIATE")
            existing = {
                (order_date, branch_id, order_id): (symbol, side, quantity)
                for order_date, branch_id, order_id, symbol, side, quantity in connection.execute("""
                    SELECT order_date, branch_id, order_id, symbol, side, quantity
                    FROM executions
                    WHERE profile_id = ? AND environment = ? AND fingerprint = ?
                        AND order_date BETWEEN ? AND ?
                """, (*scope, start, end))
            }
            for entry in values:
                row = dict(zip(TRADE_FIELDS, entry[3:]))
                key = (row["order_date"], row["branch_id"], row["order_id"])
                previous = existing.get(key)
                if previous is not None and (
                    previous[:2] != (row["symbol"], row["side"])
                    or Decimal(row["quantity"]) < Decimal(previous[2])
                ):
                    raise ExecutionConflict("Trade aggregate conflicts with previously saved execution")
                existing[key] = (row["symbol"], row["side"], row["quantity"])
            # 같은 주문의 부분체결 누적값은 더하지 않고 최신 집계로 교체한다.
            # 응답에서 빠진 기존 주문은 KIS 조회 보존 범위와 관계없이 유지한다.
            connection.executemany("""
                INSERT INTO executions
                    (profile_id, environment, fingerprint, order_date, order_id,
                     branch_id, symbol, name, side, quantity, price, amount, order_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, environment, fingerprint,
                            order_date, branch_id, order_id) DO UPDATE SET
                    symbol = excluded.symbol,
                    name = excluded.name,
                    side = excluded.side,
                    quantity = excluded.quantity,
                    price = excluded.price,
                    amount = excluded.amount,
                    order_time = excluded.order_time
            """, values)
            connection.execute("""
                INSERT INTO execution_syncs
                    (profile_id, environment, fingerprint, start_date, end_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, environment, fingerprint,
                            start_date, end_date) DO UPDATE SET
                    updated_at = excluded.updated_at
            """, (*scope, start, end, updated_at))

    def read(self, series, start, end):
        start, end = _range(start, end)
        scope = (series.profile_id, series.environment, series.fingerprint)
        with closing(self._connect()) as connection, connection:
            # 행과 마지막 성공 시각을 같은 읽기 스냅샷에서 가져온다.
            connection.execute("BEGIN")
            rows = connection.execute("""
                SELECT order_date, order_id, branch_id, symbol, name, side,
                       quantity, price, amount, order_time
                FROM executions
                WHERE profile_id = ? AND environment = ? AND fingerprint = ?
                    AND order_date BETWEEN ? AND ?
                ORDER BY order_date DESC, order_time DESC, branch_id DESC, order_id DESC
            """, (*scope, start, end)).fetchall()
            sync = connection.execute("""
                SELECT updated_at FROM execution_syncs
                WHERE profile_id = ? AND environment = ? AND fingerprint = ?
                    AND start_date = ? AND end_date = ?
            """, (*scope, start, end)).fetchone()
        return {"trades": [dict(zip(TRADE_FIELDS, row)) for row in rows],
                "total_count": len(rows), "updated_at": sync[0] if sync else None}
