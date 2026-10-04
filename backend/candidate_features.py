"""완료된 시장 거래일에 맞춘 순수 후보 지표. 선별 임계값·주문 판단은 포함하지 않는다."""

from datetime import date
from decimal import Decimal, localcontext

from backend.kis import KisError


class InsufficientFeatureHistory(KisError):
    pass


def _number(value, *, positive=False):
    if (not isinstance(value, Decimal) or not value.is_finite()
            or (value <= 0 if positive else value < 0)):
        raise KisError("후보 지표의 가격·거래량·거래대금이 올바르지 않습니다.")
    return value


def _stock_row(row):
    if not isinstance(row, dict):
        raise KisError("후보 지표의 일봉 형식이 올바르지 않습니다.")
    values = {key: _number(row.get(key), positive=True) for key in ("open", "high", "low", "close")}
    if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
        raise KisError("후보 지표의 시가·고가·저가·종가 범위가 올바르지 않습니다.")
    return {**values, "volume": _number(row.get("volume")), "turnover": _number(row.get("turnover"))}


def candidate_features(stock, benchmark, days):
    """61거래일 core와 선택적 148/253거래일 지표. 결측을 압축하거나 보간하지 않는다.

    ATR14는 기준일 포함 14개 TR의 단순 평균이다. 위험조정 RS60의 분모는
    60개 일간 수익률의 표본 표준편차(ddof=1)에 sqrt(60)을 곱한 값이다.
    """
    if (not isinstance(stock, dict) or not isinstance(benchmark, dict)
            or not isinstance(days, (list, tuple))
            or any(type(day) is not date for day in days)):
        raise KisError("후보 지표의 거래일 구성이 올바르지 않습니다.")
    days = list(days)
    if days != sorted(set(days)):
        raise KisError("후보 지표의 거래일 구성이 올바르지 않습니다.")
    if len(days) < 61 or any(day not in stock or day not in benchmark for day in days[-61:]):
        raise InsufficientFeatureHistory("후보 지표에 필요한 연속 61거래일의 일봉·지수가 부족합니다.")
    parsed_stock, parsed_index = {}, {}

    def validate(window):
        for day in window:
            if day not in parsed_stock:
                parsed_stock[day] = _stock_row(stock[day])
                parsed_index[day] = _number(benchmark[day], positive=True)

    validate(days[-61:])
    with localcontext() as context:
        context.prec = 50
        rows = [parsed_stock[day] for day in days[-61:]]
        closes = [row["close"] for row in rows]
        sma20 = sum(closes[-20:]) / 20
        sma60 = sum(closes[-60:]) / 60
        previous_sma20 = sum(closes[-25:-5]) / 20
        change60 = closes[-1] / closes[0] - 1
        index60 = parsed_index[days[-1]] / parsed_index[days[-61]] - 1
        excess60 = change60 - index60
        turnovers = sorted(row["turnover"] for row in rows[-20:])
        median_turnover = (turnovers[9] + turnovers[10]) / 2
        true_ranges = [max(rows[i]["high"] - rows[i]["low"],
                           abs(rows[i]["high"] - closes[i - 1]),
                           abs(rows[i]["low"] - closes[i - 1])) for i in range(len(rows) - 14, len(rows))]
        atr14 = sum(true_ranges) / 14
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        mean_return = sum(returns) / 60
        variance = sum((value - mean_return) ** 2 for value in returns) / 59
        scaled_volatility = (variance * 60).sqrt()
        previous_turnover = sum(row["turnover"] for row in rows[-21:-1]) / 20
        excess20 = closes[-1] / closes[-21] - parsed_index[days[-1]] / parsed_index[days[-21]]
        result = {
            "sma20": format(sma20, ".2f"), "sma60": format(sma60, ".2f"),
            "sma20_change_5d_pct": format((sma20 / previous_sma20 - 1) * 100, ".4f"),
            "return_60d_pct": format(change60 * 100, ".4f"),
            "excess_60d_pp": format(excess60 * 100, "f"),
            "excess_20d_pp": format(excess20 * 100, "f"),
            "avg_turnover_20d": format(sum(row["turnover"] for row in rows[-20:]) / 20, "f"),
            "turnover_ratio": format(rows[-1]["turnover"] / previous_turnover, "f") if previous_turnover else None,
            "median_turnover_20d": format(median_turnover, ".2f"),
            "atr14": format(atr14, ".4f"),
            "extension_atr": format((closes[-1] - sma20) / atr14, "f") if atr14 else None,
            "risk_adjusted_rs60": format(excess60 / scaled_volatility, "f") if scaled_volatility else None,
            "trend": closes[-1] > sma20 > sma60 and sma20 > previous_sma20,
            "market_up": parsed_index[days[-1]] > sum(parsed_index[day] for day in days[-60:]) / 60,
            "zero_volume_days_20d": sum(row["volume"] == 0 for row in rows[-20:]),
        }

        def optional_excess(length, recent_offset, earlier_offset):
            if len(days) < length:
                return None
            window = days[-length:]
            if any(day not in stock or day not in benchmark for day in window):
                return None
            validate(window)
            recent, earlier = days[-recent_offset - 1], days[-earlier_offset - 1]
            change = parsed_stock[recent]["close"] / parsed_stock[earlier]["close"] - 1
            market = parsed_index[recent] / parsed_index[earlier] - 1
            return format((change - market) * 100, "f")

        result["excess_6m_skip1m_pp"] = optional_excess(148, 21, 147)
        result["excess_12to7m_pp"] = optional_excess(253, 126, 252)
        return result
