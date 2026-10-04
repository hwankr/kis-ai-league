"""Locked candidate-cohort diagnostics; never a portfolio/account backtest.

KIS adjusted OHLC and adjusted share volume are used for prices/tradability.
Liquidity and volume_ratio use *original KRW turnover*, not adjusted close*volume.
Current-vintage corporate-action adjustments and a fixed universe are limitations.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / ".local/research/candidate-screen-2026-10-04"
FIELDS = ("open", "high", "low", "close", "volume", "turnover")
INDEX_CODES = {"KOSPI": "0001", "KOSDAQ": "1001"}
VERSION = 1
LEDGER_NAME = "analysis-run-ledger.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def write_json(path: Path, value: Any, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if exclusive else "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def save_ledger(path: Path, ledger: dict) -> None:
    """Replace the ledger atomically; a failed artifact write must not lose its freeze."""
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


@contextmanager
def dataset_run_lock(directory: Path):
    """OS releases the lock after a crash, allowing the recorded attempt to resume."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".analysis-run.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("Analysis is already running for this dataset") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def finite(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def frame(rows: list[dict], end: str) -> pd.DataFrame:
    rows = [row for row in rows if row["date"] <= end]
    if len({row["date"] for row in rows}) != len(rows):
        raise ValueError("Duplicate trading dates")
    result = pd.DataFrame(rows)
    if result.empty:
        return pd.DataFrame(columns=FIELDS, index=pd.DatetimeIndex([]), dtype=float)
    result.index = pd.to_datetime(result["date"], format="%Y-%m-%d")
    return result.reindex(columns=FIELDS).apply(pd.to_numeric, errors="coerce").sort_index()


@dataclass
class Dataset:
    prices: dict[str, pd.DataFrame]
    indices: dict[str, pd.DataFrame]
    boards: pd.Series
    audit: dict
    data_hash: str
    prefix_hash: str


def load_data(directory: Path, end: str, prefix_end: str) -> Dataset:
    """Only dated rows <= end enter calculations; prefix digest ignores future rows."""
    progress = read_json(directory / "progress.json")
    if progress.get("status") != "complete":
        raise ValueError("Collection is not complete; outcome analysis is disabled")
    universe = read_json(directory / "universe-snapshot.json")
    manifest = read_json(directory / "manifest.json")
    if universe.get("status") != "verified" or manifest.get("FID_ORG_ADJ_PRC") != "0":
        raise ValueError("Verified universe and adjusted daily data (parameter 0) required")
    symbols = sorted(row["symbol"] for row in universe["rows"])
    if len(symbols) != len(set(symbols)) or len(symbols) != manifest["stock_count"]:
        raise ValueError("Universe count/duplicates mismatch")
    boards = pd.Series({row["symbol"]: row["board"] for row in universe["rows"]}).reindex(symbols)
    if not boards.isin(INDEX_CODES).all():
        raise ValueError("Unknown stock market")
    raw_frames: dict[str, pd.DataFrame] = {}
    indices = {}
    digests = {}
    prefix = {"universe": universe["rows"], "series": {}}
    quality = []
    for kind, targets in (("indices", list(INDEX_CODES.values())), ("series", symbols)):
        for symbol in targets:
            path = directory / kind / f"{symbol}.json"
            data = read_json(path)
            if data.get("status") != "complete" or data.get("conflicts"):
                raise ValueError(f"Unresolved collection failure/conflict: {kind}/{symbol}")
            if data.get("universe_sha256") != manifest.get("universe_sha256"):
                raise ValueError(f"Universe provenance mismatch: {symbol}")
            if kind == "series" and data.get("board") != boards[symbol]:
                raise ValueError(f"Board mismatch: {symbol}")
            digests[f"{kind}/{symbol}"] = hashlib.sha256(path.read_bytes()).hexdigest()
            prefix["series"][f"{kind}/{symbol}"] = sorted(
                [{key: row.get(key) for key in ("date", *FIELDS, "flags")}
                 for row in data["rows"] if row["date"] <= prefix_end], key=lambda row: row["date"])
            values = frame(data["rows"], end)
            if kind == "indices":
                indices[data["board"]] = values
            else:
                raw_frames[symbol] = values
                quality.append({"symbol": symbol, "board": boards[symbol], "rows": len(values),
                                "first": str(values.index.min().date()) if len(values) else None})
    calendar = indices["KOSPI"].index
    if not calendar.equals(indices["KOSDAQ"].index) or not len(calendar):
        raise ValueError("Index trading calendars differ or are empty; cannot align safely")
    for board, values in indices.items():
        if not (values[list(FIELDS[:4])].gt(0).all(axis=1)).all():
            raise ValueError(f"Invalid index OHLC: {board}")
    for symbol, values in raw_frames.items():
        if not values.index.isin(calendar).all():
            raise ValueError(f"Stock date not present in benchmark calendar: {symbol}")
    prices = {field: pd.DataFrame({symbol: values[field].reindex(calendar)
                                  for symbol, values in raw_frames.items()}) for field in FIELDS}
    for row in quality:
        values = prices["close"][row["symbol"]]
        row["missing_calendar_rows"] = int(values.isna().sum())
        row["zero_volume_rows"] = int(prices["volume"][row["symbol"]].eq(0).sum())
    audit = {"universe_count": len(symbols), "trading_sessions": len(calendar),
             "calculation_end": end, "series": quality,
             "source_file_sha256": digests, "manifest": manifest}
    return Dataset(prices, indices, boards, audit, canonical_hash(digests), canonical_hash(prefix))


def features(data: Dataset, plan: dict) -> dict[str, pd.DataFrame]:
    p = data.prices
    c, h, low = p["close"], p["high"], p["low"]
    valid = np.isfinite(p["open"]) & p["open"].gt(0)
    for field in ("high", "low", "close"):
        valid &= np.isfinite(p[field]) & p[field].gt(0)
    valid &= h.ge(p["open"]) & h.ge(c) & low.le(p["open"]) & low.le(c) & h.ge(low)
    c = c.where(valid)
    market = pd.DataFrame({symbol: data.indices[board]["close"] for symbol, board in data.boards.items()})
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    tr = pd.DataFrame(np.maximum.reduce([(h-low).to_numpy(), (h-c.shift()).abs().to_numpy(),
                                        (low-c.shift()).abs().to_numpy()]), index=c.index, columns=c.columns)
    atr = tr.where(valid).rolling(14).mean()  # Includes signal day; no future OHLC.
    turnover = p["turnover"].where(np.isfinite(p["turnover"]) & p["turnover"].ge(0))
    previous_turnover = turnover.shift(1).rolling(20).mean()
    daily_returns = c / c.shift(1) - 1  # Never forward-fill a missing stock session.
    result = {"valid": valid, "turnover20": turnover.rolling(20).mean(),
              "volume_positive20": (p["volume"].gt(0) & np.isfinite(p["volume"])).rolling(20).sum().eq(20),
              "trend": c.gt(ma20) & ma20.gt(ma60) & ma20.gt(ma20.shift(5)),
              "volume_ratio": turnover / previous_turnover.where(previous_turnover.gt(0)),
              "extension_atr": (c-ma20) / atr.where(atr.gt(0)),
              "market_up": market.gt(market.rolling(60).mean())}
    for n in (61, 148, 253):
        result[f"history{n}"] = valid.rolling(n).sum().eq(n)
    for name, newer, older in (("rs20", 0, 20), ("rs60", 0, 60),
                               ("rs6m", 21, 147), ("rs12to7m", 126, 252)):
        result[name] = c.shift(newer)/c.shift(older) - market.shift(newer)/market.shift(older)
    vol = daily_returns.rolling(60).std(ddof=1) * math.sqrt(60)
    result["risk_adjusted"] = result["rs60"] / vol.where(vol.gt(0))
    return result


def select_candidates(f: dict, variant: dict, plan: dict, liquidity: float | None = None,
                      shortlist: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    threshold = plan["base"]["average_turnover_20d_krw"] if liquidity is None else liquidity
    limit = plan["maximum_shortlist"] if shortlist is None else shortlist
    history = variant.get("minimum_history_sessions", plan["base"]["minimum_history_sessions"])
    base = f[f"history{history}"] & f["volume_positive20"] & f["turnover20"].ge(threshold)
    selected = base.copy()
    name = variant["id"]
    supported = {"liquid_all", "liquid_top20", "rs20_top20", "rs60_top20", "rs6m_skip1m_top20",
                 "rs12to7m_top20", "trend_rs60", "trend_rs20_60", "trend_volume", "trend_extension",
                 "trend_market", "trend_risk_adjusted"}
    if name not in supported:
        raise ValueError(f"Unsupported pre-registered variant: {name}")
    rank = f["turnover20"]
    if name == "liquid_all":
        return selected, base
    if name.startswith("rs"):
        key = {"rs20_top20": "rs20", "rs60_top20": "rs60", "rs6m_skip1m_top20": "rs6m",
               "rs12to7m_top20": "rs12to7m"}[name]
        rank = f[key]
        selected &= rank.gt(0)
    elif name.startswith("trend_"):
        selected &= f["trend"] & f["rs60"].gt(0)
        rank = f["risk_adjusted"] if name == "trend_risk_adjusted" else f["rs60"]
        if name == "trend_rs20_60":
            selected &= f["rs20"].gt(0)
        elif name == "trend_volume":
            selected &= f["volume_ratio"].ge(1.5)
        elif name == "trend_extension":
            selected &= f["extension_atr"].le(2)
        elif name == "trend_market":
            selected &= f["market_up"]
    elif name != "liquid_top20":
        raise ValueError(f"Unsupported pre-registered variant: {name}")
    # Columns are code-sorted; method='first' fixes score ties without future data.
    selected &= np.isfinite(rank)
    positions = rank.where(selected).rank(axis=1, method="first", ascending=False)
    return selected & positions.le(limit), base


def forward_returns(data: Dataset, horizon: int, slippage: float, costs: dict) -> pd.DataFrame:
    p = data.prices
    entry, exit_price = p["open"].shift(-1), p["close"].shift(-horizon)
    observed = entry.gt(0) & exit_price.gt(0) & np.isfinite(entry) & np.isfinite(exit_price)
    observed &= p["volume"].shift(-1).gt(0) & p["volume"].shift(-horizon).gt(0)
    observed &= np.isfinite(p["volume"].shift(-1)) & np.isfinite(p["volume"].shift(-horizon))
    # Exact cash-on-cash scenario: entry outlay includes buy fee, exit proceeds deduct fee/tax.
    cost_factor = ((1-slippage) * (1-costs["sell_fee_rate"]-costs["sell_tax_rate"]) /
                   ((1+slippage) * (1+costs["buy_fee_rate"])))
    return (exit_price / entry * cost_factor - 1).where(observed)


@lru_cache(maxsize=128)
def bootstrap_indices(length: int, block: int, replicates: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, length-block+1, size=(replicates, math.ceil(length/block)))
    return (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(replicates, -1)[:, :length]


def block_ci(values: np.ndarray, settings: dict) -> list[float] | None:
    """Keep missing/empty signal days in calendar positions while resampling blocks."""
    block, reps = settings["block_sessions"], settings["replicates"]
    if len(values) <= block or np.isfinite(values).sum() <= block:
        return None
    draws = values[bootstrap_indices(len(values), block, reps, settings["seed"])]
    counts = np.isfinite(draws).sum(axis=1)
    means = np.divide(np.nansum(draws, axis=1), counts,
                      out=np.full(reps, np.nan), where=counts > 0)
    means = means[np.isfinite(means)]
    if len(means) < reps * .95:
        return None
    alpha = (1-settings["confidence"])/2
    return [float(v) for v in np.quantile(means, [alpha, 1-alpha])]


def summarize(daily: pd.DataFrame, settings: dict) -> dict:
    diff = daily["difference"].to_numpy()
    returns = daily["selected_return"].dropna()
    selected = int(daily["selected_count"].sum())
    missing = int(daily["missing_count"].sum())
    paired_days = int(np.isfinite(diff).sum())
    base_total = int(daily["baseline_count"].sum())
    base_missing = int(daily["baseline_missing_count"].sum())
    result = {"signal_calendar_days": len(daily), "days_with_candidates": int(daily["selected_count"].gt(0).sum()),
              "paired_days": paired_days, "candidate_observations": selected,
              "unique_candidates": sorted(set().union(*daily["symbols"].map(set))) if len(daily) else [],
              "mean_candidates": finite(daily["selected_count"].mean()),
              "empty_day_rate": finite(daily["selected_count"].eq(0).mean()),
              "missing_outcomes": missing, "missing_outcome_rate": missing/selected if selected else None,
              "baseline_observations": base_total,
              "baseline_empty_day_rate": finite(daily["baseline_count"].eq(0).mean()),
              "baseline_missing_outcomes": base_missing,
              "baseline_missing_outcome_rate": base_missing/base_total if base_total else None,
              "mean_net_return": finite(returns.mean()), "median_net_return": finite(returns.median()),
              "mean_paired_baseline_return": finite(daily.loc[np.isfinite(diff), "baseline_return"].mean()),
              "positive_day_rate": finite(returns.gt(0).mean()) if len(returns) else None,
              "net_return_q05": finite(returns.quantile(.05)) if len(returns) else None,
              "mean_paired_difference": finite(np.nanmean(diff)) if paired_days else None,
              "paired_difference_ci95": block_ci(diff, settings),
              "small_sample": paired_days < 3*settings["block_sessions"] or selected-missing < 100}
    return result


def daily_cohorts(selected: pd.DataFrame, baseline: pd.DataFrame, returns: pd.DataFrame,
                  start: str, end: str, horizon: int, boards: pd.Series, market: str) -> pd.DataFrame:
    calendar = returns.index
    exit_dates = pd.Series(calendar, index=calendar).shift(-horizon)
    allowed = (calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end)) & exit_dates.le(end).to_numpy()
    columns = boards.index if market == "ALL" else boards.index[boards.eq(market)]
    signal, base = selected.loc[allowed, columns], baseline.loc[allowed, columns]
    outcomes = returns.loc[allowed, columns]
    observable = outcomes.notna()
    # Both arms use this same observable stock-date set; never replace unknown returns by zero.
    observed_signal, observed_base = signal & observable, base & observable
    chosen_mean = outcomes.where(observed_signal).mean(axis=1)
    base_mean = outcomes.where(observed_base).mean(axis=1)
    result = pd.DataFrame({"selected_count": signal.sum(axis=1),
                           "observed_count": observed_signal.sum(axis=1),
                           "missing_count": (signal & ~observable).sum(axis=1),
                           "baseline_count": base.sum(axis=1),
                           "baseline_missing_count": (base & ~observable).sum(axis=1),
                           "selected_return": chosen_mean,
                           "baseline_return": base_mean,
                           "difference": chosen_mean-base_mean})
    result["symbols"] = [columns[mask].tolist() for mask in signal.to_numpy()]
    return result


def evaluate(data: Dataset, f: dict, plan: dict, periods: list[str], variants: list[dict],
             liquidity: float | None = None, shortlist: int | None = None,
             primary_only: bool = False) -> tuple[list[dict], list[dict]]:
    summaries, daily_records = [], []
    evaluation = plan["evaluation"]
    horizons = [evaluation["primary_horizon_sessions"]]
    if not primary_only:
        horizons += evaluation["secondary_horizon_sessions"]
    returns = {(h, s): forward_returns(data, h, s, plan["costs"])
               for h in horizons for s in plan["costs"]["slippage_each_side"]}
    for variant in variants:
        selected, baseline = select_candidates(f, variant, plan, liquidity, shortlist)
        for period in periods:
            start, end = plan["periods"][period]
            for (horizon, slippage), outcomes in returns.items():
                for market in ("ALL", "KOSPI", "KOSDAQ"):
                    daily = daily_cohorts(selected, baseline, outcomes, start, end, horizon, data.boards, market)
                    labels = {"variant": variant["id"], "period": period, "horizon": horizon,
                              "slippage_each_side": slippage, "market": market}
                    summary = {**labels, **summarize(daily, evaluation["uncertainty"])}
                    summary["boundary_purged_signal_days"] = int(((outcomes.index >= start) &
                        (outcomes.index <= end)).sum()) - len(daily)
                    summary["quarters"] = {str(q): summarize(group, evaluation["uncertainty"])
                                           for q, group in daily.groupby(daily.index.to_period("Q"))}
                    summaries.append(summary)
                    if (horizon == evaluation["primary_horizon_sessions"] and
                            slippage == plan["costs"]["primary_slippage_each_side"]):
                        for day, row in daily.iterrows():
                            daily_records.append({**labels, "date": str(day.date()),
                                                  **{k: finite(v) if isinstance(v, (float, np.floating)) else v
                                                     for k, v in row.items()}})
    return summaries, daily_records


def choose_variant(summaries: list[dict], plan: dict) -> dict:
    candidates = []
    for order, variant in enumerate(plan["variants"]):
        if variant["id"] == "liquid_all":
            continue
        metrics = {row["period"]: row for row in summaries
                   if row["variant"] == variant["id"] and row["market"] == "ALL" and
                   row["horizon"] == plan["evaluation"]["primary_horizon_sessions"] and
                   row["slippage_each_side"] == plan["costs"]["primary_slippage_each_side"]}
        differences = [metrics.get(p, {}).get("mean_paired_difference") for p in ("development", "validation")]
        if all(value is not None and value > 0 for value in differences):
            candidates.append({"variant": variant["id"], "order": order,
                               "additional_filter_count": variant["additional_filter_count"],
                               "development_difference": differences[0], "validation_difference": differences[1]})
    if not candidates:
        return {"selected_variant": None, "reason": "No variant has positive development and validation differences",
                "qualifying_variants": []}
    maximum = max(row["validation_difference"] for row in candidates)
    tied = [row for row in candidates if maximum-row["validation_difference"] <= .0005 + 1e-12]
    winner = min(tied, key=lambda row: (row["additional_filter_count"], row["order"]))
    return {"selected_variant": winner["variant"], "reason": "Locked development-validation rule",
            "qualifying_variants": candidates, "tie_tolerance_return_units": .0005}


def adoption_decision(summaries: list[dict], frozen: dict, plan: dict) -> dict:
    """Apply the pre-outcome gate to the frozen variant only; never select a replacement."""
    gate = plan["evaluation"]["adoption_gate"]
    selected = frozen.get("selected_variant")
    valid_variants = {row["id"] for row in plan["variants"] if row["id"] != "liquid_all"}
    selection_status = "missing" if selected is None else "pass" if selected in valid_variants else "fail"
    checks = [{"id": "frozen_selection", "status": selection_status, "value": selected}]

    def numeric(value):
        return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, (bool, np.bool_)) and math.isfinite(value)

    for rule in gate["checks"]:
        slippage = gate["slippage_each_side"][rule["slippage"]]
        matched = [row for row in summaries if selection_status == "pass" and
                   row.get("variant") == selected and row.get("period") == gate["period"] and
                   row.get("horizon") == gate["horizon_sessions"] and row.get("market") == rule["market"] and
                   row.get("slippage_each_side") == slippage]
        value = matched[0].get(rule["metric"]) if len(matched) == 1 else None
        if rule.get("component") == "lower":
            value = (value[0] if isinstance(value, (list, tuple)) and len(value) == 2 and
                     all(numeric(bound) for bound in value) and value[0] <= value[1] else None)
        value = float(value) if numeric(value) else None
        status = "missing" if value is None else "pass" if value > gate["minimum_exclusive"] else "fail"
        checks.append({**rule, "period": gate["period"], "horizon": gate["horizon_sessions"],
                       "slippage_each_side": slippage, "minimum_exclusive": gate["minimum_exclusive"],
                       "value": value, "status": status})
    statuses = {check["status"] for check in checks}
    status = "fail" if "fail" in statuses else "missing" if "missing" in statuses else "pass"
    adopted = status == "pass"
    return {"status": status, "adopted": adopted, "selected_variant": selected,
            "production_variant": selected if adopted else gate["fallback_variant"],
            "reselection": False, "checks": checks, "gate_fixed_at_utc": gate["fixed_at_utc"],
            "reason": "No frozen variant was selected" if selected is None else
                      gate["pass_label"] if adopted else "Frozen variant did not pass every adoption check",
            "basis": "fixed_universe_diagnostic" if adopted else "operational_default",
            "fallback_note": gate["fallback_note"], "claim_limit": gate["claim_limit"]}


def save_tables(path: Path, summaries: list[dict]) -> None:
    lines = ["# 후보 코호트 진단", "", "수익률·차이·구간은 %. 일별 후보 코호트이며 계좌 수익률이 아님.", "",
             "|기간|변형|시장|보유일|편도 슬리피지|유효일|후보 관측|빈날 %|누락 %|평균 %|기준군 차이 %p|95% CI %p|소표본|",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    def fmt(value):
        return "—" if value is None else f"{value*100:.3f}"
    for row in summaries:
        ci = row["paired_difference_ci95"]
        lines.append("|" + "|".join(map(str, [row["period"], row["variant"], row["market"], row["horizon"],
                     fmt(row["slippage_each_side"]), row["paired_days"], row["candidate_observations"],
                     fmt(row["empty_day_rate"]), fmt(row["missing_outcome_rate"]), fmt(row["mean_net_return"]),
                     fmt(row["mean_paired_difference"]), " ~ ".join(map(fmt, ci)) if ci else "—",
                     "예" if row["small_sample"] else "아니오"])) + "|")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(stage: str, plan_path: Path, directory: Path, output: Path) -> dict:
    with dataset_run_lock(directory):
        return run_locked(stage, plan_path, directory, output)


def run_locked(stage: str, plan_path: Path, directory: Path, output: Path) -> dict:
    if stage not in ("development-validation", "holdout"):
        raise ValueError("Unknown analysis stage")
    plan = read_json(plan_path)
    if not plan.get("locked_before_outcome_analysis"):
        raise ValueError("Pre-registered plan required")
    plan_hash = canonical_hash(plan)
    engine_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ledger_path = directory / LEDGER_NAME
    ledger = read_json(ledger_path) if ledger_path.exists() else {
        "version": VERSION, "plan_sha256": plan_hash, "engine_sha256": engine_hash, "stages": {}}
    previous = ledger["stages"].get(stage)
    if previous and previous["status"] == "complete":
        message = "Selection already frozen" if stage == "development-validation" else "Holdout already evaluated"
        raise ValueError(f"{message} for this dataset; changing output directories cannot restart it")
    frozen = ledger.get("frozen_selection")
    if stage == "development-validation" and not previous and (output / "selection-frozen.json").exists():
        raise ValueError("Selection already frozen; do not overwrite/reselect")
    if stage == "holdout":
        if not frozen or ledger["stages"].get("development-validation", {}).get("status") != "complete":
            raise ValueError("Development-validation selection must be frozen before holdout")
        if not previous and (output / "holdout.json").exists():
            raise ValueError("Holdout already evaluated; results may not be overwritten")
    if ledger["plan_sha256"] != plan_hash:
        raise ValueError("Plan changed after analysis registration")
    if ledger["engine_sha256"] != engine_hash:
        raise ValueError("Analysis implementation changed after registration")
    output_name = str(output.resolve())
    if previous and previous["output_dir"] != output_name:
        raise ValueError(f"Incomplete stage must resume in its original output directory: {previous['output_dir']}")
    record = previous or {"output_dir": output_name, "attempts": []}
    if record.get("status") == "running":
        # The OS lock is now ours: the preceding process terminated without recording its failure.
        record["attempts"][-1].update(status="interrupted", error="Previous process ended before completion")
    attempt = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "running"}
    record["attempts"].append(attempt)
    record["status"] = "running"
    ledger["stages"][stage] = record
    save_ledger(ledger_path, ledger)
    try:
        result = run_stage(stage, plan, directory, output, ledger, ledger_path)
        attempt.update(status="complete", finished_at=datetime.now(timezone.utc).isoformat())
        record["status"] = "complete"
        save_ledger(ledger_path, ledger)
        return result
    except BaseException as error:
        attempt.update(status="failed", finished_at=datetime.now(timezone.utc).isoformat(),
                       error=f"{type(error).__name__}: {error}")
        record["status"] = "failed"
        save_ledger(ledger_path, ledger)
        raise


def run_stage(stage: str, plan: dict, directory: Path, output: Path,
              ledger: dict, ledger_path: Path) -> dict:
    plan_hash, engine_hash = ledger["plan_sha256"], ledger["engine_sha256"]
    frozen = ledger.get("frozen_selection")
    prefix_end = plan["periods"]["validation"][1]
    periods = ["development", "validation"] if stage == "development-validation" else ["holdout"]
    end = max(plan["periods"][p][1] for p in periods)
    data = load_data(directory, end, prefix_end)
    if stage == "holdout" and data.prefix_hash != frozen["development_validation_data_sha256"]:
        raise ValueError("Pre-holdout observations changed after selection freeze")
    # Pin inputs before any outcome calculations. Failed attempts may only replay identical inputs.
    input_hash = data.prefix_hash if stage == "development-validation" else data.data_hash
    record = ledger["stages"][stage]
    if record.get("input_sha256", input_hash) != input_hash:
        raise ValueError("Analysis inputs changed after the initial attempt; cannot resume")
    record["input_sha256"] = input_hash
    save_ledger(ledger_path, ledger)
    output.mkdir(parents=True, exist_ok=True)
    f = features(data, plan)
    variants = plan["variants"] if stage == "development-validation" else [
        v for v in plan["variants"] if v["id"] in ("liquid_all", frozen["selected_variant"])]
    summaries, daily = evaluate(data, f, plan, periods, variants)
    result = {"version": VERSION, "stage": stage, "created_at": datetime.now(timezone.utc).isoformat(),
              "runtime": {"python": sys.version.split()[0], "numpy": np.__version__, "pandas": pd.__version__},
              "plan_sha256": plan_hash, "source_data_sha256": data.data_hash,
              "engine_sha256": engine_hash,
              "development_validation_data_sha256": data.prefix_hash,
              "units": "Returns/differences/fractions are decimal ratios, not percentage numbers",
              "small_sample_definition": "paired_days < 60 or observed candidate outcomes < 100; disclosure only, not selection gate",
              "bootstrap_note": "20-session moving blocks preserve missing days; CI unavailable when calendar or observed days <= block length; pointwise, not multiplicity-adjusted CIs",
              "claim_gate": plan["evaluation"]["claim_gate"],
              "cautions": [plan["universe"], plan["scope"], plan["costs"]["note"],
                           "현재 수정주가·수정거래량이며 거래대금은 당시 원주가 기준. 지수 거래대금은 사용하지 않음",
                           "배당을 제외한 가격수익률. 진입/청산 거래량 양수도 실제 체결을 보장하지 않음",
                           "결과 결측이 비무작위이면 관측가능 집합 비교에도 편향이 남음"],
              "audit": data.audit, "summaries": summaries}
    if stage == "development-validation":
        if frozen is None:
            choice = choose_variant(summaries, plan)
            frozen = {**choice, "version": VERSION, "frozen_at": result["created_at"],
                      "plan_sha256": plan_hash, "source_data_sha256": data.data_hash,
                      "engine_sha256": engine_hash,
                      "development_validation_data_sha256": data.prefix_hash,
                      "plan": plan, "selection_inputs": [r for r in summaries if r["market"] == "ALL" and
                       r["horizon"] == 5 and r["slippage_each_side"] == plan["costs"]["primary_slippage_each_side"]]}
            ledger["frozen_selection"], ledger["selection_choice"] = frozen, choice
            # Persist the canonical freeze before sensitivity or any output artifact is written.
            save_ledger(ledger_path, ledger)
        else:
            choice = ledger["selection_choice"]
        write_json(output / "selection-frozen.json", frozen)
        sensitivity = []
        if choice["selected_variant"]:
            chosen = next(v for v in variants if v["id"] == choice["selected_variant"])
            for liquidity in plan["evaluation"]["sensitivity"]["liquidity_krw"]:
                for shortlist in plan["evaluation"]["sensitivity"]["shortlist"]:
                    rows, _ = evaluate(data, f, plan, periods, [chosen], liquidity, shortlist, primary_only=True)
                    sensitivity.extend({**r, "liquidity_krw": liquidity, "shortlist": shortlist} for r in rows)
        write_json(output / "sensitivity-development-validation.json", sensitivity)
        result["selection"] = choice
    else:
        result["selection"] = {"selected_variant": frozen["selected_variant"], "reselection": False}
        result["adoption"] = adoption_decision(summaries, frozen, plan)
    write_json(output / f"{stage}.json", result)
    write_json(output / f"{stage}-daily.json", daily)
    save_tables(output / f"{stage}.md", [r for r in summaries if r["horizon"] == 5])
    return {"stage": stage, "selected_variant": frozen["selected_variant"],
            "summary_rows": len(summaries), "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("development-validation", "holdout"))
    parser.add_argument("--plan", type=Path, default=Path(__file__).with_name("plan.json"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    try:
        result = run(args.stage, args.plan, args.data_dir, args.output_dir or args.data_dir / "analysis")
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, f"Analysis stopped: {error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
