"""One frozen pullback hypothesis; reused-history research, never orders or sizing."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from backend.research_rules import evaluate_pullback


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


candidate = module("pullback_candidate_source", ROOT / "research/candidate-screen/analyze.py")
execution = module("pullback_execution_source", ROOT / "research/daily-trend/engine.py")
FIELDS = ("open", "high", "low", "close", "volume", "turnover")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def write(path, value, exclusive=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(clean(value), stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def fingerprint(directory):
    code = (HERE / "plan.json", HERE / "analyze.py", HERE / "test_analyze.py",
            ROOT / "backend/research_rules.py", ROOT / "research/daily-trend/engine.py",
            ROOT / "research/candidate-screen/analyze.py", ROOT / "research/candidate-screen/plan.json")
    files = [directory / name for name in ("manifest.json", "progress.json", "universe-snapshot.json")]
    files += sorted((directory / "indices").glob("*.json"))
    files += sorted((directory / "series").glob("*.json"))
    source_hashes = {str(path.relative_to(directory)): digest(path) for path in files}
    code_hashes = {str(path.relative_to(ROOT)): digest(path) for path in code}
    canonical = json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode()
    return {"source_files": source_hashes, "data_sha256": hashlib.sha256(canonical).hexdigest(),
            "code_files": code_hashes, "plan_sha256": digest(HERE / "plan.json"),
            "shared_code_sha256": digest(ROOT / "backend/research_rules.py"),
            "analyzer_sha256": digest(__file__)}


def freeze(directory, output):
    payload = {"frozen_at_utc": datetime.now(timezone.utc).isoformat(),
               "stage": "before_signal_and_outcome_analysis", "fingerprint": fingerprint(directory)}
    write(output / "freeze.json", payload, exclusive=True)
    return payload


def verify_freeze(directory, output):
    saved = read(output / "freeze.json")
    if saved["fingerprint"] != fingerprint(directory):
        raise ValueError("Frozen code/plan/data changed. Preserve this attempt and use a new run directory.")
    return saved


def vector_rule(prices):
    o, h, lo, c, v, amount = (prices[k] for k in FIELDS)
    valid = np.isfinite(o) & o.gt(0)
    for x in (h, lo, c):
        valid &= np.isfinite(x) & x.gt(0)
    valid &= h.ge(o) & h.ge(c) & lo.le(o) & lo.le(c)
    valid &= np.isfinite(v) & v.ge(0) & np.isfinite(amount) & amount.ge(0)
    eligible = valid.rolling(63).sum().eq(63)
    old = c.shift(3)
    sum20, sum60 = old.rolling(20).sum(), old.rolling(60).sum()
    trend = eligible & (old * 20).gt(sum20) & (sum20 * 3).gt(sum60) & (c * 60).gt(c.rolling(60).sum())
    signal = trend & c.shift(1).lt(c.shift(2)) & c.shift(2).lt(old) & c.gt(h.shift(1))
    return eligible, trend, signal


def direct_rule(window):
    if len(window) != 63 or not np.isfinite(window).all():
        return False, False, False
    o, h, lo, c, volume, amount = window.T
    valid = ((o > 0) & (h > 0) & (lo > 0) & (c > 0) & (volume >= 0) & (amount >= 0)
             & (h >= np.maximum(o, c)) & (lo <= np.minimum(o, c)))
    if not valid.all():
        return False, False, False
    recent, long = sum(c[-23:-3]), sum(c[:-3])
    trend = bool(c[-4] * 20 > recent and recent * 3 > long and c[-1] * 60 > sum(c[-60:]))
    return True, trend, bool(trend and c[-2] < c[-3] < c[-4] and c[-1] > h[-2])


def prepare(data):
    original = read(ROOT / "research/candidate-screen/plan.json")
    features = candidate.features(data, original)
    shortlist, _ = candidate.select_candidates(features, {"id": "liquid_top20"}, original)
    eligible, trend, signal = vector_rule(data.prices)
    calendar = data.prices["close"].index
    days = [d.date().isoformat() for d in calendar]
    frames = {s: pd.DataFrame({field: data.prices[field][s] for field in FIELDS}) for s in shortlist.columns}
    checked, insufficient = 0, 0
    for symbol, frame in frames.items():
        bars = {day: dict(zip(FIELDS, row)) for day, row in zip(days, frame.to_numpy())}
        values = frame.to_numpy()
        for pos in np.flatnonzero(shortlist[symbol].to_numpy()):
            shared = evaluate_pullback(bars, days[:pos+1])
            actual = tuple(bool(shared[key]) for key in ("eligible", "trend", "signal"))
            expected = tuple(bool(x.iloc[pos][symbol]) for x in (eligible, trend, signal))
            direct = direct_rule(values[max(0, pos-62):pos+1])
            if actual != expected or actual != direct:
                raise ValueError(f"Independent rule mismatch: {symbol} {days[pos]} {actual}/{expected}/{direct}")
            checked += 1
            insufficient += not actual[0]
    audit = {"shortlist_stock_dates_checked": checked, "insufficient_or_invalid_63_sessions": insufficient,
             "mismatches": 0, "methods": ["shared Decimal", "vectorized sums", "direct 63-row sums"]}
    return shortlist, eligible & shortlist, trend & shortlist, signal & shortlist, frames, audit


def bootstrap_means(values, settings):
    values = np.asarray(values, dtype=float)
    block, reps = settings["block_sessions"], settings["replicates"]
    if len(values) < block:
        return np.array([], dtype=float)
    rng = np.random.default_rng(settings["seed"])
    starts = rng.integers(0, len(values)-block+1, size=(reps, math.ceil(len(values)/block)))
    indices = (starts[..., None] + np.arange(block)).reshape(reps, -1)[:, :len(values)]
    draws = values[indices]
    counts = np.isfinite(draws).sum(axis=1)
    return np.divide(np.nansum(draws, axis=1), counts, out=np.full(reps, np.nan), where=counts > 0)


def calendar_ci(values, settings):
    values = np.asarray(values, dtype=float)
    if len(values) <= settings["block_sessions"] or np.isfinite(values).sum() < settings["small_sample_days"]:
        return None
    means = bootstrap_means(values, settings)
    finite = means[np.isfinite(means)]
    if not len(finite):
        return None
    alpha = (1-settings["confidence"])/2
    return [float(x) for x in np.quantile(finite, [alpha, 1-alpha])]


def stats(values):
    values = np.asarray([x for x in values if x is not None], dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return {key: None for key in ("mean", "median", "q05", "worst", "win_rate")}
    return {"mean": float(values.mean()), "median": float(np.median(values)),
            "q05": float(np.quantile(values, .05)), "worst": float(values.min()),
            "win_rate": float(np.mean(values > 0))}


def aggregate_markets(rows):
    total = sum(row["observed_signals"] for row in rows)
    combined = {key: sum(row[key] for row in rows) for key in
                ("signals", "controls", "observed_signals", "observed_controls", "boundary",
                 "signal_data_unknown", "control_data_unknown", "signal_fill_unverifiable", "control_fill_unverifiable")}
    for key in ("net", "control", "edge"):
        combined[key] = (sum(row[key] * row["observed_signals"] for row in rows if row["observed_signals"])
                         / total if total else None)
    return combined


def nonoverlap(records):
    blocks, result = {}, []
    for original in sorted(records, key=lambda r: (r["signal_date"], r["symbol"])):
        row = dict(original)
        row["nonoverlap_entry"] = row["signal_pos"] >= blocks.get(row["symbol"], -1)
        if row["nonoverlap_entry"]:
            if row["classification"] in ("data_unknown", "fill_unverifiable", "unclosed"):
                blocks[row["symbol"]] = math.inf
            elif row["classification"] == "observed":
                blocks[row["symbol"]] = row["exit_pos"]
        result.append(row)
    return result


class Study:
    def __init__(self, data, plan):
        self.data, self.plan = data, plan
        self.shortlist, self.eligible, self.trend, self.signal, self.frames, self.formula_audit = prepare(data)
        self.calendar = data.prices["close"].index
        self.cache = {}

    def trade(self, symbol, pos, slip, period):
        key = (symbol, pos, slip, period)
        if key in self.cache:
            return dict(self.cache[key])
        end = self.plan["periods"][period][1]
        base = dict(symbol=symbol, market=self.data.boards[symbol], period=period, slippage=slip,
                    signal_date=self.calendar[pos].date().isoformat(), signal_pos=int(pos),
                    net_return=None, entry_pos=None, exit_pos=None)
        due = pos + 6
        if due >= len(self.calendar) or self.calendar[due] > pd.Timestamp(end):
            row = dict(base, status="not_simulated", classification="boundary")
        else:
            result = execution.simulate_trade(self.frames[symbol].loc[:end], int(pos), self.plan["exit"], slip, self.plan["costs"])
            classification = {"closed": "observed", "cancelled": "fill_unverifiable", "unknown": "data_unknown",
                              "open": "unclosed", "pending_entry": "unclosed"}[result["status"]]
            if classification == "unclosed" and any(flag.startswith("exit_") and ("volume" in flag or "flat" in flag) for flag in result["flags"]):
                classification = "fill_unverifiable"
            row = dict(result, **base)
            row.update(net_return=result["net_return"], entry_pos=result["entry_pos"], exit_pos=result["exit_pos"], classification=classification)
        self.cache[key] = row
        return dict(row)

    def evaluate(self, period, slip):
        start, end = self.plan["periods"][period]
        positions = np.flatnonzero((self.calendar >= start) & (self.calendar <= end))
        records, control_records, daily = [], [], []
        for pos in positions:
            date = self.calendar[pos].date().isoformat()
            day_rows = []
            for market in ("KOSPI", "KOSDAQ"):
                selected = [s for s in self.signal.columns[self.signal.iloc[pos]] if self.data.boards[s] == market]
                controls = [s for s in self.trend.columns[self.trend.iloc[pos]] if self.data.boards[s] == market] if selected else []
                outcomes = {s: self.trade(s, pos, slip, period) for s in controls}
                selected_rows = [dict(outcomes[s]) for s in selected]
                for row in outcomes.values():
                    control_records.append(dict(row, role="control", is_signal=row["symbol"] in selected))
                records.extend(selected_rows)
                observed_controls = [r for r in outcomes.values() if r["classification"] == "observed"]
                observed = [r for r in selected_rows if r["classification"] == "observed"]
                net = stats([r["net_return"] for r in observed])["mean"]
                control = stats([r["net_return"] for r in observed_controls])["mean"]
                item = dict(date=date, period=period, slip=slip, market=market,
                            signals=len(selected), controls=len(controls), observed_signals=len(observed),
                            observed_controls=len(observed_controls), net=net, control=control,
                            edge=net-control if net is not None and control is not None else None,
                            boundary=sum(r["classification"] == "boundary" for r in selected_rows))
                for label in ("data_unknown", "fill_unverifiable"):
                    item[f"signal_{label}"] = sum(r["classification"] == label for r in selected_rows)
                    item[f"control_{label}"] = sum(r["classification"] == label for r in outcomes.values())
                day_rows.append(item)
            daily.extend(day_rows)
            daily.append(dict(date=date, period=period, slip=slip, market="ALL", **aggregate_markets(day_rows)))
        records = nonoverlap(records)
        summary = []
        for market in ("ALL", "KOSPI", "KOSDAQ"):
            rows = [r for r in records if market == "ALL" or r["market"] == market]
            controls = [r for r in control_records if market == "ALL" or r["market"] == market]
            observed = [r for r in rows if r["classification"] == "observed"]
            days = [r for r in daily if r["market"] == market]
            no_overlap = [r for r in observed if r["nonoverlap_entry"]]
            counts = {label: sum(r["classification"] == label for r in rows) for label in
                      ("observed", "boundary", "fill_unverifiable", "data_unknown", "unclosed")}
            control_counts = {label: sum(r["classification"] == label for r in controls) for label in counts}
            summary.append(dict(period=period, market=market, slip=slip, events=len(rows), counts=counts,
                control_events=len(controls), control_counts=control_counts, calendar_sessions=len(days),
                signal_days=sum(r["net"] is not None for r in days), raw_signal_days=sum(r["signals"] > 0 for r in days),
                net_day=stats([r["net"] for r in days]), edge_day=stats([r["edge"] for r in days]),
                control_day=stats([r["control"] for r in days]),
                edge_ci95=calendar_ci([np.nan if r["edge"] is None else r["edge"] for r in days], self.plan["uncertainty"]),
                trade_net=stats([r["net_return"] for r in observed]),
                nonoverlap_count=len(no_overlap), nonoverlap_net=stats([r["net_return"] for r in no_overlap])))
        return summary, records, control_records, daily


def research_gate(summaries, plan):
    failures = []
    settings = plan["research_gate"]
    def get(period, slip, market):
        return next(r for r in summaries if r["period"] == period and r["slip"] == slip and r["market"] == market)
    for period in ("development", "validation"):
        for slip in plan["costs"]["slippage"]:
            row = get(period, slip, "ALL")
            checks = {
                "events": row["counts"]["observed"] >= settings["minimum_events"],
                "signal_days": row["signal_days"] >= settings["minimum_signal_days"],
                "net": row["net_day"]["mean"] is not None and row["net_day"]["mean"] > 0,
                "edge": row["edge_day"]["mean"] is not None and row["edge_day"]["mean"] > 0,
                "nonoverlap": row["nonoverlap_net"]["mean"] is not None and row["nonoverlap_net"]["mean"] > 0,
                "unknown_data": row["counts"]["data_unknown"] == 0 and row["control_counts"]["data_unknown"] == 0,
            }
            failures.extend(f"{period}/{slip}/{name}" for name, passed in checks.items() if not passed)
        for market in ("KOSPI", "KOSDAQ"):
            row = get(period, plan["costs"]["slippage"][0], market)
            if row["signal_days"] < settings["minimum_market_days"] or row["edge_day"]["mean"] is None or row["edge_day"]["mean"] <= 0:
                failures.append(f"{period}/{market}/edge_or_sample")
    interval = get("validation", plan["costs"]["slippage"][0], "ALL")["edge_ci95"]
    if interval is None or interval[0] <= 0:
        failures.append("validation/edge_ci_lower")
    execution_issues = sum(r["counts"]["fill_unverifiable"] + r["control_counts"]["fill_unverifiable"]
                           + r["counts"]["unclosed"] + r["control_counts"]["unclosed"]
                           for r in summaries if r["market"] == "ALL")
    return dict(status="research_candidate" if not failures else "unadopted", failures=failures,
                execution_review_required=execution_issues > 0, adopted=False, order_enabled=False,
                allocation=None, maximum_positions=None,
                interpretation="All history reused; candidate gate cannot establish prospective performance.")


def run(directory, output):
    frozen = verify_freeze(directory, output)
    if (output / "results.json").exists():
        raise ValueError("Completed results already exist; preserve this run")
    plan = read(HERE / "plan.json")
    data = candidate.load_data(directory, max(interval[1] for interval in plan["periods"].values()), plan["periods"]["validation"][1])
    study = Study(data, plan)
    write(output / "formula-audit.json", study.formula_audit, exclusive=True)
    summaries, records, controls, daily = [], [], [], []
    for period in plan["periods"]:
        for slip in plan["costs"]["slippage"]:
            print(f"{period} {slip}", flush=True)
            ss, rr, cc, dd = study.evaluate(period, slip)
            summaries.extend(ss); records.extend(rr); controls.extend(cc); daily.extend(dd)
    verify_freeze(directory, output)
    result = dict(rule_id=plan["rule_id"], frozen_at_utc=frozen["frozen_at_utc"], freeze_sha256=digest(output / "freeze.json"),
                  fingerprint=frozen["fingerprint"], formula_audit=study.formula_audit,
                  summaries=summaries, decision=research_gate(summaries, plan),
                  limitations=["All periods are reused exploration; fixed-universe and current-adjustment bias remain.",
                               "Historical restrictions and original prices are unavailable; daily flat/zero-volume execution is retrospective uncertainty.",
                               "Nonoverlap removes same-symbol simultaneous positions, not market/day dependence.",
                               "No account sizing, account returns, adopted strategy or live orders."],
                  output_directory=str(output.resolve()), account_return=None)
    write(output / "results.json", result, exclusive=True)
    pd.DataFrame(records).to_json(output / "signals.jsonl.gz", orient="records", lines=True, compression="gzip")
    pd.DataFrame(controls).to_json(output / "controls.jsonl.gz", orient="records", lines=True, compression="gzip")
    pd.DataFrame(daily).to_csv(output / "daily.csv", index=False)
    write(HERE / "results.json", result)
    print(json.dumps(clean(result["decision"]), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "run"))
    parser.add_argument("--data", type=Path, default=ROOT / ".local/research/candidate-screen-2026-10-04")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/research/pullback-recovery-2026-10-05")
    args = parser.parse_args()
    if args.mode == "freeze":
        saved = freeze(args.data, args.output)
        print(json.dumps({"frozen_at_utc": saved["frozen_at_utc"],
                          "data_sha256": saved["fingerprint"]["data_sha256"],
                          "path": str(args.output / "freeze.json")}, ensure_ascii=False))
    else:
        run(args.data, args.output)
