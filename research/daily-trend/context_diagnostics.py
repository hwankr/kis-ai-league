"""완결 time5 거래와 지연한 외부 자료의 사후 연관 진단. 필터·인과·선정 근거가 아니다."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
from statistics import median

import external_context
from external_context import SERIES, align_context, load_snapshot


PERIODS = ("development", "validation", "reused_audit")
GROUPS = {
    "VIXCLS": {"field": "value", "threshold": 20.0, "unit": "index",
               "lower": "VIX <= 20", "upper": "VIX > 20"},
    "DGS10": {"field": "change_5_observations", "threshold": 0.0, "unit": "percentage_points",
              "lower": "5관측 금리변화 <= 0", "upper": "5관측 금리변화 > 0"},
    "DEXKOUS": {"field": "change_5_observations", "threshold": 0.0, "unit": "KRW_per_USD",
                "lower": "5관측 환율변화 <= 0", "upper": "5관측 환율변화 > 0"},
}
FAILURES = ("unavailable", "no_prior_observation", "stale", "insufficient_change_history")


def finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def mean(values):
    values = [value for value in values if value is not None]
    return math.fsum(values) / len(values) if values else None


def age_summary(values):
    values = [value for value in values if value is not None]
    return {"count": len(values), "min": min(values) if values else None,
            "median": median(values) if values else None,
            "max": max(values) if values else None}


def capture(path):
    path = Path(path).resolve()
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"읽는 도중 변경된 입력: {path}")
    return raw, {"path": str(path), "bytes": len(raw),
                 "sha256": hashlib.sha256(raw).hexdigest()}


def aggregate_signals(records):
    """먼저 같은 D의 거래 수익을 평균한다. 거래수로 날짜를 재가중하지 않는다."""
    by_day = defaultdict(list)
    seen = set()
    counts = Counter()
    for row in records:
        counts["read_rows"] += 1
        if (row.get("rule_id") != "time5" or finite(row.get("slippage")) != .001
                or row.get("lookback", 20) != 20 or row.get("paired_observed") is not True):
            continue
        value = finite(row.get("net_return"))
        if value is None:
            raise ValueError("paired_observed 거래의 순수익이 유한하지 않습니다.")
        period, signal, symbol = row["period"], row["signal_date"], row["symbol"]
        if period not in PERIODS:
            raise ValueError(f"알 수 없는 연구 기간: {period}")
        date.fromisoformat(signal)
        key = (period, signal, symbol)
        if key in seen:
            raise ValueError(f"중복 time5 거래: {key}")
        seen.add(key)
        by_day[(period, signal)].append(value)
        counts["included_trades"] += 1
    days = [{"period": period, "signal_date": signal, "trades": len(values),
             "net_return": mean(values), "market_difference": None,
             "market_difference_status": "unavailable"}
            for (period, signal), values in sorted(by_day.items())]
    counts["signal_days"] = len(days)
    return days, dict(counts)


def attach_market_difference(days, rows):
    """동일 날짜 ALL 관찰목록 대조군의 edge만 사용. baseline_net은 사용하지 않는다."""
    lookup = {}
    for row in rows:
        if row.get("rule") != "time5" or finite(row.get("slip")) != .001 or row.get("market") != "ALL":
            continue
        key = (row["period"], row["date"])
        if key in lookup:
            raise ValueError(f"중복 일별 대조군: {key}")
        lookup[key] = row
    for day in days:
        matched = lookup.get((day["period"], day["signal_date"]))
        if matched is None:
            continue
        net, edge = finite(matched.get("net")), finite(matched.get("edge"))
        if net is None or edge is None:
            day["market_difference_status"] = "missing_daily_value"
        elif not math.isclose(net, day["net_return"], rel_tol=1e-7, abs_tol=1e-8):
            day["market_difference_status"] = "daily_cohort_mismatch"
        elif finite(matched.get("observed_count")) != day["trades"]:
            day["market_difference_status"] = "daily_cohort_mismatch"
        else:
            day["market_difference"] = edge
            day["market_difference_status"] = "available"
    return days


def group_summary(name, rows, total_days):
    edges = [row["market_difference"] for row in rows if row["market_difference"] is not None]
    return {"group": name, "signal_days": len(rows), "trades": sum(row["trades"] for row in rows),
            "share_of_signal_days": len(rows) / total_days if total_days else None,
            "mean_net_return": mean(row["net_return"] for row in rows),
            "mean_market_difference": mean(edges), "market_difference_days": len(edges),
            "market_difference_missing_rate": 1 - len(edges) / len(rows) if rows else None,
            "observation_age_days": age_summary(row.get("age_days") for row in rows)}


def summarize_context(days, observations):
    context = {(row["signal_date"], row["series_id"]): row
               for row in align_context(sorted({day["signal_date"] for day in days}), observations)}
    output = []
    for period in PERIODS:
        period_days = [day for day in days if day["period"] == period]
        for series_id, definition in GROUPS.items():
            groups = {definition["lower"]: [], definition["upper"]: [], **{name: [] for name in FAILURES}}
            statuses, ages, available_ages = Counter(), [], []
            for day in period_days:
                external = context[(day["signal_date"], series_id)]
                value = finite(external.get(definition["field"]))
                status = external["status"]
                if status == "available_revised_diagnostic" and value is None:
                    status = "insufficient_change_history"
                if status == "available_revised_diagnostic":
                    group = definition["upper"] if value > definition["threshold"] else definition["lower"]
                    available_ages.append(external["age_days"])
                else:
                    group = status
                statuses[status] += 1
                ages.append(external["age_days"])
                groups.setdefault(group, []).append({**day, "age_days": external["age_days"]})
            missing = sum(len(groups[name]) for name in FAILURES)
            output.append({"period": period, "series_id": series_id, "signal_days": len(period_days),
                           "missing_days": missing, "missing_rate": missing / len(period_days) if period_days else None,
                           "status_counts": {key: statuses.get(key, 0) for key in ("available_revised_diagnostic", *FAILURES)},
                           "observation_age_days": age_summary(ages),
                           "available_observation_age_days": age_summary(available_ages),
                           "groups": [group_summary(name, rows, len(period_days)) for name, rows in groups.items()]})
    return output


def build_report(study, snapshot):
    study = Path(study).resolve()
    expected = [study / f"{stage}-{suffix}" for stage in ("develop", "audit")
                for suffix in ("trades.jsonl.gz", "signals.json")]
    missing = [str(path) for path in expected if not path.exists()]
    if missing:
        raise FileNotFoundError("아직 완료되지 않은 연구 입력: " + ", ".join(missing))
    files, records, market_rows = [], [], []
    for stage in ("develop", "audit"):
        for suffix in ("trades.jsonl.gz", "signals.json"):
            raw, info = capture(study / f"{stage}-{suffix}")
            files.append(info)
            if suffix == "trades.jsonl.gz":
                records.extend(json.loads(line) for line in gzip.decompress(raw).decode("utf-8").splitlines() if line.strip())
            else:
                json.loads(raw)  # 마지막 산출물이 완전한 JSON인지 확인.
        daily_path = study / f"{stage}-daily.csv"
        if daily_path.exists():
            raw, info = capture(daily_path)
            files.append(info)
            market_rows.extend(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    days, counts = aggregate_signals(records)
    attach_market_difference(days, market_rows)
    observations, manifest = load_snapshot(snapshot)
    _, snapshot_info = capture(snapshot)
    _, code_info = capture(__file__)
    _, adapter_info = capture(external_context.__file__)
    return {
        "version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "complete_exploratory_diagnostics", "point_in_time": False,
        "diagnostics_only": True, "selection_use": False, "causal_inference": False,
        "study": str(study), "inputs": files, "snapshot": snapshot_info, "code": code_info,
        "adapter_code": adapter_info, "alignment_policy": SERIES,
        "external_provenance": manifest,
        "sample": {"rule": "time5", "slippage_each_side": .001, "lookback": 20,
                   "paired_observed": True, **counts},
        "aggregation": "동일 한국 신호일의 종목 순수익 평균 후 날짜 동일가중. 중첩 사건 포함, 계좌 수익률 아님.",
        "market_difference": "동일 날짜 관찰목록 대조군 대비차이. daily.csv의 ALL·time5 수익/개수가 사건표와 일치할 때만 사용.",
        "market_difference_status_counts": dict(Counter(day["market_difference_status"] for day in days)),
        "thresholds": GROUPS,
        "threshold_note": "VIX 20·변화 0은 기술용 임의 구분이다. 최적화하거나 채택한 매매 기준이 아니다.",
        "change_note": "change_5_observations는 사용한 관측과 5개 이전 유효 관측의 차이. 5한국거래일 변화나 수익률이 아니다.",
        "limitations": manifest.get("limitations", []) + [
            "전 기간은 재사용 자료. 그룹 차이는 점추정 탐색 연관이며 유의성·인과·거래 필터의 우위를 뜻하지 않는다.",
            "중첩 거래·같은 시장 환경의 의존성을 보존한 날짜 평균이며 독립 표본 수로 해석하지 않는다.",
            "실패 상태 그룹도 수익을 표시한다. 외부 자료가 없다는 이유로 표본을 삭제하거나 0으로 대체하지 않는다.",
        ],
        "summaries": summarize_context(days, observations),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = build_report(args.study, args.snapshot)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "sample": report["sample"],
                      "source_status": {key: value["status"] for key, value in report["external_provenance"]["series"].items()}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
