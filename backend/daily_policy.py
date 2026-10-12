"""Daily provisional decisions and atomic, next-session policy activation.

Research never grants order permission. Completed runs are published only with
their policy pointer; frozen long-horizon trials are not rewritten here.
"""
from copy import deepcopy
from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path

from backend.experiment_store import encode
from backend.kis import KST
from backend.learning_policy import build_frame, policy_id, validate_policy


VERSION = 'daily-policy-v1'
LIMIT_KEYS = ('budget', 'order_cap', 'daily_buy_limit')


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def market_input_hash(data):
    return _hash({key: value for key, value in build_frame(data).items() if key != 'observed_at'})


def _stamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError('daily_timestamp_requires_timezone')
    return result


class DailyPolicyService:
    def __init__(self, store, *, enabled, now, fallback_policy, proposer=None, evaluator=None, stopping=None):
        self.store, self.enabled, self.now = store, enabled, now
        self.fallback_policy, self.proposer, self.evaluator = fallback_policy, proposer, evaluator
        self.stopping = stopping or (lambda: False)
        self.engine_version = hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes() for name in (
            'daily_policy.py', 'daily_evaluation.py', 'learning_policy.py', 'learning_evaluation.py',
            'learning_research.py'))).hexdigest()
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS daily_policy_decisions '
                       '(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, day TEXT NOT NULL, '
                       'payload TEXT NOT NULL, UNIQUE(fingerprint,day))')

    def _timestamp(self):
        return self.now().astimezone(timezone.utc).isoformat()

    def _fingerprint(self):
        return (self.store.setting('policy') or {}).get('fingerprint')

    @staticmethod
    def _key(fingerprint):
        return 'daily_policy:' + str(fingerprint)

    def state(self):
        return self.store.setting(self._key(self._fingerprint()), {})

    @staticmethod
    def _read(db, key, default=None):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _write(db, key, value):
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, encode(value)))

    def active(self):
        return deepcopy(self.state().get('active')) if self.enabled else None

    def pending(self):
        return deepcopy(self.state().get('pending')) if self.enabled else None

    def policy(self):
        active = self.active()
        return deepcopy(active['policy']) if active else self.fallback_policy()

    def history(self):
        fingerprint = self._fingerprint()
        with self.store.connect() as db:
            rows = db.execute('SELECT payload FROM daily_policy_decisions WHERE fingerprint=? '
                              'ORDER BY day DESC LIMIT 30', (fingerprint,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def snapshot(self):
        state = self.state()
        active, pending, decision = state.get('active'), state.get('pending'), state.get('last_decision')
        def brief(value, *, waiting=False):
            if not value:
                return None
            result = {'id': value['candidate_id'], 'name': value['policy']['name'], 'signal_day': value['signal_day']}
            return {**result, 'prepared_at': value.get('prepared_at')} if waiting else {
                **result, 'adopted_at': value.get('adopted_at'), 'effective_day': value.get('effective_day')}
        last = None
        if decision:
            metrics = decision.get('metrics')
            last = {key: decision.get(key) for key in ('as_of', 'decision', 'reason', 'candidate_name', 'change_family')}
            last['metrics'] = metrics if isinstance(metrics, dict) and 'champion' in metrics and 'candidate' in metrics else None
        return {'enabled': self.enabled, 'status': state.get('status', 'waiting'),
                'active': brief(active), 'pending': brief(pending, waiting=True),
                'last_decision': last, 'error': state.get('error')}

    def note(self, status, error=None, *, fingerprint=None):
        expected = fingerprint
        fingerprint = self._fingerprint()
        if not fingerprint or expected is not None and expected != fingerprint:
            return
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if (self._read(db, 'policy', {}) or {}).get('fingerprint') != fingerprint:
                return
            key = self._key(fingerprint)
            state = self._read(db, key, {})
            state.update(status=status, error=error)
            if status == 'blocked':
                state.pop('pending', None)
            self._write(db, key, state)

    def process(self, data, frames, limits, history, *, context, proposer, llm=None):
        """At most one successful proposal/decision per account and completed day."""
        if not self.enabled or self.stopping():
            return
        day = str(data['as_of'])
        fingerprint = limits.get('fingerprint') or self._fingerprint()
        frozen_limits = {key: str(limits[key]) for key in LIMIT_KEYS}
        if not fingerprint or fingerprint != self._fingerprint():
            return
        digest = market_input_hash(data)
        with self.store.connect() as db:
            row = db.execute('SELECT payload FROM daily_policy_decisions WHERE fingerprint=? AND day=?',
                             (fingerprint, day)).fetchone()
        prior = json.loads(row[0]) if row else None
        if prior and prior['market_input_hash'] != digest:
            self.note('blocked', '같은 거래일의 평가 자료가 변경되어 새 정책 적용을 보류합니다.', fingerprint=fingerprint)
            return
        if prior and prior.get('decision') != 'error':
            return
        book = (context or {}).get('initial_state')
        current = self.now().astimezone(KST)
        if (day != current.date().isoformat() or current.time() < time(16)
                or not book or book.get('as_of') != day or book.get('pending_orders') or book.get('unresolved_orders')):
            if not self.pending():
                self.note('waiting', (context or {}).get('book_error'), fingerprint=fingerprint)
            return
        if prior and (prior.get('research_input_hash') != _hash(frames)
                      or prior.get('limits') != frozen_limits or prior.get('engine_version') != self.engine_version):
            self.note('blocked', '재시도 중 평가 자료·한도·코드 변경', fingerprint=fingerprint)
            return
        parent = self.policy()
        state = self.state()
        parent_id, revision = policy_id(parent), state.get('revision', 0)
        if prior and prior.get('parent_id') != parent_id:
            self.note('blocked', '당일 심사 후 적용 정책이 변경됐습니다.', fingerprint=fingerprint)
            return
        decision_id = _hash([VERSION, fingerprint, day])
        self.note('researching', fingerprint=fingerprint)
        proposal = prior.get('proposal') if prior else None
        try:
            if proposal is None:
                proposal = proposer(parent, frames, frozen_limits, history, llm=llm)
            candidate = validate_policy(proposal['policy'])
            # Persist an accepted model response before any further computation.
            record = {'id': decision_id, 'version': VERSION, 'engine_version': self.engine_version,
                      'fingerprint': fingerprint, 'as_of': day,
                      'created_at': prior['created_at'] if prior else self._timestamp(),
                      'parent_id': parent_id, 'revision': revision, 'market_input_hash': digest,
                      'research_input_hash': _hash(frames), 'limits': frozen_limits,
                      'proposal': deepcopy(proposal), 'candidate_name': candidate['name'],
                      'change_family': proposal.get('change_family'), 'decision': 'error',
                      'reason': '일일 개선 평가 재시도 대기', 'metrics': None,
                      'book_hash': _hash(book), 'basis': 'development_only'}
            with self.store.connect() as db:
                db.execute('INSERT INTO daily_policy_decisions VALUES (?,?,?,?) '
                           'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                           (decision_id, fingerprint, day, encode(record)))
            from backend.daily_evaluation import evaluate_daily_candidate
            result = (self.evaluator or evaluate_daily_candidate)(parent, proposal, frames, frozen_limits)
            if self.stopping():
                return
            record.update({key: result.get(key) for key in ('decision', 'reason', 'metrics', 'changes')})
            record['evaluated_at'] = self._timestamp()
            pending = {'decision_id': decision_id, 'policy': candidate, 'candidate_id': policy_id(candidate),
                       'signal_day': day, 'parent_id': parent_id, 'revision': revision,
                       'fingerprint': fingerprint, 'limits': frozen_limits, 'market_input_hash': digest,
                       'engine_version': self.engine_version,
                       'prepared_at': None}
            fallback_id = policy_id(self.fallback_policy())
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                settings = self._read(db, 'policy', {})
                key = self._key(fingerprint)
                latest = self._read(db, key, {})
                latest_id = (latest.get('active') or {}).get('candidate_id', fallback_id)
                if (settings.get('fingerprint') != fingerprint or latest.get('revision', 0) != revision
                        or latest_id != parent_id or any(str(settings.get(key)) != frozen_limits[key] for key in LIMIT_KEYS)):
                    record.update(decision='blocked', reason='심사 중 계좌·한도·적용 정책이 변경됐습니다.')
                latest.update(status='prepared' if record['decision'] == 'apply' else record['decision'],
                              last_decision=record, error=None)
                # A new completed session supersedes yesterday's unactivated plan.
                latest.pop('pending', None)
                if record['decision'] == 'apply':
                    latest['pending'] = pending
                self._write(db, key, latest)
                db.execute('UPDATE daily_policy_decisions SET payload=? WHERE id=?', (encode(record), decision_id))
        except Exception:
            self.note('error', '일일 개선 평가를 완료하지 못했습니다. 기존 정책을 유지합니다.', fingerprint=fingerprint)
            raise

    def _current_pending(self, db, supplied):
        fingerprint = supplied.get('fingerprint')
        settings = self._read(db, 'policy', {})
        key = self._key(fingerprint)
        state = self._read(db, key, {})
        pending = state.get('pending')
        if (not self.enabled or not pending or settings.get('fingerprint') != fingerprint
                or any(pending.get(field) != supplied.get(field) for field in
                       ('decision_id', 'candidate_id', 'parent_id', 'revision', 'signal_day', 'market_input_hash'))
                or state.get('revision', 0) != pending['revision']
                or pending.get('engine_version', self.engine_version) != self.engine_version
                or any(str(settings.get(field)) != pending['limits'][field] for field in LIMIT_KEYS)):
            return None, key
        active_id = (state.get('active') or {}).get('candidate_id')
        if active_id is not None and active_id != pending['parent_id']:
            return None, key
        return state, key

    def prepare(self, pending, run):
        """Stage signals without exposing them as the latest executable run."""
        if not self.enabled or self.stopping():
            return False
        try:
            if (policy_id(validate_policy(run['learning_policy'])) != pending['candidate_id']
                    or run['as_of'] != pending['signal_day'] or run.get('status') != 'ready'
                    or _stamp(run['created_at']) > self.now()):
                return False
            encode(run)
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                state, key = self._current_pending(db, pending)
                if state is None:
                    return False
                existing = state['pending'].get('prepared_run')
                if existing:
                    # First complete signal set is frozen, including after restart.
                    return existing.get('id') == run.get('id')
                state['pending'].update(prepared_run=deepcopy(run), prepared_at=self._timestamp())
                self._write(db, key, state)
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def activate(self, pending, *, effective_day):
        """Caller has verified the next exchange session under runner.lock."""
        if not self.enabled or self.stopping():
            return False
        today = self.now().astimezone(KST).date()
        try:
            if date.fromisoformat(effective_day) != today or pending['signal_day'] >= effective_day:
                return False
            fallback_id = policy_id(self.fallback_policy())
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                state, key = self._current_pending(db, pending)
                if state is None:
                    return False
                if not self._read(db, 'enabled', False) or self._read(db, 'user_paused', False):
                    return False
                stored = state['pending']
                run = stored.get('prepared_run')
                active = state.get('active') or {}
                if (not run or active.get('effective_day') == effective_day
                        or active.get('candidate_id', fallback_id) != stored['parent_id']
                        or _stamp(run['created_at']).astimezone(KST) >= datetime.combine(today, time(9), KST)
                        or policy_id(validate_policy(run['learning_policy'])) != stored['candidate_id']
                        or run['as_of'] != stored['signal_day']):
                    return False
                saved_run = deepcopy(run)
                analysis = saved_run.get('analysis')
                if isinstance(analysis, dict) and analysis.get('decisions') == saved_run.get('signals'):
                    saved_run['analysis'] = {name: value for name, value in analysis.items() if name != 'decisions'}
                db.execute('INSERT INTO runs VALUES (?,?,?,?)',
                           (run['id'], run['as_of'], run['created_at'], encode(saved_run)))
                self.store._index_run(db, run)
                revision = state.get('revision', 0) + 1
                active = {name: deepcopy(stored[name]) for name in
                          ('policy', 'candidate_id', 'signal_day', 'parent_id', 'fingerprint', 'limits', 'decision_id')}
                active.update(adopted_at=self._timestamp(), effective_day=effective_day, run_id=run['id'], revision=revision)
                state.update(active=active, revision=revision, status='active', error=None)
                state.pop('pending', None)
                self._write(db, key, state)
                db.execute('INSERT OR IGNORE INTO learning_policies VALUES (?,?)',
                           (active['candidate_id'], encode(active['policy'])))
                record_row = db.execute('SELECT payload FROM daily_policy_decisions WHERE id=?', (stored['decision_id'],)).fetchone()
                record = json.loads(record_row[0])
                record['activation'] = {name: active[name] for name in ('adopted_at', 'effective_day', 'run_id', 'revision')}
                db.execute('UPDATE daily_policy_decisions SET payload=? WHERE id=?', (encode(record), stored['decision_id']))
            return True
        except (KeyError, TypeError, ValueError):
            return False
