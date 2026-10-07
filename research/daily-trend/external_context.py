"""키 없는 FRED 현재 수정본 수집·과거 진단용 결합. 매매 필터로 사용하지 않는다.

관측일을 한국 신호일보다 엄격히 앞당겨도 당시 공표본을 복원하지 못한다.
DEXKOUS는 H.10 주간 공표 때문에 추가로 14일 지연한다. 이는 보수적 가정이며
예외적 공표 지연·FRED 반영 지연·수정을 해결하는 point-in-time 보장이 아니다.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import io
import json
import math
from datetime import date, datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SERIES = {
    "VIXCLS": {
        "label": "미국 VIX 종가", "unit": "index",
        "source": "https://fred.stlouisfed.org/series/VIXCLS",
        "release_source": "https://www.cboe.com/tradable_products/vix/vix_historical_data",
        "minimum_age_days": 1, "maximum_age_days": 10,
        "timing": "미국 관측일 < 한국 신호일. 실제 과거 FRED 반영시각 미확인.",
    },
    "DGS10": {
        "label": "미국 국채 10년 금리", "unit": "percent",
        "source": "https://fred.stlouisfed.org/series/DGS10",
        "release_source": "https://www.federalreserve.gov/feeds/H15.html",
        "minimum_age_days": 1, "maximum_age_days": 10,
        "timing": "H.15 미국 영업일 16:15 공표. 미국 관측일 < 한국 신호일.",
    },
    "DEXKOUS": {
        "label": "원/달러 뉴욕 정오 환율", "unit": "KRW_per_USD",
        "source": "https://fred.stlouisfed.org/series/DEXKOUS",
        "release_source": "https://www.federalreserve.gov/releases/h10/",
        "minimum_age_days": 14, "maximum_age_days": 35,
        "timing": "H.10 전주 관측을 다음 월요일 16:15 공표(미국 휴일 연기). 14역일 지연 가정.",
    },
}
LIMITATIONS = [
    "현재 수정본의 사후 진단이며 과거 시점 공표본(point-in-time)이 아니다.",
    "관측일과 공표일·FRED 반영일은 다르다. 지연 가정으로 수정 편향을 없앨 수 없다.",
    "외부 변수로 기존 전략을 소급 필터링·최적화·채택하지 않는다.",
    "휴장 결측은 이전 관측만 사용하고 오래된 값은 stale로 제외한다. 미래값 역채움 금지.",
]
REALTIME_SOURCE = "https://fred.stlouisfed.org/docs/api/fred/realtime_period.html"
MAX_BYTES = 5_000_000


def _date(value: str | date) -> date:
    if isinstance(value, datetime):
        raise ValueError("날짜만 사용하세요. 시간대 포함 시각은 호출자가 한국 신호일로 변환해야 합니다.")
    return value if isinstance(value, date) else date.fromisoformat(value)


def parse_csv(raw: bytes, series_id: str) -> list[dict]:
    """FRED 빈칸과 '.'는 누락이다. 중복 날짜·비정상 수치는 거부한다."""
    if series_id not in SERIES:
        raise ValueError(f"지원하지 않는 시리즈: {series_id}")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if reader.fieldnames != ["observation_date", series_id]:
        raise ValueError(f"예상과 다른 CSV 열: {reader.fieldnames}")
    rows, seen = [], set()
    for row in reader:
        observation = _date(row["observation_date"]).isoformat()
        if observation in seen:
            raise ValueError(f"중복 관측일: {observation}")
        seen.add(observation)
        text = (row[series_id] or "").strip()
        if text in ("", "."):
            continue
        value = float(text)
        if not math.isfinite(value):
            raise ValueError(f"유한하지 않은 수치: {observation}")
        rows.append({"observation_date": observation, "value": value})
    return sorted(rows, key=lambda row: row["observation_date"])


def fetch_snapshot(output_dir: str | Path, start: str, end: str,
                   timeout: float = 20.0) -> dict:
    """원본·해시·조회시각을 매번 새 하위 폴더에 보관한다. 실패도 manifest에 남긴다."""
    start_date, end_date = _date(start), _date(end)
    if start_date > end_date:
        raise ValueError("start는 end 이하여야 합니다.")
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("timeout은 0초 초과 60초 이하여야 합니다.")
    started = datetime.now(timezone.utc)
    folder = Path(output_dir).resolve() / started.strftime("snapshot-%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {
        "version": 1, "started_at_utc": started.isoformat(),
        "start": start_date.isoformat(), "end": end_date.isoformat(),
        "usage": "exploratory_diagnostics_only", "point_in_time": False,
        "limitations": LIMITATIONS, "realtime_source": REALTIME_SOURCE,
        "manifest_path": str(folder / "manifest.json"), "series": {},
    }
    for series_id, spec in SERIES.items():
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urlencode({
            "id": series_id, "cosd": start_date.isoformat(), "coed": end_date.isoformat(),
        })
        metadata = {**spec, "request_url": url, "status": "unavailable",
                    "raw_file": None, "sha256": None, "observations": 0}
        try:
            request = Request(url, headers={"User-Agent": "KIS-AI-League-research/1.0"})
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_BYTES + 1)
                metadata.update({"http_status": response.status,
                                 "response_url": response.url,
                                 "content_type": response.headers.get("Content-Type")})
            if len(raw) > MAX_BYTES:
                raise ValueError("응답 크기 제한을 초과했습니다.")
            raw_file = f"{series_id}.csv"
            (folder / raw_file).write_bytes(raw)
            metadata.update({"raw_file": raw_file, "bytes": len(raw),
                             "sha256": hashlib.sha256(raw).hexdigest()})
            rows = [row for row in parse_csv(raw, series_id)
                    if start_date.isoformat() <= row["observation_date"] <= end_date.isoformat()]
            metadata.update({"status": "ok" if rows else "no_observations",
                             "observations": len(rows),
                             "first_observation": rows[0]["observation_date"] if rows else None,
                             "last_observation": rows[-1]["observation_date"] if rows else None})
        except (OSError, HTTPException, ValueError, UnicodeError, csv.Error, KeyError, TypeError) as exc:
            metadata["error"] = f"{type(exc).__name__}: {exc}"
        metadata["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["series"][series_id] = metadata
    Path(manifest["manifest_path"]).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_snapshot(manifest_path: str | Path) -> tuple[dict, dict]:
    """원본 해시를 확인해 로드한다. 실패/변조/파싱 실패 시 시리즈를 빈 목록으로 반환한다."""
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    observations = {}
    for series_id in SERIES:
        info = manifest["series"].get(series_id, {"status": "unavailable"})
        observations[series_id] = []
        if info["status"] != "ok":
            continue
        try:
            if info["raw_file"] != f"{series_id}.csv":
                raise ValueError("예상과 다른 원본 파일명")
            raw = (path.parent / info["raw_file"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != info["sha256"]:
                raise ValueError("원본 SHA256 불일치")
            observations[series_id] = [row for row in parse_csv(raw, series_id)
                                      if manifest["start"] <= row["observation_date"] <= manifest["end"]]
        except (OSError, ValueError, UnicodeError, csv.Error, KeyError, TypeError) as exc:
            info.update({"status": "unavailable", "load_error": f"{type(exc).__name__}: {exc}"})
    return observations, manifest


def align_context(signal_dates: Iterable[str | date], observations: dict) -> list[dict]:
    """한국 장마감 신호일별 과거 관측만 결합. 반환값은 거래 가능 시점 인증이 아니다."""
    prepared = {}
    for series_id in SERIES:
        values = sorted(observations.get(series_id, []), key=lambda row: row["observation_date"])
        dates = [_date(row["observation_date"]) for row in values]
        if len(set(dates)) != len(dates):
            raise ValueError(f"중복 관측일: {series_id}")
        if any(not math.isfinite(float(row["value"])) for row in values):
            raise ValueError(f"유한하지 않은 수치: {series_id}")
        prepared[series_id] = (dates, values)
    result = []
    for signal in signal_dates:
        signal = _date(signal)
        for series_id, spec in SERIES.items():
            dates, values = prepared[series_id]
            row = {"signal_date": signal.isoformat(), "series_id": series_id,
                   "observation_date": None, "age_days": None, "value": None,
                   "change_5_observations": None, "status": "unavailable",
                   "point_in_time": False, "diagnostics_only": True}
            cutoff = date.fromordinal(signal.toordinal() - spec["minimum_age_days"])
            index = bisect.bisect_right(dates, cutoff) - 1
            if dates and index < 0:
                row["status"] = "no_prior_observation"
            if index >= 0:
                age = (signal - dates[index]).days
                assert dates[index] < signal
                row.update({"observation_date": dates[index].isoformat(), "age_days": age})
                if age > spec["maximum_age_days"]:
                    row["status"] = "stale"
                else:
                    value = float(values[index]["value"])
                    row.update({"status": "available_revised_diagnostic", "value": value})
                    if index >= 5:
                        row["change_5_observations"] = value - float(values[index - 5]["value"])
            result.append(row)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--signal-dates", help="선택: 한국 신호일 YYYY-MM-DD 쉼표 목록")
    args = parser.parse_args()
    manifest = fetch_snapshot(args.output, args.start, args.end)
    if args.signal_dates:
        observations, _ = load_snapshot(manifest["manifest_path"])
        aligned = align_context(args.signal_dates.split(","), observations)
        output = Path(manifest["manifest_path"]).parent / "context.csv"
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(aligned[0]))
            writer.writeheader()
            writer.writerows(aligned)
    print(json.dumps({"manifest_path": manifest["manifest_path"],
                      "status": {series: info["status"] for series, info in manifest["series"].items()}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
