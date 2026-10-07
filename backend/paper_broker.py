"""KIS 모의계좌 지정가 주문 어댑터. 재시도·주문 원장은 호출자가 관리한다.

공식 계약: https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock
order_cash, order_rvsecncl, inquire_daily_ccld, inquire_psbl_order (2026-10-05 확인).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re

from backend import kis
from backend.kis import AccountProfile, KisError, KST, client_for_profile


PAPER_URL = "https://openapivts.koreainvestment.com:29443"
MAX_PAGES = 1000


class BrokerRejected(KisError):
    """주문 미전송 또는 KIS의 명시적인 업무 거절. 재시도 결정은 호출자에게 있다."""

    def __init__(self, message="모의 주문이 거절됐습니다.", *, code=None):
        super().__init__(message)
        self.code = code


class BrokerUnknown(KisError):
    """주문 접수 여부 불명. 새 주문을 보내지 말고 주문 내역과 대조해야 한다."""


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Z]{6}", value):
        raise ValueError("종목코드는 영문 대문자·숫자 6자리여야 합니다.")
    return value


def _quantity(value):
    if type(value) is not int or not 1 <= value <= 999_999_999:
        raise ValueError("주문수량은 양의 정수여야 합니다.")
    return value


def _price(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}", value) or int(value) <= 0:
        raise ValueError("지정가는 양의 정수 원화여야 합니다.")
    return str(int(value))


def _number(value, *, integer=False, positive=False, signed=False):
    pattern = (r"-?" if signed else "") + (r"[0-9]+" if integer else r"[0-9]+(?:\.[0-9]+)?")
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(pattern, value.strip()):
        raise KisError("모의계좌 응답의 수량·금액이 올바르지 않습니다.")
    result = Decimal(value.strip())
    if positive and result <= 0:
        raise KisError("모의계좌 응답의 수량·금액이 올바르지 않습니다.")
    if integer:
        return int(result)
    text = format(result, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _identity(value, size):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1," + str(size) + r"}", value):
        raise KisError("모의계좌 응답의 주문 식별값이 올바르지 않습니다.")
    return value


def _order_time(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
            raise ValueError
        return datetime.strptime(value, "%H%M%S").strftime("%H:%M:%S")
    except ValueError:
        raise KisError("모의계좌 응답의 주문시각이 올바르지 않습니다.") from None


def _order_row(raw, start, end):
    if not isinstance(raw, dict):
        raise KisError("모의 주문 내역 형식이 올바르지 않습니다.")
    try:
        raw_date = raw.get("ord_dt")
        if not isinstance(raw_date, str) or not re.fullmatch(r"[0-9]{8}", raw_date):
            raise ValueError
        day = datetime.strptime(raw_date, "%Y%m%d").date()
        if not start <= day <= end:
            raise ValueError
        symbol = _symbol(raw.get("pdno"))
    except ValueError:
        raise KisError("모의 주문 내역의 종목·날짜가 올바르지 않습니다.") from None
    side = {"01": "sell", "02": "buy"}.get(raw.get("sll_buy_dvsn_cd"))
    if side is None or raw.get("cncl_yn") not in ("Y", "N"):
        raise KisError("모의 주문 내역의 매매·취소 구분이 올바르지 않습니다.")
    quantity = _number(raw.get("ord_qty"), integer=True, positive=True)
    filled = _number(raw.get("tot_ccld_qty"), integer=True)
    remaining = _number(raw.get("rmn_qty"), integer=True)
    cancelled = _number(raw.get("cnc_cfrm_qty"), integer=True)
    rejected = _number(raw.get("rjct_qty"), integer=True)
    if filled + remaining + cancelled + rejected > quantity:
        raise KisError("모의 주문 내역의 누적 수량이 주문수량을 초과합니다.")
    if remaining == 0 and filled + cancelled + rejected != quantity:
        raise KisError("모의 주문 내역의 잔량 소멸 사유를 확인할 수 없습니다.")
    average = _number(raw.get("avg_prvs"), positive=filled > 0)
    amount = _number(raw.get("tot_ccld_amt"), positive=filled > 0)
    if filled == 0 and (Decimal(average) != 0 or Decimal(amount) != 0):
        raise KisError("미체결 주문의 체결금액이 0이 아닙니다.")
    original = raw.get("orgn_odno")
    original = None if original in (None, "") else _identity(original, 20)
    if original and int(original) == 0:
        original = None
    status = ("filled" if filled == quantity else "partial" if filled and remaining else
              "open" if remaining else "cancelled" if cancelled else "rejected")
    return {"order_date": day.isoformat(), "order_id": _identity(raw.get("odno"), 20),
            "branch_id": _identity(raw.get("ord_gno_brno"), 10), "original_order_id": original,
            "symbol": symbol, "side": side, "quantity": quantity, "filled_quantity": filled,
            "remaining_quantity": remaining, "cancelled_quantity": cancelled,
            "rejected_quantity": rejected, "limit_price": _number(raw.get("ord_unpr")),
            "average_price": average, "filled_amount": amount, "status": status,
            "cancelled": raw["cncl_yn"] == "Y", "order_time": _order_time(raw.get("ord_tmd"))}


class PaperBroker:
    """모의 URL·KRX·현금 지정가만 지원한다. 생성이나 조회는 주문하지 않는다."""

    def __init__(self, profile: AccountProfile, *, client=None, now=None):
        profile.settings.validate_credentials()
        profile.settings.validate_account()
        self.profile = profile
        self.client = client if client is not None else client_for_profile(profile)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._check_client()

    def _check_client(self):
        if kis.PAPER_URL != PAPER_URL or self.client.settings != self.profile.settings:
            raise KisError("모의계좌 연결 설정이 일치하지 않습니다.")

    def _stamp(self):
        value = self.now()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise KisError("모의계좌 조회 시각을 확인할 수 없습니다.")
        return value.astimezone(timezone.utc).isoformat()

    def _account(self):
        self._check_client()
        self.profile.settings.validate_account()
        return {"CANO": self.profile.settings.account, "ACNT_PRDT_CD": self.profile.settings.product_code}

    def _get(self, path, tr_id, params, continuation=""):
        self._check_client()
        data, headers = self.client._get(path, tr_id, params, continuation)
        if not isinstance(data, dict) or data.get("rt_cd") != "0" or not isinstance(headers, dict):
            raise KisError("모의계좌 조회 성공 여부를 확인할 수 없습니다.")
        return data, headers

    def _pages(self, path, tr_id, params):
        seen = set()
        for page in range(MAX_PAGES):
            data, headers = self._get(path, tr_id, params, "N" if page else "")
            rows = data.get("output1")
            if not isinstance(rows, list):
                raise KisError("모의계좌 목록 응답이 올바르지 않습니다.")
            yield rows, data.get("output2")
            continuation = headers.get("tr_cont", "")
            if not isinstance(continuation, str):
                raise KisError("모의계좌 연속조회 상태가 올바르지 않습니다.")
            continuation = continuation.strip()
            if continuation in ("", "D", "E"):
                return
            if continuation not in ("M", "F"):
                raise KisError("모의계좌 연속조회 상태를 확인할 수 없습니다.")
            cursor = (data.get("ctx_area_fk100"), data.get("ctx_area_nk100"))
            if not all(isinstance(value, str) and len(value) <= 100 for value in cursor):
                raise KisError("모의계좌 연속조회 키가 올바르지 않습니다.")
            cursor = tuple(value.strip() for value in cursor)
            if not any(cursor) or cursor in seen:
                raise KisError("모의계좌 연속조회가 진행되지 않습니다. 부분 결과를 반환하지 않습니다.")
            seen.add(cursor)
            params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
        raise KisError("모의계좌 연속조회 한도를 초과했습니다. 부분 결과를 반환하지 않습니다.")

    def snapshot(self):
        params = {**self._account(), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                  "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
                  "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        holdings, cash, total = {}, None, None
        for rows, summary in self._pages("/uapi/domestic-stock/v1/trading/inquire-balance", "VTTC8434R", params):
            if not isinstance(summary, list) or len(summary) != 1 or not isinstance(summary[0], dict):
                raise KisError("모의계좌 잔고 요약이 올바르지 않습니다.")
            cash = _number(summary[0].get("dnca_tot_amt"), signed=True)
            total = _number(summary[0].get("tot_evlu_amt"), signed=True)
            for raw in rows:
                if not isinstance(raw, dict):
                    raise KisError("모의계좌 보유종목 형식이 올바르지 않습니다.")
                try:
                    symbol = _symbol(raw.get("pdno"))
                except ValueError:
                    raise KisError("모의계좌 보유종목 식별값이 올바르지 않습니다.") from None
                holding = {"quantity": _number(raw.get("hldg_qty"), integer=True),
                           "sellable_quantity": _number(raw.get("ord_psbl_qty"), integer=True),
                           "average_price": _number(raw.get("pchs_avg_pric")),
                           "price": _number(raw.get("prpr"))}
                if holding["sellable_quantity"] > holding["quantity"] or symbol in holdings:
                    raise KisError("모의계좌 보유종목 수량이 일치하지 않습니다.")
                holdings[symbol] = holding
        return {"environment": "paper", "cash": cash, "total_value": total,
                "holdings": holdings, "as_of": self._stamp()}

    def quote(self, symbol):
        _symbol(symbol)
        self._check_client()
        status = self.client.stock_status(symbol)
        fields = ("temp_halted", "managed", "liquidation", "investment_caution", "short_overheated")
        excluded = [field for field in fields if status.get(field) is True]
        if status.get("warning_code") in ("01", "02", "03"):
            excluded.append("market_warning")
        known = (status.get("status") == "ok" and status.get("symbol") == symbol
                 and not status.get("unknown_fields")
                 and all(type(status.get(field)) is bool for field in fields)
                 and status.get("warning_code") in ("00", "01", "02", "03"))
        price = status.get("current_price")
        try:
            price = _price(price)
        except ValueError:
            price, known = None, False
        return {"symbol": symbol, "price": price, "eligible": known and not excluded,
                "reason": excluded[0] if excluded else None if known else "status_unknown",
                "as_of": self._stamp()}

    def buyability(self, symbol, limit_price):
        symbol, limit_price = _symbol(symbol), _price(limit_price)
        # 공식 안내의 시장가 기준 무미수 가능수량과 지정가의 현금 한도를 함께 적용한다.
        data, _ = self._get("/uapi/domestic-stock/v1/trading/inquire-psbl-order", "VTTC8908R", {
            **self._account(), "PDNO": symbol, "ORD_UNPR": limit_price, "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
        output = data.get("output")
        if not isinstance(output, dict):
            raise KisError("모의계좌 매수가능 응답이 올바르지 않습니다.")
        cash = _number(output.get("nrcvb_buy_amt"))
        quantity = min(_number(output.get("nrcvb_buy_qty"), integer=True), int(Decimal(cash) // Decimal(limit_price)))
        return {"cash": cash, "quantity": quantity, "as_of": self._stamp()}

    def market_session(self, symbol):
        """최신 양수 거래량 분봉의 시각. 서버 수신시각을 체결시각으로 대체하지 않는다."""
        _symbol(symbol)
        self._check_client()
        now = datetime.fromisoformat(self._stamp()).astimezone(KST)
        data = self.client.chart_minutes(symbol, now.strftime("%H%M%S"))
        if (not isinstance(data, dict) or data.get("rt_cd") != "0"
                or not isinstance(data.get("output1"), dict)
                or data["output1"].get("stck_shrn_iscd", symbol) != symbol
                or not isinstance(data.get("output2"), list) or len(data["output2"]) > 100):
            raise KisError("모의 주문용 분봉 응답이 올바르지 않습니다.")
        observed = {}
        for row in data["output2"]:
            try:
                if (not isinstance(row, dict) or not isinstance(row.get("stck_bsop_date"), str)
                        or not re.fullmatch(r"[0-9]{8}", row["stck_bsop_date"])):
                    raise ValueError
                day = datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date()
                clock = datetime.strptime(_order_time(row.get("stck_cntg_hour")), "%H:%M:%S").time()
                if clock.second != 0:
                    raise ValueError
                stamp = datetime.combine(day, clock, KST)
                if stamp > now:
                    raise ValueError
            except (ValueError, TypeError):
                raise KisError("모의 주문용 분봉 시각이 올바르지 않습니다.") from None
            volume = _number(row.get("cntg_vol"), integer=True)
            if not volume:
                continue
            price = _number(row.get("stck_prpr"), integer=True, positive=True)
            opened = _number(row.get("stck_oprc"), integer=True, positive=True)
            high = _number(row.get("stck_hgpr"), integer=True, positive=True)
            low = _number(row.get("stck_lwpr"), integer=True, positive=True)
            if not low <= min(opened, price) <= max(opened, price) <= high:
                raise KisError("모의 주문용 분봉 가격 범위가 올바르지 않습니다.")
            result = {"session_date": day.isoformat(), "last_trade_at": stamp.isoformat(),
                      "price": str(price), "volume": volume}
            if stamp in observed and observed[stamp] != result:
                raise KisError("모의 주문용 분봉이 서로 일치하지 않습니다.")
            observed[stamp] = result
        if not observed:
            raise KisError("최근 거래가 있는 모의 주문용 분봉이 없습니다.")
        return observed[max(observed)]

    def session_days(self, start, end):
        """100행 API 상한보다 짧은 30일 조각을 모두 읽어 거래일을 반환한다."""
        try:
            if not all(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
                       for value in (start, end)):
                raise ValueError
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            today = datetime.fromisoformat(self._stamp()).astimezone(KST).date()
            if first > last or last > today or (last - first).days > 370:
                raise ValueError
        except ValueError:
            raise ValueError("거래일 조회 범위는 오늘까지 최대 371일이어야 합니다.") from None
        self._check_client()
        dates, cursor = set(), first
        while cursor <= last:
            chunk_end = min(last, cursor + timedelta(days=29))
            rows = self.client.index_daily(cursor.isoformat(), chunk_end.isoformat(), "0001")
            if not isinstance(rows, list) or len(rows) > 30:
                raise KisError("거래일 확인용 지수 응답이 올바르지 않습니다.")
            chunk = set()
            for row in rows:
                try:
                    if (not isinstance(row, dict) or not isinstance(row.get("stck_bsop_date"), str)
                            or not re.fullmatch(r"[0-9]{8}", row["stck_bsop_date"])):
                        raise ValueError
                    day = datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date()
                    if not cursor <= day <= chunk_end or day in chunk:
                        raise ValueError
                except (ValueError, TypeError):
                    raise KisError("거래일 확인용 지수의 날짜가 올바르지 않습니다.") from None
                _number(row.get("bstp_nmix_prpr"), positive=True)
                _number(row.get("acml_vol"), integer=True, positive=True)
                chunk.add(day)
            dates.update(chunk)
            cursor = chunk_end + timedelta(days=1)
        if not dates:
            raise KisError("확인 가능한 거래일이 없습니다.")
        return sorted(day.isoformat() for day in dates)

    def orders(self, start, end):
        start_date, end_date = kis.validate_execution_range(start, end)
        today = self.now().astimezone(KST).date()
        month_index = today.year * 12 + today.month - 4
        cutoff = date(month_index // 12, month_index % 12 + 1, 1)
        periods = []
        if start_date < cutoff:
            periods.append((start_date, min(end_date, cutoff - timedelta(days=1)), "VTSC9215R"))
        if end_date >= cutoff:
            periods.append((max(start_date, cutoff), end_date, "VTTC0081R"))
        orders = {}
        for period_start, period_end, tr_id in periods:
            params = {**self._account(), "INQR_STRT_DT": period_start.strftime("%Y%m%d"),
                      "INQR_END_DT": period_end.strftime("%Y%m%d"), "SLL_BUY_DVSN_CD": "00",
                      "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "",
                      "INQR_DVSN_1": "", "INQR_DVSN_3": "00", "EXCG_ID_DVSN_CD": "ALL",
                      "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
            for rows, summary in self._pages("/uapi/domestic-stock/v1/trading/inquire-daily-ccld", tr_id, params):
                if not isinstance(summary, dict):
                    raise KisError("모의 주문 요약 응답이 올바르지 않습니다.")
                for raw in rows:
                    row = _order_row(raw, period_start, period_end)
                    key = (row["order_date"], row["branch_id"], row["order_id"])
                    previous = orders.get(key)
                    if previous is not None:
                        stable = ("symbol", "side", "quantity", "limit_price", "original_order_id", "order_time")
                        cumulative = ("filled_quantity", "cancelled_quantity", "rejected_quantity")
                        if (any(previous[field] != row[field] for field in stable)
                                or any(previous[field] > row[field] for field in cumulative)
                                or previous["remaining_quantity"] < row["remaining_quantity"]
                                or Decimal(previous["filled_amount"]) > Decimal(row["filled_amount"])):
                            raise KisError("모의 주문 누적 응답이 서로 일치하지 않습니다.")
                    orders[key] = row
        return sorted(orders.values(), key=lambda row: (row["order_date"], row["order_time"], row["order_id"]))

    def _mutate(self, path, tr_id, body):
        # 인증 실패는 주문 전이다. 네트워크 요청을 시작한 뒤의 실패는 보수적으로 불명 처리한다.
        self._check_client()
        try:
            token = self.client.token()
        except Exception:
            raise BrokerRejected("모의 주문 전 인증에 실패했습니다.") from None
        headers = {"authorization": "Bearer " + token, "appkey": self.profile.settings.app_key,
                   "appsecret": self.profile.settings.app_secret, "tr_id": tr_id, "custtype": "P"}
        try:
            data, _ = self.client._request(path, headers=headers, body=body)
        except KisError as error:
            # PaperClient의 검증된 업무 오류만 확정 거절로 분류한다. HTTP 오류·타임아웃은 불명이다.
            match = re.fullmatch(r"KIS 업무 응답 오류(?: \(([A-Z]{3}[0-9]{5})\))?", str(error))
            if match:
                raise BrokerRejected("모의 주문이 KIS에서 거절됐습니다.", code=match.group(1)) from None
            raise BrokerUnknown("모의 주문 접수 여부를 확인할 수 없습니다. 주문 내역을 대조해야 합니다.") from None
        except Exception:
            raise BrokerUnknown("모의 주문 접수 여부를 확인할 수 없습니다. 주문 내역을 대조해야 합니다.") from None
        if isinstance(data, dict) and isinstance(data.get("rt_cd"), str) and data["rt_cd"] != "0":
            code = data.get("msg_cd")
            code = code if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}[0-9]{5}", code) else None
            raise BrokerRejected("모의 주문이 KIS에서 거절됐습니다.", code=code)
        try:
            if not isinstance(data, dict) or data.get("rt_cd") != "0" or not isinstance(data.get("output"), dict):
                raise KisError("응답 형식 오류")
            output = {key.lower(): value for key, value in data["output"].items()}
            result = {"order_id": _identity(output.get("odno"), 20),
                      "branch_id": _identity(output.get("krx_fwdg_ord_orgno"), 10),
                      "order_time": _order_time(output.get("ord_tmd"))}
            if int(result["order_id"]) == 0:
                raise KisError("빈 주문 번호")
            return result
        except Exception:
            raise BrokerUnknown("모의 주문 응답의 식별값을 확인할 수 없습니다. 주문 내역을 대조해야 합니다.") from None

    def submit(self, symbol, side, quantity, limit_price):
        symbol, quantity, limit_price = _symbol(symbol), _quantity(quantity), _price(limit_price)
        if side not in ("buy", "sell"):
            raise ValueError("주문 방향은 buy 또는 sell이어야 합니다.")
        body = {**self._account(), "PDNO": symbol, "ORD_DVSN": "00", "ORD_QTY": str(quantity),
                "ORD_UNPR": limit_price, "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "sell" else "",
                "CNDT_PRIC": ""}
        return self._mutate("/uapi/domestic-stock/v1/trading/order-cash", "VTTC0012U" if side == "buy" else "VTTC0011U", body)

    def cancel(self, order_id, branch_id, symbol, quantity):
        symbol, quantity = _symbol(symbol), _quantity(quantity)
        try:
            order_id, branch_id = _identity(order_id, 20), _identity(branch_id, 10)
        except KisError:
            raise ValueError("취소할 주문 식별값이 올바르지 않습니다.") from None
        # 정정취소가능조회는 공식 예제에 실전 TR만 있다. 모의에서는 당일 전체 내역으로 잔량을 재확인한다.
        today = self.now().astimezone(KST).date().isoformat()
        found = [row for row in self.orders(today, today) if row["order_id"] == order_id and row["branch_id"] == branch_id]
        if len(found) != 1 or found[0]["symbol"] != symbol or not 0 < found[0]["remaining_quantity"] <= quantity:
            raise BrokerRejected("취소할 모의 주문과 현재 잔량이 일치하지 않습니다.")
        body = {**self._account(), "KRX_FWDG_ORD_ORGNO": branch_id, "ORGN_ODNO": order_id,
                "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX"}
        return self._mutate("/uapi/domestic-stock/v1/trading/order-rvsecncl", "VTTC0013U", body)
