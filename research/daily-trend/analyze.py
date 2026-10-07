"""Reused-history daily breakout/exit research. Never sends orders or sizes accounts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from engine import simulate_trade

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("candidate_research", ROOT / "research/candidate-screen/analyze.py")
candidate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = candidate
spec.loader.exec_module(candidate)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(path).with_suffix(Path(path).suffix + ".tmp")
    temporary.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_digest():
    paths = (HERE / "analyze.py", HERE / "engine.py", ROOT / "research/candidate-screen/analyze.py",
             ROOT / "research/candidate-screen/plan.json")
    return candidate.canonical_hash({str(p.relative_to(ROOT)): digest(p) for p in paths})


def prepare(data, plan):
    old_plan = read(ROOT / "research/candidate-screen/plan.json")
    features = candidate.features(data, old_plan)
    shortlist, _ = candidate.select_candidates(features, {"id": "liquid_top20"}, old_plan)
    p = data.prices
    previous = p["close"].shift()
    tr = pd.DataFrame(np.maximum.reduce([(p["high"]-p["low"]).to_numpy(),
                       (p["high"]-previous).abs().to_numpy(), (p["low"]-previous).abs().to_numpy()]),
                      index=previous.index, columns=previous.columns)
    atr = tr.where(features["valid"]).rolling(14).mean()
    levels = {n: p["high"].where(features["valid"]).shift().rolling(n).max()
              for n in plan["diagnostics"]["entry_sensitivity_lookbacks"]}
    signals = {n: shortlist & p["close"].gt(level) & np.isfinite(atr) & atr.gt(0)
               for n, level in levels.items()}
    frames = {s: pd.DataFrame({field: values[s] for field, values in p.items()}) for s in p["close"].columns}
    return features, shortlist, atr, levels, signals, frames


def period_positions(calendar, interval):
    return np.flatnonzero((calendar >= interval[0]) & (calendar <= interval[1]))


def ci(series, plan, selection=False):
    settings = dict(plan["uncertainty"])
    if selection:
        settings["confidence"] = settings["selection_confidence"]
    return candidate.block_ci(np.asarray(series, dtype=float), settings)


def stats(values):
    x = pd.Series(values, dtype=float).dropna()
    if x.empty:
        return {k: None for k in ("mean", "median", "win_rate", "q05", "worst")}
    return dict(mean=float(x.mean()), median=float(x.median()), win_rate=float(x.gt(0).mean()),
                q05=float(x.quantile(.05)), worst=float(x.min()))


class Study:
    def __init__(self, data, plan):
        self.data, self.plan = data, plan
        self.f, self.shortlist, self.atr, self.levels, self.signals, self.frames = prepare(data, plan)
        self.calendar = data.prices["close"].index
        self.cache = {}

    def trade(self, symbol, pos, rule, slip):
        key = (symbol, pos, rule["id"], slip)
        if key not in self.cache:
            params = dict(rule, breakout_level=float(self.levels[20].iloc[pos][symbol]),
                          atr=float(self.atr.iloc[pos][symbol]))
            result = simulate_trade(self.frames[symbol], int(pos), params, slip, self.plan["costs"])
            result.update(symbol=symbol, market=self.data.boards[symbol])
            self.cache[key] = result
        return self.cache[key]

    @staticmethod
    def eligible(result, end):
        return result["status"] == "closed" and result["exit_date"] <= end

    def evaluate(self, period, rule, slip, lookback=20):
        start, end = self.plan["periods"][period]
        positions = period_positions(self.calendar, [start, end])
        baseline_rule = {"id": f"time{rule['max_hold']}", "exit": "time", "max_hold": rule["max_hold"]}
        records, daily, nonoverlap = [], [], []
        blocked_until = {}
        for pos in positions:
            date = self.calendar[pos].date().isoformat()
            selected = self.signals[lookback].iloc[pos]
            symbols = selected.index[selected].tolist()
            day_results, liquid = [], []
            # Fixed maturity removes right-edge opportunities equally across exit variants.
            boundary = pos + rule["max_hold"] + 1 >= len(self.calendar) or self.calendar[min(pos + rule["max_hold"] + 1, len(self.calendar)-1)] > pd.Timestamp(end)
            if symbols:
                for symbol in self.shortlist.columns[self.shortlist.iloc[pos]]:
                    base = self.trade(symbol, pos, baseline_rule, slip)
                    liquid.append(base)
                for symbol in symbols:
                    result = dict(self.trade(symbol, pos, rule, slip))
                    base = self.trade(symbol, pos, baseline_rule, slip)
                    result.update(period=period, lookback=lookback, boundary=bool(boundary),
                                  baseline_status=base["status"], baseline_exit_date=base["exit_date"],
                                  baseline_unknown_reason=base["unknown_reason"],
                                  baseline_net=base["net_return"] if self.eligible(base, end) and not boundary else None)
                    good = not result["boundary"] and self.eligible(result, end) and self.eligible(base, end)
                    result["paired_observed"] = bool(good)
                    result["pair_exclusion"] = ("scheduled_boundary" if boundary else
                        f"strategy_{result['status']}" if result["status"] != "closed" else
                        "strategy_delayed_past_period" if result["exit_date"] > end else
                        f"baseline_{base['status']}" if base["status"] != "closed" else
                        "baseline_delayed_past_period" if base["exit_date"] > end else None)
                    result["exit_difference"] = result["net_return"]-base["net_return"] if good else None
                    result["market_up"] = bool(self.f["market_up"].iloc[pos][symbol])
                    result["volume_ratio"] = float(self.f["volume_ratio"].iloc[pos][symbol])
                    result["extension_atr"] = float(self.f["extension_atr"].iloc[pos][symbol])
                    result["gap_atr"] = (result["entry_raw"]-self.data.prices["close"].iloc[pos][symbol])/self.atr.iloc[pos][symbol] if result["entry_raw"] else None
                    # Only completed prior exits release a symbol. Unknown held positions remain blocked.
                    active = pos >= blocked_until.get(symbol, -1)
                    result["nonoverlap_entry"] = active
                    if active:
                        if result["status"] == "unknown":
                            blocked_until[symbol] = len(self.calendar)
                        elif result["entry_pos"] is not None:
                            blocked_until[symbol] = result["exit_pos"] if result["status"] == "closed" else len(self.calendar)
                        if good:
                            nonoverlap.append(result)
                    records.append(result)
                    day_results.append(result)
            for market in ("ALL", "KOSPI", "KOSDAQ"):
                selected_rows = [r for r in day_results if market == "ALL" or r["market"] == market]
                observed = [r for r in selected_rows if r["paired_observed"]]
                # Same exit-complete stock-date universe for the selected subset and liquid cohort.
                unavailable_selected = {r["symbol"] for r in selected_rows if not r["paired_observed"]}
                liquid_rows = [r for r in liquid if (market == "ALL" or r["market"] == market) and self.eligible(r, end) and not boundary
                               and r["symbol"] not in unavailable_selected]
                liquid_symbols = {r["symbol"] for r in liquid_rows}
                observed = [r for r in observed if r["symbol"] in liquid_symbols]
                liquid_mean = float(np.mean([r["net_return"] for r in liquid_rows])) if liquid_rows else np.nan
                mean = float(np.mean([r["net_return"] for r in observed])) if observed else np.nan
                daily.append(dict(date=date, market=market, selected_count=len(selected_rows), observed_count=len(observed),
                                  liquid_count=len(liquid_rows), net=mean, liquid=liquid_mean,
                                  liquid_unresolved=sum(not self.eligible(r, end) and not boundary for r in liquid
                                                        if market == "ALL" or r["market"] == market),
                                  edge=mean-liquid_mean, exit_edge=float(np.mean([r["exit_difference"] for r in observed])) if observed else np.nan))
        frame = pd.DataFrame(daily)
        summaries = []
        for market in ("ALL", "KOSPI", "KOSDAQ"):
            days = frame.loc[frame.market.eq(market)]
            all_rows = [r for r in records if market == "ALL" or r["market"] == market]
            rows = [r for r in all_rows if r["paired_observed"]]
            independent = [r for r in nonoverlap if market == "ALL" or r["market"] == market]
            counts = pd.Series([r["symbol"] for r in rows]).value_counts()
            totals = pd.Series([r["net_return"] for r in rows], dtype=float)
            top = sorted([r["net_return"] for r in rows], reverse=True)
            trim = max(1, int(np.ceil(len(top)*.01))) if top else 0
            summaries.append(dict(period=period, rule=rule["id"], slip=slip, lookback=lookback, market=market,
                events=len(all_rows), completed=len(rows), signal_days=int(days.net.notna().sum()),
                boundary=sum(r["boundary"] for r in all_rows), statuses=pd.Series([r["status"] for r in all_rows]).value_counts().to_dict(),
                unresolved_mature=sum(not r["boundary"] and not r["paired_observed"] for r in all_rows),
                baseline_unresolved_mature=int(days.liquid_unresolved.sum()),
                pair_exclusions=pd.Series([r["pair_exclusion"] for r in all_rows if r["pair_exclusion"]]).value_counts().to_dict(),
                net_day=stats(days.net), edge_day=stats(days.edge), edge_ci95=ci(days.edge, self.plan),
                edge_ci_selection=ci(days.edge, self.plan, True), exit_edge_day=stats(days.exit_edge),
                trade_net=stats(totals), median_mae=float(np.median([r["mae"] for r in rows])) if rows else None,
                median_mfe=float(np.median([r["mfe"] for r in rows])) if rows else None,
                median_close_mae=float(np.median([r["close_mae"] for r in rows])) if rows else None,
                holding_mean=float(np.mean([r["holding_days"] for r in rows])) if rows else None,
                exit_reasons=pd.Series([r["exit_reason"] for r in rows]).value_counts().to_dict(),
                unique_symbols=len(counts), largest_symbol_share=float(counts.iloc[0]/len(rows)) if len(counts) else None,
                mean_without_best_1pct=float(np.mean(top[trim:])) if len(top)>trim else None,
                nonoverlap_count=len(independent), nonoverlap_net=stats([r["net_return"] for r in independent])))
        return summaries, records, frame


def select(summaries, plan):
    checks, eligible = [], []
    for order, rule in enumerate(plan["rules"]):
        rows = [r for r in summaries if r["rule"] == rule["id"] and r["lookback"] == 20]
        failures = []
        for period in ("development", "validation"):
            for slip in plan["costs"]["slippage"]:
                r = next(r for r in rows if r["period"] == period and r["slip"] == slip and r["market"] == "ALL")
                for key, passed in {
                    "positive_net": r["net_day"]["mean"] is not None and r["net_day"]["mean"] > 0,
                    "positive_edge": r["edge_day"]["mean"] is not None and r["edge_day"]["mean"] > 0,
                    "sample": r["completed"] >= plan["selection"]["minimum_events"] and r["signal_days"] >= plan["selection"]["minimum_signal_days"],
                    "complete": r["unresolved_mature"] == 0 and r["baseline_unresolved_mature"] == 0,
                    "nonoverlap": r["nonoverlap_net"]["mean"] is not None and r["nonoverlap_net"]["mean"] > 0,
                }.items():
                    if not passed:
                        failures.append(f"{period}/{slip}/{key}")
            if period == "validation":
                r = next(r for r in rows if r["period"] == period and r["slip"] == .001 and r["market"] == "ALL")
                if not r["edge_ci_selection"] or r["edge_ci_selection"][0] <= 0:
                    failures.append("validation/selection_adjusted_ci")
            for market in ("KOSPI", "KOSDAQ"):
                r = next(r for r in rows if r["period"] == period and r["slip"] == .001 and r["market"] == market)
                if r["signal_days"] < 10 or r["edge_day"]["mean"] is None or r["edge_day"]["mean"] <= 0:
                    failures.append(f"{period}/{market}/edge_or_sample")
        score = next(r["net_day"]["mean"] for r in rows if r["period"] == "validation" and r["slip"] == .002 and r["market"] == "ALL")
        row = dict(rule=rule["id"], failures=failures, score=score, order=order)
        checks.append(row)
        if not failures:
            eligible.append(row)
    best = max([r["score"] for r in eligible], default=None)
    chosen = min((r for r in eligible if best-r["score"] <= .0005), key=lambda r:r["order"]) if eligible else None
    return dict(selected_rule=chosen["rule"] if chosen else None, status="research_candidate" if chosen else "no_supported_rule",
                checks=checks, live_trading_enabled=False, allocation=None, maximum_positions=None)


def segments(records):
    rows = [r for r in records if r["paired_observed"] and r["slippage"] == .001]
    output = []
    predicates = {"market_up":lambda r:r["market_up"], "volume_ge_1_5":lambda r:r["volume_ratio"]>=1.5,
                  "extension_le_2ATR":lambda r:r["extension_atr"]<=2, "gap_le_0_5ATR":lambda r:r["gap_atr"]<=.5}
    for name, predicate in predicates.items():
        for value in (False, True):
            selected = [r for r in rows if predicate(r)==value]
            output.append(dict(segment=name, value=value, events=len(selected), net=stats([r["net_return"] for r in selected]),
                               note="탐색 진단; 필터 채택·독립 표본 주장 금지"))
    return output


def run(args):
    plan = read(HERE / "plan.json")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    freeze_path = output / "selection.json"
    if args.stage == "develop":
        periods, end = ["development", "validation"], plan["periods"]["validation"][1]
    else:
        periods, end = ["reused_audit"], plan["periods"]["reused_audit"][1]
        if not freeze_path.exists():
            raise ValueError("Run develop and freeze selection before reused audit")
    data = candidate.load_data(Path(args.data), end, plan["periods"]["validation"][1])
    provenance = dict(plan_sha256=digest(HERE / "plan.json"), code_sha256=code_digest(), prefix_sha256=data.prefix_hash,
                      data_sha256=data.data_hash, generated_at=datetime.now(timezone.utc).isoformat(), calculation_end=end)
    provenance_path = output / f"{args.stage}-provenance.json"
    if provenance_path.exists():
        previous = read(provenance_path)
        for key in ("plan_sha256", "code_sha256", "prefix_sha256", "data_sha256"):
            if previous[key] != provenance[key]:
                raise ValueError(f"Existing stage {key} changed; preserve this attempt and use a new output directory")
    if freeze_path.exists():
        previous = read(freeze_path)
        for key in ("plan_sha256", "code_sha256", "prefix_sha256"):
            if previous["provenance"][key] != provenance[key]:
                raise ValueError(f"Existing selection {key} changed; preserve it and use a new output directory")
    if args.stage == "audit":
        freeze = read(freeze_path)
        for key in ("plan_sha256", "code_sha256", "prefix_sha256"):
            if freeze["provenance"][key] != provenance[key]:
                raise ValueError(f"Frozen {key} changed; use a new experiment directory")
    if not provenance_path.exists():
        write(provenance_path, provenance)
    else:
        provenance = read(provenance_path)
    study = Study(data, plan)
    all_summaries, all_records, all_daily, paths, sensitivity, regimes = [], [], [], [], [], []
    for period in periods:
        for rule in plan["rules"]:
            for slip in plan["costs"]["slippage"]:
                print(f"{period} {rule['id']} {slip}", flush=True)
                summary, records, daily = study.evaluate(period, rule, slip)
                all_summaries.extend(summary)
                all_records.extend(records)
                daily["rule"], daily["period"], daily["slip"] = rule["id"], period, slip
                all_daily.extend(daily.to_dict("records"))
                if rule["id"] == "time5" and slip == .001:
                    regimes.extend([dict(period=period, **r) for r in segments(records)])
        for horizon in plan["diagnostics"]["horizons"]:
            rule = dict(id=f"time{horizon}", exit="time", max_hold=horizon)
            summary, _, _ = study.evaluate(period, rule, .001)
            paths.extend(summary)
        if period == "development":
            for lookback in plan["diagnostics"]["entry_sensitivity_lookbacks"]:
                summary, _, _ = study.evaluate(period, dict(id="time5", exit="time", max_hold=5), .001, lookback)
                sensitivity.extend(summary)
    decision = select(all_summaries, plan) if args.stage == "develop" else read(freeze_path)["decision"]
    result = dict(provenance=provenance, decision=decision, summaries=all_summaries,
                  path_diagnostics=paths, lookback_sensitivity=sensitivity, conditional_diagnostics=regimes,
                  limitations=plan["warning"], account_return=None)
    write(output / f"{args.stage}-summary.json", result)
    if args.stage == "develop":
        frozen = dict(provenance=provenance, decision=decision)
        if freeze_path.exists():
            prior = read(freeze_path)
            if prior["decision"] != clean(decision) or any(prior["provenance"][k] != provenance[k] for k in ("plan_sha256", "code_sha256", "prefix_sha256")):
                raise ValueError("Existing selection differs; preserve it and use a new output directory")
        else:
            write(freeze_path, frozen)
    # CSV stores all rejected/unknown/boundary rows; nothing is zero-filled or silently dropped.
    pd.DataFrame(all_records).to_json(output / f"{args.stage}-trades.jsonl.gz", orient="records", lines=True, compression="gzip")
    pd.DataFrame(all_daily).to_csv(output / f"{args.stage}-daily.csv", index=False)
    # Explain the newest signals; this is a research snapshot, not a buy instruction.
    pos = len(study.calendar)-1
    latest = []
    for symbol in study.shortlist.columns[study.shortlist.iloc[pos]]:
        latest.append(dict(symbol=symbol, market=data.boards[symbol], signal_date=study.calendar[pos].date().isoformat(),
                           close=float(data.prices["close"].iloc[pos][symbol]), breakout_level=float(study.levels[20].iloc[pos][symbol]),
                           atr14=float(study.atr.iloc[pos][symbol]), breakout=bool(study.signals[20].iloc[pos][symbol]),
                           status="research_only", selected_rule=decision["selected_rule"]))
    write(output / f"{args.stage}-signals.json", dict(provenance=provenance, records=latest, order_enabled=False))
    print(json.dumps(clean(decision), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("develop", "audit"), required=True)
    parser.add_argument("--data", default=str(ROOT / ".local/research/candidate-screen-2026-10-04"))
    parser.add_argument("--output", default=str(ROOT / ".local/research/daily-trend-2026-10-04"))
    args = parser.parse_args()
    with candidate.dataset_run_lock(Path(args.output)):
        run(args)
