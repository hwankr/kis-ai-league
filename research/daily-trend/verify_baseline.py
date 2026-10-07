"""Independent raw-series audit of the time5 liquidity comparison cohort.

No engine/candidate/analyze helpers or saved trade outcomes are imported.
Reads fixed-universe KIS source series and reconstructs eligibility, ranking,
breakouts, next-open execution and the published daily comparison averages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ("open", "high", "low", "close", "volume", "turnover")
ENDS = {"development": "2025-12-31", "validation": "2026-06-30", "reused_audit": "2026-10-02"}
EXTRA_SIGNALS = ("2025-06-23", "2026-01-19", "2026-01-21")
SLIP, BUY_FEE, SELL_FEE, TAX = .001, .000140527, .000140527, .002


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_source(directory):
    universe = read(directory / "universe-snapshot.json")
    symbols = sorted(row["symbol"] for row in universe["rows"])
    boards = {row["symbol"]: row["board"] for row in universe["rows"]}
    calendars = [pd.DatetimeIndex(sorted(row["date"] for row in read(directory / "indices" / f"{code}.json")["rows"]))
                 for code in ("0001", "1001")]
    if not calendars[0].equals(calendars[1]):
        raise ValueError("Benchmark calendars disagree")
    calendar = calendars[0]
    arrays = {name: np.full((len(calendar), len(symbols)), np.nan) for name in FIELDS}
    hashes = {}
    for col, symbol in enumerate(symbols):
        path = directory / "series" / f"{symbol}.json"
        raw = read(path)
        hashes[symbol] = digest(path)
        if raw["status"] != "complete" or raw["conflicts"]:
            raise ValueError(f"Unresolved source: {symbol}")
        rows = pd.DataFrame(raw["rows"]).set_index("date")
        rows.index = pd.to_datetime(rows.index)
        if not rows.index.is_unique or not rows.index.isin(calendar).all():
            raise ValueError(f"Invalid source dates: {symbol}")
        aligned = rows.reindex(calendar)
        for name in FIELDS:
            arrays[name][:, col] = pd.to_numeric(aligned[name], errors="coerce").to_numpy()
    prices = {name: pd.DataFrame(value, index=calendar, columns=symbols) for name, value in arrays.items()}
    return calendar, symbols, boards, prices, hashes


def reconstruct(prices):
    o, h, lo, c = (prices[name] for name in ("open", "high", "low", "close"))
    valid = np.isfinite(o) & o.gt(0)
    for frame in (h, lo, c):
        valid &= np.isfinite(frame) & frame.gt(0)
    valid &= h.ge(o) & h.ge(c) & lo.le(o) & lo.le(c) & h.ge(lo)
    history = valid.rolling(61, min_periods=61).sum().eq(61)
    positive_volume = (np.isfinite(prices["volume"]) & prices["volume"].gt(0)).rolling(20, min_periods=20).sum().eq(20)
    turnover = prices["turnover"].where(np.isfinite(prices["turnover"]) & prices["turnover"].ge(0))
    average = turnover.rolling(20, min_periods=20).mean()
    eligibility = history & positive_volume & average.ge(10_000_000_000)
    # Explicit lexicographic ranking; independent of the production rank helper.
    shortlist = pd.DataFrame(False, index=c.index, columns=c.columns)
    for pos in range(len(c)):
        eligible_symbols = eligibility.columns[eligibility.iloc[pos]]
        ordered = sorted(eligible_symbols, key=lambda symbol: (-average.iloc[pos][symbol], symbol))
        shortlist.loc[c.index[pos], ordered[:20]] = True
    prior_high = h.where(valid).shift(1).rolling(20, min_periods=20).max()
    previous_close = c.shift(1)
    ranges = np.maximum.reduce([(h-lo).to_numpy(), (h-previous_close).abs().to_numpy(),
                                (lo-previous_close).abs().to_numpy()])
    atr = pd.DataFrame(ranges, index=c.index, columns=c.columns).where(valid).rolling(14, min_periods=14).mean()
    breakout = shortlist & c.gt(prior_high) & np.isfinite(atr) & atr.gt(0)
    return shortlist, breakout, average


def quote_problem(row):
    o, h, lo, c, volume = row[:5]
    if not np.isfinite(row[:5]).all():
        return "missing_quote"
    if min(o, h, lo, c) <= 0 or volume < 0 or h < max(o, c) or lo > min(o, c) or h < lo:
        return "invalid_quote"
    return None


def independent_time5(array, calendar, signal, end):
    entry, due = signal + 1, signal + 6
    if due >= len(calendar) or calendar[due] > pd.Timestamp(end):
        return {"status": "scheduled_boundary", "net": None}
    problem = quote_problem(array[entry])
    if problem:
        return {"status": "unknown", "net": None, "reason": problem,
                "date": calendar[entry].date().isoformat()}
    if array[entry, 4] == 0 or array[entry, 1] == array[entry, 2]:
        return {"status": "cancelled", "net": None,
                "reason": "entry_zero_volume_or_flat", "date": calendar[entry].date().isoformat(),
                "zero_volume": bool(array[entry, 4] == 0),
                "flat_bar": bool(array[entry, 1] == array[entry, 2])}
    entry_price = array[entry, 0] * (1 + SLIP)
    delays, untradeable = [], []
    for pos in range(entry, len(calendar)):
        row = array[pos]
        problem = quote_problem(row)
        if problem:
            return {"status": "unknown", "net": None, "reason": problem,
                    "date": calendar[pos].date().isoformat(), "delays": delays,
                    "untradeable_quotes": untradeable}
        if row[4] == 0 or row[1] == row[2]:
            untradeable.append({"date": calendar[pos].date().isoformat(),
                                "zero_volume": bool(row[4] == 0), "flat_bar": bool(row[1] == row[2])})
        if pos < due:
            continue
        if row[4] == 0 or row[1] == row[2]:
            delays.append(calendar[pos].date().isoformat())
            continue
        exit_date = calendar[pos].date().isoformat()
        if exit_date > end:
            return {"status": "delayed_past_period", "net": None,
                    "date": exit_date, "delays": delays, "untradeable_quotes": untradeable}
        net = row[0] * (1-SLIP) * (1-SELL_FEE-TAX) / (entry_price * (1+BUY_FEE)) - 1
        return {"status": "closed", "net": float(net), "entry_date": calendar[entry].date().isoformat(),
                "exit_date": exit_date, "delays": delays, "untradeable_quotes": untradeable}
    return {"status": "open", "net": None, "delays": delays, "untradeable_quotes": untradeable}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / ".local/research/candidate-screen-2026-10-04")
    parser.add_argument("--results", type=Path, default=ROOT / ".local/research/daily-trend-2026-10-04-v2")
    args = parser.parse_args()
    calendar, symbols, boards, prices, hashes = load_source(args.data)
    shortlist, breakout, average = reconstruct(prices)
    arrays = {symbol: np.column_stack([prices[field][symbol].to_numpy() for field in FIELDS]) for symbol in symbols}
    frames = [pd.read_csv(args.results / f"{stage}-daily.csv") for stage in ("develop", "audit")]
    daily = pd.concat(frames, ignore_index=True)
    reference = daily[(daily.rule == "time5") & (daily.slip == SLIP)]
    all_market = reference[reference.market == "ALL"]
    paired_dates = set(all_market.loc[all_market.net.notna(), "date"])
    dates = sorted(paired_dates | set(EXTRA_SIGNALS))
    comparisons, exceptions, differences = [], [], []
    checks = 0
    for day in dates:
        expected_rows = reference[reference.date == day]
        if expected_rows.empty:
            raise ValueError(f"Missing reference date: {day}")
        period = expected_rows.iloc[0].period
        pos = calendar.get_loc(pd.Timestamp(day))
        chosen = shortlist.columns[shortlist.iloc[pos]].tolist()
        signal_symbols = set(breakout.columns[breakout.iloc[pos]])
        outcomes = {symbol: independent_time5(arrays[symbol], calendar, pos, ENDS[period]) for symbol in chosen}
        for symbol, outcome in outcomes.items():
            if outcome["status"] != "closed" or outcome.get("untradeable_quotes"):
                exceptions.append({"signal_date": day, "period": period, "symbol": symbol,
                                   "breakout": symbol in signal_symbols, **outcome})
        day_detail = {"date": day, "period": period, "shortlist": chosen,
                      "shortlist_average_turnover": {s: float(average.iloc[pos][s]) for s in chosen},
                      "signals": sorted(signal_symbols), "markets": []}
        for _, expected in expected_rows.iterrows():
            subset = [symbol for symbol in chosen if expected.market == "ALL" or boards[symbol] == expected.market]
            valid = [symbol for symbol in subset if outcomes[symbol]["status"] == "closed"]
            selected = [symbol for symbol in subset if symbol in signal_symbols]
            observed = [symbol for symbol in valid if symbol in signal_symbols]
            liquid = float(np.mean([outcomes[s]["net"] for s in valid])) if valid else None
            net = float(np.mean([outcomes[s]["net"] for s in observed])) if observed else None
            computed = {"selected_count": len(selected), "observed_count": len(observed),
                        "liquid_count": len(valid), "liquid_unresolved": len(subset)-len(valid),
                        "liquid": liquid, "net": net, "edge": net-liquid if net is not None and liquid is not None else None}
            delta = {}
            for key, value in computed.items():
                saved = expected[key]
                equal = pd.isna(saved) if value is None else not pd.isna(saved) and abs(value-saved) <= 1e-12
                checks += 1
                if not equal:
                    differences.append({"date": day, "market": expected.market, "metric": key,
                                        "computed": value, "saved": None if pd.isna(saved) else float(saved)})
                if value is not None and not pd.isna(saved):
                    delta[key] = abs(float(value)-float(saved))
            day_detail["markets"].append({"market": expected.market, "computed": computed,
                                           "absolute_differences": delta})
        comparisons.append(day_detail)
    max_liquid = max(m["absolute_differences"].get("liquid", 0) for d in comparisons for m in d["markets"])
    report = {
        "scope": "time5, 편도슬리피지0.1%, 전체 paired 신호일 및 세 진입취소 신호일의 유동성대조군 독립검산",
        "implementation": "원본series→61일OHLC/20일volume/20일turnover/코드동률상위20→다음시가진입→5일종가후첫거래가능시가청산. 기존 연구·엔진 helper 호출 없음.",
        "summary": {"paired_signal_days": len(paired_dates), "total_checked_days": len(dates),
                    "extra_signal_days": sorted(set(EXTRA_SIGNALS)-paired_dates),
                    "stock_date_paths": sum(len(r["shortlist"]) for r in comparisons),
                    "metric_checks": checks, "differences": len(differences),
                    "maximum_absolute_liquid_difference": max_liquid,
                    "exception_paths": len(exceptions)},
        "source_hashes": hashes,
        "verifier_sha256": digest(__file__),
        "reference_hashes": {f"{stage}-daily.csv": digest(args.results / f"{stage}-daily.csv") for stage in ("develop", "audit")},
        "limitations": "같은 KIS 수정가격 원본의 독립 계산 검산. 당시 구성·제한상태·원주가·체결 가능성의 독립 검증 아님.",
        "differences": differences, "exceptions": exceptions, "dates": comparisons,
    }
    destination = args.results / "baseline-verification.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(json.dumps({"cancelled": sum(r["status"] == "cancelled" for r in exceptions),
                      "unknown": sum(r["status"] == "unknown" for r in exceptions),
                      "delayed_exit": sum(bool(r.get("delays")) for r in exceptions),
                      "holding_untradeable_quote": sum(bool(r.get("untradeable_quotes")) for r in exceptions)}, ensure_ascii=False))
    print(str(destination))
    if differences:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
