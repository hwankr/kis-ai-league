"""Offline synthetic selection -> fixed trial -> replacement loop calibration.

This is a falsification model, not a market return forecast or a proof of error
control. It never loads account configuration, market data, orders or an LLM.
Run with the project Python: -m scripts.validate_learning --histories 64 --trials 20
"""
import argparse
import hashlib
import json
import math
import random
import time
from pathlib import Path

from backend.learning_evaluation import daily_metrics, evaluate_candidate, make_evaluation_spec


PARALLEL_RULE = {
    'kind': 'operational_comparison_not_optimality_or_error_guarantee',
    'maximum_campaign_false_rate': .10, 'maximum_false_rate_increase': .02,
    'minimum_discovery_days_saved': 6, 'interval_confidence': .95,
    'decision': 'adopt only if independent validation intervals support every condition',
}
COMPARISON_SCENARIOS = ('null', 'boundary', 'positive', 'correlated_null', 'overlap', 'reversal', 'volatile')
TUNING_SEED, VALIDATION_SEED = 732409, 941027
# Both results have informed design review; neither is a fresh holdout now.
REVIEWED_SEEDS = (TUNING_SEED, VALIDATION_SEED)


def _wilson(successes, total):
    if not total:
        return [0., 1.]
    p, z = successes / total, 1.96
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    width = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0., center - width), 4), round(min(1., center + width), 4)]


def _history_cluster_interval(values, *, bounds=(0., 1.)):
    # Campaigns inside one adaptive history are dependent. Resample complete
    # independently generated histories, never pretend campaigns are IID.
    if not values:
        return list(bounds)
    if all(value == values[0] for value in values):
        # Zero observed events do not prove zero population risk. For bounded
        # values, the probability of an unseen nonzero event bounds its mean.
        # Exact binomial upper tail: (1-u)^n=.025; n is independent histories.
        # https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm
        unseen = 1 - .025 ** (1 / len(values))
        low, high = bounds
        if values[0] == 0:
            return [min(0., low * unseen), max(0., high * unseen)]
        if values[0] == low:
            return [low, low + (high - low) * unseen]
        if values[0] == high:
            return [high - (high - low) * unseen, high]
        return [low, high]  # Degenerate bootstrap away from a fixed boundary.
    rng = random.Random(145701)
    means = sorted(sum(values[rng.randrange(len(values))] for _ in values) / len(values) for _ in range(999))
    return [means[24], means[974]]


def _ar_step(rng, prior, deviation, correlation=.35):
    return correlation * prior + deviation * math.sqrt(1 - correlation * correlation) * rng.gauss(0, 1)


def _conditional_truth(horizon, quality, incumbent_quality, candidate_noise, incumbent_noise,
                       market, *, common=0., common_scale=0., own_scale=1.,
                       overlap_days=0, reversal_day=None):
    """Full-window mean hypotheses, conditional on the frozen generator state.

    These are analytic forecasts of the synthetic process, never its realized
    future returns. Shared holdings cancel only in the relative comparison.
    """
    parent, candidate = [], []
    for day in range(horizon):
        decay = .35 ** (day + 1)
        if day < overlap_days:
            inherited = .0005 + decay * (market + common)
            parent.append(inherited)
            candidate.append(inherited)
        else:
            base = .0005 + decay * (market + common_scale * common)
            effect = -quality if reversal_day is not None and day >= reversal_day else quality
            parent.append(base + incumbent_quality + own_scale * incumbent_noise * decay)
            candidate.append(base + effect + own_scale * candidate_noise * decay)
    relative = math.fsum(right - left for left, right in zip(parent, candidate)) / horizon
    cash = math.fsum(candidate) / horizon
    # Both designs charge the same stress cost, so it cancels against the
    # incumbent but still matters against cash. Equality belongs to H0.
    return {'beneficial': relative > math.log1p(.01) / horizon + 1e-12 and cash - .00004 > 1e-12,
            'relative_daily_log': relative, 'cash_daily_log': cash,
            'stress_cash_daily_log': cash - .00004,
            'post_regime_effect': -quality - incumbent_quality if reversal_day is not None else None}


def _trial(rng, horizon, scenario, incumbent_quality, incumbent_noise):
    # Sixteen adaptively scored development candidates, sharing market shocks.
    # Winner noise is carried into the future rather than optimistically erased.
    quality = [(.001 if index < 8 and scenario != "null" else 0.) for index in range(16)]
    noises, development = [0.] * 16, [[] for _ in range(16)]
    market = 0.
    for _ in range(60):
        market = _ar_step(rng, market, .0015)
        for index in range(16):
            noises[index] = _ar_step(rng, noises[index], .0015)
            development[index].append(.0005 + market + quality[index] + noises[index])
    def score(series):
        level, peak, drawdown = 0., 0., 0.
        for value in series:
            level += value
            peak = max(peak, level)
            drawdown = max(drawdown, peak - level)
        return level - .75 * drawdown - .25 * abs(sum(series[:30]) - sum(series[30:]))
    selected = max(range(16), key=lambda index: score(development[index]))
    candidate_quality, candidate_noise = quality[selected], noises[selected]
    truth = _conditional_truth(horizon, candidate_quality, incumbent_quality, candidate_noise,
                               incumbent_noise, market,
                               reversal_day=horizon * 2 // 3 if scenario == 'regime' else None)
    incumbent, candidate = [], []
    for day in range(horizon):
        market = _ar_step(rng, market, .0015)
        incumbent_noise = _ar_step(rng, incumbent_noise, .0015)
        candidate_noise = _ar_step(rng, candidate_noise, .0015)
        effect = -candidate_quality if scenario == "regime" and day >= horizon * 2 // 3 else candidate_quality
        incumbent.append(.0005 + market + incumbent_quality + incumbent_noise)
        candidate.append(.0005 + market + effect + candidate_noise)
    # An invalid slot still consumes the whole scheduled market interval. This
    # includes outcome-associated failures, never an immediate same-window retry.
    invalid = rng.random() < .04 or min(candidate) < -.008
    return (daily_metrics(incumbent, [value - .00004 for value in incumbent]),
            daily_metrics(candidate, [value - .00004 for value in candidate]),
            candidate_quality, incumbent_noise, candidate_noise, invalid, truth)


def validate_loops(*, histories=64, trials=20, repetitions=399, seed=732409, horizons=(40, 60, 120)):
    if type(histories) is not int or not 1 <= histories <= 1000 or type(trials) is not int or not 5 <= trials <= 100 or trials % 5:
        raise ValueError("complete_five_trial_campaigns_required")
    started, outcomes = time.perf_counter(), {}
    for horizon in horizons:
        by_scenario = {}
        for scenario_index, scenario in enumerate(("null", "positive", "regime")):
            any_switch, any_false, positive_success, positive_trials, false_switch, invalids, campaign_false = 0, 0, 0, 0, 0, 0, 0
            positive_scheduled = 0
            post_regime_count, post_regime_negative, post_regime_effect_sum = 0, 0, 0.
            history_campaign_rates = []
            for history in range(histories):
                rng = random.Random(seed + scenario_index * 100000 + history)
                incumbent_quality, incumbent_noise = 0., 0.
                switched, false, campaigns = False, False, set()
                for sequence in range(1, trials + 1):
                    a, b, quality, noise_a, noise_b, invalid, truth = _trial(rng, horizon, scenario, incumbent_quality, incumbent_noise)
                    beneficial = truth['beneficial']
                    positive_scheduled += beneficial
                    spec = make_evaluation_spec(sequence, horizon, bootstrap_repetitions=repetitions)
                    if invalid:
                        invalids += 1
                        incumbent_noise = noise_a
                        continue
                    if beneficial:
                        positive_trials += 1
                    decision = evaluate_candidate(a, b, spec)
                    if decision["decision"] == "promote":
                        switched = True
                        if beneficial:
                            positive_success += 1
                        else:
                            false, false_switch = True, false_switch + 1
                            campaigns.add(spec["campaign"])
                        if truth['post_regime_effect'] is not None:
                            post_regime_count += 1
                            post_regime_negative += truth['post_regime_effect'] < 0
                            post_regime_effect_sum += truth['post_regime_effect']
                        incumbent_quality = -quality if scenario == 'regime' else quality
                        incumbent_noise = noise_b
                    else:
                        incumbent_noise = noise_a
                any_switch += switched
                any_false += false
                campaign_false += len(campaigns)
                history_campaign_rates.append(len(campaigns) / (trials // 5))
            campaign_total = histories * math.ceil(trials / 5)
            by_scenario[scenario] = {"histories": histories, "trials_per_history": trials,
                "any_switch_histories": any_switch, "any_false_switch_histories": any_false,
                "any_false_switch_rate": round(any_false / histories, 4),
                "any_false_switch_wilson95": _wilson(any_false, histories),
                "false_switches": false_switch, "invalid_slots_consumed": invalids,
                "campaign_false_rate": round(campaign_false / campaign_total, 4),
                "campaign_false_history_bootstrap95": _history_cluster_interval(history_campaign_rates),
                "beneficial_trial_detection": round(positive_success / positive_trials, 4) if positive_trials else None,
                "beneficial_trials": positive_trials, "beneficial_detected": positive_success,
                "beneficial_scheduled": positive_scheduled,
                "beneficial_detection_including_invalid": round(positive_success / positive_scheduled, 4) if positive_scheduled else None,
                "post_regime": {"promotions": post_regime_count, "negative_effects": post_regime_negative,
                    "mean_daily_log_effect": post_regime_effect_sum / post_regime_count if post_regime_count else None}}
        outcomes[str(horizon)] = by_scenario
    # A predeclared operational choice, not evidence that this horizon has power
    # for actual market effects. The 10bp/day synthetic improvement is explicit.
    passing = [horizon for horizon in horizons
               if outcomes[str(horizon)]["null"]["campaign_false_rate"] <= .10
               and (outcomes[str(horizon)]["positive"]["beneficial_trial_detection"] or 0) >= .60
               and outcomes[str(horizon)]["regime"]["campaign_false_rate"] <= .10]
    return {"selected_horizon": min(passing) if passing else None,
            "selection_rule": "shortest: null campaign<=10%, valid beneficial trial detection>=60%, reversal campaign<=10%",
            "design": {"candidates": 16, "development_intervals": 60, "improvement_daily_log": .001,
                       "noise_daily_sigma": .0015, "ar1": .35, "reversal": "last third effect changes sign",
                       "campaign_trials": 5, "alpha_per_trial": .02, "bootstrap_repetitions": repetitions,
                       "statistical_truth": "conditional full-window relative mean > log(1.01)/horizon and normal/stress cash means > 0",
                       "invalid_slots": "4% random or extreme negative candidate day; full horizon consumed",
                       "seed": seed, "lifetime_error_guarantee": False},
            "outcomes": outcomes, "seconds": round(time.perf_counter() - started, 2)}


def _comparison_window(seed, window, scenario, prior_noise, horizon=60):
    """Generate all sixteen opportunities before either design chooses candidates.

    Shared market shocks and twenty inherited-holding days do not become new
    independent samples when a second candidate is admitted. No future value is
    used to rank the two hypotheses.
    """
    rng = random.Random(seed + window * 10007)
    delta = math.log1p(.01) / horizon
    if scenario == 'boundary':
        qualities = [delta] * 16  # Relative to this window's actual incumbent.
    elif scenario in ('positive', 'overlap', 'reversal'):
        qualities = [.0006 if index < 8 else 0. for index in range(16)]
    else:
        qualities = [0.] * 16
    correlation = .75 if scenario in ('correlated_null', 'overlap') else .15
    common_scale, own_scale = math.sqrt(correlation), math.sqrt(1 - correlation)
    noises, development, market, common = [0.] * 16, [[] for _ in range(16)], 0., 0.
    for _ in range(60):
        market, common = _ar_step(rng, market, .0015), _ar_step(rng, common, .0015)
        for index in range(16):
            noises[index] = _ar_step(rng, noises[index], .0015)
            development[index].append(.0005 + market + common_scale * common + own_scale * noises[index] + qualities[index])
    def development_score(series):
        level, peak, drawdown = 0., 0., 0.
        for value in series:
            level += value
            peak, drawdown = max(peak, level), max(drawdown, peak - level)
        return level - .75 * drawdown - .25 * abs(sum(series[:30]) - sum(series[30:]))
    ranked = sorted(range(16), key=lambda index: (-development_score(development[index]), index))
    # Conditional on the development selection, AR residuals have a known
    # nonzero future mean. Remove precisely that mean in the boundary scenario;
    # an unconditional long-run quality delta alone is not the boundary null.
    decay = sum(.35 ** day for day in range(1, horizon + 1)) / horizon
    conditional_shifts = [own_scale * (value - prior_noise) * decay for value in noises]
    initial_state = {'market': market, 'common': common, 'candidate_noise': list(noises),
                     'incumbent_noise': prior_noise, 'common_scale': common_scale, 'own_scale': own_scale}
    paths, incumbent, inherited = [[] for _ in range(16)], [], []
    for day in range(horizon):
        sigma = .0015 * (3 if scenario == 'volatile' and day >= horizon // 2 else 1)
        market, common = _ar_step(rng, market, sigma), _ar_step(rng, common, sigma)
        prior_noise = _ar_step(rng, prior_noise, sigma)
        incumbent.append(.0005 + market + common_scale * common + own_scale * prior_noise)
        inherited.append(.0005 + market + common)
        for index in range(16):
            noises[index] = _ar_step(rng, noises[index], sigma)
            paths[index].append(.0005 + market + common_scale * common + own_scale * noises[index])
    # Fixed invalid slots are never replaced, and their alpha is not reassigned.
    invalid = [rng.random() < .04 for _ in range(16)]
    return {'ranked': ranked, 'qualities': qualities, 'candidate_noise': paths,
            'incumbent_noise': incumbent, 'inherited': inherited,
            'conditional_noise_shifts': conditional_shifts,
            'initial_state': initial_state,
            'last_noise': noises, 'incumbent_last_noise': prior_noise, 'invalid': invalid}


def _comparison_history(seed, windows, scenario, count, repetitions=None):
    state = {'quality': 0., 'noise': 0., 'next_window': 1, 'first_discovery': None,
             'false_campaigns': [], 'false_switches': 0, 'invalid_slots': 0,
             'mc_uncertain': 0, 'switches': 0, 'beneficial_scheduled': 0,
             'beneficial_valid': 0, 'beneficial_detected': 0,
             'post_regime_count': 0, 'post_regime_negative': 0, 'post_regime_effect_sum': 0.}
    horizon = 60
    while state['next_window'] <= windows:
        window = state['next_window']
        market = _comparison_window(seed, window, scenario, state['noise'], horizon)
        selected = market['ranked'][:count]  # The development priority is frozen.
        accepted = None
        for priority, index in enumerate(selected, 1):
            quality = market['qualities'][index]
            if scenario == 'boundary':
                quality += state['quality'] - market['conditional_noise_shifts'][index]
            initial = market['initial_state']
            truth = _conditional_truth(horizon, quality, state['quality'], initial['candidate_noise'][index],
                initial['incumbent_noise'], initial['market'], common=initial['common'],
                common_scale=initial['common_scale'], own_scale=initial['own_scale'],
                overlap_days=20 if scenario == 'overlap' else 0,
                reversal_day=40 if scenario == 'reversal' else None)
            state['beneficial_scheduled'] += truth['beneficial']
            if market['invalid'][index]:
                state['invalid_slots'] += 1
                continue
            state['beneficial_valid'] += truth['beneficial']
            parent, candidate = [], []
            for day in range(horizon):
                effect = -quality if scenario == 'reversal' and day >= 40 else quality
                if scenario == 'overlap' and day < 20:
                    parent.append(market['inherited'][day])
                    candidate.append(market['inherited'][day])
                else:
                    parent.append(market['incumbent_noise'][day] + state['quality'])
                    candidate.append(market['candidate_noise'][index][day] + effect)
            spec = make_evaluation_spec(window, candidate_count=count, candidate_index=priority,
                                        bootstrap_repetitions=repetitions)
            result = evaluate_candidate(daily_metrics(parent, [item - .00004 for item in parent]),
                                        daily_metrics(candidate, [item - .00004 for item in candidate]), spec)
            state['mc_uncertain'] += bool(result.get('statistics', {}).get('mc_uncertain'))
            if result['decision'] == 'promote' and accepted is None:
                accepted = (index, quality, truth)
        if accepted:
            index, quality, truth = accepted
            if truth['beneficial']:
                state['beneficial_detected'] += 1
                if state['first_discovery'] is None:
                    state['first_discovery'] = window * horizon
            else:
                campaign = (window - 1) // 5 + 1
                if campaign not in state['false_campaigns']:
                    state['false_campaigns'].append(campaign)
                state['false_switches'] += 1
            if truth['post_regime_effect'] is not None:
                state['post_regime_count'] += 1
                state['post_regime_negative'] += truth['post_regime_effect'] < 0
                state['post_regime_effect_sum'] += truth['post_regime_effect']
            state['quality'] = -quality if scenario == 'reversal' else quality
            state['noise'] = market['last_noise'][index]
            state['switches'] += 1
        else:
            state['noise'] = market['incumbent_last_noise']
        state['next_window'] += 1
        # Durable restart: policy, priority-independent window number and noise
        # survive. New hypotheses are drawn only at the next scheduled boundary.
        state = json.loads(json.dumps(state))
    return {**state, 'discovery_days': state['first_discovery'] or (windows + 1) * horizon,
            'campaign_false_rate': len(state['false_campaigns']) / (windows // 5)}


def _paired_interval(values, *, bounds=(-1., 1.)):
    return _history_cluster_interval(values, bounds=bounds)


def compare_parallel(*, histories=32, windows=10, seed=None, repetitions=None, phase='validation',
                     development_seeds=()):
    """Paired diagnostic comparison; a new seed alone cannot prove a holdout."""
    if type(histories) is not int or not 2 <= histories <= 1000 or type(windows) is not int or not 5 <= windows <= 100 or windows % 5:
        raise ValueError('comparison_requires_independent_histories_and_complete_campaigns')
    if phase not in ('tuning', 'validation'):
        raise ValueError('invalid_comparison_phase')
    if any(type(value) is not int for value in development_seeds):
        raise ValueError('invalid_development_seeds')
    seed = (TUNING_SEED if phase == 'tuning' else VALIDATION_SEED) if seed is None else seed
    if type(seed) is not int:
        raise ValueError('invalid_comparison_seed')
    development_seeds = sorted(set(REVIEWED_SEEDS).union(development_seeds))
    source_files = [Path(__file__), Path(__file__).resolve().parents[1] / 'backend' / 'learning_evaluation.py']
    source_hash = hashlib.sha256(b'\0'.join(path.read_bytes().replace(b'\r\n', b'\n') for path in source_files)).hexdigest()
    started, outcomes = time.perf_counter(), {}
    for number, scenario in enumerate(COMPARISON_SCENARIOS):
        variants = {1: [], 2: []}
        for history in range(histories):
            path_seed = seed + number * 1000003 + history * 101
            for count in (1, 2):
                variants[count].append(_comparison_history(path_seed, windows, scenario, count, repetitions))
        summaries = {}
        for count, paths in variants.items():
            false_rates = [item['campaign_false_rate'] for item in paths]
            any_false = sum(bool(item['false_campaigns']) for item in paths)
            summaries[str(count)] = {
                'campaign_false_rate': sum(false_rates) / histories,
                'campaign_false_history_interval95': _history_cluster_interval(false_rates),
                'any_false_history_rate': any_false / histories,
                'any_false_history_wilson95': _wilson(any_false, histories),
                'mean_discovery_days': sum(item['discovery_days'] for item in paths) / histories,
                'discovered_histories': sum(item['first_discovery'] is not None for item in paths),
                'invalid_slots': sum(item['invalid_slots'] for item in paths),
                'mc_uncertain': sum(item['mc_uncertain'] for item in paths),
                'beneficial_scheduled': sum(item['beneficial_scheduled'] for item in paths),
                'beneficial_valid': sum(item['beneficial_valid'] for item in paths),
                'beneficial_detected': sum(item['beneficial_detected'] for item in paths),
                'post_regime': {'promotions': sum(item['post_regime_count'] for item in paths),
                    'negative_effects': sum(item['post_regime_negative'] for item in paths),
                    'mean_daily_log_effect': (sum(item['post_regime_effect_sum'] for item in paths) /
                        sum(item['post_regime_count'] for item in paths))
                        if any(item['post_regime_count'] for item in paths) else None},
            }
        saved = [one['discovery_days'] - two['discovery_days'] for one, two in zip(variants[1], variants[2])]
        increased = [two['campaign_false_rate'] - one['campaign_false_rate'] for one, two in zip(variants[1], variants[2])]
        outcomes[scenario] = {'designs': summaries, 'days_saved': sum(saved) / histories,
            'days_saved_interval95': _paired_interval(saved, bounds=(-windows * 60., windows * 60.)),
            'false_rate_increase': sum(increased) / histories,
            'false_rate_increase_interval95': _paired_interval(increased)}
    errors_ok = all(item['designs']['2']['campaign_false_history_interval95'][1] <= PARALLEL_RULE['maximum_campaign_false_rate'] and
                    item['false_rate_increase_interval95'][1] <= PARALLEL_RULE['maximum_false_rate_increase']
                    for item in outcomes.values())
    discovery_ok = all(outcomes[key]['days_saved_interval95'][0] >= PARALLEL_RULE['minimum_discovery_days_saved']
                       for key in ('positive', 'overlap'))
    official_mc = repetitions is None
    separated = seed not in development_seeds
    conditions_pass = phase == 'validation' and separated and official_mc and errors_ok and discovery_ok
    return {'phase': phase, 'seed': seed, 'histories': histories, 'windows': windows,
            'rule': dict(PARALLEL_RULE), 'production_mc': official_mc,
            'development_seeds': development_seeds, 'seed_separated_from_declared_development': separated,
            'source_sha256': source_hash,
            'validation_scope': 'seed separation does not establish prior non-use or preregistration; no automatic adoption',
            'specs': {str(count): make_evaluation_spec(1, candidate_count=count,
                        bootstrap_repetitions=repetitions) for count in (1, 2)},
            'design': {'development_candidates': 16, 'development_intervals': 60, 'horizon': 60,
                       'scenario_order': list(COMPARISON_SCENARIOS), 'noise_sigma': .0015,
                       'ar1': .35, 'candidate_correlation': [.15, .75], 'inherited_overlap_days': 20,
                       'positive_daily_log_effect': .0006, 'boundary_effect': 'log(1.01)/60',
                       'boundary_conditioning': 'subtract exact expected selected AR residual difference; incumbent updated after replacement',
                       'statistical_truth': 'conditional full-window relative mean > log(1.01)/60 and normal/stress cash means > 0',
                       'volatile_sigma_multiplier_after_day30': 3, 'censor_days': (windows + 1) * 60,
                       'not_covered': 'actual DSL search, order book, or execution-model validity'},
            'errors_pass': errors_ok, 'discovery_pass': discovery_ok, 'comparison_conditions_pass': conditions_pass,
            'conclusion': ('comparison_conditions_met_confirm_independent_preregistration' if conditions_pass
                           else 'parallel_benefit_not_established_keep_one'),
            'outcomes': outcomes, 'seconds': round(time.perf_counter() - started, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--histories", type=int, default=64)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=399)
    parser.add_argument("--seed", type=int)
    parser.add_argument('--compare-parallel', action='store_true')
    parser.add_argument('--phase', choices=('tuning', 'validation'), default='validation')
    parser.add_argument('--development-seed', type=int, action='append', default=[],
                        help='previously used design seed; repeat for every additional development seed')
    args = parser.parse_args()
    if not 1 <= args.histories <= 1000 or not 5 <= args.trials <= 100 or args.trials % 5:
        parser.error("histories 1..1000 and trials 5..100 in multiples of five required")
    result = (compare_parallel(histories=args.histories, windows=args.trials, seed=args.seed, phase=args.phase,
                               development_seeds=args.development_seed)
              if args.compare_parallel else validate_loops(histories=args.histories, trials=args.trials,
                                   repetitions=args.repetitions, seed=TUNING_SEED if args.seed is None else args.seed))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
