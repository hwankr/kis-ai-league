"""검증해 고정한 관찰 후보 정책. 현재 제한 상태는 별도로 확인한다."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from backend.eligibility import EligibilityService, assess_master
from backend.kis import KST, KisError, ROOT

SCORES = {
    "liquid_top20": ("avg_turnover_20d", "20일 평균 거래대금", "억원", 61),
    "rs20_top20": ("excess_20d_pp", "20일 지수 대비", "%p", 61),
    "rs60_top20": ("excess_60d_pp", "60일 지수 대비", "%p", 61),
    "rs6m_skip1m_top20": ("excess_6m_skip1m_pp", "최근 1개월 제외 6개월 지수 대비", "%p", 148),
    "rs12to7m_top20": ("excess_12to7m_pp", "12~7개월 지수 대비", "%p", 253),
    **{key: ("excess_60d_pp", "60일 지수 대비", "%p", 61) for key in
       ("trend_rs60", "trend_rs20_60", "trend_volume", "trend_extension", "trend_market")},
    "trend_risk_adjusted": ("risk_adjusted_rs60", "변동성 조정 상대강도", "", 61),
}
REASONS = {
    "halted": "거래정지", "temp_halted": "거래정지", "liquidation": "정리매매",
    "managed": "관리종목", "low_liquidity": "저유동성 종목", "investment_caution": "투자주의환기",
    "market_warning": "시장경보", "preferred_share": "우선주", "short_overheated": "단기과열",
    "master_row_missing": "종목 상태 없음", "board_mismatch": "시장 정보 불일치",
}
QUOTE_FLAGS = ("temp_halted", "managed", "liquidation", "investment_caution", "short_overheated")


def _number(value):
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        result = Decimal(value)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _mark(row, status, reasons, score=None, rank=None):
    row["selection"] = {"status": status, "rank": rank,
                        "score": format(score, "f") if score is not None else None,
                        "reasons": list(dict.fromkeys(reasons))}


class CandidateSelector:
    def __init__(self, policy_path=ROOT / "config" / "candidate-selection.json", *, eligibility=None, now=None):
        self.policy_path = Path(policy_path)
        self.now = now or (lambda: datetime.now(KST))
        self.eligibility = eligibility or EligibilityService(now=self.now)

    def _policy(self):
        try:
            with self.policy_path.open("rb") as stream:
                raw = stream.read(32769)
        except FileNotFoundError:
            return None
        except OSError:
            raise KisError("후보 선별 정책을 읽지 못했습니다.") from None
        try:
            if len(raw) > 32768:
                raise ValueError()
            policy = json.loads(raw)
            if (policy["version"] != 1 or policy["variant"] not in SCORES
                    or type(policy["maximum_shortlist"]) is not int or policy["maximum_shortlist"] != 20
                    or type(policy["average_turnover_20d_krw"]) is not int
                    or policy["average_turnover_20d_krw"] != 10_000_000_000
                    or policy["minimum_raw_price_krw"] != 1000
                    or not isinstance(policy["decision"], str) or not policy["decision"].strip()
                    or not isinstance(policy["evidence_path"], str) or not policy["evidence_path"].strip()):
                raise ValueError()
            policy["policy_id"] = hashlib.sha256(raw).hexdigest()
            return policy
        except (ValueError, KeyError, TypeError):
            raise KisError("후보 선별 정책 형식이 올바르지 않습니다.") from None

    def enabled(self):
        return self._policy() is not None

    @property
    def policy_id(self):
        policy = self._policy()
        return policy["policy_id"] if policy else None

    @property
    def history_sessions(self):
        policy = self._policy()
        return SCORES[policy["variant"]][3] if policy else 21

    @staticmethod
    def _criteria(policy):
        variant = policy["variant"]
        field, label, _, history = SCORES[variant]
        rules = ["대회 고정 목록 내 보통주, 확인된 거래 제한·시장경보·주의환기·단기과열 제외",
                 "조회 현재가 1,000원 이상; 상태 미확인은 후보 보류",
                 f"연속 {history}거래일 유효 일봉, 최근 20거래일 모두 거래량 양수",
                 "기준일 포함 20거래일 평균 거래대금 100억원 이상"]
        if variant.startswith("trend_"):
            rules += ["수정종가 > 20일 평균 > 60일 평균, 20일 평균이 5거래일 전보다 상승",
                      "60거래일 소속 지수 대비 수익률 양수"]
        elif variant.startswith("rs"):
            rules += [f"{label} 수익률 양수"]
        if variant == "trend_rs20_60":
            rules += ["20거래일 소속 지수 대비 수익률 양수"]
        elif variant == "trend_volume":
            rules += ["기준일 거래대금 / 직전 20거래일 평균 거래대금 ≥ 1.5"]
        elif variant == "trend_extension":
            rules += ["수정종가와 20일 평균의 차이 ≤ 14일 평균 True Range의 2배"]
        elif variant == "trend_market":
            rules += ["소속 지수 종가 > 지수 60일 평균"]
        rules += [f"조건 통과 후 {label} 내림차순 최대 20종목, 동점은 종목코드 순"]
        return rules

    @staticmethod
    def _technical(feature, policy):
        if not isinstance(feature, dict) or feature.get("error"):
            return "unverified", [feature.get("error") or "일봉 지표 확인 필요"] if isinstance(feature, dict) else ["일봉 지표 확인 필요"], None
        variant = policy["variant"]
        field = SCORES[variant][0]
        score = _number(feature.get(field))
        turnover = _number(feature.get("avg_turnover_20d"))
        zero_days = feature.get("zero_volume_days_20d")
        if turnover is None or type(zero_days) is not int or not 0 <= zero_days <= 20:
            return "unverified", ["거래대금·거래량 확인 필요"], score
        reasons = []
        if turnover < policy["average_turnover_20d_krw"]:
            reasons.append("평균 거래대금 100억원 미만")
        if zero_days:
            reasons.append("최근 20거래일 내 거래량 없는 날 존재")
        if reasons:
            return "excluded", reasons, score
        if score is None:
            return "unverified", ["순위 지표에 필요한 일봉 부족 또는 지표 미확정"], None
        if variant.startswith("rs") and score <= 0:
            reasons.append("지수 대비 수익률 양수 조건 미충족")
        if variant.startswith("trend_"):
            rs = _number(feature.get("excess_60d_pp"))
            if type(feature.get("trend")) is not bool or rs is None:
                return "unverified", ["추세·상대강도 확인 필요"], score
            if not feature["trend"]:
                reasons.append("상승 추세 조건 미충족")
            if rs <= 0:
                reasons.append("60일 지수 대비 수익률 0 이하")
            checks = {
                "trend_rs20_60": ("excess_20d_pp", lambda value: value > 0, "20일 지수 대비 수익률 0 이하"),
                "trend_volume": ("turnover_ratio", lambda value: value >= Decimal("1.5"), "거래대금 배율 1.5 미만"),
                "trend_extension": ("extension_atr", lambda value: value <= 2, "20일 평균 대비 이격 2 ATR 초과"),
            }
            if variant in checks:
                key, test, reason = checks[variant]
                value = _number(feature.get(key))
                if value is None:
                    return "unverified", ["추가 조건 지표 확인 필요"], score
                if not test(value):
                    reasons.append(reason)
            if variant == "trend_market":
                if type(feature.get("market_up")) is not bool:
                    return "unverified", ["지수 추세 확인 필요"], score
                if not feature["market_up"]:
                    reasons.append("지수 60일 평균 상회 조건 미충족")
        return ("excluded", reasons, score) if reasons else ("pass", [], score)

    @staticmethod
    def _quote_status(quote, symbol, minimum_price):
        if not isinstance(quote, dict) or quote.get("symbol") != symbol:
            return "unverified", ["현재가·장중 상태 확인 필요"]
        excluded, unknown = [], []
        for field in QUOTE_FLAGS:
            if quote.get(field) is True:
                excluded.append(REASONS[field])
            elif quote.get(field) is not False:
                unknown.append("장중 상태 확인 필요")
        if quote.get("warning_code") in ("01", "02", "03"):
            excluded.append("시장경보")
        elif quote.get("warning_code") != "00":
            unknown.append("시장경보 상태 확인 필요")
        price = _number(quote.get("current_price"))
        if price is None or price <= 0:
            unknown.append("현재가 확인 필요")
        elif price < minimum_price:
            excluded.append("현재가 1,000원 미만")
        if quote.get("status") != "ok" or quote.get("unknown_fields"):
            unknown.append("현재가·장중 상태 미확정")
        return ("excluded", excluded + unknown) if excluded else ("unverified", unknown) if unknown else ("pass", [])

    def select(self, rows, features, client):
        policy = self._policy()
        if policy is None:
            raise KisError("후보 선별 정책이 없습니다.")
        try:
            master = self.eligibility.master_snapshot()
            if not isinstance(master, dict):
                raise ValueError()
        except Exception:
            master = {"status": "error", "rows": {}, "observed_at": None,
                      "error": "거래 제한 종목 상태를 확인하지 못했습니다."}
        pending = []
        for row in rows:
            if master.get("status") != "ok" or master.get("stale"):
                _mark(row, "unverified", ["거래 제한 상태 갱신 필요"])
                continue
            assessment = assess_master(master.get("rows", {}).get(row["symbol"]), row["board"])
            if assessment["status"] != "pass":
                reasons = [REASONS.get(reason, "종목 상태 확인 필요") for reason in assessment["reasons"]]
                _mark(row, "excluded" if assessment["status"] == "excluded" else "unverified", reasons)
                continue
            if row.get("status") != "ok":
                _mark(row, "unverified", [row.get("error") or "가격 비교 미완료"])
                continue
            status, reasons, score = self._technical(features.get(row["symbol"]), policy)
            if SCORES[policy["variant"]][0] == "avg_turnover_20d" and score is not None:
                score /= Decimal(100_000_000)  # 화면·정렬 모두 억원 단위.
            if status == "pass":
                pending.append((score, row))
            else:
                _mark(row, status, reasons, score)
        selected = 0
        for score, row in sorted(pending, key=lambda pair: (-pair[0], pair[1]["symbol"])):
            if selected >= policy["maximum_shortlist"]:
                _mark(row, "reserve", ["순위 대기 · 현재가·장중 상태 미확인"], score)
                continue
            observed = None
            try:
                quote = client.stock_status(row["symbol"])
                status, reasons = self._quote_status(quote, row["symbol"], policy["minimum_raw_price_krw"])
                if isinstance(quote, dict):
                    observed = {key: quote.get(key) for key in
                                ("symbol", "current_price", "status", "unknown_fields", "warning_code", *QUOTE_FLAGS)}
            except Exception:
                status, reasons = "unverified", ["현재가·장중 상태 조회 실패"]
            if status == "pass":
                selected += 1
                _mark(row, "selected", [], score, selected)
            else:
                _mark(row, status, reasons, score)
            if observed is not None:
                row["selection"]["observed_status"] = observed
                row["selection"]["status_observed_at"] = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        if self.policy_id != policy["policy_id"]:
            raise KisError("조회 중 후보 선별 정책이 변경되었습니다. 전체 조회를 다시 실행하세요.")
        counts = {state: sum(row["selection"]["status"] == state for row in rows)
                  for state in ("selected", "reserve", "excluded", "unverified")}
        unresolved = counts["unverified"]
        failed = master.get("status") != "ok" or master.get("stale") or unresolved and not selected
        error = master.get("error") or (f"{unresolved}종목 선별 확인 필요" if unresolved else None)
        return {"policy_id": policy["policy_id"], "status": "error" if failed else "ready",
                "label": "관찰 기준", "score_label": SCORES[policy["variant"]][1],
                "score_unit": SCORES[policy["variant"]][2], "criteria": self._criteria(policy),
                "checked_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
                "master_observed_at": master.get("observed_at"), "counts": counts,
                "error": error}
