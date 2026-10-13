"""Explicit, reviewed reporting-only upgrades; never infer transitive compatibility."""

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
