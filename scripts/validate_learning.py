"""Offline synthetic selection -> fixed trial -> replacement loop calibration.

This is a falsification model, not a market return forecast or a proof of error
control. It never loads account configuration, market data, orders or an LLM.
Run with the project Python: -m scripts.validate_learning --histories 64 --trials 20
"""
import argparse
import json
import math
import random
import time

from backend.learning_evaluation import daily_metrics, evaluate_candidate, make_evaluation_spec


def _wilson(successes, total):
    if not total:
        return [0., 1.]
    p, z = successes / total, 1.96
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    width = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0., center - width), 4), round(min(1., center + width), 4)]


def _history_cluster_interval(values):
    # Campaigns inside one adaptive history are dependent. Resample complete
    # independently generated histories, never pretend campaigns are IID.
    rng = random.Random(145701)
    means = sorted(sum(values[rng.randrange(len(values))] for _ in values) / len(values) for _ in range(999))
    return [round(means[24], 4), round(means[974], 4)]


def _ar_step(rng, prior, deviation, correlation=.35):
    return correlation * prior + deviation * math.sqrt(1 - correlation * correlation) * rng.gauss(0, 1)


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
            candidate_quality, incumbent_noise, candidate_noise, invalid)


def validate_loops(*, histories=64, trials=20, repetitions=399, seed=732409, horizons=(40, 60, 120)):
    if type(histories) is not int or not 1 <= histories <= 1000 or type(trials) is not int or not 5 <= trials <= 100 or trials % 5:
        raise ValueError("complete_five_trial_campaigns_required")
    started, outcomes = time.perf_counter(), {}
    for horizon in horizons:
        by_scenario = {}
        for scenario_index, scenario in enumerate(("null", "positive", "regime")):
            any_switch, any_false, positive_success, positive_trials, false_switch, invalids, campaign_false = 0, 0, 0, 0, 0, 0, 0
            positive_scheduled = 0
            history_campaign_rates = []
            for history in range(histories):
                rng = random.Random(seed + scenario_index * 100000 + history)
                incumbent_quality, incumbent_noise = 0., 0.
                switched, false, campaigns = False, False, set()
                for sequence in range(1, trials + 1):
                    a, b, quality, noise_a, noise_b, invalid = _trial(rng, horizon, scenario, incumbent_quality, incumbent_noise)
                    beneficial = quality > incumbent_quality and scenario != "regime"
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
                        incumbent_quality, incumbent_noise = quality, noise_b
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
                "beneficial_detection_including_invalid": round(positive_success / positive_scheduled, 4) if positive_scheduled else None}
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
                       "invalid_slots": "4% random or extreme negative candidate day; full horizon consumed",
                       "seed": seed, "lifetime_error_guarantee": False},
            "outcomes": outcomes, "seconds": round(time.perf_counter() - started, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--histories", type=int, default=64)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=399)
    parser.add_argument("--seed", type=int, default=732409)
    args = parser.parse_args()
    if not 1 <= args.histories <= 1000 or not 5 <= args.trials <= 100 or args.trials % 5:
        parser.error("histories 1..1000 and trials 5..100 in multiples of five required")
    print(json.dumps(validate_loops(histories=args.histories, trials=args.trials,
                                   repetitions=args.repetitions, seed=args.seed), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
