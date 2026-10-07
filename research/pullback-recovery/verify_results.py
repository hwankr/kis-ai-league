"""Independent raw-price, timing, cost and matched-comparison check (stdlib only)."""
import argparse
from collections import defaultdict
import csv
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(output, source):
    plan = read(Path(__file__).with_name("plan.json"))
    result = read(output / "results.json")
    for name, expected in result["fingerprint"]["source_files"].items():
        assert digest(source / name) == expected, name
    for name, expected in result["fingerprint"]["code_files"].items():
        assert digest(ROOT / name) == expected, name
    calendar = [r["date"] for r in read(source / "indices/0001.json")["rows"]]
    calendar = sorted(calendar)
    assert calendar == sorted(r["date"] for r in read(source / "indices/1001.json")["rows"])
    positions = {day: index for index, day in enumerate(calendar)}
    raw, signal_groups, control_groups = {}, defaultdict(list), defaultdict(list)
    checked = nulls = 0
    maximum_error = 0.0
    fee = Decimal(str(plan["costs"]["buy_fee"]))
    charges = Decimal(str(plan["costs"]["sell_fee"])) + Decimal(str(plan["costs"]["sell_tax"]))
    for filename, groups in (("signals.jsonl.gz", signal_groups), ("controls.jsonl.gz", control_groups)):
        with gzip.open(output / filename, "rt", encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                symbol = item["symbol"]
                if symbol not in raw:
                    raw[symbol] = {r["date"]: r for r in read(source / f"series/{symbol}.json")["rows"]}
                p = positions[item["signal_date"]]
                window = [raw[symbol][d] for d in calendar[p-62:p+1]]
                closes = [Decimal(r["close"]) for r in window]
                assert len(closes) == 63
                assert closes[-4] > sum(closes[-23:-3]) / 20 > sum(closes[:-3]) / 60
                assert closes[-1] > sum(closes[-60:]) / 60
                if filename.startswith("signals"):
                    assert closes[-2] < closes[-3] < closes[-4]
                    assert closes[-1] > Decimal(window[-2]["high"])
                if item["classification"] != "observed":
                    assert item["net_return"] is None
                    nulls += 1
                    continue
                assert item["entry_date"] == calendar[p + 1]
                exit_pos = positions[item["exit_date"]]
                assert exit_pos >= p + 6
                assert item["exit_date"] <= plan["periods"][item["period"]][1]
                for day in calendar[p + 6:exit_pos]:
                    bar = raw[symbol][day]
                    assert Decimal(bar["volume"]) == 0 or Decimal(bar["high"]) == Decimal(bar["low"])
                buy, sell = (Decimal(raw[symbol][item[key]]["open"]) for key in ("entry_date", "exit_date"))
                assert buy == Decimal(str(item["entry_raw"])) and sell == Decimal(str(item["exit_raw"]))
                slip = Decimal(str(item["slippage"]))
                net = float(sell * (1-slip) * (1-charges) / (buy * (1+slip) * (1+fee)) - 1)
                error = abs(net - item["net_return"])
                assert error < 1e-9, (symbol, item["signal_date"], error)
                maximum_error = max(maximum_error, error)
                key = (item["period"], item["slippage"], item["signal_date"], item["market"])
                groups[key].append((symbol, net))
                checked += 1
    daily_expected = {}
    for key, signals in signal_groups.items():
        controls = control_groups[key]
        assert {s for s, _ in signals}.issubset({s for s, _ in controls})
        mean = sum(v for _, v in signals) / len(signals)
        baseline = sum(v for _, v in controls) / len(controls)
        daily_expected[key] = (len(signals), mean, baseline, mean-baseline)
    matched = 0
    with (output / "daily.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if not row["net"]:
                continue
            key = (row["period"], float(row["slip"]), row["date"], row["market"])
            if row["market"] == "ALL":
                markets = [value for candidate, value in daily_expected.items() if candidate[:3] == key[:3]]
                count = sum(value[0] for value in markets)
                expected = tuple(sum(value[i] * value[0] for value in markets) / count for i in (1, 2, 3))
            else:
                expected = daily_expected[key][1:]
            for field, value in zip(("net", "control", "edge"), expected):
                assert abs(float(row[field])-value) < 1e-9, (key, field)
            matched += 1
    return {"checked_signal_and_control_paths": checked, "nonobserved_null_returns": nulls,
            "matched_day_market_rows": matched, "max_cost_error": maximum_error,
            "results_sha256": digest(output / "results.json"), "verifier_sha256": digest(__file__),
            "scope": "All emitted signal/control raw conditions, observed next-open timing and prices, costs, and market-matched daily aggregation; not independent universe selection or actual execution."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=ROOT / ".local/research/candidate-screen-2026-10-04")
    args = parser.parse_args()
    checked = verify(args.output, args.source)
    (args.output / "independent-verification.json").write_text(json.dumps(checked, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(checked))
