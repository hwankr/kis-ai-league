"""One fixed-horizon comparison for provisional paper-policy replacement.

Paired circular-block bootstrap estimates are conditional on the replay model,
not a guarantee of statistical significance in non-stationary markets. A fixed
five-trial campaign spends 10% in five 2% tests. New campaigns are explicit;
the lifetime probability of any false replacement is NOT bounded by 10%.
"""
import hashlib
import json
import math
import random


EVALUATION_VERSION = "paired-block-paper-v1"
DEFAULT_HORIZON = 60
HORIZONS = (40, 60, 120)
CAMPAIGN_SIZE = 5
CAMPAIGN_ALPHA = .10
BOOTSTRAP_REPETITIONS = 999
BLOCK_LENGTHS = (10, 20)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def make_evaluation_spec(sequence, horizon=DEFAULT_HORIZON, *, bootstrap_repetitions=BOOTSTRAP_REPETITIONS):
    """Freeze this before observing evaluation outcomes; invalid trials use a slot."""
    if type(sequence) is not int or sequence < 1 or type(horizon) is not int or horizon not in HORIZONS:
        raise ValueError("invalid_evaluation_sequence_or_horizon")
    if type(bootstrap_repetitions) is not int or not 199 <= bootstrap_repetitions <= 20000:
        raise ValueError("invalid_bootstrap_repetitions")
    value = {"version": EVALUATION_VERSION, "phase": "paper_provisional", "sequence": sequence,
             "horizon": horizon, "campaign": (sequence - 1) // CAMPAIGN_SIZE + 1,
             "campaign_trial": (sequence - 1) % CAMPAIGN_SIZE + 1,
             "campaign_size": CAMPAIGN_SIZE, "campaign_alpha": CAMPAIGN_ALPHA,
             "test_alpha": CAMPAIGN_ALPHA / CAMPAIGN_SIZE,
             "bootstrap_repetitions": bootstrap_repetitions, "block_lengths": list(BLOCK_LENGTHS),
             "minimum_relative_growth": .01, "maximum_drawdown_pct": 15.,
             "maximum_drawdown_worsening_pp": 2., "recent_intervals": horizon // 3,
             "cash_daily_log_return": 0., "seed": 230917 + sequence}
    return {**value, "id": _digest(value)}


def _validate_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("invalid_evaluation_spec")
    expected = make_evaluation_spec(spec.get("sequence"), spec.get("horizon"),
                                    bootstrap_repetitions=spec.get("bootstrap_repetitions"))
    if spec != expected:
        raise ValueError("evaluation_spec_changed")
    return expected


def _number(value):
    if type(value) not in {float, int} or not math.isfinite(value):
        raise ValueError("invalid_daily_return")
    return float(value)


def _series(metrics):
    if not isinstance(metrics, dict) or metrics.get("valid") is not True:
        raise ValueError("invalid_portfolio_evaluation")
    normal, stress = metrics.get("daily_log_returns"), metrics.get("stress_daily_log_returns")
    if not isinstance(normal, list) or not isinstance(stress, list) or len(normal) != len(stress):
        raise ValueError("missing_daily_returns")
    normal, stress = [_number(value) for value in normal], [_number(value) for value in stress]
    if metrics.get("intervals", len(normal)) != len(normal):
        raise ValueError("interval_count_mismatch")
    curves = [metrics.get("equity_curve"), metrics.get("stress_equity_curve")]
    dates = []
    for curve in curves:
        if not isinstance(curve, list) or len(curve) != len(normal) + 1:
            raise ValueError("missing_evaluation_dates")
        values = [item.get("as_of") if isinstance(item, dict) else None for item in curve]
        if any(not isinstance(day, str) for day in values) or any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError("invalid_evaluation_dates")
        dates.append(values)
    if dates[0] != dates[1]:
        raise ValueError("cost_scenarios_not_aligned")
    return normal, stress, dates[0]


def _return_and_drawdown(series):
    cumulative, peak, drawdown = 0., 0., 0.
    for value in series:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, -math.expm1(cumulative - peak) * 100)
    return math.expm1(cumulative) * 100, drawdown


def _quantile(values, probability):
    values = sorted(values)
    return values[min(len(values) - 1, max(0, math.ceil(probability * len(values)) - 1))]


def paired_block_bootstrap(components, deltas, spec):
    """Resample identical circular date blocks across every paired comparison.

Returns upper-tail approximate p-values and daily-log-growth lower bounds. All
comparisons and both block lengths must pass; they are not independent tests.
"""
    names = list(components)
    n, alpha, repetitions = spec["horizon"], spec["test_alpha"], spec["bootstrap_repetitions"]
    means = {name: math.fsum(components[name]) / n for name in names}
    result = {}
    for block_length in spec["block_lengths"]:
        rng = random.Random(spec["seed"] + block_length * 1009)
        block_sums = []
        for name in names:
            row = components[name]
            doubled = row + row
            prefix = [0.]
            for value in doubled:
                prefix.append(prefix[-1] + value - means[name])
            block_sums.append([prefix[start + block_length] - prefix[start] for start in range(n)])
        draws = [[] for _ in names]
        for _ in range(repetitions):
            starts = [rng.randrange(n) for _ in range(n // block_length)]
            for values, sums in zip(draws, block_sums):
                values.append(math.fsum(sums[start] for start in starts) / n)
        stats = {}
        for name, values in zip(names, draws):
            observed = means[name] - deltas[name]
            stats[name] = {"p_value": (1 + sum(value >= observed for value in values)) / (repetitions + 1),
                           "daily_lower_bound": means[name] - _quantile(values, 1 - alpha)}
        result[str(block_length)] = stats
    return {"blocks": result,
            "p_max": max(item["p_value"] for block in result.values() for item in block.values()),
            "minimum_p_resolution": 1 / (repetitions + 1),
            "interpretation": "model_conditional_bootstrap; no_lifetime_error_guarantee"}


def evaluate_candidate(incumbent, candidate, spec):
    """Return promote/keep/waiting/invalid; caller durably permits one final decision.

Extra observations are ignored. Trade counts are diagnostics, never a gate.
Fixed-period NAV changes already contain cash, costs and unrealized holdings.
"""
    try:
        spec = _validate_spec(spec)
    except (TypeError, ValueError):
        return {"decision": "invalid", "reason": "평가 시작 시 고정한 기준이 변경됐습니다.",
                "phase": "paper_provisional", "statistics": {"error": "evaluation_spec_changed"}}
    result = {"decision": "waiting", "reason": "고정 평가 기간 관측 중", "phase": "paper_provisional",
              "spec_id": spec["id"], "statistics": {"horizon": spec["horizon"], "sequence": spec["sequence"],
                "campaign": spec["campaign"], "campaign_trial": spec["campaign_trial"],
                "campaign_alpha": spec["campaign_alpha"], "test_alpha": spec["test_alpha"]}}
    if all(isinstance(item, dict) and item.get("error") == "insufficient_frames" for item in (incumbent, candidate)):
        result["statistics"]["intervals"] = 0
        return result
    try:
        a, a_stress, a_dates = _series(incumbent)
        b, b_stress, b_dates = _series(candidate)
        n = spec["horizon"]
        if a_dates[:n + 1] != b_dates[:n + 1]:
            raise ValueError("portfolios_not_date_paired")
        result["statistics"]["intervals"] = min(len(a), len(b))
        if min(len(a), len(b)) < n:
            return result
        a, a_stress, b, b_stress = [row[:n] for row in (a, a_stress, b, b_stress)]
        components = {"normal_incumbent": [right - left for left, right in zip(a, b)],
                      "stress_incumbent": [right - left for left, right in zip(a_stress, b_stress)],
                      "normal_cash": [value - spec["cash_daily_log_return"] for value in b],
                      "stress_cash": [value - spec["cash_daily_log_return"] for value in b_stress]}
        paths = {name: _return_and_drawdown(row) for name, row in
                 (("incumbent", a), ("incumbent_stress", a_stress), ("candidate", b), ("candidate_stress", b_stress))}
        result["statistics"].update(intervals=n, start_day=a_dates[0], end_day=a_dates[n],
            relative_return_pct={name: math.expm1(math.fsum(row)) * 100 for name, row in components.items()},
            return_pct={name: value[0] for name, value in paths.items()},
            drawdown_pct={name: value[1] for name, value in paths.items()})
        minimum = math.log1p(spec["minimum_relative_growth"])
        economic = (all(math.fsum(components[key]) >= minimum for key in ("normal_incumbent", "stress_incumbent"))
                    and all(math.fsum(components[key]) > 0 for key in ("normal_cash", "stress_cash")))
        risk = all(paths[key][1] <= min(spec["maximum_drawdown_pct"], paths[base][1] + spec["maximum_drawdown_worsening_pp"])
                   for key, base in (("candidate", "incumbent"), ("candidate_stress", "incumbent_stress")))
        recent = math.fsum(components["stress_incumbent"][-spec["recent_intervals"]:]) > 0
        result["statistics"].update(economic_pass=economic, risk_pass=risk, recent_pass=recent)
        result["decision"] = "keep"
        if not economic or not risk or not recent:
            result["reason"] = ("고정 기간 최소 개선·현금 초과 기준 미달" if not economic else
                                "포트폴리오 낙폭 기준 미달" if not risk else "최근 구간에서 개선이 유지되지 않음")
            return result
        deltas = {key: minimum / n if key.endswith("incumbent") else 0. for key in components}
        bootstrap = paired_block_bootstrap(components, deltas, spec)
        result["statistics"].update(bootstrap)
        if bootstrap["p_max"] <= spec["test_alpha"]:
            result.update(decision="promote", reason="고정 기간 비용·불확실성·낙폭 기준 통과: 모의 정책 교체")
        else:
            result["reason"] = "블록 재표본화에서 개선 근거가 충분하지 않음"
        return result
    except (TypeError, ValueError, KeyError, OverflowError) as error:
        result.update(decision="invalid", reason="같은 날짜의 포트폴리오 수익률을 확인할 수 없습니다.")
        result["statistics"]["error"] = str(error) if str(error) in {
            "invalid_portfolio_evaluation", "invalid_daily_return", "missing_daily_returns", "interval_count_mismatch",
            "missing_evaluation_dates", "invalid_evaluation_dates", "cost_scenarios_not_aligned", "portfolios_not_date_paired"
        } else "invalid_evaluation_input"
        return result


def daily_metrics(normal, stress=None, *, first_day="2020-01-01"):
    """Synthetic-validation helper; never creates market observations or a ledger."""
    from datetime import date, timedelta
    stress = normal if stress is None else stress
    day, dates = date.fromisoformat(first_day), []
    while len(dates) < len(normal) + 1:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day += timedelta(days=1)
    def curve(values):
        cumulative, result = 0., [{"as_of": dates[0], "equity": 1.}]
        for day, value in zip(dates[1:], values):
            cumulative += value
            result.append({"as_of": day, "equity": math.exp(cumulative)})
        return result
    return {"valid": True, "intervals": len(normal), "daily_log_returns": list(normal),
            "stress_daily_log_returns": list(stress), "equity_curve": curve(normal),
            "stress_equity_curve": curve(stress)}
