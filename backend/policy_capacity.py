"""Canonical, bounded allocation/exit controls; never changes existing lots."""
from copy import deepcopy
from datetime import date, datetime
import hashlib

from backend.experiment_store import encode
from backend.learning_policy import policy_id, validate_policy


VERSION = 'capacity-packet-v2'
LEVELS = (8, 16)
ROLES = ('B', 'A', 'E', 'AE')
SIGNAL_HOLD_VERSION = 'signal-hold-packet-v1'
SIGNAL_TEMPLATE_VERSION = 'canonical-defensive-signals-v1'
SIGNAL_FAMILIES = ('trend', 'relative', 'recovery', 'liquid')
HOLD_LEVELS = (1, 2, 4)


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def capacity_packet(base, *, top_n=8, champion=None):
    """Freeze a 2x2 factor packet with only the predeclared future-lot edits."""
    return _packet(base, top_n, policy_id(base if champion is None else champion))


def _packet(base, top_n, actual_parent_id):
    if type(top_n) is not int or top_n not in LEVELS:
        raise ValueError('invalid_capacity_level')
    base = validate_policy(deepcopy(base))
    if base['kind'] != 'rules':
        raise ValueError('capacity_base_requires_rules')
    allocation = deepcopy(base)
    allocation.update(allocation_mode='equal_eligible', max_positions=20)
    for strategy in allocation['strategies']:
        strategy['top_n'] = max(top_n, strategy['top_n'])
    def exits(value):
        value = deepcopy(value)
        for strategy in value['strategies']:
            strategy['holding_sessions'] = max(1, min(strategy['holding_sessions'], 2))
            if strategy.get('exit_mode') == 'signal_extension':
                strategy['max_holding_sessions'] = max(strategy['holding_sessions'],
                                                       min(strategy['max_holding_sessions'], 2))
        return value
    members = {role: validate_policy(value) for role, value in
               zip(ROLES, (base, allocation, exits(base), exits(allocation)))}
    member_ids = {role: policy_id(value) for role, value in members.items()}
    unique_ids = list(dict.fromkeys(member_ids.values()))
    roles_by_id = {identity: [role for role in ROLES if member_ids[role] == identity] for identity in unique_ids}
    result = {'version': VERSION, 'top_n': top_n, 'base_id': member_ids['B'],
              'actual_parent_id': actual_parent_id,
              'members': members, 'member_ids': member_ids, 'unique_ids': unique_ids,
              'degenerate_roles': [roles for roles in roles_by_id.values() if len(roles) > 1]}
    result['hash'] = _hash(result)
    return result


def capacity_metadata(packet, policy_or_id):
    """One proposal can refer to shared controls in both predeclared packets."""
    identity = policy_or_id if isinstance(policy_or_id, str) else policy_id(policy_or_id)
    expected = _packet(packet['members']['B'], packet['top_n'], packet['actual_parent_id'])
    if packet != expected or identity not in packet['unique_ids']:
        raise ValueError('invalid_capacity_packet')
    return {'version': VERSION, 'top_n': packet['top_n'], 'base_id': packet['base_id'],
            'actual_parent_id': packet['actual_parent_id'],
            'packet_hash': packet['hash'], 'member_ids': deepcopy(packet['member_ids']),
            'roles': [role for role in ROLES if packet['member_ids'][role] == identity]}


def _signal_base_changes(champion, base):
    """Only one signal family may differ; every other field stays exact."""
    if champion == base:
        return []
    if len(champion['strategies']) != len(base['strategies']):
        raise ValueError('invalid_capacity_contract')
    for family, fields in (('conditions', ('conditions',)), ('ranking', ('rank_by', 'descending'))):
        restored, changes = deepcopy(base), []
        # Policy labels carry no executable behavior; strategy slots/names do
        # remain exact so a renamed/reordered slot cannot disguise another base.
        restored['name'] = champion['name']
        if family == 'conditions':
            before, after = champion.get('candidate_scope', 'unrestricted'), base.get('candidate_scope', 'unrestricted')
            if before != after:
                changes.append({'path': 'candidate_scope', 'before': before, 'after': after})
            if 'candidate_scope' in champion:
                restored['candidate_scope'] = champion['candidate_scope']
            else:
                restored.pop('candidate_scope', None)
        for index, (parent, strategy) in enumerate(zip(champion['strategies'], base['strategies'])):
            for key in fields:
                if parent[key] != strategy[key]:
                    changes.append({'path': f'strategies[{index}].{key}',
                                    'before': deepcopy(parent[key]), 'after': deepcopy(strategy[key])})
                restored['strategies'][index][key] = deepcopy(parent[key])
        if restored == champion and changes:
            return changes
    raise ValueError('invalid_capacity_contract')


def validate_capacity_member(champion, proposal, pool, *, limits=None, frames=None):
    """Verify exact policies and complete controls from the actual same pool.

    Only a rules incumbent can authorize this compound-change exception. A
    degenerate control equal to the incumbent is an explicit incumbent reference.
    Metadata is checked on every referenced control, not trusted as permission.
    """
    try:
        if any(isinstance(item, dict) and item.get('version') == SIGNAL_HOLD_VERSION
               for item in proposal.get('capacity_packets', [])):
            return _validate_signal_hold_member(champion, proposal, pool, limits=limits, frames=frames)
        champion = validate_policy(deepcopy(champion))
        candidate = validate_policy(deepcopy(proposal['policy']))
        metadata = proposal['capacity_packets']
        if (champion['kind'] != 'rules' or not isinstance(metadata, list) or not metadata
                or not isinstance(pool, list) or not pool):
            raise ValueError('invalid_capacity_contract')
        entries = {}
        for item in pool:
            identity = policy_id(item['policy'])
            if identity in entries:
                raise ValueError('invalid_capacity_contract')
            entries[identity] = item
        candidate_id, actual_parent_id = policy_id(candidate), policy_id(champion)
        if candidate_id not in entries or entries[candidate_id] != proposal:
            raise ValueError('invalid_capacity_contract')
        def reconstruct(item):
            if item['actual_parent_id'] != actual_parent_id:
                raise ValueError('invalid_capacity_contract')
            base = champion if item['base_id'] == actual_parent_id else validate_policy(entries[item['base_id']]['policy'])
            _signal_base_changes(champion, base)
            return capacity_packet(base, top_n=item['top_n'], champion=champion)
        packets, seen = [], set()
        for item in metadata:
            packet = reconstruct(item)
            expected = capacity_metadata(packet, candidate_id)
            if item != expected or packet['hash'] in seen:
                raise ValueError('invalid_capacity_contract')
            seen.add(packet['hash'])
            packets.append(packet)
        # A shared control can connect both levels. Validate the entire linked
        # packet graph, including its limits, rather than trusting extra metadata
        # that merely contains one valid reference.
        pending, checked = list(packets), {}
        while pending:
            packet = pending.pop()
            if packet['hash'] in checked:
                continue
            checked[packet['hash']] = packet
            if packet['base_id'] != actual_parent_id:
                # S x A x E includes the actual incumbent's complete 2x2
                # controls at the SAME allocation level, not an unrelated base.
                pending.append(capacity_packet(champion, top_n=packet['top_n'], champion=champion))
            for role in ROLES:
                identity = packet['member_ids'][role]
                if identity == actual_parent_id:
                    continue
                control = entries.get(identity)
                if (control is None or validate_policy(control['policy']) != packet['members'][role]
                        or not isinstance(control.get('capacity_packets'), list)
                        or capacity_metadata(packet, identity) not in control['capacity_packets']
                        or 'limits' in control and control['limits'] != limits):
                    raise ValueError('invalid_capacity_contract')
                control_seen = set()
                for supplied in control['capacity_packets']:
                    reconstructed = reconstruct(supplied)
                    if (supplied != capacity_metadata(reconstructed, identity)
                            or reconstructed['hash'] in control_seen):
                        raise ValueError('invalid_capacity_contract')
                    pending.append(reconstructed)
                    control_seen.add(reconstructed['hash'])
        changes = []
        for key in ('candidate_scope', 'allocation_mode', 'max_positions'):
            default = 'unrestricted' if key == 'candidate_scope' else 'legacy'
            before, after = champion.get(key, default), candidate.get(key, default)
            if before != after:
                changes.append({'path': key, 'before': before, 'after': after})
        for index, (before, after) in enumerate(zip(champion['strategies'], candidate['strategies'])):
            for key in ('conditions', 'rank_by', 'descending', 'top_n', 'holding_sessions', 'max_holding_sessions'):
                if before.get(key) != after.get(key):
                    changes.append({'path': f'strategies[{index}].{key}',
                                    'before': before.get(key), 'after': after.get(key)})
        return {'version': VERSION, 'base_id': packets[0]['base_id'], 'actual_parent_id': actual_parent_id,
                'candidate_id': candidate_id,
                'packets': [capacity_metadata(packet, candidate_id) for packet in packets],
                'control_packets': [{'version': VERSION, 'top_n': packet['top_n'], 'base_id': packet['base_id'],
                    'actual_parent_id': actual_parent_id, 'packet_hash': packet['hash'],
                    'member_ids': deepcopy(packet['member_ids'])} for packet in checked.values()],
                'changes': changes, 'existing_lot_exits_preserved': True}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('invalid_capacity_contract') from None


def _signal_templates(summaries):
    """Fixed grammar variant zero; thresholds use observations, never returns."""
    required = ('breakout_20d_pct', 'volume_ratio', 'excess_20d_pp', 'return_20d_pct',
                'return_5d_pct', 'turnover', 'close_sma20_pct', 'close_sma60_pct')
    if any(key not in summaries or summaries[key].get('count', 0) <= 0 for key in required):
        raise ValueError('signal_hold_feature_summary_missing')
    def condition(feature, op, value):
        return {'feature': feature, 'op': op, 'value': round(float(value), 6)}
    definitions = (
        ('trend', 'breakout_20d_pct', True, [condition('close_sma20_pct', 'gt', 0),
            condition('close_sma60_pct', 'gt', 0),
            condition('breakout_20d_pct', 'gt', max(-2, summaries['breakout_20d_pct']['q75'])),
            condition('volume_ratio', 'gt', max(.8, summaries['volume_ratio']['q50']))]),
        ('relative', 'excess_20d_pp', True, [
            condition('excess_20d_pp', 'gt', max(0, summaries['excess_20d_pp']['q75'])),
            condition('return_5d_pct', 'gt', 0), condition('close_sma20_pct', 'gt', 0)]),
        ('recovery', 'return_5d_pct', False, [
            condition('return_20d_pct', 'gt', max(0, summaries['return_20d_pct']['q50'])),
            condition('return_5d_pct', 'lt', min(2, summaries['return_5d_pct']['q25'])),
            condition('close_sma60_pct', 'gt', 0)]),
        ('liquid', 'turnover', True, [condition('turnover', 'gt', max(0, summaries['turnover']['q50'])),
            condition('excess_20d_pp', 'gt', 0), condition('close_sma20_pct', 'gt', 0)]),
    )
    return {family: {'conditions': tests, 'rank_by': ranking, 'descending': descending}
            for family, ranking, descending, tests in definitions}


def signal_hold_packet(champion, frames):
    """Freeze 16 or fewer hypotheses and the exact incumbent reference.

    C bundles allocation and entry filters; C1/C2/C4 isolate future holding age.
    Four signal families cross those three ages. This is a complete registered
    comparison packet, not a full factorial of every changed policy field.
    Existing lots, global limits, weights, reserves, stops and takes are inputs
    to evaluation, never rewritten by this generator.
    """
    from backend.learning_research import _behavior_id, _summaries
    champion = validate_policy(deepcopy(champion))
    if champion['kind'] != 'rules':
        raise ValueError('signal_hold_requires_rules_parent')
    if not isinstance(frames, list) or not frames:
        raise ValueError('signal_hold_frames_required')
    try:
        days = [date.fromisoformat(frame['as_of']) for frame in frames]
        observed = [datetime.fromisoformat(frame['observed_at']) for frame in frames]
        if (any(before >= after for before, after in zip(days, days[1:]))
                or any(value.utcoffset() is None or value > observed[-1] for value in observed)):
            raise ValueError('invalid_signal_hold_frame_dates')
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid_signal_hold_frame_dates') from None
    summaries = _summaries(frames)
    templates = _signal_templates(summaries)
    common = deepcopy(champion)
    common.update(allocation_mode='equal_eligible', max_positions=20,
                  held_before_top_n=True, no_same_day_reentry=True)
    for strategy in common['strategies']:
        strategy['top_n'] = 8
    def holding(value, sessions):
        value = deepcopy(value)
        for strategy in value['strategies']:
            strategy['holding_sessions'] = sessions
            if 'max_holding_sessions' in strategy:
                strategy['max_holding_sessions'] = sessions
        return value
    members = {'P': champion, 'C': common}
    members.update({f'C{sessions}': holding(common, sessions) for sessions in HOLD_LEVELS})
    for family in SIGNAL_FAMILIES:
        signal = deepcopy(common)
        for strategy in signal['strategies']:
            strategy.update(deepcopy(templates[family]))
        for sessions in HOLD_LEVELS:
            members[f'T_{family}_{sessions}'] = holding(signal, sessions)
    members = {role: validate_policy(value) for role, value in members.items()}
    # Explicit default/unused optional fields can give one behavior two IDs.
    # Prefer the actual incumbent, then the first fixed role, so such a control
    # is an explicit reference rather than a duplicate with no measured delta.
    behaviors = {}
    for role, value in members.items():
        behavior = _behavior_id(value)
        members[role] = deepcopy(behaviors.setdefault(behavior, value))
    member_ids = {role: policy_id(value) for role, value in members.items()}
    parent_id = member_ids['P']
    identities = list(dict.fromkeys(member_ids.values()))
    roles_by_id = {identity: [role for role, value in member_ids.items() if value == identity]
                   for identity in identities}
    result = {'version': SIGNAL_HOLD_VERSION, 'template_version': SIGNAL_TEMPLATE_VERSION,
        'input_hash': _hash(frames), 'summary_hash': _hash(summaries),
        'signal_day': days[-1].isoformat(), 'input_observed_at': frames[-1]['observed_at'],
        'threshold_basis': 'all_supplied_frames_known_at_decision; reused_development_not_confirmatory',
        'thresholds': templates, 'actual_parent_id': parent_id, 'base_id': parent_id,
        'top_n': 8, 'hold_levels': list(HOLD_LEVELS), 'members': members, 'member_ids': member_ids,
        'candidate_ids': [identity for identity in identities if identity != parent_id],
        'degenerate_roles': [roles for roles in roles_by_id.values() if len(roles) > 1],
        'comparison_design': 'incumbent_plus_common_capacity_hold_controls_and_four_signal_families; not_full_factorial'}
    if len(result['candidate_ids']) > 16:
        raise ValueError('signal_hold_pool_exceeds_bound')
    result['hash'] = _hash(result)
    return result


def signal_hold_metadata(packet, policy_or_id):
    """Compact binding; authority comes from reconstructing exact frame inputs."""
    identity = policy_or_id if isinstance(policy_or_id, str) else policy_id(policy_or_id)
    if (packet.get('version') != SIGNAL_HOLD_VERSION
            or packet.get('hash') != _hash({key: value for key, value in packet.items() if key != 'hash'})
            or identity not in packet.get('candidate_ids', [])
            or packet['member_ids'] != {role: policy_id(value) for role, value in packet['members'].items()}):
        raise ValueError('invalid_signal_hold_packet')
    return {**{key: deepcopy(packet[key]) for key in ('version', 'template_version', 'input_hash', 'summary_hash',
               'signal_day', 'input_observed_at', 'threshold_basis',
               'actual_parent_id', 'base_id', 'top_n', 'hold_levels', 'member_ids')},
            'packet_hash': packet['hash'],
            'roles': [role for role, value in packet['member_ids'].items() if value == identity]}


def _validate_signal_hold_member(champion, proposal, pool, *, frames, limits):
    from backend.learning_proposals import template_proposal_source
    packet = signal_hold_packet(champion, frames)
    if not isinstance(pool, list) or not pool or len(pool) > 16:
        raise ValueError('invalid_capacity_contract')
    entries = {}
    for item in pool:
        identity = policy_id(item['policy'])
        if identity in entries:
            raise ValueError('invalid_capacity_contract')
        entries[identity] = item
    if set(entries) != set(packet['candidate_ids']):
        raise ValueError('invalid_capacity_contract')
    for identity, item in entries.items():
        if (item.get('capacity_packets') != [signal_hold_metadata(packet, identity)]
                or item.get('proposal_source') != template_proposal_source(packet, identity)
                or 'limits' in item and item['limits'] != limits):
            raise ValueError('invalid_capacity_contract')
    identity = policy_id(proposal['policy'])
    if identity not in entries or entries[identity] != proposal:
        raise ValueError('invalid_capacity_contract')
    parent, candidate = validate_policy(champion), validate_policy(proposal['policy'])
    changes = []
    for key in ('allocation_mode', 'max_positions', 'held_before_top_n', 'no_same_day_reentry'):
        if parent.get(key) != candidate.get(key):
            changes.append({'path': key, 'before': parent.get(key), 'after': candidate.get(key)})
    for index, (before, after) in enumerate(zip(parent['strategies'], candidate['strategies'])):
        for key in ('conditions', 'rank_by', 'descending', 'top_n', 'holding_sessions', 'max_holding_sessions'):
            if before.get(key) != after.get(key):
                changes.append({'path': f'strategies[{index}].{key}',
                                'before': deepcopy(before.get(key)), 'after': deepcopy(after.get(key))})
    metadata = signal_hold_metadata(packet, identity)
    controls = {key: deepcopy(metadata[key]) for key in ('version', 'template_version', 'input_hash', 'summary_hash',
                'signal_day', 'input_observed_at', 'threshold_basis', 'actual_parent_id', 'base_id', 'top_n',
                'hold_levels', 'member_ids', 'packet_hash')}
    # Pool completeness is structural. Computational validity is local to the
    # candidate's actual comparison: P plus the same-age C for a signal role.
    # A losing control remains valid; another family's failed replay does not
    # invalidate this pair. Aliased roles share the same measured policy.
    required_roles = {'P', *metadata['roles']}
    required_roles.update(f'C{role.rsplit("_", 1)[1]}' for role in metadata['roles'] if role.startswith('T_'))
    controls['required_roles'] = [role for role in packet['member_ids'] if role in required_roles]
    return {'version': SIGNAL_HOLD_VERSION, 'base_id': packet['base_id'],
            'actual_parent_id': packet['actual_parent_id'], 'candidate_id': identity,
            'packets': [metadata], 'control_packets': [controls], 'changes': changes,
            'existing_lot_exits_preserved': True, 'comparison_design': packet['comparison_design']}
