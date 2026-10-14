"""Daily provisional decisions and atomic, next-session policy activation.

Research never grants order permission. Completed runs are published only with
their policy pointer; frozen long-horizon trials are not rewritten here.
"""
from copy import deepcopy
from datetime import date, datetime, time, timezone
import hashlib
import json

from backend.experiment_store import encode
from backend.kis import KST
from backend.learning_policy import build_frame, policy_id, validate_policy
from backend.source_version import DAILY_SOURCES, LEARNING_SOURCES, source_version


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
        self.engine_version = source_version(DAILY_SOURCES)
        self.learning_engine_version = source_version(LEARNING_SOURCES)
        from backend.daily_comparison import ensure_tables
        ensure_tables(store)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS daily_policy_decisions '
                       '(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, day TEXT NOT NULL, '
                       'payload TEXT NOT NULL, UNIQUE(fingerprint,day))')
            db.execute('CREATE TABLE IF NOT EXISTS daily_policy_preparations '
                       '(decision_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL)')
            for (payload,) in db.execute("SELECT value FROM settings WHERE key GLOB 'daily_policy:*'").fetchall():
                self._archive_preparation(db, json.loads(payload).get('pending'))

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
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value '
                   'WHERE settings.value IS NOT excluded.value',
                   (key, encode(value)))

    @staticmethod
    def _archive_preparation(db, pending):
        if not pending or not isinstance(pending.get('prepared_run'), dict):
            return
        if db.execute('SELECT 1 FROM daily_policy_preparations WHERE decision_id=?',
                      (pending['decision_id'],)).fetchone():
            return
        db.execute('INSERT INTO daily_policy_preparations VALUES (?,?,?,?)',
                   (pending['decision_id'], pending['fingerprint'], pending['signal_day'], encode(pending)))

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
        status, error = state.get('status', 'waiting'), state.get('error')
        if pending and status != 'blocked':
            if pending.get('engine_version', self.engine_version) != self.engine_version:
                status, error = 'blocked', '준비 정책의 심사 엔진 버전이 달라 적용을 보류합니다.'
            else:
                from backend.learning_compatibility import version_matches
                run = pending.get('prepared_run')
                if run is not None and not version_matches(run.get('learning_engine_version'), self.learning_engine_version):
                    status, error = 'blocked', '준비 정책의 학습 엔진 버전이 달라 적용을 보류합니다.'
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
        return {'enabled': self.enabled, 'status': status,
                'active': brief(active), 'pending': brief(pending, waiting=True),
                'last_decision': last, 'error': error,
                'activation_check': deepcopy(state.get('activation_check'))}

    def note(self, status, error=None, *, fingerprint=None, decision_id=None, details=None):
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
            pending = state.get('pending')
            if decision_id is not None and (pending or {}).get('decision_id') != decision_id:
                return
            self._archive_preparation(db, pending)
            # A read retry or an older research task cannot reopen a blocked plan.
            if pending and state.get('status') == 'blocked' and status != 'blocked':
                return
            check = {key: deepcopy(value) for key, value in details.items() if key != 'checked_at'} if details is not None else None
            previous_check = {key: value for key, value in (state.get('activation_check') or {}).items() if key != 'checked_at'}
            if (state.get('status') == status and state.get('error') == error
                    and (check is None or check == previous_check)):
                return
            state.update(status=status, error=error)
            if check is not None:
                state['activation_check'] = {**check, 'checked_at': self._timestamp()}
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
        from backend.daily_inputs import freeze_inputs, input_matches
        with self.store.connect() as db:
            row = db.execute('SELECT payload FROM daily_policy_decisions WHERE fingerprint=? AND day=?',
                             (fingerprint, day)).fetchone()
        prior = json.loads(row[0]) if row else None
        if prior and not input_matches(prior, data):
            self.note('blocked', '같은 거래일의 평가 자료가 변경되어 새 정책 적용을 보류합니다.', fingerprint=fingerprint)
            return
        if prior and prior.get('decision') != 'error':
            return
        # Never retrofit a legacy decision or refreeze an accepted response on retry.
        signal_inputs = deepcopy(prior.get('signal_inputs')) if prior else freeze_inputs(data)
        if prior is None:
            baseline = next((frame.get('baseline_signals', []) for frame in reversed(frames)
                             if str(frame.get('as_of')) == day), [])
            if not isinstance(baseline, list):
                raise ValueError('invalid_daily_baseline_signals')
            signal_inputs['baseline_signals'] = deepcopy(baseline)
            signal_inputs['baseline_hash'] = _hash(baseline)
        digest = prior['market_input_hash'] if prior else signal_inputs['hash']
        book = (context or {}).get('initial_state')
        evaluation_context = deepcopy((context or {}).get('daily_evaluation'))
        current = self.now().astimezone(KST)
        if (day != current.date().isoformat() or current.time() < time(16)
                or not book or book.get('as_of') != day or book.get('pending_orders') or book.get('unresolved_orders')):
            if not self.pending():
                self.note('waiting', (context or {}).get('book_error'), fingerprint=fingerprint)
            return
        if prior and (prior.get('research_input_hash') != _hash(frames)
                      or prior.get('limits') != frozen_limits or prior.get('engine_version') != self.engine_version
                      or prior.get('evaluation_context') != evaluation_context):
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
                      'book_hash': _hash(book), 'basis': 'development_only',
                      'evaluation_context': evaluation_context}
            if signal_inputs is not None:
                record['signal_inputs'] = deepcopy(signal_inputs)
            seed = deepcopy(prior.get('comparison_seed')) if prior else None
            if prior is None:
                seed = {'parent_policy': deepcopy(parent), 'candidate_policy': deepcopy(candidate),
                        'initial_state': deepcopy(book),
                        'execution_model': deepcopy((context or {}).get('execution_model') or {'mode': 'daily_open'}),
                        'observed_at': self._timestamp(), 'limits': deepcopy(frozen_limits), 'signal_day': day}
            if seed is not None:
                record['comparison_seed'] = seed
            with self.store.connect() as db:
                db.execute('INSERT INTO daily_policy_decisions VALUES (?,?,?,?) '
                           'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                           (decision_id, fingerprint, day, encode(record)))
            from backend.daily_evaluation import select_daily_candidate
            proposal, result = select_daily_candidate(parent, proposal, frames, frozen_limits,
                evaluator=self.evaluator, evaluation_context=evaluation_context)
            candidate = validate_policy(proposal['policy'])
            if self.stopping():
                return
            record.update({key: result.get(key) for key in ('decision', 'reason', 'metrics', 'changes')})
            record.update(proposal=deepcopy(proposal), candidate_name=candidate['name'],
                          change_family=proposal.get('change_family'))
            if seed is not None:
                seed['candidate_policy'] = deepcopy(candidate)
                record['comparison_seed'] = deepcopy(seed)
            record['evaluated_at'] = self._timestamp()
            pending = {'decision_id': decision_id, 'policy': candidate, 'candidate_id': policy_id(candidate),
                       'signal_day': day, 'parent_id': parent_id, 'revision': revision,
                       'fingerprint': fingerprint, 'limits': frozen_limits, 'market_input_hash': digest,
                       'engine_version': self.engine_version,
                       'prepared_at': None}
            if signal_inputs is not None:
                pending['signal_inputs'] = deepcopy(signal_inputs)
            if seed is not None:
                pending['comparison_seed'] = deepcopy(seed)
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
                latest.pop('activation_check', None)
                # A new completed session supersedes yesterday's unactivated plan.
                self._archive_preparation(db, latest.get('pending'))
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
        if (not self.enabled or not pending or state.get('status') == 'blocked'
                or settings.get('fingerprint') != fingerprint
                or any(pending.get(field) != supplied.get(field) for field in
                       ('decision_id', 'candidate_id', 'parent_id', 'revision', 'signal_day', 'market_input_hash', 'signal_inputs'))
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
                    self._archive_preparation(db, state['pending'])
                    return existing.get('id') == run.get('id')
                state['pending'].update(prepared_run=deepcopy(run), prepared_at=self._timestamp())
                self._archive_preparation(db, state['pending'])
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
                self._archive_preparation(db, stored)
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
                state.pop('activation_check', None)
                state.pop('pending', None)
                self._write(db, key, state)
                db.execute('INSERT OR IGNORE INTO learning_policies VALUES (?,?)',
                           (active['candidate_id'], encode(active['policy'])))
                record_row = db.execute('SELECT payload FROM daily_policy_decisions WHERE id=?', (stored['decision_id'],)).fetchone()
                record = json.loads(record_row[0])
                record['activation'] = {name: active[name] for name in ('adopted_at', 'effective_day', 'run_id', 'revision')}
                db.execute('UPDATE daily_policy_decisions SET payload=? WHERE id=?', (encode(record), stored['decision_id']))
                seed = record.get('comparison_seed')
                if seed is not None:
                    followup = {'comparison_seed': deepcopy(seed), 'id': stored['decision_id'],
                                'fingerprint': stored['fingerprint'], 'effective_day': effective_day,
                                'adopted_at': active['adopted_at'], 'engine_version': self.engine_version}
                    db.execute('INSERT OR IGNORE INTO daily_policy_followups VALUES (?,?,?)',
                               (stored['decision_id'], stored['fingerprint'], encode(followup)))
            return True
        except (KeyError, TypeError, ValueError):
            return False
