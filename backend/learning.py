"""Prospective policy research, independent of the order worker.

One frozen challenger is compared with its incumbent on new daily observations.
Development data never count toward promotion. No broker or order API is used.
"""
from copy import deepcopy
from datetime import datetime, time, timezone
import hashlib
import json
from pathlib import Path
import threading

from backend.experiment_store import encode
from backend.kis import KST
from backend.request_gate import file_lock

def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class LearningService:
    def __init__(self, store, *, enabled, now, proposer=None, evaluator=None, llm_factory=None, development_reader=None):
        self.store, self.enabled, self.now = store, enabled, now
        from backend.learning_account import LearningAccount
        LearningAccount(store)
        self.proposer, self.evaluator, self.llm_factory = proposer, evaluator, llm_factory
        self.development_reader = development_reader
        self.worker = None
        self.stopping = False
        self.lock = threading.Lock()
        self.pending = None
        self.attempted = None
        self.version = hashlib.sha256(Path(__file__).read_bytes() +
                                      Path(__file__).with_name("learning_policy.py").read_bytes() +
                                      Path(__file__).with_name("learning_evaluation.py").read_bytes()).hexdigest()
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS learning_frames
                    (day TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_policies
                    (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS learning_trials
                    (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    def _timestamp(self):
        return self.now().astimezone(timezone.utc).isoformat(timespec="microseconds")

    def state(self):
        return self.store.setting("learning", {})

    def champion(self):
        from backend.learning_policy import BASELINE_POLICY
        return deepcopy(self.state().get("champion", {}).get("policy", BASELINE_POLICY))

    def frames(self, limit=300):
        with self.store.connect() as db:
            values = db.execute("SELECT payload FROM learning_frames ORDER BY day DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in reversed(values)]

    def history(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT payload FROM learning_trials ORDER BY created_at DESC,id DESC").fetchall()
        history = [json.loads(row[0]) for row in rows]
        return [item if index < 30 else {"candidate_id": item["candidate_id"], "policy": item["policy"], "decision": item["decision"]}
                for index, item in enumerate(history)]

    def _research_history(self):
        from backend.learning_policy import policy_id
        history = self.history()
        fingerprint = (self.store.setting("policy") or {}).get("fingerprint")
        groups = {}
        for order in self.store.orders():
            if not fingerprint or order.get("fingerprint") != fingerprint or order.get('submission_skipped'):
                continue
            identity = order.get("policy_id")
            if not identity:
                from backend.learning_policy import BASELINE_POLICY
                identity = policy_id(BASELINE_POLICY)
            groups.setdefault(identity, []).append(order)
        def execution(identity):
            orders = groups.get(identity, [])
            terminal = [order for order in orders if order["status"] in {"filled", "cancelled", "rejected"}]
            accepted = [order for order in terminal if order["status"] != "rejected"]
            return {"terminal_orders": len(terminal), "unresolved_orders": len(orders) - len(terminal),
                    "rejection_rate": sum(order["status"] == "rejected" for order in terminal) / len(terminal) if terminal else None,
                    "fill_ratio": sum(order["filled_quantity"] / order["quantity"] for order in accepted) / len(accepted) if accepted else None}
        for item in history:
            item["execution"] = execution(item["candidate_id"])
        champion = self.champion()
        return [{"policy": champion, "candidate_id": policy_id(champion), "decision": "execution_feedback",
                 "execution": execution(policy_id(champion))}, *history]

    def snapshot(self):
        from backend.learning_policy import policy_id
        state, policy = self.state(), self.champion()
        trial = state.get("trial")
        return {"enabled": self.enabled,
                "status": "disabled" if not self.enabled else state.get("status", "waiting"),
                "champion": {"id": policy_id(policy), "name": policy["name"],
                             "adopted_at": state.get("champion", {}).get("adopted_at")},
                "challenger": {"id": trial["candidate_id"], "name": trial["policy"]["name"],
                               "started_at": trial["created_at"],
                               "sessions": trial.get('elapsed_intervals', 0)} if trial else None,
                "last_evaluation": state.get("last_evaluation"), "last_change": state.get("last_change"),
                "error": state.get("error")}

    def enqueue(self, data, baseline_signals, limits, *, source_id, context=None):
        if not self.enabled or self.stopping:
            return
        # The input object belongs to an immutable completed analysis. Repeated
        # polling does not create another LLM request, evaluation or database row.
        with self.lock:
            if self.attempted == source_id:
                return
            self.pending = (data, baseline_signals, limits, source_id, context)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, daemon=True, name="policy-research")
                self.worker.start()

    def close(self):
        self.stopping = True
        if self.worker and self.worker.is_alive():
            self.worker.join(5)
        return self.worker is None or not self.worker.is_alive()

    def _work(self):
        while not self.stopping:
            with self.lock:
                work, self.pending = self.pending, None
                if work is None:
                    self.worker = None
                    return
                data, baseline, limits, self.attempted, context = work
            try:
                with file_lock(self.store.path.parent / "learning.lock", blocking=False):
                    self.process(data, baseline, limits, context=context)
            except BlockingIOError:
                return
            except Exception:
                # Research failure is visible but never changes order permission.
                state = self.state()
                state.update(status="error", error="개선 실험을 완료하지 못했습니다. 다음 완료 자료에서 재시도합니다.")
                self.store.save_setting("learning", state)

    def _save(self, state, trial=None):
        from backend.learning_policy import policy_id
        if self.stopping:
            return
        with self.store.connect() as db:
            db.execute("INSERT INTO settings VALUES ('learning',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                       "WHERE settings.value != excluded.value", (encode(state),))
            for policy in [state.get("champion", {}).get("policy"), (state.get("trial") or {}).get("policy")]:
                if policy:
                    db.execute("INSERT OR IGNORE INTO learning_policies VALUES (?,?)", (policy_id(policy), encode(policy)))
            for trial in (trial if isinstance(trial, list) else [trial]):
                if trial is None:
                    continue
                db.execute("INSERT INTO learning_trials VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload "
                           "WHERE learning_trials.payload != excluded.payload", (trial["id"], trial["created_at"], encode(trial)))
            fingerprint, day = state.get('scorecard_fingerprint'), state.get('scorecard_day')
            if fingerprint and day:
                row = db.execute('SELECT payload FROM learning_daily WHERE fingerprint=? AND day=?', (fingerprint, day)).fetchone()
                if row:
                    payload = json.loads(row[0])
                    payload['comparisons'] = state.get('scorecard')
                    db.execute('UPDATE learning_daily SET payload=? WHERE fingerprint=? AND day=? AND payload!=?',
                               (encode(payload), fingerprint, day, encode(payload)))

    @staticmethod
    def _cohort_error(frames, data):
        # Catch adjusted-history revisions without rewriting the original evidence.
        histories = {symbol: {str(day): bar for day, bar in bars.items()}
                     for symbol, bars in data.get("histories", {}).items()}
        for frame in frames:
            for row in frame["rows"]:
                bar = histories.get(row["symbol"], {}).get(frame["as_of"])
                if bar and any(float(bar[field]) != float(row[field]) for field in ("open", "high", "low", "close", "volume")):
                    return "평가 기간의 원본 가격이 수정되어 비교를 무효 처리합니다."
        for previous, following in zip(frames, frames[1:]):
            deadline = datetime.combine(datetime.fromisoformat(following["as_of"]).date(), time(9), KST)
            if datetime.fromisoformat(previous["captured_at"]) >= deadline:
                return "장전까지 완료되지 않은 관측이 있어 비교를 무효 처리합니다."
        return None

    @staticmethod
    def judge(incumbent, candidate, spec=None):
        from backend.learning_evaluation import make_evaluation_spec, evaluate_candidate
        result = evaluate_candidate(incumbent, candidate, spec or make_evaluation_spec(1))
        return result["decision"], result["reason"]

    def _scorecard(self, state, frames, limits, model, fingerprint, initial_state=None):
        """Carry the unchanged first policy forward; never select only winning windows."""
        from backend.learning_policy import BASELINE_POLICY, portfolio_metrics
        frame = frames[-1]
        if state.get('scorecard_fingerprint') not in (None, fingerprint):
            # Each account has its own record; previous account history remains in SQLite.
            self.store.save_setting('learning_scorecard:' + str(state['scorecard_fingerprint']),
                                    {key: value for key, value in state.items() if key.startswith('scorecard')})
            for key in list(state):
                if key.startswith('scorecard'):
                    state.pop(key)
            state.update(self.store.setting('learning_scorecard:' + str(fingerprint), {}))
        state['scorecard_fingerprint'] = fingerprint
        score = state.setdefault('scorecard', {'baseline_return_pct': None, 'market_return_pct': None,
            'cash_return_pct': 0, 'status': 'waiting', 'basis': 'observed_quotes_model',
            'market_name': 'KOSPI 가격지수', 'error': None, 'start_day': frame['as_of'], 'ai_return_pct': None})
        if state.get('scorecard_day') == frame['as_of']:
            return
        if not state.get('scorecard_start'):
            if initial_state and initial_state.get('pending_orders'):
                return
            state['scorecard_start'] = frame['as_of']
            state['scorecard_capital'] = str(initial_state['equity'] if initial_state else limits['budget'])
            state['scorecard_limits'] = deepcopy(limits)
            state['scorecard_model'] = deepcopy(model)
            state['scorecard_engine'] = self.version
            state['scorecard_market'] = frame.get('benchmark_prices', {}).get('KOSPI')
            state['scorecard_book'] = deepcopy(initial_state) if initial_state else {
                'as_of': frame['as_of'], 'cash': str(limits['budget']),
                'positions': [], 'reserved_cash': '0', 'pending_entries': []}
            account = self.store.setting('learning_account:' + str(fingerprint), {})
            state['scorecard_ai_unit'] = account.get('unit')
            score.update(baseline_return_pct=0., status='ready')
        elif score['status'] != 'invalid':
            previous = next((item for item in frames if item['as_of'] == state.get('scorecard_day')), None)
            if previous is None or state.get('scorecard_engine') != self.version:
                score.update(status='invalid', baseline_return_pct=None, error='최초 정책 비교 자료 또는 평가 버전 변경')
            else:
                result = portfolio_metrics(BASELINE_POLICY, [previous, frame], state['scorecard_limits'],
                    initial_state=state['scorecard_book'], execution_model=state['scorecard_model'])
                if not result.get('valid'):
                    score.update(status='invalid', baseline_return_pct=None, error=result.get('error'))
                else:
                    state['scorecard_book'] = result['final_state']
                    equity = result['equity_curve'][-1]['equity']
                    score.update(baseline_return_pct=(float(equity) / float(state['scorecard_capital']) - 1) * 100,
                                 status='ready')
        first, current = state.get('scorecard_market'), frame.get('benchmark_prices', {}).get('KOSPI')
        score['market_return_pct'] = (float(current) / float(first) - 1) * 100 if first and current else None
        account = self.store.setting('learning_account:' + str(fingerprint), {})
        unit, first_unit = account.get('unit'), state.get('scorecard_ai_unit')
        score['ai_return_pct'] = (float(unit) / float(first_unit) - 1) * 100 if unit and first_unit else None
        score['start_day'] = state['scorecard_start']
        state['scorecard_day'] = frame['as_of']

    def _attribution(self, trial, cohort):
        from backend.learning_policy import portfolio_metrics
        evidence = dict.fromkeys(('signal_excess_pp', 'execution_excess_pp', 'transition_excess_pp',
                                  'execution_drag_pp', 'capital_drag_pp'))
        parents, candidates = [], []
        for kwargs in ({}, {'execution_model': trial['execution_model']},
                       {'execution_model': trial['execution_model'], 'initial_state': trial.get('initial_state')}):
            parents.append(portfolio_metrics(trial['incumbent'], cohort, trial['limits'], **kwargs))
            candidates.append(portfolio_metrics(trial['policy'], cohort, trial['limits'], **kwargs))
        for key, parent, candidate in zip(('signal_excess_pp', 'execution_excess_pp', 'transition_excess_pp'), parents, candidates):
            if parent.get('valid') and candidate.get('valid'):
                evidence[key] = candidate['net_return_pct'] - parent['net_return_pct']
        signal, execution, transition = (evidence[key] for key in
            ('signal_excess_pp', 'execution_excess_pp', 'transition_excess_pp'))
        if signal is not None and execution is not None:
            evidence['execution_drag_pp'] = execution - signal
        if execution is not None and transition is not None:
            evidence['capital_drag_pp'] = transition - execution
        stage = ('insufficient' if any(value is None for value in (signal, execution, transition)) else
                 'signal' if signal <= 0 else 'execution' if execution <= 0 else
                 'capital' if transition <= 0 else 'supported')
        return {'stage': stage, 'evidence': evidence}

    def process(self, data, baseline_signals, limits, *, context=None):
        from backend.learning_policy import BASELINE_POLICY, build_frame, policy_id, portfolio_metrics, validate_policy
        from backend.learning_research import propose_candidate
        from backend.learning_evaluation import make_evaluation_spec, evaluate_candidate
        if not self.enabled or self.stopping:
            return
        production_context = context is not None
        context = context or {}
        fingerprint = limits.get('fingerprint') or (self.store.setting('policy') or {}).get('fingerprint')
        limits = {key: str(limits[key]) for key in ('budget', 'order_cap', 'daily_buy_limit')}
        frame = build_frame(data, baseline_signals)
        frame['captured_at'] = self._timestamp()
        frame['execution_quotes'] = deepcopy(context.get('execution_quotes', {}))
        digest = _hash({key: value for key, value in frame.items() if key not in {'observed_at', 'captured_at'}})
        state = self.state()
        with self.store.connect() as db:
            prior = db.execute('SELECT digest FROM learning_frames WHERE day=?', (frame['as_of'],)).fetchone()
            if prior is None:
                db.execute('INSERT INTO learning_frames VALUES (?,?,?)', (frame['as_of'], digest, encode(frame)))
        trial = state.get('trial')
        if prior and prior[0] != digest:
            state.update(status='error', error='동일 거래일 관측이 변경됐습니다. 원본과 평가 종료일은 유지합니다.')
            if trial:
                trial.update(invalid_reason=state['error'], decision='invalid')
            self._save(state, trial)
            return
        if (state.get('processed_day') == frame['as_of'] and state.get('limits') == limits
                and state.get('engine_version') == self.version):
            return
        frames = self.frames()
        if not frames or frames[-1]['as_of'] != frame['as_of']:
            return
        state.setdefault('champion', {'policy': deepcopy(BASELINE_POLICY), 'adopted_at': None})
        model = deepcopy(context.get('execution_model') or {'mode': 'daily_open'})
        state.update(error=None, limits=limits)
        if not production_context or context.get('initial_state'):
            self._scorecard(state, frames, limits, model, fingerprint, context.get('initial_state'))
        if trial:
            trial.setdefault('sequence', state.get('trial_sequence', 1))
            state['trial_sequence'] = max(state.get('trial_sequence', 0), trial['sequence'])
            trial.setdefault('evaluation_spec', make_evaluation_spec(trial['sequence']))
            trial.setdefault('execution_model', model)
            if trial.get('engine_version') != self.version:
                trial['invalid_reason'] = '평가 코드 버전 변경'
            if trial['limits'] != limits or trial.get('fingerprint') != fingerprint:
                trial['invalid_reason'] = '운용 계좌 또는 한도 변경'
            # Count exchange return intervals, not executed trades or polling calls.
            trial['calendar_days'] = sorted(set(trial.get('calendar_days', [])) |
                {day for day in frame['session_days'] if trial['start_day'] < day <= frame['as_of']})
            elapsed = len(trial['calendar_days'])
            trial['elapsed_intervals'] = elapsed
            cohort = [item for item in frames if item['as_of'] >= trial['start_day']]
            horizon = trial['evaluation_spec']['horizon']
            trial.setdefault('initial_state', context.get('initial_state'))
            error = trial.get('invalid_reason') or self._cohort_error(cohort, data)
            evaluate = self.evaluator or portfolio_metrics
            kwargs = {} if self.evaluator else {'initial_state': trial.get('initial_state'),
                                                'execution_model': trial['execution_model']}
            a = evaluate(trial['incumbent'], cohort[:horizon + 1], trial['limits'], **kwargs)
            b = evaluate(trial['policy'], cohort[:horizon + 1], trial['limits'], **kwargs)
            if error or len(cohort) > 1 and (not a.get('valid') or not b.get('valid')):
                trial['invalid_reason'] = error or a.get('error') or b.get('error') or '평가 자료 부족'
            result = ({'decision': 'invalid', 'reason': trial['invalid_reason'], 'phase': 'paper_provisional'}
                      if trial.get('invalid_reason') else evaluate_candidate(a, b, trial['evaluation_spec']))
            decision, reason = result['decision'], result['reason']
            state['last_evaluation'] = {'as_of': frame['as_of'], 'sessions': elapsed,
                'required_sessions': horizon, 'champion_return_pct': a.get('net_return_pct'),
                'challenger_return_pct': b.get('net_return_pct'), 'champion_drawdown_pct': a.get('max_drawdown_pct'),
                'challenger_drawdown_pct': b.get('max_drawdown_pct'), 'closed_trades': b.get('closed_trades', 0),
                'decision': decision, 'reason': reason, 'phase': 'paper_provisional',
                'statistics': result.get('statistics')}
            trial.update(metrics={'champion': a, 'challenger': b}, decision=decision, reason=reason,
                         evaluated_through=frame['as_of'], statistics=result.get('statistics'))
            # Invalid attempts consume their original interval too. Never erase an unlucky window.
            finished = elapsed >= horizon
            if finished and decision == 'promote':
                before = state['champion']
                state['previous_champion'] = before
                state['champion'] = {'policy': trial['policy'], 'adopted_at': self._timestamp(),
                                     'phase': 'paper_provisional'}
                state['last_change'] = {'at': self._timestamp(), 'from': policy_id(before['policy']),
                    'to': trial['candidate_id'], 'reason': reason, 'trial_id': trial['id']}
            if finished:
                if decision == 'waiting':
                    trial.update(decision='keep', reason='고정 평가 기간의 근거 부족')
                trial['attribution'] = self._attribution(trial, cohort[:horizon + 1])
                state.pop('trial', None)
            self._save(state, trial)
        state['engine_version'] = self.version
        state.pop('guard', None)  # Relative rollback must earn a new fixed-window comparison.
        if not state.get('trial'):
            book = context.get('initial_state')
            if production_context and (not book or book.get('pending_orders') or book.get('unresolved_orders')
                    or book.get('as_of') != frame['as_of'] or frame['as_of'] != self.now().astimezone(KST).date().isoformat()):
                state.update(status='waiting', error=context.get('book_error'), processed_day=frame['as_of'])
                self._save(state)
                return
            state.update(status='researching')
            self._save(state)
            development = frames
            if self.development_reader and len(frames) < 120:
                try:
                    historical = self.development_reader(data)
                    combined = {item['as_of']: item for item in historical if item['as_of'] <= frame['as_of']}
                    combined.update({item['as_of']: item for item in frames})
                    development = [combined[day] for day in sorted(combined)][-300:]
                except Exception:
                    pass
            sequence = state.get('trial_sequence', 0) + 1
            previous = state.get('previous_champion', {}).get('policy')
            if previous and sequence % 3 == 0 and policy_id(previous) != policy_id(state['champion']['policy']):
                proposal = {'policy': previous, 'method': 'prior_policy_retest', 'development': None,
                    'rationale': '이전 정책도 새 관측에서 같은 기준으로 비교합니다.',
                    'parent_id': policy_id(state['champion']['policy']), 'change_family': 'rollback',
                    'hypothesis': '이전 정책으로 전환하면 현재 상태의 성과가 개선되는가', 'changes': []}
            else:
                llm = self.llm_factory() if self.llm_factory else None
                proposal = (self.proposer or propose_candidate)(state['champion']['policy'], development, limits,
                    self._research_history(), llm=llm)
            policy = validate_policy(proposal['policy'])
            identity = policy_id(policy)
            if identity == policy_id(state['champion']['policy']):
                raise ValueError('duplicate_challenger')
            created = self._timestamp()
            trial = {'id': _hash([identity, created]), 'candidate_id': identity, 'created_at': created,
                'start_day': max(frame['as_of'], self.now().astimezone(KST).date().isoformat()),
                'policy': policy, 'incumbent': deepcopy(state['champion']['policy']), 'limits': limits,
                'fingerprint': fingerprint, 'engine_version': self.version, 'sequence': sequence,
                'evaluation_spec': make_evaluation_spec(sequence), 'execution_model': model,
                'execution_basis': deepcopy(context.get('execution_basis')),
                'initial_state': deepcopy(book), 'decision': 'waiting', 'elapsed_intervals': 0,
                **{key: proposal.get(key) for key in ('method', 'rationale', 'development',
                    'parent_id', 'change_family', 'hypothesis', 'changes')}}
            state.update(trial=trial, trial_sequence=sequence)
        state.update(status='evaluating', processed_day=frame['as_of'])
        self._save(state, state.get('trial'))
