"""Reviewed compatibility scopes; never infer transitive compatibility."""
import hashlib
import json

# 2026-10-12: date-key benchmark repair and separately stored market comparisons.
# Frozen signals, quotes, positions, replay and promotion criteria are unchanged.
# This is a direct migration, not an alias: original trial versions stay intact.
# Any later change to the five evaluation files needs its own compatibility review.
REPORTING_ONLY_UPGRADES = frozenset({(
    '3c4aab6471f3d2453474a289cb7cb8c74f8cbf015d7f97d2f872639ef5f94655',
    'a9c9b6f1994138a3daac25d432fca3cd5f14e68ad17778117abd38635004b9d4',
)})


def version_matches(recorded, current):
    return bool(recorded and current and
                (recorded == current or (recorded, current) in REPORTING_ONLY_UPGRADES))


# Only an unchanged, already frozen live-entry run can cross a reviewed research
# observation upgrade. Each tuple pins old/new engines, run ID and the complete
# run_index summary, including signals, policy, entry context and analysis ID.
# 2026-10-21: reviewed observation/readiness changes leave execution semantics
# unchanged. Only the original completed 8edd run crosses this exact upgrade;
# its 81 signals and complete summary are preserved, never regenerated.
# These exceptions never authorize research frames, trials, prepared policies
# or activation. Source and original hashes are sealed in the local repair audit.
LIVE_ENTRY_RUN_UPGRADES = frozenset({(
    'py-lf-v1:b8876af865dc5adea3b2219e2db6d78df51bff058afad845f277bce230867358',
    'py-lf-v1:bf628d8894049b8f168788fcd36db7d73ea12bd875e6d6cfbcdb29a3d9e8d9d3',
    '8edd96584bef4f9fba59788cb3022de1',
    '673267ea0b210cdd8fd059c0ad019ce70614393f5843d75098100c05f86ef024',
)})


def live_entry_version_matches(run, current):
    """Check an execution summary without relabeling its recorded engine."""
    if not isinstance(run, dict):
        return False
    recorded = run.get('learning_engine_version')
    if not isinstance(recorded, str) or not recorded or not isinstance(current, str) or not current:
        return False
    if recorded == current:
        return True
    # The live runner reads run_index summaries. Do not silently discard extra
    # fields or grant a research/full-run caller the same exception.
    if 'analysis' in run or not isinstance(run.get('id'), str):
        return False
    try:
        payload = json.dumps(run, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError):
        return False
    digest = hashlib.sha256(payload).hexdigest()
    return (recorded, current, run['id'], digest) in LIVE_ENTRY_RUN_UPGRADES


# Daily-policy orchestration preserves this fixed initial-policy replay only.
# These exceptions must never authorize a frozen trial or a prepared order run.
SCORECARD_ONLY_UPGRADES = frozenset({
    ('3c4aab6471f3d2453474a289cb7cb8c74f8cbf015d7f97d2f872639ef5f94655',
     'd98ff33aff3c5882e7ef3d433e6836153516364b30e9e229d3866ce23f7b78cf'),
    ('a9c9b6f1994138a3daac25d432fca3cd5f14e68ad17778117abd38635004b9d4',
     'd98ff33aff3c5882e7ef3d433e6836153516364b30e9e229d3866ce23f7b78cf'),
    # 2026-10-13: reviewed scorecard replay is unchanged. Only research-history
    # delivery and source-version calculation changed in the learning sources.
    # The last three raw IDs are 6553272's CRLF, LF and observed mixed checkout.
    # These exact pairs must not validate a frozen trial or prepared order run.
    *((old, 'py-lf-v1:4d07d05ba03cc65e975844877bd5b2a7596d70f337cf4e9ccacaf7626aa8db03') for old in (
        '3c4aab6471f3d2453474a289cb7cb8c74f8cbf015d7f97d2f872639ef5f94655',
        'a9c9b6f1994138a3daac25d432fca3cd5f14e68ad17778117abd38635004b9d4',
        'd98ff33aff3c5882e7ef3d433e6836153516364b30e9e229d3866ce23f7b78cf',
        '51703606e3d768336d3f770c8fb1acc5126945d1ae80ef9c323523487a00cede',
        '10b9e8644d4116a5827dc556727ef39ded67e77b2c67ab713721208f016f8bcb',
    )),
})


def scorecard_version_matches(recorded, current):
    return version_matches(recorded, current) or (recorded, current) in SCORECARD_ONLY_UPGRADES


# A single-session recovery of the original 10/22 executable summary only.
# This pins the reviewed staged runner; any further source change needs review.
LIVE_ENTRY_RECOVERY_UPGRADES = frozenset({(
    'py-lf-v1:bf628d8894049b8f168788fcd36db7d73ea12bd875e6d6cfbcdb29a3d9e8d9d3',
    'py-lf-v1:9ddcbcab4074fc1a974e75dd184749754829f4dfdd4dad5b2e327b8abf77f787',
    '2026-10-23', 'ad04281393a54527af5924829aa932e1', 3,
    '2113a2b2cf6dbfffe1da0de55c6e227fb9c8c53b7fdec236ce41e3ce92e1d603',
    '87083173b1450f17dab9865679f8bceccbf9ff9a1a3ebe67bb38eaf657f7aa11',
)})


def scoped_live_entry_version_matches(run, current, *, execution_day, active):
    """A direct reviewed transition; never a research or activation alias."""
    if live_entry_version_matches(run, current):
        return True
    if not isinstance(run, dict) or 'analysis' in run or not isinstance(active, dict):
        return False
    from backend.learning_policy import policy_id
    try:
        policy = run['learning_policy']
        if active.get('policy') != policy or active.get('candidate_id', policy_id(policy)) != policy_id(policy):
            return False
        payload = json.dumps(run, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        return (run.get('learning_engine_version'), current, execution_day, run['id'],
                active['revision'], policy_id(policy), hashlib.sha256(payload).hexdigest()) in LIVE_ENTRY_RECOVERY_UPGRADES
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
