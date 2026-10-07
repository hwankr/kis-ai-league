"""Durable experiment records and order intents, separate from frozen research."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ExperimentStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, created_at TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, intent_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def save_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, encode(value)))

    def add_run(self, run):
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?)", (run["id"], run["as_of"], run["created_at"], encode(run)))

    def runs(self, limit=100):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM runs ORDER BY created_at DESC,id DESC LIMIT ?", (limit,))]

    def reserve_order(self, order, key):
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?,?)",
                                (order["id"], key, order["created_at"], encode(order)))
            return cursor.rowcount == 1

    def save_order(self, order):
        with self.connect() as db:
            cursor = db.execute("UPDATE orders SET payload=? WHERE id=?", (encode(order), order["id"]))
            if cursor.rowcount != 1:
                raise ValueError("주문 원본이 없습니다.")

    def orders(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM orders ORDER BY created_at,id")]

    def event(self, kind, message, at=None):
        at = at or datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO events(at,kind,message) VALUES (?,?,?)", (at, kind, message))

    def events(self, limit=40):
        with self.connect() as db:
            return [{"at": row[0], "kind": row[1], "message": row[2]} for row in db.execute(
                "SELECT at,kind,message FROM events ORDER BY id DESC LIMIT ?", (limit,))]
