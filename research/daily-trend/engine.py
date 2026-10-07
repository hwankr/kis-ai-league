"""Pure daily-bar execution model; no account sizing or strategy selection.

Signals use completed closes and execute at the next available opening quote.
Costs and slippage are decimal rates. Missing data never imply a trading halt.
"""
from __future__ import annotations

from numbers import Integral
from typing import Any

import numpy as np
import pandas as pd


OHLC = ("open", "high", "low", "close")
EXITS = {"time", "draft", "failed_breakout", "atr_trail"}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _date(frame: pd.DataFrame, position: int) -> str:
    return pd.Timestamp(frame.index[position]).date().isoformat()


def _bar(row: pd.Series) -> tuple[dict[str, float] | None, str | None]:
    values = {name: _number(row[name]) for name in (*OHLC, "volume")}
    if any(values[name] is None for name in OHLC):
        return None, "missing_ohlc"
    if any(values[name] <= 0 for name in OHLC):
        return None, "invalid_ohlc"
    if (values["high"] < max(values["open"], values["close"])
            or values["low"] > min(values["open"], values["close"])
            or values["high"] < values["low"]):
        return None, "invalid_ohlc"
    if values["volume"] is None:
        return None, "missing_volume"
    if values["volume"] < 0:
        return None, "invalid_volume"
    return values, None


def simulate_trade(frame: pd.DataFrame, signal_pos: int, rule: dict,
                   slippage: float, costs: dict) -> dict:
    """Simulate one long trade on a complete, aligned market-session index.

    ``rule`` takes id, max_hold, exit and, when relevant, breakout_level or atr.
    Entry is signal_pos + 1; holding-session one is the entry session. An exit
    close on holding-session max_hold sells at the following session's open.
    ``draft`` exits at close <= 97% of entry or close <= the current SMA10.
    ATR is fixed at the signal value; test the previous stop before ratcheting
    it with the current close. Never use the current high to ratchet the stop.

    Zero-volume/flat bars cancel entries and delay pending sells. Their known
    OHLC remain quoted-price excursions, not claims about executable fills.
    Missing/invalid bars produce unknown, with returns unset. A truncated
    future produces pending_entry or open. Caller owns period boundaries.

    costs: buy_fee, sell_fee, sell_tax. Cash outlay includes the buy fee;
    sale proceeds subtract both sell charges. No account capital is assumed.
    MAE/MFE and close_mae include zero and use slipped entry as denominator.
    Exit-session excursions include only the raw open, never its later bar.
    """
    required = {*OHLC, "volume"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Missing columns: {sorted(required - set(frame.columns))}")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("frame must have a DatetimeIndex of market sessions")
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing or frame.index.hasnans:
        raise ValueError("Market-session dates must be unique, ordered and nonmissing")
    if not isinstance(signal_pos, Integral) or isinstance(signal_pos, bool):
        raise ValueError("signal_pos must be an integer position")
    signal_pos = int(signal_pos)
    if not 0 <= signal_pos < len(frame):
        raise ValueError("signal_pos is outside frame")
    exit_type = rule.get("exit")
    if exit_type not in EXITS:
        raise ValueError(f"Unsupported exit rule: {exit_type}")
    max_hold = rule.get("max_hold")
    if not isinstance(max_hold, Integral) or isinstance(max_hold, bool) or max_hold < 1:
        raise ValueError("max_hold must be a positive number of market sessions")
    slip = _number(slippage)
    if slip is None or not 0 <= slip < 1:
        raise ValueError("slippage must be a finite rate in [0, 1)")
    rates = {}
    for name in ("buy_fee", "sell_fee", "sell_tax"):
        rates[name] = _number(costs.get(name, 0.0))
        if rates[name] is None or not 0 <= rates[name] < 1:
            raise ValueError(f"{name} must be a finite rate in [0, 1)")
    if rates["sell_fee"] + rates["sell_tax"] >= 1:
        raise ValueError("Combined sale charges must be below 100%")
    breakout = _number(rule.get("breakout_level"))
    atr = _number(rule.get("atr"))
    if exit_type == "failed_breakout" and (breakout is None or breakout <= 0):
        raise ValueError("failed_breakout requires positive signal breakout_level")
    if exit_type == "atr_trail" and (atr is None or atr <= 0):
        raise ValueError("atr_trail requires positive signal atr")

    result = {
        "rule_id": rule.get("id", exit_type), "status": "pending_entry",
        "signal_date": _date(frame, signal_pos), "signal_pos": signal_pos,
        "entry_date": None, "entry_pos": None, "entry_raw": None, "entry_price": None,
        "exit_date": None, "exit_pos": None, "exit_raw": None, "exit_price": None,
        "exit_signal_date": None, "exit_signal_pos": None, "exit_reason": None,
        "holding_days": 0, "exit_delay_days": 0, "last_observed_date": None,
        "gross_return": None, "net_return": None, "raw_return": None,
        "mae": None, "mfe": None, "close_mae": None,
        "opening_gap": None, "exit_opening_gap": None, "gap_min": None, "gap_max": None,
        "stop_price": None, "unknown_reason": None, "failure_date": None,
        "slippage": slip, "costs": rates, "flags": [], "events": [],
    }

    def flag(value: str) -> None:
        if value not in result["flags"]:
            result["flags"].append(value)

    def unknown(reason: str, position: int) -> dict:
        result.update(status="unknown", unknown_reason=reason, failure_date=_date(frame, position))
        flag(reason)
        if result["entry_date"] is not None:
            flag("incomplete_price_path")
        return result

    signal, error = _bar(frame.iloc[signal_pos])
    if error:
        return unknown(error, signal_pos)
    entry_pos = signal_pos + 1
    if entry_pos == len(frame):
        flag("entry_beyond_data")
        return result
    entry_bar, error = _bar(frame.iloc[entry_pos])
    if error:
        return unknown(error, entry_pos)
    entry_blockers = []
    if entry_bar["volume"] == 0:
        entry_blockers.append("entry_zero_volume")
    if entry_bar["high"] == entry_bar["low"]:
        entry_blockers.append("entry_flat_bar_fill_unconfirmed")
    if entry_blockers:
        result.update(status="cancelled", failure_date=_date(frame, entry_pos))
        for blocker in entry_blockers:
            flag(blocker)
        return result

    entry = entry_bar["open"] * (1 + slip)
    result.update(status="open", entry_date=_date(frame, entry_pos), entry_pos=entry_pos,
                  entry_raw=entry_bar["open"], entry_price=entry,
                  opening_gap=entry_bar["open"] / signal["close"] - 1,
                  mae=0.0, mfe=0.0, close_mae=0.0)
    previous_close = signal["close"]
    highest_close = None
    stop = entry - 2 * atr if exit_type == "atr_trail" else None
    result["stop_price"] = stop
    pending_reason = None

    def prices(low: float, high: float, close: float | None = None) -> None:
        result["mae"] = min(result["mae"], low / entry - 1)
        result["mfe"] = max(result["mfe"], high / entry - 1)
        if close is not None:
            result["close_mae"] = min(result["close_mae"], close / entry - 1)

    for position in range(entry_pos, len(frame)):
        bar, error = _bar(frame.iloc[position])
        if error:
            return unknown(error, position)
        date = _date(frame, position)
        result["last_observed_date"] = date
        gap = bar["open"] / previous_close - 1
        result["gap_min"] = gap if result["gap_min"] is None else min(result["gap_min"], gap)
        result["gap_max"] = gap if result["gap_max"] is None else max(result["gap_max"], gap)
        zero_volume = bar["volume"] == 0
        flat = bar["high"] == bar["low"]

        if pending_reason is not None:
            if not zero_volume and not flat:
                sale = bar["open"] * (1 - slip)
                prices(bar["open"], bar["open"])
                result.update(status="closed", exit_date=date, exit_pos=position,
                              exit_raw=bar["open"], exit_price=sale,
                              holding_days=position - entry_pos,
                              exit_opening_gap=gap,
                              raw_return=bar["open"] / entry_bar["open"] - 1,
                              gross_return=sale / entry - 1,
                              net_return=(sale * (1 - rates["sell_fee"] - rates["sell_tax"])
                                          / (entry * (1 + rates["buy_fee"])) - 1))
                result["events"].append({"date": date, "event": "exit_fill", "reason": pending_reason})
                return result
            result["exit_delay_days"] += 1
            if zero_volume:
                flag("exit_zero_volume_delay")
            if flat:
                flag("exit_flat_bar_fill_unconfirmed")
            result["events"].append({"date": date, "event": "exit_delayed",
                                     "zero_volume": zero_volume, "flat_bar": flat})

        # Every observed market session counts toward time exposure, including halts.
        result["holding_days"] = position - entry_pos + 1
        prices(bar["low"], bar["high"], bar["close"])
        previous_close = bar["close"]
        if zero_volume:
            flag("holding_zero_volume")
        if flat:
            flag("holding_flat_bar")
        if pending_reason is not None:
            continue

        reason = None
        # A zero-volume row has no fresh traded close; only the time clock runs.
        if not zero_volume:
            if exit_type == "draft":
                if bar["close"] <= entry * 0.97:
                    reason = "draft_stop"
                else:
                    history = frame.iloc[max(0, position - 9):position + 1]
                    if len(history) < 10:
                        return unknown("insufficient_sma10_history", position)
                    historical_bars = [_bar(row) for _, row in history.iterrows()]
                    if any(error is not None for _, error in historical_bars):
                        return unknown("invalid_sma10_history", position)
                    sma10 = sum(row["close"] for row, _ in historical_bars) / 10
                    if bar["close"] <= sma10:
                        reason = "draft_sma10"
            elif exit_type == "failed_breakout" and bar["close"] <= breakout:
                reason = "failed_breakout"
            elif exit_type == "atr_trail":
                if bar["close"] <= stop:
                    reason = "atr_trail"
                else:
                    highest_close = (bar["close"] if highest_close is None
                                     else max(highest_close, bar["close"]))
                    stop = max(stop, highest_close - 2 * atr)
                    result["stop_price"] = stop
        if reason is None and result["holding_days"] >= max_hold:
            reason = "time"
        if reason is not None:
            pending_reason = reason
            result.update(exit_reason=reason, exit_signal_date=date, exit_signal_pos=position)
            result["events"].append({"date": date, "event": "exit_signal", "reason": reason,
                                     "stop_price": stop})

    flag("exit_beyond_data" if pending_reason is not None else "holding_at_data_end")
    return result
