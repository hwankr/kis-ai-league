"""Version Python sources independently of checkout CRLF/LF conversion."""
import hashlib
from pathlib import Path


HASH_SCHEME = 'py-lf-v1'
LEARNING_SOURCES = ('learning.py', 'experiments.py', 'daily_inputs.py', 'research_rules.py', 'learning_policy.py', 'learning_evaluation.py',
                    'learning_account.py', 'learning_attribution.py', 'learning_history.py', 'portfolio_plan.py',
                    'daily_comparison.py', 'intraday_policy.py', 'intraday_runtime.py',
                    'intraday_scenarios.py', 'maintenance.py', 'source_version.py')
DAILY_SOURCES = ('daily_policy.py', 'experiments.py', 'daily_inputs.py', 'research_rules.py', 'daily_evaluation.py', 'learning_policy.py',
                 'learning_evaluation.py', 'learning_research.py', 'learning_proposals.py', 'learning_history.py',
                 'learning_account.py', 'portfolio_plan.py', 'policy_transition.py',
                 'policy_award.py', 'policy_award_scenarios.py', 'policy_capacity.py',
                 'daily_comparison.py', 'intraday_policy.py', 'intraday_runtime.py',
                 'intraday_scenarios.py', 'maintenance.py', 'source_version.py')


def source_version(names, *, root=None):
    """Normalize physical CRLF only; preserve all other source bytes and boundaries."""
    directory = Path(root) if root is not None else Path(__file__).parent
    digest = hashlib.sha256()
    for name in names:
        source = (directory / name).read_bytes().replace(b'\r\n', b'\n')
        digest.update(name.encode('utf-8') + b'\0')
        digest.update(len(source).to_bytes(8, 'big'))
        digest.update(source)
    return HASH_SCHEME + ':' + digest.hexdigest()
