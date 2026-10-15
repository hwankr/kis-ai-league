"""Explicit, one-shot intraday policy revisions; never an ordinary daily bypass.

The caller holds runner.lock. ``guard_reader(db)`` is a pure read of the same
transaction and returns fingerprint, account_id, controls, economic_book,
pending_orders, unresolved_orders, protected and blocked. Controls contain the
exact policy/enabled/user_paused/mirror_intent settings. Economic book identity
includes fills, lot quantities/costs/frozen exits and reservations, excluding
quotes and observation timestamps. Its projection is owned by the executor.

``evaluation_validator(receipt) is True`` means the caller verified the actual
selection proof, original closing context and its economic-book projection.
It must not evaluate again, fetch live prices, or accept an arbitrary receipt.
The receipt binds the selected candidate and all hashes checked below. Hashes
are integrity bindings, not authentication: authorization comes from the
explicitly approved local command that stages this record, never a scheduler.
"""
from copy import deepcopy
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import re
import sqlite3

from backend.daily_inputs import validate_entry_selection
from backend.experiment_store import encode
from backend.kis import KST
from backend.learning_policy import policy_id, signals, validate_policy


VERSION = 'intraday-policy-transition-v1'
RECEIPT_VERSION = 'intraday-evaluation-receipt-v1'
LIMIT_KEYS = ('budget', 'order_cap', 'daily_buy_limit')
RECEIPT_BINDINGS = ('fingerprint', 'account_id', 'signal_day', 'effective_day',
                    'engine_version', 'learning_engine_version', 'analysis_version',
                    'source_decision_id', 'source_decision_hash', 'source_preparation_hash',
                    'closing_context_hash', 'economic_book_hash', 'controls_hash')


def canonical_hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def seal(value):
    """Return an independent JSON object with a canonical self-hash."""
    result = deepcopy(value)
    result.pop('hash', None)
    result['hash'] = canonical_hash(result)
    return result


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _stamp(value):
    instant = datetime.fromisoformat(value)
    _require(instant.utcoffset() is not None, 'intraday_timezone_required')
    return instant


def _sealed(value):
    return isinstance(value, dict) and value.get('hash') == canonical_hash(
        {key: item for key, item in value.items() if key != 'hash'})


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _stored_run(run):
    result = deepcopy(run)
    analysis = result.get('analysis')
    if isinstance(analysis, dict) and analysis.get('decisions') == result.get('signals'):
        result['analysis'].pop('decisions')
    return result


class IntradayPolicyService:
    def __init__(self, store, *, now, engine_version, learning_engine_version,
                 analysis_version, approval_id, effective_day, guard_reader,
                 evaluation_validator, stopping=None):
        _require(callable(guard_reader) and callable(evaluation_validator), 'intraday_validators_required')
        _require(all(isinstance(value, str) and value for value in
                     (engine_version, learning_engine_version, analysis_version)), 'intraday_versions_required')
        self.store, self.now = store, now
        self.engine_version, self.learning_engine_version = engine_version, learning_engine_version
        self.analysis_version = analysis_version
        _require(isinstance(approval_id, str) and bool(approval_id)
                 and date.fromisoformat(effective_day).isoformat() == effective_day,
                 'intraday_explicit_approval_required')
        self.approval_id, self.effective_day = approval_id, effective_day
        self.guard_reader, self.evaluation_validator = guard_reader, evaluation_validator
        self.stopping = stopping or (lambda: False)
        self.last_error = None
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS intraday_policy_transitions (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    effective_day TEXT NOT NULL, approval_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intraday_policy_activations (
                    transition_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    effective_day TEXT NOT NULL, revision INTEGER NOT NULL,
                    run_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                    UNIQUE(fingerprint,effective_day), UNIQUE(fingerprint,revision));
            ''')

    @staticmethod
    def _read(db, key, default=None):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _key(fingerprint):
        return 'daily_policy:' + fingerprint

    def _now(self):
        value = self.now()
        _require(isinstance(value, datetime) and value.utcoffset() is not None, 'intraday_clock_invalid')
        return value.astimezone(KST)

    def _sources(self, db, proposal):
        identity = proposal['source_decision_id']
        decision = db.execute('SELECT fingerprint,day,payload FROM daily_policy_decisions WHERE id=?',
                              (identity,)).fetchone()
        preparation = db.execute('SELECT fingerprint,day,payload FROM daily_policy_preparations WHERE decision_id=?',
                                 (identity,)).fetchone()
        _require(decision is not None and preparation is not None, 'intraday_source_missing')
        for row, key in ((decision, 'source_decision_hash'), (preparation, 'source_preparation_hash')):
            _require(row[:2] == (proposal['fingerprint'], proposal['signal_day'])
                     and canonical_hash(json.loads(row[2])) == proposal[key], 'intraday_source_changed')
        return json.loads(preparation[2])

    def _validate(self, db, proposal, *, check_time):
        _require(_sealed(proposal) and proposal.get('version') == VERSION, 'intraday_proposal_invalid')
        _require(isinstance(proposal['id'], str) and re.fullmatch('[A-Za-z0-9_-]{8,128}', proposal['id']),
                 'intraday_transition_id_invalid')
        _require(proposal['account_id'] == 'competition' and bool(proposal['fingerprint']),
                 'intraday_account_invalid')
        for key in ('engine_version', 'learning_engine_version', 'analysis_version'):
            _require(proposal[key] == getattr(self, key), 'intraday_engine_changed')
        effective_day, signal_day = proposal['effective_day'], proposal['signal_day']
        _require(effective_day == self.effective_day
                 and date.fromisoformat(signal_day) < date.fromisoformat(effective_day), 'intraday_signal_day_invalid')
        created, expires = _stamp(proposal['created_at']), _stamp(proposal['expires_at'])
        start = datetime.combine(date.fromisoformat(effective_day), time(9, 5), KST)
        end = datetime.combine(date.fromisoformat(effective_day), time(15, 15), KST)
        _require(start <= created < expires <= end, 'intraday_preparation_window_invalid')
        authorization = proposal['authorization']
        _require(set(authorization) == {'id', 'scope', 'issued_at', 'expires_at', 'parent_revision'}
                 and authorization['id'] == self.approval_id
                 and authorization['scope'] == 'intraday_policy_transition'
                 and _stamp(authorization['issued_at']) <= created
                 and _stamp(authorization['issued_at']).astimezone(KST).date().isoformat() == effective_day
                 and expires <= _stamp(authorization['expires_at']) <= end,
                 'intraday_authorization_invalid')
        parent = proposal['parent_active']
        _require(type(parent['revision']) is int and parent['revision'] >= 1
                 and authorization['parent_revision'] == parent['revision']
                 and parent['fingerprint'] == proposal['fingerprint']
                 and parent['candidate_id'] == policy_id(parent['policy'])
                 and parent['signal_day'] == signal_day
                 and parent['decision_id'] == proposal['source_decision_id']
                 and parent['effective_day'] == effective_day
                 and not parent.get('transition_id'), 'intraday_parent_invalid')
        limits = proposal['limits']
        _require(set(limits) == set(LIMIT_KEYS) and limits == parent['limits'], 'intraday_limits_changed')
        amounts = [Decimal(str(limits[key])) for key in LIMIT_KEYS]
        _require(all(value.is_finite() and value > 0 for value in amounts)
                 and amounts[1] <= amounts[2] <= amounts[0], 'intraday_limits_invalid')
        _require(all(_digest(proposal[key]) for key in ('source_decision_hash', 'source_preparation_hash',
                 'closing_context_hash', 'economic_book_hash', 'controls_hash')), 'intraday_hash_invalid')
        candidate = validate_policy(proposal['policy'])
        _require(candidate == proposal['policy'] and policy_id(candidate) == proposal['candidate_id']
                 and proposal['candidate_id'] != parent['candidate_id'], 'intraday_candidate_invalid')
        receipt = proposal['evaluation_receipt']
        _require(_sealed(receipt) and receipt.get('version') == RECEIPT_VERSION
                 and receipt.get('decision') == 'apply'
                 and receipt.get('selected_candidate_id') == proposal['candidate_id']
                 and all(receipt.get(key) == proposal[key] for key in RECEIPT_BINDINGS)
                 and receipt.get('parent_active_hash') == canonical_hash(parent)
                 and receipt.get('closing_economic_book_hash') == proposal['economic_book_hash']
                 and _digest(receipt.get('selection_hash')),
                 'intraday_evaluation_binding_invalid')
        _require(self.evaluation_validator(deepcopy(receipt)) is True, 'intraday_evaluation_unverified')
        source = self._sources(db, proposal)
        run = proposal['prepared_run']
        _require(isinstance(run['id'], str) and bool(run['id']) and run['id'] != parent['run_id']
                 and run.get('intraday_transition_id') == proposal['id']
                 and 'daily_decision_id' not in run and run.get('status') == 'ready'
                 and run['as_of'] == signal_day and created <= _stamp(run['created_at']) < expires
                 and run['learning_policy'] == candidate
                 and run['learning_engine_version'] == self.learning_engine_version
                 and run['version_id'] == self.analysis_version,
                 'intraday_run_invalid')
        original_run = source['prepared_run']
        _require(original_run['id'] == parent['run_id']
                 and policy_id(original_run['learning_policy']) == parent['candidate_id'],
                 'intraday_original_parent_changed')
        _require(run['analysis']['input'] == original_run['analysis']['input']
                 and run['input_hash'] == original_run['input_hash']
                 and run['input_hash'] == canonical_hash(run['analysis']['input'])
                 and run['analysis']['input_hash'] == run['input_hash']
                 and run['analysis']['version_id'] == run['version_id']
                 and run['analysis'].get('decisions', run['signals']) == run['signals'],
                 'intraday_frozen_inputs_changed')
        contract = source['signal_inputs']
        _require(contract['frame']['as_of'] == signal_day, 'intraday_source_day_invalid')
        selection = receipt.get('entry_selection')
        _require(not candidate.get('held_before_top_n') or selection is not None,
                 'intraday_entry_selection_missing')
        _require(validate_entry_selection(run, candidate, expected=selection, frame=contract['frame']),
                 'intraday_entry_selection_invalid')
        expected = deepcopy(contract['baseline_signals'])
        if candidate['kind'] == 'rules':
            expected += signals(candidate, contract['frame'],
                                entry_context=selection['context'] if selection else None)
        _require(run['signals'] == expected, 'intraday_frozen_signals_changed')
        if check_time:
            now = self._now()
            _require(now.weekday() < 5 and created <= _stamp(run['created_at']) <= now < expires
                     and start <= now < end, 'intraday_activation_window_invalid')
        return run

    def _guard(self, db, proposal):
        policy = self._read(db, 'policy', {})
        state = self._read(db, self._key(proposal['fingerprint']), {})
        _require(policy.get('fingerprint') == proposal['fingerprint']
                 and policy.get('account_id') == proposal['account_id']
                 and {key: policy.get(key) for key in LIMIT_KEYS} == proposal['limits'],
                 'intraday_account_or_limits_changed')
        _require(self._read(db, 'enabled', False) is True
                 and self._read(db, 'user_paused', False) is False, 'intraday_disabled')
        _require(state.get('revision') == proposal['parent_active']['revision']
                 and state.get('active') == proposal['parent_active'] and not state.get('pending'),
                 'intraday_parent_changed')
        guard = self.guard_reader(db)
        _require(guard['fingerprint'] == proposal['fingerprint'] and guard['account_id'] == proposal['account_id']
                 and guard['blocked'] is False and guard['protected'] is False
                 and guard['pending_orders'] == [] and guard['unresolved_orders'] == [], 'intraday_book_not_ready')
        controls = guard['controls']
        for key, default in (('policy', {}), ('enabled', False), ('user_paused', False), ('mirror_intent', None)):
            _require(key in controls and controls[key] == self._read(db, key, default), 'intraday_controls_unbound')
        _require(canonical_hash(controls) == proposal['controls_hash'], 'intraday_controls_changed')
        _require(isinstance(guard['economic_book'], dict)
                 and canonical_hash(guard['economic_book']) == proposal['economic_book_hash'], 'intraday_book_changed')
        _require(not self.stopping(), 'intraday_stopping')
        return state

    @staticmethod
    def _broker_day(proposal, today, broker_days):
        _require(isinstance(broker_days, (list, tuple, set)) and bool(broker_days), 'intraday_broker_days_missing')
        days = sorted(set(broker_days))
        _require(all(date.fromisoformat(item).isoformat() == item for item in days), 'intraday_broker_days_invalid')
        prior = [item for item in days if item < today]
        _require(today == proposal['effective_day'] and today in days and prior
                 and prior[-1] == proposal['signal_day'], 'intraday_not_next_broker_day')

    def stage(self, proposal):
        """Archive a validated immutable proposal without publishing or activating."""
        self.last_error = None
        try:
            proposal = deepcopy(proposal)
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._validate(db, proposal, check_time=True)
                self._guard(db, proposal)
                previous = db.execute('SELECT payload FROM intraday_policy_transitions WHERE id=?',
                                      (proposal['id'],)).fetchone()
                if previous:
                    _require(previous[0] == encode(proposal), 'intraday_transition_conflict')
                    return True
                _require(not db.execute('SELECT 1 FROM runs WHERE id=?', (proposal['prepared_run']['id'],)).fetchone(),
                         'intraday_run_already_published')
                _require(not db.execute('SELECT 1 FROM intraday_policy_transitions WHERE approval_id=?',
                                        (proposal['authorization']['id'],)).fetchone(), 'intraday_approval_already_used')
                db.execute('INSERT INTO intraday_policy_transitions VALUES (?,?,?,?,?)',
                           (proposal['id'], proposal['fingerprint'], proposal['effective_day'],
                            proposal['authorization']['id'], encode(proposal)))
            return True
        except (KeyError, TypeError, ValueError, ArithmeticError, sqlite3.IntegrityError) as error:
            self.last_error = str(error)
            return False

    def activate(self, transition_id, *, broker_days):
        """Publish the exact staged run and revision atomically, once per account/day."""
        self.last_error = None
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT payload FROM intraday_policy_transitions WHERE id=?', (transition_id,)).fetchone()
                _require(row is not None, 'intraday_transition_missing')
                proposal = json.loads(row[0])
                _require(not db.execute('SELECT 1 FROM intraday_policy_activations WHERE transition_id=?',
                                        (transition_id,)).fetchone(), 'intraday_already_activated')
                run = self._validate(db, proposal, check_time=True)
                self._broker_day(proposal, self._now().date().isoformat(), broker_days)
                state = self._guard(db, proposal)
                _require(not db.execute('SELECT 1 FROM intraday_policy_activations WHERE fingerprint=? AND effective_day=?',
                            (proposal['fingerprint'], proposal['effective_day'])).fetchone(), 'intraday_day_already_used')
                revision = proposal['parent_active']['revision'] + 1
                adopted_at = self._now().astimezone(timezone.utc).isoformat()
                # Check the expiry again after validation, directly before publication.
                _require(_stamp(adopted_at) < _stamp(proposal['expires_at']) and not self.stopping(),
                         'intraday_activation_expired')
                active = {key: deepcopy(proposal[key]) for key in
                          ('policy', 'candidate_id', 'signal_day', 'fingerprint', 'limits')}
                active.update(parent_id=proposal['parent_active']['candidate_id'],
                    activation_kind='intraday', type='intraday_override', transition_id=transition_id,
                    source_decision_id=proposal['source_decision_id'], adopted_at=adopted_at,
                    effective_day=proposal['effective_day'], run_id=run['id'], revision=revision)
                activation = seal({'transition_id': transition_id, 'proposal_hash': proposal['hash'],
                    'run_hash': canonical_hash(_stored_run(run)), 'active': active,
                    'parent_active_hash': canonical_hash(proposal['parent_active']),
                    'evaluation_receipt_hash': proposal['evaluation_receipt']['hash']})
                db.execute('INSERT INTO runs VALUES (?,?,?,?)',
                           (run['id'], run['as_of'], run['created_at'], encode(_stored_run(run))))
                self.store._index_run(db, run)
                db.execute('INSERT INTO intraday_policy_activations VALUES (?,?,?,?,?,?)',
                           (transition_id, proposal['fingerprint'], proposal['effective_day'], revision,
                            run['id'], encode(activation)))
                db.execute('INSERT OR IGNORE INTO learning_policies VALUES (?,?)',
                           (active['candidate_id'], encode(active['policy'])))
                state.update(active=active, revision=revision, status='active', error=None,
                             last_intraday_transition=transition_id)
                state.pop('activation_check', None)
                db.execute('UPDATE settings SET value=? WHERE key=?',
                           (encode(state), self._key(proposal['fingerprint'])))
            return True
        except (KeyError, TypeError, ValueError, ArithmeticError, sqlite3.IntegrityError) as error:
            self.last_error = str(error)
            return False

    def resolve_run(self, *, today, broker_days):
        """Read only this activated revision's immutable run, never latest analysis.

        Expiry limits activation, not execution of an already activated run.
        The normal executor continues to enforce live entry/exit/risk controls.
        """
        self.last_error = None
        try:
            with self.store.connect() as db:
                db.execute('BEGIN')
                policy = self._read(db, 'policy', {})
                state = self._read(db, self._key(policy['fingerprint']), {})
                active = state.get('active') or {}
                if active.get('activation_kind') != 'intraday':
                    return None
                identity = active['transition_id']
                row = db.execute('SELECT payload FROM intraday_policy_transitions WHERE id=?', (identity,)).fetchone()
                activated = db.execute('SELECT payload FROM intraday_policy_activations WHERE transition_id=?', (identity,)).fetchone()
                _require(row is not None and activated is not None, 'intraday_activation_missing')
                proposal, activation = json.loads(row[0]), json.loads(activated[0])
                run = self._validate(db, proposal, check_time=False)
                self._broker_day(proposal, today, broker_days)
                _require(today == self._now().date().isoformat(), 'intraday_resolver_day_invalid')
                _require(_sealed(activation) and activation['active'] == active
                         and state['revision'] == active['revision'] == proposal['parent_active']['revision'] + 1
                         and activation['proposal_hash'] == proposal['hash']
                         and activation['evaluation_receipt_hash'] == proposal['evaluation_receipt']['hash']
                         and activation['parent_active_hash'] == canonical_hash(proposal['parent_active'])
                         and policy['account_id'] == proposal['account_id']
                         and {key: policy.get(key) for key in LIMIT_KEYS} == proposal['limits'],
                         'intraday_activation_changed')
                stored = db.execute('SELECT day,created_at,payload FROM runs WHERE id=?', (run['id'],)).fetchone()
                index = db.execute('SELECT summary FROM run_index WHERE id=?', (run['id'],)).fetchone()
                _require(stored is not None and index is not None
                         and stored[:2] == (run['as_of'], run['created_at'])
                         and json.loads(stored[2]) == _stored_run(run)
                         and canonical_hash(json.loads(stored[2])) == activation['run_hash']
                         and json.loads(index[0]) == {key: value for key, value in run.items() if key != 'analysis'},
                         'intraday_published_run_changed')
                return deepcopy(run)
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            self.last_error = str(error)
            return None

    def resolve_analysis_run(self):
        """Reuse verified signals for display; execution still verifies live days."""
        try:
            with self.store.connect() as db:
                fingerprint = self._read(db, 'policy', {})['fingerprint']
                active = self._read(db, self._key(fingerprint), {})['active']
                row = db.execute('SELECT payload FROM intraday_policy_transitions WHERE id=?',
                                 (active['transition_id'],)).fetchone()
                _require(row is not None, 'intraday_transition_missing')
                proposal = json.loads(row[0])
                days = proposal['evaluation_receipt']['broker_days']
            return self.resolve_run(today=self._now().date().isoformat(), broker_days=days)
        except (KeyError, TypeError, ValueError) as error:
            self.last_error = str(error)
            return None

    def snapshot(self):
        """Compact separate provenance; daily decision history remains untouched."""
        with self.store.connect() as db:
            fingerprint = (self._read(db, 'policy', {}) or {}).get('fingerprint')
            rows = db.execute('SELECT id,effective_day FROM intraday_policy_transitions WHERE fingerprint=? '
                              'ORDER BY effective_day DESC,id DESC', (fingerprint,)).fetchall()
            active = (self._read(db, self._key(str(fingerprint)), {}) or {}).get('active')
            return {'version': VERSION, 'staged': [{'id': row[0], 'effective_day': row[1]} for row in rows],
                    'active': deepcopy(active) if active and active.get('activation_kind') == 'intraday' else None,
                    'error': self.last_error}
