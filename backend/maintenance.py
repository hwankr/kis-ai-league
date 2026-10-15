"""Durable deployment gate, independent of user trading intent and financial records."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from backend.request_gate import file_lock


class MaintenanceError(ValueError):
    pass


class MaintenanceGate:
    """Use the engine's submission lock; a closed gate survives process replacement.

    Missing state is the legacy, never-initialized open state. Deployment must close
    the gate explicitly before replacing a legacy process, which does not read it.
    Existing but unreadable state fails closed. No trading settings are modified.
    """

    def __init__(self, directory, *, now=None):
        self.directory = Path(directory)
        self.path = self.directory / 'maintenance.sqlite3'
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._seen = self.path.exists()

    @staticmethod
    def _validate(state):
        if (not isinstance(state, dict) or state.get('version') != 1
                or type(state.get('revision')) is not int or state['revision'] < 0
                or type(state.get('closed')) is not bool):
            raise MaintenanceError('maintenance_state_invalid')
        return state

    def snapshot(self):
        if not self.path.exists():
            if self._seen:
                raise MaintenanceError('maintenance_state_missing')
            return {'version': 1, 'revision': 0, 'closed': False,
                    'updated_at': None, 'reason': None, 'initialized': False}
        self._seen = True
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
                db.execute('PRAGMA query_only=ON')
                row = db.execute('SELECT payload FROM gate WHERE id=1').fetchone()
            if row is None:
                raise MaintenanceError('maintenance_state_missing')
            return self._validate(json.loads(row[0]))
        except (sqlite3.Error, OSError, json.JSONDecodeError) as error:
            raise MaintenanceError('maintenance_state_unreadable') from error

    def _change(self, closed, reason, expected_revision):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise MaintenanceError('maintenance_reason_required')
        with file_lock(self.directory / 'submission.lock'):
            previous = self.snapshot()
            if expected_revision is not None and previous['revision'] != expected_revision:
                raise MaintenanceError('maintenance_revision_changed')
            if previous['closed'] is closed:
                return previous
            state = {'version': 1, 'revision': previous['revision'] + 1, 'closed': closed,
                     'updated_at': self.now().astimezone(timezone.utc).isoformat(),
                     'reason': reason.strip(), 'initialized': True}
            self.directory.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS gate (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS audit (revision INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
                payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
                db.execute('INSERT INTO gate VALUES (1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (payload,))
                db.execute('INSERT INTO audit VALUES (?,?)', (state['revision'], payload))
            self._seen = True
            return state

    def close(self, reason):
        return self._change(True, reason, None)

    def open(self, *, expected_revision, reason):
        if type(expected_revision) is not int or expected_revision < 0:
            raise MaintenanceError('maintenance_expected_revision_required')
        return self._change(False, reason, expected_revision)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path, help='Exact experiment runtime directory')
    parser.add_argument('action', choices=('status', 'close', 'open'))
    parser.add_argument('--reason')
    parser.add_argument('--expected-revision', type=int)
    args = parser.parse_args(argv)
    gate = MaintenanceGate(args.directory)
    try:
        if args.action == 'status':
            result = gate.snapshot()
        elif args.action == 'close':
            result = gate.close(args.reason)
        else:
            result = gate.open(expected_revision=args.expected_revision, reason=args.reason)
    except (MaintenanceError, OSError, sqlite3.Error, TimeoutError):
        print(json.dumps({'status': 'error', 'message': 'maintenance operation failed; gate not confirmed open'}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
