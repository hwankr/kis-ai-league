"""Preserve validated LLM proposals without changing candidate selection."""
from copy import deepcopy
import hashlib
from pathlib import Path

from backend.experiment_store import encode
from backend.learning_policy import policy_id, validate_policy


def template_proposal_source(packet, policy_or_id):
    """Describe deterministic generation separately from any LLM observation.

    frame_input_hash covers threshold inputs only. It is deliberately not named
    research_input_hash: the latter also binds champion, limits and history.
    """
    from backend.policy_capacity import signal_hold_metadata
    metadata = signal_hold_metadata(packet, policy_or_id)
    identity = policy_or_id if isinstance(policy_or_id, str) else policy_id(policy_or_id)
    roles = metadata['roles']
    return {'source': 'deterministic_signal_hold_packet',
            'representation': 'validated_normalized_policy', 'candidate_id': identity,
            'actual_parent_id': packet['actual_parent_id'], 'packet_hash': packet['hash'],
            'template_version': packet['template_version'],
            'frame_input_hash': packet['input_hash'], 'summary_hash': packet['summary_hash'],
            'threshold_basis': packet['threshold_basis'],
            'roles': deepcopy(roles),
            'template_families': list(dict.fromkeys(role.split('_')[1] for role in roles if role.startswith('T_'))),
            'control_roles': [role for role in roles if not role.startswith('T_')],
            'llm_generated': False, 'registered_before_candidate_performance': True}


def proposal_context(champion, frames, limits, history, state, provider, requested_at, *, competition_context=None,
                     award_context=None):
    from backend.learning_research import public_competition_context
    competition = public_competition_context(competition_context)
    try:
        public = provider.public_config() if provider is not None else {}
    except Exception:
        # Metadata is optional for custom providers; proposal persistence is not.
        public = {}
    public = public if isinstance(public, dict) else {}
    # These are provider metadata, never endpoints, credentials or raw responses.
    metadata = {key: public[key] for key in ('provider', 'model', 'reasoning_effort')
                if isinstance(public.get(key), str) and len(public[key]) <= 100}
    inputs = {'champion': champion, 'frames': frames, 'limits': limits, 'history': history}
    if competition is not None:
        inputs['competition_context'] = competition
    if award_context is not None:
        from backend.daily_inputs import validate_award_context, freeze_research_inputs
        from backend.daily_policy import VERSION as DAILY_VERSION
        if not validate_award_context(award_context):
            raise ValueError('invalid_award_context')
        inputs['award_context'] = deepcopy(award_context)
        request = freeze_research_inputs(champion, frames, limits, history, award_context)
        decision_id = hashlib.sha256(encode([DAILY_VERSION, state.get('fingerprint'),
                                             award_context['signal_day']]).encode()).hexdigest()
    return {
        'as_of': frames[-1]['as_of'] if frames else None,
        'requested_at': requested_at,
        'fingerprint': state.get('fingerprint'),
        'engine_version': state.get('engine_version'),
        'parent_id': policy_id(champion),
        'limits': deepcopy(limits),
        'development_sessions': len(frames),
        'provider': metadata,
        **({'competition_context': deepcopy(competition)} if competition is not None else {}),
        **({'award_context_hash': award_context['hash'], 'research_request_hash': request['hash'],
            'daily_decision_id': decision_id, 'research_input_recipe': 'daily-request-plus-competition-v1'}
           if award_context is not None else {'research_input_preimage': deepcopy(inputs)}),
        'research_input_hash': hashlib.sha256(encode(inputs).encode('utf-8')).hexdigest(),
        'research_version': hashlib.sha256(Path(__file__).with_name('learning_research.py').read_bytes()).hexdigest(),
    }


def proposal_input_preimage(context, decision=None):
    """Reconstruct an LLM input from its one authoritative daily request copy."""
    from backend.daily_inputs import validate_research_inputs
    if context.get('research_input_recipe') == 'daily-request-plus-competition-v1':
        request = (decision or {}).get('research_inputs')
        if (not decision or decision.get('id') != context.get('daily_decision_id')
                or not validate_research_inputs(request)
                or request['hash'] != context.get('research_request_hash')
                or request['award_context']['hash'] != context.get('award_context_hash')):
            raise ValueError('daily_research_request_unavailable_or_changed')
        inputs = {key: deepcopy(request[key]) for key in ('champion', 'frames', 'limits', 'history', 'award_context')}
        if context.get('competition_context') is not None:
            if context['competition_context'] != request['award_context']['competition_context']:
                raise ValueError('daily_research_competition_changed')
            inputs['competition_context'] = deepcopy(context['competition_context'])
    else:
        inputs = deepcopy(context.get('research_input_preimage'))
    if inputs is None or hashlib.sha256(encode(inputs).encode()).hexdigest() != context.get('research_input_hash'):
        raise ValueError('research_input_hash_mismatch')
    return inputs


def save_llm_proposal(store, policy, *, context, captured_at):
    policy = validate_policy(deepcopy(policy))
    row = {**deepcopy(context), 'captured_at': captured_at,
           'role': 'development_only', 'source': 'llm',
           'representation': 'validated_normalized_policy',
           'policy': policy, 'candidate_id': policy_id(policy)}
    identity = hashlib.sha256(encode({key: row[key] for key in (
        'research_input_hash', 'candidate_id', 'fingerprint', 'engine_version',
        'research_version', 'provider')}).encode('utf-8')).hexdigest()
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS learning_proposals '
                   '(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)')
        # An observation is immutable; a repeated callback cannot replace it.
        db.execute('INSERT OR IGNORE INTO learning_proposals VALUES (?,?,?)',
                   (identity, captured_at, encode(row)))
