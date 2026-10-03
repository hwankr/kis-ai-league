"""KIS 수정 일봉과 당일 API 1분봉을 검증해 대시보드 차트로 반환한다."""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, time as daytime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from backend.history import series_for_profile
from backend.chart_cache import DiskChartCache
from backend.kis import KST, KisError, ROOT, _quote_number, client_for_profile, load_profiles
from backend.market import load_market_settings
from backend.symbol_names import SymbolNames

INTERVALS = {"day", "5m", "15m"}
MAX_CACHE = 32
MAX_MINUTE_PAGES = 16


def validate_chart_request(symbol, interval):
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
        raise KisError("종목코드는 숫자 6자리로 입력하세요.")
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise KisError("차트 주기는 day·5m·15m 중에서 선택하세요.")


def _rows(data, symbol):
    if not isinstance(data, dict) or not isinstance(data.get("output2"), list):
        raise KisError("차트 응답 형식이 예상과 다릅니다.")
    output = data.get("output1", {})
    if not isinstance(output, dict):
        raise KisError("차트 응답 형식이 예상과 다릅니다.")
    if output.get("stck_shrn_iscd", symbol) != symbol:
        raise KisError("차트 응답의 종목코드가 요청과 다릅니다.")
    rows = data["output2"]
    if len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
        raise KisError("차트 응답 형식이 예상과 다릅니다.")
    return rows


def _date(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
            raise ValueError
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise KisError("차트 응답의 거래일이 올바르지 않습니다.") from None


def _minute_time(row):
    day = _date(row.get("stck_bsop_date"))
    value = row.get("stck_cntg_hour")
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
            raise ValueError
        clock = datetime.strptime(value, "%H%M%S").time()
        if clock.second != 0:
            raise ValueError
    except ValueError:
        raise KisError("분봉 응답의 시각이 올바르지 않습니다.") from None
    return datetime.combine(day, clock, KST)


def _ohlcv(row, *, minute):
    volume = _quote_number(row.get("cntg_vol" if minute else "acml_vol"), integer=True)
    # 거래가 없는 시각의 채움 봉은 표시하지 않는다.
    if minute and Decimal(volume) == 0:
        return None
    prices = {name: _quote_number(row.get(key), positive=True) for name, key in (
        ("open", "stck_oprc"), ("high", "stck_hgpr"), ("low", "stck_lwpr"),
        ("close", "stck_prpr" if minute else "stck_clpr"))}
    if not (Decimal(prices["low"]) <= min(Decimal(prices["open"]), Decimal(prices["close"]))
            <= max(Decimal(prices["open"]), Decimal(prices["close"])) <= Decimal(prices["high"])):
        raise KisError("차트 응답의 시가·고가·저가·종가 범위가 올바르지 않습니다.")
    return {**prices, "volume": volume}


def daily_bars(data, symbol, start, now):
    result = {}
    for row in _rows(data, symbol):
        day = _date(row.get("stck_bsop_date"))
        if not start <= day <= now.date():
            raise KisError("일봉 응답에 조회 기간 밖의 거래일이 있습니다.")
        values = _ohlcv(row, minute=False)
        bar = {"time": day.isoformat(), **values,
               "partial": day == now.date() and now.time() < daytime(15, 30)}
        if day in result and result[day] != bar:
            raise KisError("같은 거래일의 일봉 응답이 서로 다릅니다.")
        result[day] = bar
    return [result[key] for key in sorted(result)]


def _minute_snapshot(client, symbol, now, previous=None, previous_until=None):
    """최신 페이지부터 조회하고 정상 캐시의 마지막 두 분까지 겹치면 병합한다."""
    cursor = min(now.time().replace(tzinfo=None), daytime(15, 30))
    session = None
    result = {}
    previous = previous or []
    previous_session = datetime.fromisoformat(previous[-1]["time"]).date() if previous else None
    overlap = previous_until - timedelta(minutes=1) if previous_until else None
    latest = None
    for _ in range(MAX_MINUTE_PAGES):
        rows = _rows(client.chart_minutes(symbol, cursor.strftime("%H%M%S")), symbol)
        if not rows:
            if session is None:
                if cursor < daytime(15, 30):
                    start = now.date() - timedelta(days=180)
                    days = daily_bars(client.chart_daily(symbol, start, now.date()), symbol, start, now)
                    if days:
                        latest_day = datetime.strptime(days[-1]["time"], "%Y-%m-%d").date()
                        if latest_day < now.date():
                            # 장전·휴일 아침에는 현재 시각의 분봉이 비어 있을 수 있다.
                            # 일봉으로 과거 거래일을 확인한 경우에만 전일 종가까지 요청한다.
                            session, cursor = latest_day, daytime(15, 30)
                            continue
                return [], None
            raise KisError("분봉 조회가 장 시작까지 완료되지 않았습니다. 다시 조회하세요.")
        stamped = [(_minute_time(row), row) for row in rows]
        if any(stamp.date() > now.date() for stamp, _ in stamped):
            raise KisError("분봉 응답에 미래 거래일이 있습니다.")
        if session is None:
            session = max(stamp.date() for stamp, _ in stamped)
            if previous_session is not None and session < previous_session:
                raise KisError("분봉 응답의 거래일이 기존 기록보다 이릅니다. 다시 조회하세요.")
            if session < now.date() and cursor < daytime(15, 30):
                # 지난 거래일이면 오늘의 시각으로 잘라 보여주지 않는다.
                cursor = daytime(15, 30)
                continue
        if any(stamp.date() > session for stamp, _ in stamped):
            raise KisError("분봉 연속조회 중 거래일이 변경됐습니다. 다시 조회하세요.")
        if previous_session is not None and session < previous_session:
            raise KisError("분봉 응답의 거래일이 기존 기록보다 이릅니다. 다시 조회하세요.")
        current = [(stamp, row) for stamp, row in stamped if stamp.date() == session]
        if not current:
            break  # 이전 거래일에 도달했다.
        eligible = [(stamp, row) for stamp, row in current
                    if stamp.time() <= cursor and stamp <= now]
        if not eligible:
            raise KisError("분봉 연속조회가 진행되지 않습니다. 다시 조회하세요.")
        for stamp, row in eligible:
            if not daytime(9) <= stamp.time() <= daytime(15, 30):
                continue
            values = _ohlcv(row, minute=True)
            bar = {"time": stamp.isoformat(), **values, "partial": False} if values is not None else None
            if stamp in result and result[stamp] != bar:
                raise KisError("같은 시각의 분봉 응답이 서로 다릅니다.")
            result[stamp] = bar
            latest = stamp if latest is None else max(latest, stamp)
        earliest = min(stamp for stamp, _ in eligible)
        if previous_session == session and previous_until is not None:
            if latest is not None and latest < previous_until:
                raise KisError("분봉 응답의 최신 시각이 기존 기록보다 이릅니다. 다시 조회하세요.")
            if overlap in result:
                # 재조회 범위의 거래량 0 행도 권위 있게 덮어써 기존 봉을 제거한다.
                for bar in previous:
                    stamp = datetime.fromisoformat(bar["time"])
                    if stamp < earliest:
                        result[stamp] = bar
                break
        if earliest.time() <= daytime(9) or any(stamp.date() < session for stamp, _ in stamped):
            break
        next_cursor = (earliest - timedelta(seconds=1)).time()
        if next_cursor >= cursor:
            raise KisError("분봉 연속조회가 진행되지 않습니다. 다시 조회하세요.")
        cursor = next_cursor
    else:
        raise KisError("분봉 연속조회 한도를 초과했습니다. 다시 조회하세요.")
    return [result[key] for key in sorted(result) if result[key] is not None], latest


def minute_bars(client, symbol, now):
    """캐시 없는 전체 조회. 중간 실패 시 부분 목록을 반환하지 않는다."""
    return _minute_snapshot(client, symbol, now)[0]


def aggregate_minutes(bars, interval, now):
    """09:00 기준 구간. 15:30 종가 경매는 별도 봉이며 빈 구간을 만들지 않는다."""
    size = {"5m": 5, "15m": 15}[interval]
    groups = {}
    for bar in bars:
        stamp = datetime.fromisoformat(bar["time"])
        bucket = stamp.replace(minute=(stamp.minute // size) * size, second=0, microsecond=0)
        key = bucket.isoformat()
        if key not in groups:
            end = bucket + timedelta(minutes=1 if bucket.time() == daytime(15, 30) else size)
            groups[key] = {**bar, "time": key, "partial": bucket <= now < end}
        else:
            group = groups[key]
            group["high"] = max(group["high"], bar["high"], key=Decimal)
            group["low"] = min(group["low"], bar["low"], key=Decimal)
            group["close"] = bar["close"]
            group["volume"] = str(Decimal(group["volume"]) + Decimal(bar["volume"]))
    return [groups[key] for key in sorted(groups)]


@dataclass
class _Cached:
    snapshot: dict = field(default_factory=dict)
    next_attempt: float = 0
    observed_at: datetime | None = None
    minute_until: datetime | None = None


def _next_open(stamp):
    opening = stamp.replace(hour=9, minute=0, second=0, microsecond=0)
    if opening <= stamp:
        opening += timedelta(days=1)
    while opening.weekday() >= 5:
        opening += timedelta(days=1)
    return opening


def _closed_until(cached):
    """16시 이후·휴일·장전의 성공한 관측만 다음 평일 09시까지 재사용한다."""
    observed = cached.observed_at
    if observed is None or not cached.snapshot.get("bars"):
        return None
    session = datetime.strptime(cached.snapshot["as_of"], "%Y-%m-%d").date()
    if observed.weekday() >= 5 or observed.time() >= daytime(16):
        return _next_open(observed)
    if observed.time() < daytime(9) and session < observed.date():
        return _next_open(observed)
    return None


def _aware_time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid cache timestamp")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Invalid cache timezone")
    return stamp.astimezone(KST)


def _validated_saved(data, symbol, kind, now):
    """디스크 내용은 신뢰하지 않고 메모리에 넣기 전 전체 스냅샷을 검사한다."""
    if (not isinstance(data, dict) or set(data) != {
            "version", "identity", "symbol", "kind", "observed_at", "minute_until", "bars"}
            or data["symbol"] != symbol or data["kind"] != kind):
        raise ValueError("Invalid cache schema")
    observed = _aware_time(data["observed_at"])
    if observed > now:
        raise ValueError("Future cache observation")
    bars = data["bars"]
    if not isinstance(bars, list) or not 1 <= len(bars) <= (100 if kind == "day" else 391):
        raise ValueError("Invalid cache bar count")
    keys = set()
    session = None
    last = None
    for bar in bars:
        if (not isinstance(bar, dict) or set(bar) != {"time", "open", "high", "low", "close", "volume", "partial"}
                or type(bar["partial"]) is not bool):
            raise ValueError("Invalid cache bar")
        if kind == "day":
            stamp = _date(bar["time"].replace("-", ""))
            if bar["time"] != stamp.isoformat() or not observed.date() - timedelta(days=180) <= stamp <= observed.date():
                raise ValueError("Invalid cache date")
        else:
            stamp = _aware_time(bar["time"])
            if (bar["time"] != stamp.isoformat() or stamp.second or stamp.microsecond
                    or not daytime(9) <= stamp.time() <= daytime(15, 30) or stamp > observed
                    or session is not None and session != stamp.date()):
                raise ValueError("Invalid cache minute")
            session = stamp.date()
        if bar["time"] in keys or last is not None and stamp <= last:
            raise ValueError("Invalid cache ordering")
        keys.add(bar["time"])
        last = stamp
        values = _ohlcv({"stck_oprc": bar["open"], "stck_hgpr": bar["high"], "stck_lwpr": bar["low"],
                         "stck_prpr": bar["close"], "stck_clpr": bar["close"], "cntg_vol": bar["volume"],
                         "acml_vol": bar["volume"]}, minute=kind == "minute")
        if values is None or any(bar[key] != value for key, value in values.items()):
            raise ValueError("Invalid cache numbers")
    until = None
    if kind == "minute":
        until = _aware_time(data["minute_until"])
        if (data["minute_until"] != until.isoformat() or until.date() != session or until < last
                or until > observed or until.second or until.microsecond
                or not daytime(9) <= until.time() <= daytime(15, 30)):
            raise ValueError("Invalid cache coverage")
    elif data["minute_until"] is not None:
        raise ValueError("Invalid daily cache coverage")
    return bars, observed, until


def _present(cached, interval):
    result = deepcopy(cached.snapshot)
    result["interval"] = interval
    if interval != "day" and result["bars"]:
        result["bars"] = aggregate_minutes(result["bars"], interval, cached.observed_at)
    return result


class ChartService:
    def __init__(self, config_path=ROOT / "config.local.toml", client_factory=client_for_profile,
                 clock=time.monotonic, now=None, symbol_names=None, disk_cache=None):
        self.config_path = Path(config_path)
        self.client_factory, self.clock = client_factory, clock
        self.now = now or (lambda: datetime.now(KST))
        self.symbol_names = symbol_names if symbol_names is not None else SymbolNames(
            self.config_path.parent / ".local" / "stock-names.json")
        self.disk_cache = disk_cache if disk_cache is not None else DiskChartCache(
            self.config_path.parent / ".local" / "chart-cache")
        self.lock = threading.Lock()
        self.cache = OrderedDict()

    def snapshot(self, symbol, interval="day", force=False):
        empty = {"status": "error", "symbol": symbol, "market": "KRX", "environment": "paper",
                 "interval": interval, "updated_at": None, "as_of": None, "stale": False,
                 "error": None, "bars": [], "source": "KIS", "adjusted": interval == "day"}
        try:
            validate_chart_request(symbol, interval)
            if type(force) is not bool:
                raise KisError("차트 강제 새로고침 값이 올바르지 않습니다.")
            settings = load_market_settings(self.config_path)
            profile = load_profiles(self.config_path).select(settings.account)
            profile.settings.validate_credentials()
            now = self.now().astimezone(KST)
            # 날짜를 키에 넣지 않아 자정·재시작에도 정상 기록을 보존한다.
            series = series_for_profile(profile)
            kind = "day" if interval == "day" else "minute"
            key = (series, symbol, kind)
            identity = hashlib.sha256(json.dumps([series.profile_id, series.environment, series.fingerprint,
                                                  symbol, kind], separators=(",", ":")).encode()).hexdigest()
        except KisError as error:
            return {**empty, "error": str(error)}
        except Exception:
            return {**empty, "error": "차트 조회 설정을 읽지 못했습니다."}
        # 동일 요청은 첫 응답을 공유한다. 캐시와 잠금 목록도 무한히 증가하지 않는다.
        with self.lock:
            cached = self.cache.get(key)
            if cached is None:
                cached = self.cache[key] = _Cached()
                try:
                    saved = self.disk_cache.read(identity)
                    bars, observed, until = _validated_saved(saved, symbol, kind, now)
                    cached.snapshot = {**empty, "status": "ok", "error": None, "bars": bars,
                                       "as_of": bars[-1]["time"][:10],
                                       "updated_at": observed.astimezone(timezone.utc).isoformat(timespec="seconds")}
                    cached.observed_at, cached.minute_until = observed, until
                    age = (now - observed).total_seconds()
                    cached.next_attempt = self.clock() + max(0, (60 if kind == "day" else 30) - age)
                    try:
                        name = self.symbol_names.lookup((symbol,)).get(symbol)
                        if name:
                            cached.snapshot["name"] = name
                    except Exception:
                        pass
                except Exception:
                    pass  # 손상·구버전·설정이 다른 파일은 캐시 미스로 처리한다.
            self.cache.move_to_end(key)
            while len(self.cache) > MAX_CACHE:
                self.cache.popitem(last=False)
            blocked = self.clock() < cached.next_attempt
            closed_until = _closed_until(cached)
            if cached.snapshot:
                if cached.snapshot.get("error"):
                    if blocked:  # 강제 새로고침도 오류 재시도 대기 시간은 지킨다.
                        return _present(cached, interval)
                elif not force and (blocked or closed_until is not None and now < closed_until):
                    return _present(cached, interval)
            try:
                client = self.client_factory(profile)
                if interval == "day":
                    start = now.date() - timedelta(days=180)
                    bars = daily_bars(client.chart_daily(symbol, start, now.date()), symbol, start, now)
                    until = None
                else:
                    bars, until = _minute_snapshot(client, symbol, now, cached.snapshot.get("bars"), cached.minute_until)
                if not bars:
                    raise KisError("조회 가능한 차트 데이터가 없습니다. 종목·거래시간을 확인하세요.")
                if cached.snapshot.get("as_of") and bars[-1]["time"][:10] < cached.snapshot["as_of"]:
                    raise KisError("차트 응답의 거래일이 기존 기록보다 이릅니다. 다시 조회하세요.")
                name = None
                try:
                    name = self.symbol_names.lookup((symbol,)).get(symbol)
                except Exception:
                    pass
                result = {**empty, "status": "ok", "error": None, "bars": bars,
                          "as_of": bars[-1]["time"][:10],
                          "updated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds")}
                if name:
                    result["name"] = name
                cached.observed_at = now
                cached.minute_until = until
                try:
                    self.disk_cache.write(identity, {"symbol": symbol, "kind": kind, "bars": bars,
                                                     "observed_at": now.astimezone(timezone.utc).isoformat(),
                                                     "minute_until": until.isoformat() if until else None})
                except Exception:
                    pass  # 캐시 저장 장애가 정상 API 차트 표시를 막지 않는다.
            except KisError as error:
                result = {**(cached.snapshot or empty), "status": "error", "error": str(error),
                          "stale": bool(cached.snapshot.get("bars"))}
            except Exception:
                result = {**(cached.snapshot or empty), "status": "error",
                          "error": "차트를 조회하지 못했습니다. 잠시 후 다시 조회하세요.",
                          "stale": bool(cached.snapshot.get("bars"))}
            cached.snapshot = result
            cached.next_attempt = self.clock() + (10 if result["error"] else 60 if interval == "day" else 30)
            return _present(cached, interval)
