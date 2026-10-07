"""Offline execution audit against backtesting.py 0.6.6 (AGPL-3.0).

This optional research verifier is not imported by the product. Its isolated
dependency directory is .local/research/daily-trend-oss-env. It compares a single
price-unit trade, not account returns, and never optimizes or reads study results.

Official semantics and package metadata checked 2026-10-04:
https://kernc.github.io/backtesting.py/doc/backtesting/backtesting.html
https://pypi.org/project/backtesting/0.6.6/
https://github.com/kernc/backtesting.py/blob/master/LICENSE.md
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.metadata
import io
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ISOLATED = ROOT / ".local/research/daily-trend-oss-env"
sys.path.insert(0, str(ISOLATED))

from backtesting import Backtest, Strategy
from engine import simulate_trade

ZERO_COSTS = {"buy_fee": 0, "sell_fee": 0, "sell_tax": 0}
FIELDS = ("open", "high", "low", "close", "volume")
SYMBOLS = ("005930", "000660", "005380", "035420", "068270")
SIGNAL_DATES = ("2025-03-04", "2025-05-07", "2025-08-01", "2025-10-01")
RULES = (
    {"id": "time5", "exit": "time", "max_hold": 5},
    {"id": "time10", "exit": "time", "max_hold": 10},
    {"id": "draft5", "exit": "draft", "max_hold": 5},
    {"id": "failed5", "exit": "failed_breakout", "max_hold": 5},
    {"id": "failed10", "exit": "failed_breakout", "max_hold": 10},
    {"id": "atr5", "exit": "atr_trail", "max_hold": 5},
    {"id": "atr10", "exit": "atr_trail", "max_hold": 10},
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strategy_class(signal_position, configuration):
    """Independent decisions; all fills and trade returns belong to the OSS broker."""
    class FixedOneUnit(Strategy):
        def init(self):
            self.decision_reason = None
            self.decision_date = None
            self.trailing_stop = None

        def next(self):
            position = len(self.data.Close) - 1
            if position == signal_position:
                self.buy(size=1)
                return
            if not self.trades or self.decision_reason is not None:
                return
            trade = self.trades[0]
            close = float(self.data.Close[-1])
            reason = None
            mode = configuration["exit"]
            if mode == "draft":
                if close <= .97 * trade.entry_price:
                    reason = "draft_stop"
                elif close <= float(np.mean(self.data.Close[-10:])):
                    reason = "draft_sma10"
            elif mode == "failed_breakout":
                if close <= configuration["breakout_level"]:
                    reason = "failed_breakout"
            elif mode == "atr_trail":
                distance = 2 * configuration["atr"]
                if self.trailing_stop is None:
                    self.trailing_stop = trade.entry_price - distance
                if close <= self.trailing_stop:
                    reason = "atr_trail"
                else:
                    self.trailing_stop = max(self.trailing_stop, close - distance)
            if reason is None and position - trade.entry_bar + 1 >= configuration["max_hold"]:
                reason = "time"
            if reason is not None:
                self.decision_reason = reason
                self.decision_date = self.data.index[-1].date().isoformat()
                trade.close()
    return FixedOneUnit


def oss_trade(frame, signal_position, configuration):
    prices = frame.loc[:, FIELDS].rename(columns=str.title)
    # Nonbinding starting cash permits exactly one whole price-unit position.
    cash = float(prices.High.max() * 10 + 1000)
    runner = Backtest(prices, strategy_class(signal_position, configuration), cash=cash,
                      commission=0, spread=0, margin=1, trade_on_close=False,
                      exclusive_orders=True, finalize_trades=False)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = runner.run()
    trades = result["_trades"]
    if len(trades) != 1:
        raise AssertionError(f"Expected one closed OSS trade; got {len(trades)}")
    row = trades.iloc[0]
    strategy = result["_strategy"]
    return {"entry_date": row.EntryTime.date().isoformat(),
            "exit_date": row.ExitTime.date().isoformat(),
            "entry_raw": float(row.EntryPrice), "exit_raw": float(row.ExitPrice),
            "net_return": float(row.ReturnPct), "size": int(row.Size),
            "exit_reason": strategy.decision_reason,
            "exit_signal_date": strategy.decision_date}


def parameters(frame, signal_position):
    """Separate, direct indicator arithmetic from the raw prefix only."""
    rows = frame.iloc[:signal_position + 1]
    level = float(max(rows.high.iloc[-21:-1]))
    ranges = []
    for i in range(len(rows) - 14, len(rows)):
        current, previous = rows.iloc[i], rows.iloc[i - 1]
        ranges.append(max(current.high - current.low,
                          abs(current.high - previous.close),
                          abs(current.low - previous.close)))
    return {"breakout_level": level, "atr": float(sum(ranges) / 14)}


def common_conditions(frame):
    values = frame.loc[:, FIELDS].to_numpy(dtype=float)
    return bool(np.isfinite(values).all() and (values > 0).all()
                and frame.high.gt(frame.low).all()
                and frame.high.ge(frame[["open", "close"]].max(axis=1)).all()
                and frame.low.le(frame[["open", "close"]].min(axis=1)).all())


def synthetic_cases():
    """Ten fixed price paths, specified before looking at historical outcomes."""
    for case in range(10):
        n, signal = 70, 40
        frame = pd.DataFrame({"open": 90., "high": 92., "low": 88., "close": 90.,
                              "volume": 1_000_000.},
                             index=pd.bdate_range("2024-01-02", periods=n))
        frame.iloc[signal:, :4] = [100, 102, 98, 100]
        entry = signal + 1
        def setbar(offset, **values):
            for key, value in values.items():
                frame.loc[frame.index[entry + offset], key] = value
        if case == 1:
            setbar(0, close=96, low=95)
            setbar(1, open=89, low=88)
        elif case == 2:
            frame.iloc[:signal, :4] = [99, 101, 97, 99]
            setbar(0, close=98, low=97)
        elif case == 3:
            setbar(0, low=70)
        elif case == 4:
            setbar(0, close=111, high=112)
            setbar(1, open=111, close=100, high=112, low=99)
        elif case == 5:
            setbar(0, high=180, low=30)
            setbar(1, close=99, low=98)
        elif case == 6:
            setbar(0, close=91, low=90)
        elif case == 7:
            for offset in range(11):
                price = 100 + 2 * offset
                setbar(offset, open=price, close=price + 1, high=price + 2, low=price - 1)
        elif case == 8:
            setbar(0, open=110, high=112, low=98)
        elif case == 9:
            setbar(4, close=120, high=121)
            setbar(5, open=115, high=118, low=98)
        yield f"synthetic_{case:02d}", frame, signal


def historical_cases(directory):
    calendar_raw = json.loads((directory / "indices/0001.json").read_text(encoding="utf-8-sig"))
    calendar = pd.DatetimeIndex(sorted(row["date"] for row in calendar_raw["rows"]
                                      if row["date"] <= "2025-12-31"))
    for symbol in SYMBOLS:
        path = directory / "series" / f"{symbol}.json"
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = [row for row in raw["rows"] if row["date"] <= "2025-12-31"]
        frame = pd.DataFrame(rows).set_index("date")
        frame.index = pd.to_datetime(frame.index)
        frame = frame.loc[:, FIELDS].astype(float).reindex(calendar)
        for day in SIGNAL_DATES:
            signal = calendar.get_loc(pd.Timestamp(day))
            start, stop = signal - 30, signal + 13
            yield f"{symbol}/{day}", frame.iloc[start:stop].copy(), 30, sha256(path)


def compare_case(label, frame, signal, source, source_hash=None):
    if not common_conditions(frame):
        return [{"case": label, "source": source, "status": "unsupported_common_conditions",
                 "note": "표본 교체 없음: 결측·거래량0·flat bar의 체결정책은 공통 범위 밖"}]
    extra = parameters(frame, signal)
    records = []
    for base_rule in RULES:
        configuration = dict(base_rule, **extra)
        ours = simulate_trade(frame, signal, configuration, 0, ZERO_COSTS)
        independent = oss_trade(frame, signal, configuration)
        same = ours["status"] == "closed" and independent["size"] == 1
        for key in ("entry_date", "exit_date", "exit_reason", "exit_signal_date"):
            same &= ours[key] == independent[key]
        deltas = {key: abs(ours[key] - independent[key])
                  for key in ("entry_raw", "exit_raw", "net_return")}
        same &= all(value <= 1e-12 for value in deltas.values())
        # An independent cash-flow identity checks the OSS ReturnPct convention.
        manual = independent["exit_raw"] / independent["entry_raw"] - 1
        same &= abs(manual - independent["net_return"]) <= 1e-12
        records.append({"case": label, "source": source, "source_sha256": source_hash,
                        "rule": configuration, "status": "match" if same else "mismatch",
                        "our_status": ours["status"], "independent": independent,
                        "absolute_differences": deltas})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path,
                        default=ROOT / ".local/research/candidate-screen-2026-10-04")
    parser.add_argument("--output", type=Path,
                        default=ROOT / ".local/research/daily-trend-independent-2026-10-04.json")
    args = parser.parse_args()
    version = importlib.metadata.version("backtesting")
    if version != "0.6.6":
        raise ValueError(f"Pinned OSS version changed: {version}")
    records = []
    for label, frame, signal in synthetic_cases():
        records.extend(compare_case(label, frame, signal, "synthetic"))
    for label, frame, signal, source_hash in historical_cases(args.data):
        records.extend(compare_case(label, frame, signal, "development_fixed_time", source_hash))
    matches = sum(row["status"] == "match" for row in records)
    mismatches = sum(row["status"] == "mismatch" for row in records)
    unsupported = sum(row["status"] == "unsupported_common_conditions" for row in records)
    package_file = Path(sys.modules["backtesting"].__file__)
    broker_file = package_file.parent / "backtesting.py"
    versions = {name: importlib.metadata.version(name) for name in
                ("backtesting", "bokeh", "numpy", "pandas", "Jinja2", "MarkupSafe",
                 "narwhals", "PyYAML", "tornado", "xyzservices", "tqdm", "colorama")}
    report = {
        "scope": "고정 시점의 수정가격 1단위·비용0·슬립0 실행 대조; 실제 계좌성과·신호선택 검증 아님",
        "package": {"name": "backtesting", "version": version, "license": "AGPL-3.0",
                    "isolated_location": str(ISOLATED), "module_sha256": sha256(package_file),
                    "broker_sha256": sha256(broker_file), "runtime_versions": versions,
                    "metadata": "https://pypi.org/project/backtesting/0.6.6/",
                    "semantics": "https://kernc.github.io/backtesting.py/doc/backtesting/backtesting.html"},
        "execution": {"trade_on_close": False, "finalize_trades": False, "size": 1,
                      "commission": 0, "spread": 0, "slippage": 0},
        "sample_policy": {"symbols": SYMBOLS, "dates": SIGNAL_DATES, "synthetic_paths": 10,
                          "warmup_sessions": 30, "history_period": "development",
                          "selection": "성과 계산 전 지정한 5종목×4일; 실제 돌파 발생 여부로 선택하지 않음"},
        "engine_sha256": sha256(HERE / "engine.py"),
        "verifier_sha256": sha256(__file__),
        "summary": {"matched": matches, "mismatched": mismatches, "unsupported_cases": unsupported,
                    "historical_comparisons": sum(r["source"] == "development_fixed_time" for r in records),
                    "synthetic_comparisons": sum(r["source"] == "synthetic" for r in records),
                    "maximum_absolute_return_difference": max(
                        (r["absolute_differences"]["net_return"] for r in records
                         if "absolute_differences" in r), default=None)},
        "limitations": ["거래량0·flat bar·결측의 정책은 공통 비교에서 제외하며 표본을 대체하지 않음",
                        "비용·슬리피지·미체결·실제 거래가능성은 이 OSS 대조로 검증하지 않음",
                        "시장가 체결과 독립 작성 청산식을 비교하며 계좌 수익률은 비교하지 않음",
                        "AGPL 의존성은 별도 오프라인 연구 검산에만 사용; 제품 의존성에 추가하지 않음"],
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(str(args.output))
    if mismatches or unsupported or matches != 210:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
