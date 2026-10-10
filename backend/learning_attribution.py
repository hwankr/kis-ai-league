"""Paired four-cell model diagnostics, never a causal verdict or adoption gate."""
import math
import random

from backend.learning_evaluation import _series, _validate_spec
from backend.learning_policy import _symbol, portfolio_metrics

VERSION = "paired-four-cell-v1"
CONTRASTS = ("D00", "D01", "D10", "D11")
EFFECTS = ("execution_cash", "execution_inherited", "capital_daily", "capital_observed", "gamma")
HYPOTHESES = {
    "signal": "현금·일봉 시가 모형에서 불리했던 종목 선택을 한 계열만 수정해 새 기간에 비교한다.",
    "execution": "동일 진입 조건에서 진입 갭 또는 유동성 조건 한 계열을 수정해 두 시작 상태의 집행 차이를 비교한다.",
    "capital": "진입 조건을 유지하고 비중·보유 한도만 수정해 두 집행 모형의 상태 인계 차이를 비교한다.",
    "interaction": "조건부 효과의 부호가 달라 한 계열만 수정한 뒤 현금·인계 상태를 다시 짝지어 비교한다.",
    "supported": "관측된 모형 내 우세를 원인으로 단정하지 않고 한 계열의 추가 변경을 새 기간에 비교한다.",
    "uncertain": "수정 방향이 미확정이므로 원인을 가정하지 않고 한 계열의 사전 가설을 새 기간에 비교한다.",
    "insufficient": "고정 기간의 짝지은 근거를 더 확보하고 현재 자료는 개발 가설로만 사용한다.",
}


def _effects(cells):
    rows = dict(cells)
    rows.update(execution_cash=[b - a for a, b in zip(cells["D00"], cells["D10"])],
                execution_inherited=[b - a for a, b in zip(cells["D01"], cells["D11"])],
                capital_daily=[b - a for a, b in zip(cells["D00"], cells["D01"])],
                capital_observed=[b - a for a, b in zip(cells["D10"], cells["D11"])],
                gamma=[d - c - b + a for a, b, c, d in zip(*(cells[key] for key in CONTRASTS))])
    return rows


def _bands(rows, spec):
    """Fixed circular blocks shared across cells, costs and execution assumptions.

    These basic bootstrap ranges diagnose uncertainty under the supplied model;
    they are not calibrated causal confidence intervals.
    """
    n, repetitions = spec["horizon"], spec["bootstrap_repetitions"]
    alpha = spec["test_alpha"]
    names = list(rows)
    totals = {key: math.fsum(row) for key, row in rows.items()}
    limits = {key: [math.inf, -math.inf] for key in names}
    for length in spec["block_lengths"]:
        if n % length:
            raise ValueError("unaligned_attribution_blocks")
        rng = random.Random(spec["seed"] + length * 1009)
        sums = []
        for key in names:
            mean, prefix = totals[key] / n, [0.]
            for value in rows[key] + rows[key]:
                prefix.append(prefix[-1] + value - mean)
            sums.append([prefix[start + length] - prefix[start] for start in range(n)])
        samples = [[] for _ in names]
        for _ in range(repetitions):
            starts = [rng.randrange(n) for _ in range(n // length)]
            for values, blocks in zip(samples, sums):
                values.append(math.fsum(blocks[start] for start in starts))
        for key, values in zip(names, samples):
            values.sort()
            low = totals[key] - values[min(repetitions - 1, math.ceil((1 - alpha / 2) * repetitions) - 1)]
            high = totals[key] - values[max(0, math.ceil(alpha / 2 * repetitions) - 1)]
            limits[key][0], limits[key][1] = min(limits[key][0], low), max(limits[key][1], high)
    return limits


def _sign(value):
    # Numerical cancellation is not a substantive effect threshold.
    value = round(value, 12)
    return 1 if value > 0 else -1 if value < 0 else 0


def _range_sign(bounds):
    return 1 if _sign(bounds[0]) > 0 else -1 if _sign(bounds[1]) < 0 else 0


def _direction(points, bounds):
    for left, right in (("execution_cash", "execution_inherited"), ("capital_daily", "capital_observed")):
        if _sign(points[left]) * _sign(points[right]) < 0:
            return "interaction" if _range_sign(bounds[left]) * _range_sign(bounds[right]) < 0 else "uncertain"
    if _range_sign(bounds["D11"]) > 0:
        return "supported"
    possible = []
    if _range_sign(bounds["D00"]) < 0:
        possible.append("signal")
    if all(_range_sign(bounds[key]) < 0 for key in ("execution_cash", "execution_inherited")):
        possible.append("execution")
    if all(_range_sign(bounds[key]) < 0 for key in ("capital_daily", "capital_observed")):
        possible.append("capital")
    return possible[0] if len(possible) == 1 else "uncertain"


def attribute_trial(trial, frames, *, evaluator=None):
    """Replay every outcome with e=execution model, h=inherited initial state.

    D values and ranges use cumulative log-return difference * 100 (log %p).
    Only compact contrasts survive; no curves or daily series are returned.
    """
    result = {"version": VERSION, "stage": "insufficient", "status": "unresolved", "unit": "log_pp",
              "range_basis": "fixed_paired_circular_blocks_approximate",
              "contrasts": dict.fromkeys(CONTRASTS), "effects": dict.fromkeys(EFFECTS),
              "ranges": {key: None for key in (*CONTRASTS, *EFFECTS)}, "reason": "paired_data_incomplete"}
    try:
        spec = _validate_spec(trial["evaluation_spec"])
        n = spec["horizon"]
        if len(frames) < n + 1 or trial.get("initial_state") is None:
            return {**result, "hypothesis": HYPOTHESES[result["stage"]]}
        frames = frames[:n + 1]
        evaluate = evaluator or portfolio_metrics
        cells = {}
        for e, model in ((0, {"mode": "daily_open"}), (1, trial["execution_model"])):
            for h, book in ((0, None), (1, trial["initial_state"])):
                cells[f"D{e}{h}"] = [evaluate(policy, frames, trial["limits"], execution_model=model, initial_state=book)
                                       for policy in (trial["incumbent"], trial["policy"])]
        expected_dates = None

        def paired(metrics):
            nonlocal expected_dates
            a, a_stress, a_dates = _series(metrics[0])
            b, b_stress, b_dates = _series(metrics[1])
            if len(a) < n or len(b) < n or a_dates[:n + 1] != b_dates[:n + 1]:
                raise ValueError("unpaired_attribution_dates")
            if expected_dates is None:
                expected_dates = a_dates[:n + 1]
            if a_dates[:n + 1] != expected_dates:
                raise ValueError("unpaired_attribution_dates")
            return [[right - left for left, right in zip(lefts[:n], rights[:n])]
                    for lefts, rights in ((a, b), (a_stress, b_stress))]

        base = {key: paired(value) for key, value in cells.items()}
        conditions = {"base": base}
        profile = trial["execution_model"]
        scenarios = profile.get("scenarios", [])
        if scenarios and set(spec.get("scenario_ids", [])) != {item["id"] for item in scenarios}:
            raise ValueError("attribution_scenarios_changed")
        for scenario in scenarios:
            key = scenario["id"]
            observed = {name: paired([metric["scenario_results"][key] for metric in cells[name]]) for name in ("D10", "D11")}
            conditions[key] = {"D00": base["D00"], "D01": base["D01"], **observed}
        components = {}
        for scenario, values in conditions.items():
            for cost in range(2):
                components.update({(scenario, cost, key): row for key, row in
                                   _effects({key: rows[cost] for key, rows in values.items()}).items()})
        bands = _bands(components, spec)
        points = {key: math.fsum(row) for key, row in components.items()}
        result["contrasts"] = {key: round(points["base", 0, key] * 100, 8) for key in CONTRASTS}
        result["effects"] = {key: round(points["base", 0, key] * 100, 8) for key in EFFECTS}
        result["ranges"] = {key: {"low": round(min(value[0] for index, value in bands.items() if index[2] == key) * 100, 8),
                                  "high": round(max(value[1] for index, value in bands.items() if index[2] == key) * 100, 8)}
                            for key in (*CONTRASTS, *EFFECTS)}
        directions = {_direction({key: points[scenario, cost, key] for key in (*CONTRASTS, *EFFECTS)},
                                 {key: bands[scenario, cost, key] for key in (*CONTRASTS, *EFFECTS)})
                      for scenario in conditions for cost in range(2)}
        stage = next(iter(directions)) if len(directions) == 1 else "uncertain"
        reason = "model_conditional_hypothesis" if stage != "uncertain" else "direction_not_robust"
        if any({-1, 1} <= {_sign(value) for index, value in points.items() if index[2] == key}
               for key in (*CONTRASTS, *EFFECTS[:-1])):
            stage, reason = "uncertain", "assumption_direction_flip"
        if trial.get("invalid_reason") or trial.get("decision") == "invalid":
            stage, reason = "uncertain", "invalid_trial"
        elif not scenarios or any(metric.get("promotion_supported") is not True for name in ("D10", "D11") for metric in cells[name]):
            stage, reason = "uncertain", "execution_support_unconfirmed"
        result.update(stage=stage, status="supported" if stage not in {"uncertain", "insufficient"} else "unresolved", reason=reason)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        result.update(stage="uncertain", reason="paired_evaluation_invalid")
    return {**result, "hypothesis": HYPOTHESES[result["stage"]]}


def assess_hypothesis(trial, attribution):
    """Assess the already-frozen directional hypothesis on this new trial.

    A component effect and final portfolio advantage are separate statements.
    Unsupported execution assumptions remain development evidence only.
    """
    test = trial.get("hypothesis_test")
    test = test if isinstance(test, dict) else {}
    metric = test.get("metric") if test.get("metric") in (*CONTRASTS, *EFFECTS) else None
    valid = (isinstance(attribution, dict) and attribution.get("version") == VERSION
             and attribution.get("unit") == "log_pp" and attribution.get("stage") != "insufficient"
             and attribution.get("reason") not in {"invalid_trial", "paired_evaluation_invalid", "execution_support_unconfirmed"})

    def assess(key, permitted=True):
        ranges = attribution.get("ranges") if valid and permitted else None
        bounds = ranges.get(key) if isinstance(ranges, dict) else None
        if (not isinstance(bounds, dict) or any(type(bounds.get(side)) not in (float, int)
                or not math.isfinite(bounds[side]) for side in ("low", "high")) or bounds["low"] > bounds["high"]):
            return {"metric": key, "status": "unresolved", "range": None}
        status = "supported" if bounds["low"] > 0 else "contradicted" if bounds["high"] < 0 else "unresolved"
        return {"metric": key, "status": status, "range": dict(bounds)}

    frozen = (metric is not None and test.get("direction") == "positive"
              and not (test.get("source_trial_id") and test["source_trial_id"] == trial.get("id")))
    return {**assess(metric, frozen), "direction": "positive" if frozen else None,
            "final_advantage": assess("D11"), "basis": "new_trial_model_conditional"}


def cash_quote_symbols(trial, frames, *, evaluator=None):
    """Return cash-start replay holdings needing future decision-price observations.

    Include every frozen fill/cost scenario; retain no replay paths. Missing data
    stays invalid and never creates an assumed holding or execution outcome.
    """
    if not isinstance(frames, list) or len(frames) < 2:
        return []
    symbols, evaluate = set(), evaluator or portfolio_metrics
    for key in ("incumbent", "policy"):
        try:
            result = evaluate(trial[key], frames, trial["limits"], initial_state=None,
                              execution_model=trial["execution_model"])
        except (ValueError, TypeError, KeyError, ArithmeticError):
            continue
        if not isinstance(result, dict):
            continue
        scenarios = result.get("scenario_results")
        for replay in [result, *(scenarios.values() if isinstance(scenarios, dict) else [])]:
            if not isinstance(replay, dict) or replay.get("valid") is not True:
                continue
            for state_key in ("final_state", "stress_final_state"):
                state = replay.get(state_key)
                positions = state.get("positions") if isinstance(state, dict) else None
                for position in positions if isinstance(positions, list) else []:
                    symbol = position.get("symbol") if isinstance(position, dict) else None
                    try:
                        symbols.add(_symbol(symbol))
                    except ValueError:
                        continue
    return sorted(symbols)
