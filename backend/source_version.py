"""Version Python sources independently of checkout CRLF/LF conversion."""
import hashlib
from pathlib import Path


HASH_SCHEME = 'py-lf-v1'
LEARNING_SOURCES = ('learning.py', 'learning_policy.py', 'learning_evaluation.py',
                    'learning_account.py', 'learning_attribution.py', 'source_version.py')
DAILY_SOURCES = ('daily_policy.py', 'daily_evaluation.py', 'learning_policy.py',
                 'learning_evaluation.py', 'learning_research.py', 'source_version.py')


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
