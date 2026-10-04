"""대회 대상 전체의 완료된 일봉을 같은 거래일 창으로 비교한다."""

from copy import deepcopy
from datetime import datetime, time as daytime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import tempfile
import threading

from backend.chart import _date, _ohlcv, _rows
from backend.candidate_features import candidate_features
from backend.candidate_history import HistoryCache
from backend.kis import KST, KisError, ROOT, _quote_number, client_for_profile, load_profiles
from backend.market import load_market_settings
from backend.request_gate import file_lock
from backend.universe import load_universe

METRICS = ("close", "return_5d_pct", "return_20d_pct", "excess_5d_pp", "excess_20d_pp",
           "avg_turnover_20d", "turnover_ratio")
BENCHMARKS = {"KOSPI": "0001", "KOSDAQ": "1001"}


class InsufficientHistory(KisError):
    pass


def completed_cutoff(now):
    now = now.astimezone(KST)
    return now.date() if now.time() >= daytime(16) else now.date() - timedelta(days=1)


def index_series(rows, start, end, minimum=21):
    if not isinstance(rows, list) or len(rows) > 100:
        raise KisError("지수 일봉 응답 형식이 올바르지 않습니다.")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise KisError("지수 일봉 응답 형식이 올바르지 않습니다.")
        day = _date(row.get("stck_bsop_date"))
        if not start <= day <= end or day in result:
            raise KisError("지수 일봉 날짜가 중복되거나 조회 범위를 벗어났습니다.")
        result[day] = Decimal(_quote_number(row.get("bstp_nmix_prpr"), positive=True))
    if len(result) < minimum:
        raise KisError(f"비교에 필요한 지수 일봉 {minimum}거래일이 부족합니다.")
    return result


def stock_series(data, symbol, start, end):
    result = {}
    for row in _rows(data, symbol):
        day = _date(row.get("stck_bsop_date"))
        if not start <= day <= end or day in result:
            raise KisError("종목 일봉 날짜가 중복되거나 조회 범위를 벗어났습니다.")
        values = _ohlcv(row, minute=False)
        result[day] = {key: Decimal(values[key]) for key in ("open", "high", "low", "close", "volume")}
        result[day]["turnover"] = Decimal(_quote_number(row.get("acml_tr_pbmn")))
    return result


def calculate_metrics(stock, benchmark, days):
    """days는 해당 시장 지수의 연속된 21거래일. 결측을 압축하거나 보간하지 않는다."""
    if len(days) != 21 or days != sorted(set(days)):
        raise KisError("비교 거래일 구성이 올바르지 않습니다.")
    if any(day not in stock for day in days):
        raise InsufficientHistory("기준일과 이전 20거래일의 일봉이 모두 필요합니다.")
    if any(day not in benchmark for day in days):
        raise KisError("비교 지수의 거래일이 맞지 않습니다.")
    rows = [stock[day] for day in days]
    close = rows[-1]["close"]
    if rows[-1]["volume"] == 0:
        raise InsufficientHistory("기준일 거래량이 없습니다.")
    previous_turnover = sum(row["turnover"] for row in rows[:-1]) / 20
    result = {"close": format(close, "f"),
              "avg_turnover_20d": format(sum(row["turnover"] for row in rows[1:]) / 20, ".2f"),
              "turnover_ratio": format(rows[-1]["turnover"] / previous_turnover, ".4f")
              if previous_turnover else None}
    for length in (5, 20):
        change = (close / rows[-length - 1]["close"] - 1) * 100
        market = (benchmark[days[-1]] / benchmark[days[-length - 1]] - 1) * 100
        result[f"return_{length}d_pct"] = format(change, ".4f")
        result[f"excess_{length}d_pp"] = format(change - market, ".4f")
    return result


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json(path, limit=4 * 1024 * 1024):
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("파일 크기 초과")
    return json.loads(content)


def _identity(universe):
    return hashlib.sha256(json.dumps([universe.get("as_of"), universe.get("source_url"),
                                     universe.get("rows", [])], sort_keys=True).encode()).hexdigest()


def _metadata(universe):
    return {key: universe.get(key) for key in
            ("status", "as_of", "checked_at", "source_url", "count", "error")}


def _valid_screening(screening, rows):
    """복원된 선별 메타가 깨져도 검증된 가격 비교는 재조회할 수 있게 남긴다."""
    states = ("selected", "reserve", "excluded", "unverified")
    try:
        if (not isinstance(screening, dict) or screening["status"] not in ("ready", "error")
                or any(not isinstance(screening[key], str) for key in ("policy_id", "label", "score_label", "score_unit"))
                or not screening["policy_id"]
                or not isinstance(screening["criteria"], list)
                or any(not isinstance(value, str) for value in screening["criteria"])
                or screening["error"] is not None and not isinstance(screening["error"], str)
                or not isinstance(screening["counts"], dict)):
            return False
        for key in ("checked_at", "master_observed_at"):
            value = screening[key]
            if value is None and key == "master_observed_at":
                continue
            if not isinstance(value, str) or datetime.fromisoformat(value).utcoffset() is None:
                return False
        counts = dict.fromkeys(states, 0)
        ranks = []
        for row in rows:
            selection = row["selection"]
            if (not isinstance(selection, dict) or selection["status"] not in states
                    or not isinstance(selection["reasons"], list)
                    or any(not isinstance(value, str) for value in selection["reasons"])):
                return False
            rank, score = selection["rank"], selection["score"]
            if rank is not None and (type(rank) is not int or not 1 <= rank <= 2 ** 53 - 1):
                return False
            if score is not None and (not isinstance(score, str) or len(score) > 128
                    or not Decimal(score).is_finite() or not math.isfinite(float(score))):
                return False
            if selection["status"] == "selected":
                if rank is None or score is None or row["status"] != "ok":
                    return False
                ranks.append(rank)
            counts[selection["status"]] += 1
        return (counts["selected"] <= 20 and sorted(ranks) == list(range(1, counts["selected"] + 1))
                and all(type(screening["counts"].get(key)) is int and screening["counts"][key] == count
                        for key, count in counts.items()))
    except Exception:
        return False


class CandidateService:
    def __init__(self, config_path=ROOT / "config.local.toml", *, universe_loader=load_universe,
                 client_factory=client_for_profile, directory=None, now=None, selector=None):
        self.config_path = Path(config_path)
        self.directory = Path(directory) if directory is not None else self.config_path.parent / ".local" / "candidates"
        self.universe_loader = universe_loader
        self.client_factory = client_factory
        self.now = now or (lambda: datetime.now(KST))
        if selector is None:
            from backend.candidate_selection import CandidateSelector
            selector = CandidateSelector(now=self.now)
        self.selector = selector
        self.history = HistoryCache(self.directory / "history")
        self.lock = threading.RLock()
        self.worker = None
        self.identity = None
        self.state = {"status": "idle", "error": None, "updated_at": None, "as_of": None,
                      "stale": False, "progress": {"completed": 0, "total": 0}, "rows": []}
        self._restore()

    def _universe(self):
        try:
            return self.universe_loader()
        except Exception:
            return {"status": "unverified", "as_of": None, "checked_at": None,
                    "source_url": None, "count": 0, "rows": [],
                    "error": "대회 대상 종목 목록을 읽지 못했습니다."}

    def _restore(self):
        try:
            saved = _read_json(self.directory / "latest.json")
            universe = self._universe()
            if (universe.get("status") != "verified" or saved["version"] != 1
                    or saved["identity"] != _identity(universe)):
                return
            state = saved["state"]
            expected = {row["symbol"]: row for row in universe["rows"]}
            if (state["status"] != "complete" or not isinstance(state["rows"], list)
                    or len(state["rows"]) != len(expected)
                    or {row["symbol"] for row in state["rows"]} != set(expected)
                    or state["progress"] != {"completed": len(expected), "total": len(expected)}):
                return
            datetime.fromisoformat(state["updated_at"])
            datetime.strptime(state["as_of"], "%Y-%m-%d")
            requested = datetime.strptime(state["requested_through"], "%Y-%m-%d").date()
            if state["as_of"] > requested.isoformat():
                return
            for row in state["rows"]:
                if (row["status"] not in {"ok", "excluded", "error"}
                        or any(row[key] != expected[row["symbol"]][key] for key in ("name", "board"))
                        or row["as_of"] != state["as_of"]
                        or (row["error"] is not None and not isinstance(row["error"], str))):
                    return
                for key in METRICS:
                    value = row[key]
                    if value is not None and (not isinstance(value, str) or not Decimal(value).is_finite()):
                        return
                    if row["status"] != "ok" and value is not None:
                        return
                    if row["status"] == "ok" and key != "turnover_ratio" and value is None:
                        return
                if row["status"] == "ok" and (Decimal(row["close"]) <= 0
                        or Decimal(row["avg_turnover_20d"]) < 0
                        or row["turnover_ratio"] is not None and Decimal(row["turnover_ratio"]) < 0):
                    return
            if not _valid_screening(state.get("screening"), state["rows"]):
                state.pop("screening", None)
                for row in state["rows"]:
                    row.pop("selection", None)
            self.identity, self.state = saved["identity"], state
        except Exception:
            pass

    def snapshot(self):
        universe = self._universe()
        with self.lock:
            result = deepcopy(self.state)
            if (result.get("requested_through")
                    and result["requested_through"] < completed_cutoff(self.now()).isoformat()):
                result["stale"] = bool(result["rows"])
            if self.identity is not None and (universe.get("status") != "verified"
                                             or self.identity != _identity(universe)):
                result.update(stale=bool(result["rows"]), error="대상 종목 목록이 변경되었습니다. 전체 조회를 다시 실행하세요.")
            self._screening_freshness(result)
            return {**result, "universe": _metadata(universe)}

    def _policy(self):
        if not self.selector.enabled():
            return None, None
        policy_id, sessions = self.selector.policy_id, self.selector.history_sessions
        if not isinstance(policy_id, str) or not policy_id or sessions not in (61, 148, 253):
            raise KisError("후보 선별 기준이 올바르지 않습니다.")
        return policy_id, sessions

    def _screening_freshness(self, result):
        try:
            policy_id, _ = self._policy()
        except KisError as error:
            result.update(stale=bool(result["rows"]), error=str(error))
            return
        except Exception:
            result.update(stale=bool(result["rows"]), error="후보 선별 기준을 읽지 못했습니다.")
            return
        screening = result.get("screening")
        saved_id = screening.get("policy_id") if isinstance(screening, dict) else None
        if policy_id != saved_id:
            result.update(stale=bool(result["rows"]), error="후보 선별 기준이 변경되었습니다. 전체 조회를 다시 실행하세요.")
        elif policy_id is not None:
            try:
                observations = [screening["checked_at"], screening["master_observed_at"]]
                observations += [row["selection"]["status_observed_at"] for row in result["rows"]
                                 if row.get("selection", {}).get("status_observed_at")]
                timestamps = [datetime.fromisoformat(value) for value in observations]
                fresh = all(value.tzinfo is not None and timedelta(0) <= self.now() - value < timedelta(hours=6)
                            for value in timestamps)
            except (KeyError, ValueError, TypeError):
                fresh = False
            if not fresh:
                result.update(stale=bool(result["rows"]), error="후보 선별 상태를 다시 확인해야 합니다. 전체 조회를 다시 실행하세요.")

    def _check_policy(self, expected):
        if self._policy() != expected:
            raise KisError("조회 중 후보 선별 기준이 변경되었습니다. 전체 조회를 다시 실행하세요.")

    def start(self):
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                return self.snapshot()
            universe = self._universe()
            if universe["status"] != "verified" or not universe["rows"]:
                self.state.update(status="error", error=universe.get("error") or "대회 대상 종목 목록 확인이 필요합니다.",
                                  stale=bool(self.state["rows"]))
                return self.snapshot()
            self.state.update(status="running", error=None, stale=bool(self.state["rows"]),
                              progress={"completed": 0, "total": len(universe["rows"])})
            self.worker = threading.Thread(target=self._run, args=(deepcopy(universe),),
                                           name="candidate-comparison", daemon=True)
            self.worker.start()
            return self.snapshot()

    def _cached(self, key, end, fetch, validate, ttl_seconds=None):
        path = self.directory / "daily" / f"{key}.json"
        try:
            saved = _read_json(path)
            age = (self.now() - datetime.fromisoformat(saved["observed_at"])).total_seconds()
            if (saved["version"] == 1 and saved["end"] == end.isoformat()
                    and age >= 0 and (ttl_seconds is None or age < ttl_seconds)):
                return validate(saved["data"])
        except Exception:
            pass
        data = fetch()
        parsed = validate(data)
        _write_json(path, {"version": 1, "end": end.isoformat(), "source": "KIS paper KRX",
                           "observed_at": self.now().astimezone(timezone.utc).isoformat(), "data": data})
        return parsed

    def _run(self, universe):
        try:
            with file_lock(self.directory / "run.lock", blocking=False):
                self._collect(universe)
        except BlockingIOError:
            self._fail("다른 서버에서 후보 비교를 실행 중입니다. 완료 후 다시 조회하세요.")
        except KisError as error:
            self._fail(str(error))
        except Exception:
            self._fail("후보 비교를 완료하지 못했습니다. 설정·네트워크·로컬 저장 파일을 확인하세요.")

    def _fail(self, message):
        with self.lock:
            self.state.update(status="error", error=message, stale=bool(self.state["rows"]))

    def _collect(self, universe):
        policy = self._policy()
        selecting = policy[0] is not None
        settings = load_market_settings(self.config_path)
        profile = load_profiles(self.config_path).select(settings.account)
        profile.settings.validate_credentials()
        client = self.client_factory(profile)
        end = completed_cutoff(self.now())
        start = end - timedelta(days=180)
        indices = {}
        for board in sorted({row["board"] for row in universe["rows"]}):
            code = BENCHMARKS[board]
            indices[board] = self._cached("index-" + code, end,
                lambda code=code: client.index_daily(start.isoformat(), end.isoformat(), code),
                lambda data: index_series(data, start, end), ttl_seconds=60)
        latest = {max(series) for series in indices.values()}
        if len(latest) != 1:
            raise KisError("코스피·코스닥 지수의 최신 거래일이 다릅니다. 다시 조회하세요.")
        as_of = latest.pop()
        windows = {board: sorted(series)[-21:] for board, series in indices.items()}
        if len({tuple(days) for days in windows.values()}) != 1:
            raise KisError("코스피·코스닥 비교 기간의 거래일이 일치하지 않습니다.")
        history_start = as_of - timedelta(days=600)
        feature_indices, feature_windows, feature_errors, features = {}, {}, {}, {}
        if selecting:
            for board, series in indices.items():
                code = BENCHMARKS[board]
                try:
                    expanded = self.history.extend("index", code, series, policy[1],
                        lambda cursor, code=code: index_series(
                            client.index_daily(history_start.isoformat(), cursor.isoformat(), code),
                            history_start, cursor, minimum=0))
                    feature_indices[board] = expanded
                    feature_windows[board] = sorted(expanded)
                except KisError as error:
                    feature_errors[board] = str(error)
                except Exception:
                    feature_errors[board] = "선별용 지수 과거 이력을 조회하지 못했습니다."
            if len({tuple(days) for days in feature_windows.values()}) > 1:
                for board in feature_windows:
                    feature_errors[board] = "코스피·코스닥 선별 기간의 거래일이 일치하지 않습니다."
        rows = []
        for stock in universe["rows"]:
            symbol, board = stock["symbol"], stock["board"]
            row = {**stock, "status": "ok", "error": None, "as_of": as_of.isoformat(),
                   **dict.fromkeys(METRICS)}
            try:
                def validate_stock(data):
                    parsed = stock_series(data, symbol, start, as_of)
                    return parsed, calculate_metrics(parsed, indices[board], windows[board])

                parsed, metrics = self._cached("stock-" + symbol, as_of,
                    lambda: client.chart_daily(symbol, start, as_of),
                    validate_stock)
                row.update(metrics)
            except InsufficientHistory as error:
                row.update(status="excluded", error=str(error))
            except KisError as error:
                row.update(status="error", error=str(error))
            except Exception:
                row.update(status="error", error="종목 일봉 조회·저장에 실패했습니다.")
            if selecting:
                if row["status"] != "ok":
                    features[symbol] = {"error": row["error"]}
                elif board in feature_errors:
                    features[symbol] = {"error": feature_errors[board]}
                else:
                    try:
                        expanded = self.history.extend("stock", symbol, parsed, feature_windows[board],
                            lambda cursor: stock_series(client.chart_daily(symbol, history_start, cursor),
                                                        symbol, history_start, cursor))
                        features[symbol] = candidate_features(expanded, feature_indices[board], feature_windows[board])
                    except KisError as error:
                        features[symbol] = {"error": str(error)}
                    except Exception:
                        features[symbol] = {"error": "선별용 종목 과거 이력·지표를 계산하지 못했습니다."}
            rows.append(row)
            with self.lock:
                self.state["progress"] = {"completed": len(rows), "total": len(universe["rows"])}
        current = self._universe()
        if current.get("status") != "verified" or _identity(current) != _identity(universe):
            raise KisError("조회 중 대상 종목 목록이 변경되었습니다. 다시 조회하세요.")
        failed = sum(row["status"] == "error" for row in rows)
        result = {"status": "complete", "error": f"{failed}종목 조회 실패" if failed else None,
                  "updated_at": self.now().astimezone(timezone.utc).isoformat(timespec="seconds"),
                  "as_of": as_of.isoformat(), "requested_through": end.isoformat(), "stale": False,
                  "progress": {"completed": len(rows), "total": len(rows)}, "rows": rows}
        self._check_policy(policy)
        if selecting:
            result["screening"] = self.selector.select(rows, features, client)
            if (not _valid_screening(result["screening"], rows)
                    or result["screening"].get("policy_id") != policy[0]):
                raise KisError("후보 선별 결과의 기준이 조회 조건과 다릅니다.")
        self._check_policy(policy)
        current = self._universe()
        if current.get("status") != "verified" or _identity(current) != _identity(universe):
            raise KisError("선별 중 대상 종목 목록이 변경되었습니다. 다시 조회하세요.")
        identity = _identity(universe)
        _write_json(self.directory / "latest.json", {"version": 1, "identity": identity, "state": result})
        with self.lock:
            self.identity, self.state = identity, result
