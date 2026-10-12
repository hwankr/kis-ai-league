"""Preserve validated LLM proposals without changing candidate selection."""
from copy import deepcopy
import hashlib
from pathlib import Path

from backend.experiment_store import encode
from backend.learning_policy import policy_id, validate_policy


def proposal_context(champion, frames, limits, history, state, provider, requested_at):
    try:
        public = provider.public_config() if provider is not None else {}
    except Exception:
        # Metadata is optional for custom providers; proposal persistence is not.
        public = {}
    public = public if isinstance(public, dict) else {}
    # These are provider metadata, never endpoints, credentials or raw responses.
    metadata = {key: public[key] for key in ('provider', 'model', 'reasoning_effort')
                if isinstance(public.get(key), str) and len(public[key]) <= 100}
    return {
        'as_of': frames[-1]['as_of'] if frames else None,
        'requested_at': requested_at,
        'fingerprint': state.get('fingerprint'),
        'engine_version': state.get('engine_version'),
        'parent_id': policy_id(champion),
        'limits': deepcopy(limits),
        'development_sessions': len(frames),
        'provider': metadata,
        'research_input_hash': hashlib.sha256(encode({
            'champion': champion, 'frames': frames, 'limits': limits, 'history': history,
        }).encode('utf-8')).hexdigest(),
        'research_version': hashlib.sha256(Path(__file__).with_name('learning_research.py').read_bytes()).hexdigest(),
    }


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
