"""Frozen account anchors and post-activation paired policy observations.

Only the first settled close is retained. Current holdings are never injected
into an earlier frame. These are modeled counterfactuals, not broker returns.
"""
from copy import deepcopy
from datetime import date, datetime, time
import hashlib
import json
import math
import re

from backend.experiment_store import encode
from backend.learning_policy import KST, LEGACY_IDS, portfolio_metrics, validate_policy


def ensure_tables(store):
    with store.connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS daily_policy_contexts (
                fingerprint TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(fingerprint,day));
            CREATE TABLE IF NOT EXISTS daily_policy_followups (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL);
        ''')


def freeze_context(store, fingerprint, frame, book, model, limits, observed_at, version):
    try:
        if (not fingerprint or not isinstance(book, dict) or not isinstance(model, dict)
                or not version or book.get('as_of') != frame['as_of']):
            return
        observed = datetime.fromisoformat(observed_at)
        if (observed.utcoffset() is None or observed.astimezone(KST).date().isoformat() != frame['as_of']
                or observed.astimezone(KST).time() < time(16)
                or book.get('pending_orders') or book.get('unresolved_orders')):
            return
        value = {'initial_state': deepcopy(book), 'execution_model': deepcopy(model),
                 'observed_at': observed_at, 'engine_version': version,
                 'limits': {key: str(limits[key]) for key in ('budget', 'order_cap', 'daily_buy_limit')}}
    except (KeyError, TypeError, ValueError, AttributeError):
        return
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO daily_policy_contexts VALUES (?,?,?)',
                   (fingerprint, frame['as_of'], encode(value)))


def evaluation_context(store, fingerprint, frames, limits, version):
    if not fingerprint or len(frames) < 2:
        return None
    days = {frame['as_of'] for frame in frames[:-1]}
    expected = {key: str(limits[key]) for key in ('budget', 'order_cap', 'daily_buy_limit')}
    with store.connect() as db:
        rows = db.execute('SELECT day,payload FROM daily_policy_contexts WHERE fingerprint=? ORDER BY day',
                          (fingerprint,)).fetchall()
    for day, payload in rows:
        value = json.loads(payload)
        if day in days and value['engine_version'] == version and value['limits'] == expected:
            context = {key: deepcopy(value[key]) for key in ('initial_state', 'execution_model', 'observed_at')}
            # Validate the stored anchor against the actual following frame. A
            # current book alone cannot create a historical evaluation interval.
            from backend.daily_evaluation import _evaluation_window
            try:
                _evaluation_window(frames, context)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            return context
    return None


def observation_books(store, fingerprint, as_of, limits, version):
    """Quote requirements only: never substitute live holdings into a frozen book."""
    if not fingerprint:
        return []
    expected = {key: str(limits[key]) for key in ('budget', 'order_cap', 'daily_buy_limit')}
    with store.connect() as db:
        rows = db.execute('''SELECT payload FROM daily_policy_contexts
            WHERE fingerprint=? AND day<=? ORDER BY day DESC LIMIT 120''', (fingerprint, as_of)).fetchall()
    books = []
    for (payload,) in rows:
        value = json.loads(payload)
        if value['engine_version'] == version and value['limits'] == expected:
            books.append(deepcopy(value['initial_state']))
    return books


def _brief(result):
    keys = ('net_return_pct', 'stress_return_pct', 'max_drawdown_pct', 'stress_drawdown_pct', 'closed_trades')
    value = {key: result.get(key) for key in keys}
    value['costs'] = {key: result.get('diagnostics', {}).get('normal', {}).get(key)
                      for key in ('fees_paid', 'taxes_paid')}
    return value


def _paired_summary(results):
    if not all(result.get('valid') for result in results):
        raise ValueError(next(result.get('error') or 'invalid_replay' for result in results if not result.get('valid')))
    for result in results:
        for key in ('net_return_pct', 'stress_return_pct'):
            if type(result.get(key)) not in (float, int) or not math.isfinite(result[key]):
                raise ValueError('invalid_replay_return')
    return {'unchanged': _brief(results[0]), 'candidate': _brief(results[1]),
            'delta_net_return_pp': results[1]['net_return_pct'] - results[0]['net_return_pct'],
            'delta_stress_return_pp': results[1]['stress_return_pct'] - results[0]['stress_return_pct']}


def _scenario_summary(results, model):
    scenarios = model.get('scenarios') if isinstance(model, dict) else None
    if scenarios is None:
        return {}
    identities = [item['id'] for item in scenarios]
    if (not identities or len(set(identities)) != len(identities)
            or any(set(result.get('scenario_results', {})) != set(identities) for result in results)):
        raise ValueError('unpaired_execution_scenarios')
    values = {key: _paired_summary([result['scenario_results'][key] for result in results]) for key in identities}
    representative = results[0].get('representative_scenario')
    if representative not in values or results[1].get('representative_scenario') != representative:
        raise ValueError('unpaired_representative_scenario')
    return {'scenario_comparisons': values, 'representative_scenario': representative,
            'scenario_count': len(values), 'execution_supported': all(result.get('promotion_supported') is True for result in results),
            **{name + '_' + key: bound(item[key] for item in values.values())
               for key in ('delta_net_return_pp', 'delta_stress_return_pp')
               for name, bound in (('worst', min), ('best', max))}}


def _require_observed_legacy_decisions(policies, frames):
    if not any(policy['kind'] == 'legacy' for policy in policies):
        return
    # The final close's signals cannot affect any evaluated return interval.
    # Every earlier close needs all four real decisions before its next open.
    for frame, following in zip(frames, frames[1:]):
        decisions = frame.get('baseline_signals')
        try:
            captured = datetime.fromisoformat(frame.get('captured_at', ''))
        except (TypeError, ValueError):
            raise ValueError('legacy_baseline_signals_unavailable') from None
        if (frame.get('role') == 'development_only' or captured.utcoffset() is None
                or captured >= datetime.combine(date.fromisoformat(following['as_of']), time(9), KST)
                or not isinstance(decisions, list) or not decisions
                or any(not isinstance(item, dict) or item.get('strategy_id') not in LEGACY_IDS
                       or item.get('status') != 'ready' or item.get('action') not in {'buy', 'hold', 'avoid'}
                       for item in decisions)
                or {item['strategy_id'] for item in decisions} != set(LEGACY_IDS)):
            raise ValueError('legacy_baseline_signals_unavailable')


def update_followups(store, fingerprint, frames, version):
    """Recompute only from immutable seeds and observations after their close."""
    with store.connect() as db:
        saved = db.execute('SELECT id,payload FROM daily_policy_followups WHERE fingerprint=?',
                           (fingerprint,)).fetchall()
    for identity, payload in saved:
        record = json.loads(payload)
        seed = record.get('comparison_seed', {})
        day = seed.get('signal_day')
        selected = [frame for frame in frames if day and frame['as_of'] >= day]
        # The first actual following session, not a guessed weekday, activates it.
        if not day or not selected or selected[0]['as_of'] != day:
            status = {'status': 'unavailable', 'error': 'comparison_anchor_missing'}
        elif len(selected) < 2:
            status = {'status': 'waiting', 'basis': 'post_activation_paired_model', 'intervals': 0}
        elif selected[1]['as_of'] != record.get('effective_day'):
            status = {'status': 'unavailable', 'error': 'activation_session_mismatch'}
        elif record.get('engine_version') != version:
            status = {'status': 'unavailable', 'error': 'evaluation_version_changed'}
        else:
            digest = hashlib.sha256(encode([seed, selected, version, record.get('effective_day'),
                                           record.get('adopted_at')]).encode()).hexdigest()
            if (record.get('comparison') or {}).get('input_hash') == digest:
                continue
            try:
                observed = datetime.fromisoformat(seed['observed_at'])
                activated = datetime.fromisoformat(record['adopted_at'])
                if observed.utcoffset() is None or activated.utcoffset() is None or observed > activated:
                    raise ValueError('late_comparison_seed')
                start = datetime.combine(date.fromisoformat(day), time(16), KST)
                next_open = datetime.combine(date.fromisoformat(selected[1]['as_of']), time(9), KST)
                if not start <= observed < next_open:
                    raise ValueError('comparison_seed_not_known_at_anchor')
                if (seed['initial_state'].get('as_of') != day
                        or seed['initial_state'].get('pending_orders') or seed['initial_state'].get('unresolved_orders')):
                    raise ValueError('invalid_comparison_book')
                if activated.astimezone(KST).date().isoformat() != record['effective_day']:
                    raise ValueError('activation_date_mismatch')
                policies = [validate_policy(seed[key]) for key in ('parent_policy', 'candidate_policy')]
                _require_observed_legacy_decisions(policies, selected)
                results = [portfolio_metrics(deepcopy(policy), deepcopy(selected), deepcopy(seed['limits']),
                    initial_state=deepcopy(seed['initial_state']), execution_model=deepcopy(seed['execution_model']))
                    for policy in policies]
                status = {'status': 'ready', 'basis': 'post_activation_paired_model', 'input_hash': digest,
                          'start_day': day, 'end_day': selected[-1]['as_of'], 'intervals': len(selected)-1,
                          **_paired_summary(results), **_scenario_summary(results, seed['execution_model'])}
            except Exception as error:
                reason = str(error)
                status = {'status': 'unavailable', 'error': reason if re.fullmatch(r'[a-z][a-z0-9_]{0,100}', reason)
                          else 'comparison_evaluation_failed'}
        record['comparison'] = status
        with store.connect() as db:
            db.execute('UPDATE daily_policy_followups SET payload=? WHERE id=?', (encode(record), identity))


def followup_summaries(store, fingerprint):
    with store.connect() as db:
        rows = db.execute('SELECT id,payload FROM daily_policy_followups WHERE fingerprint=? ORDER BY rowid DESC LIMIT 30',
                          (fingerprint,)).fetchall()
    return [{'decision_id': identity, 'effective_day': (value := json.loads(payload)).get('effective_day'),
             **deepcopy(value.get('comparison', {'status': 'waiting', 'basis': 'post_activation_paired_model'}))}
            for identity, payload in rows]
