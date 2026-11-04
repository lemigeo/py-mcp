"""Open-Meteo 예보 연동 (https://open-meteo.com/en/docs).

좌표와 날짜 범위로 일별 예보를 가져와 '맑은 정도'(0~1)를 계산한다.
점수 계산은 모델이 아니라 이 코드가 한다 (같은 입력 → 같은 결과).

환경변수 (모두 선택)
  OPEN_METEO_API_KEY    상업적 이용 시 필요. 있으면 customer-api.open-meteo.com을 apikey와 함께 호출한다.
  OPEN_METEO_BASE_URL   엔드포인트 직접 지정 (테스트/자체 호스팅용)
  OPEN_METEO_TTL        캐시 유지 시간(초), 기본 3600
  OPEN_METEO_TIMEOUT    요청 타임아웃(초), 기본 10

주의: 무료 API는 비상업적 용도 전용이다. 상업적으로 쓸 계획이면 약관(Licence/Pricing 페이지)을 확인할 것.
"""
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import db  # 로깅 설정 / .env 로드

FREE_URL = "https://api.open-meteo.com/v1/forecast"
COMMERCIAL_URL = "https://customer-api.open-meteo.com/v1/forecast"

MAX_FORECAST_DAYS = 16  # 오늘 포함 최대 16일 (문서 기준)
MAX_PAST_DAYS = 92

DAILY_VARS = [
    "weather_code",
    "sunshine_duration",
    "daylight_duration",
    "precipitation_probability_max",
    "temperature_2m_max",
    "temperature_2m_min",
]

# '흐림' 기준: 하루 최대 강수확률이 이 값(%) 이상이면 흐림으로 본다.
CLOUDY_PRECIP_PROB = 70

# 맑은 정도 = 일조 비율과 '비 안 올 확률'의 가중 평균. 흐림/비/눈/뇌우면 상한을 둔다.
W_SUNSHINE = 0.6
W_DRY = 0.4
CAP_PRECIP_CODE = 0.4   # 흐림(강수확률 70% 이상) 또는 WMO 51 이상(이슬비/비/눈/소나기)
CAP_STORM_CODE = 0.2    # WMO 95 이상(뇌우)

_cache: dict[tuple, tuple[float, dict]] = {}


class WeatherError(RuntimeError):
    pass


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def is_cloudy(precip_prob: float | None, code: int | None) -> bool:
    """흐림 여부. 강수확률이 있으면 CLOUDY_PRECIP_PROB(70%) 이상일 때 흐림.
    강수확률이 없으면 날씨 코드로 대신 판단한다 (흐림/비/눈 등: WMO 3 또는 51 이상)."""
    if precip_prob is not None:
        return precip_prob >= CLOUDY_PRECIP_PROB
    return code is not None and (code == 3 or code >= 51)


def weather_label(code: int | None, precip_prob: float | None = None) -> str:
    """WMO weather code와 강수확률 → 짧은 한국어 설명.

    비/눈/뇌우/안개는 코드 기준 그대로 쓰고, 그 외에는 강수확률 70% 이상이면 '흐림'으로 분류한다.
    """
    if code is not None:
        if code in (45, 48):
            return "안개"
        if 51 <= code <= 57:
            return "이슬비"
        if 61 <= code <= 67 or 80 <= code <= 82:
            return "비"
        if 71 <= code <= 77 or 85 <= code <= 86:
            return "눈"
        if code >= 95:
            return "뇌우"
    if is_cloudy(precip_prob, code):
        return "흐림"
    if code is None:
        return "알 수 없음"
    if code == 0:
        return "맑음"
    if code in (1, 2):
        return "대체로 맑음"
    if code == 3:
        return "구름 많음"  # 구름은 많지만 강수확률이 낮아 흐림 기준에는 미달
    return "기타"


def clearness(day: dict) -> float | None:
    """하루치 값(dict)에서 맑은 정도 0~1을 계산한다. 계산할 근거가 없으면 None."""
    sun, dl = day.get("sunshine_duration"), day.get("daylight_duration")
    pp = day.get("precipitation_probability_max")
    code = day.get("weather_code")

    parts = []
    if sun is not None and dl:
        parts.append((W_SUNSHINE, _clamp(sun / dl)))
    if pp is not None:
        parts.append((W_DRY, 1 - _clamp(pp / 100)))

    if parts:
        score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    elif code is not None:  # 일조/강수확률이 없으면 코드만으로 대략 추정
        score = 1.0 if code == 0 else 0.8 if code in (1, 2) else 0.5 if code == 3 else 0.3
    else:
        return None

    if code is not None and code >= 95:
        score = min(score, CAP_STORM_CODE)
    elif (code is not None and code >= 51) or is_cloudy(pp, code):
        score = min(score, CAP_PRECIP_CODE)
    return round(score, 3)


def is_clear_day(code: int | None, precip_prob: float | None, clear: float | None) -> bool:
    """맑은 날 = 흐리지 않고(강수확률 70% 미만) 비/눈/뇌우/안개 코드가 아닌 날. 데이터가 없으면 맑은 날로 보지 않는다."""
    if clear is None:
        return False
    if is_cloudy(precip_prob, code):
        return False
    return code is None or code <= 3


def _parse_date(value: str, name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"{name}은(는) YYYY-MM-DD 형식이어야 합니다: {value!r}") from None


def validate(lat: float, lon: float, start: str, end: str, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("위도는 -90~90, 경도는 -180~180 범위여야 합니다.")
    s, e = _parse_date(start, "start_date"), _parse_date(end, "end_date")
    if e < s:
        raise ValueError("end_date는 start_date 이후여야 합니다.")
    if s < today - timedelta(days=MAX_PAST_DAYS):
        raise ValueError(f"과거 {MAX_PAST_DAYS}일보다 이전 날짜는 조회할 수 없습니다.")
    if e > today + timedelta(days=MAX_FORECAST_DAYS - 1):
        raise ValueError(
            f"예보는 오늘 포함 {MAX_FORECAST_DAYS}일 이내만 조회할 수 있습니다 (최대 {today + timedelta(days=MAX_FORECAST_DAYS - 1)}). "
            "더 먼 날짜는 평년 기후 데이터가 필요합니다."
        )
    return s, e


def _endpoint() -> tuple[str, dict]:
    base = os.environ.get("OPEN_METEO_BASE_URL")
    key = os.environ.get("OPEN_METEO_API_KEY")
    if base:
        return base, ({"apikey": key} if key else {})
    if key:
        return COMMERCIAL_URL, {"apikey": key}
    return FREE_URL, {}


def _fetch(lat: float, lon: float, start: date, end: date) -> dict:
    base, extra = _endpoint()
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "daily": ",".join(DAILY_VARS),
        "timezone": "auto",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        **extra,
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "py-mcp/0.1"})
    timeout = float(os.environ.get("OPEN_METEO_TIMEOUT", "10"))
    db.log.info("open-meteo: %s", base)  # 키가 로그에 남지 않도록 쿼리스트링은 기록하지 않는다
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:  # 오류 시 {"error": true, "reason": "..."} 형태로 이유를 준다
            reason = json.load(e).get("reason", "")
        except Exception:
            reason = ""
        raise WeatherError(f"Open-Meteo 오류 (HTTP {e.code}): {reason or e.reason}") from e
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:  # URLError, 타임아웃, 연결 끊김 등
        raise WeatherError(f"Open-Meteo 호출 실패: {e}") from e


def get_forecast(lat: float, lon: float, start_date: str, end_date: str) -> dict:
    """일별 예보와 맑은 정도를 반환한다.

    반환: {latitude, longitude, timezone, average_clearness, days: [{date, clearness, weather, ...}]}
    """
    s, e = validate(lat, lon, start_date, end_date)
    ttl = float(os.environ.get("OPEN_METEO_TTL", "3600"))
    key = (round(lat, 2), round(lon, 2), s, e)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]

    raw = _fetch(lat, lon, s, e)
    daily = raw.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        raise WeatherError("Open-Meteo 응답에 일별 데이터가 없습니다.")

    days = []
    for i, t in enumerate(times):
        row = {k: (daily.get(k) or [None] * len(times))[i] for k in DAILY_VARS}
        sun = row["sunshine_duration"]
        days.append({
            "date": t,
            "clearness": clearness(row),
            "weather_code": row["weather_code"],
            "weather": weather_label(row["weather_code"], row["precipitation_probability_max"]),
            "is_cloudy": is_cloudy(row["precipitation_probability_max"], row["weather_code"]),
            "is_clear": is_clear_day(row["weather_code"], row["precipitation_probability_max"], clearness(row)),
            "sunshine_hours": round(sun / 3600, 1) if sun is not None else None,
            "precipitation_probability_max": row["precipitation_probability_max"],
            "temp_max": row["temperature_2m_max"],
            "temp_min": row["temperature_2m_min"],
        })
    scores = [d["clearness"] for d in days if d["clearness"] is not None]
    result = {
        "latitude": raw.get("latitude", lat),
        "longitude": raw.get("longitude", lon),
        "timezone": raw.get("timezone"),
        "average_clearness": round(sum(scores) / len(scores), 3) if scores else None,
        "cloudy_days": sum(1 for d in days if d["is_cloudy"]),
        "clear_days": sum(1 for d in days if d["is_clear"]),
        "recommendable": any(d["is_clear"] for d in days),  # 맑은 날이 하나도 없으면 False → 추천 제외
        "days": days,
    }
    _cache[key] = (time.time(), result)
    return result


RECENT_DAYS = 7


def get_weather(lat: float, lon: float, target_date: str | None = None, today: date | None = None) -> dict:
    """날씨 판단의 근거 데이터를 정책에 따라 고른다.

    - 날짜가 주어지면 그날의 날씨(예보)를 쓴다.
    - 날짜가 없으면 최근 7일(어제까지)의 데이터를 쓴다.
    - 주어진 날짜가 조회 가능 범위 밖이면 최근 7일로 대체하고 note에 이유를 남긴다.
    반환: get_forecast 결과 + basis("date" | "recent_7_days"), period, note
    """
    today = today or date.today()
    note = None

    if target_date:
        d = _parse_date(target_date, "date")  # 형식 오류는 ValueError
        lo, hi = today - timedelta(days=MAX_PAST_DAYS), today + timedelta(days=MAX_FORECAST_DAYS - 1)
        if lo <= d <= hi:
            res = get_forecast(lat, lon, d.isoformat(), d.isoformat())
            return {**res, "basis": "date", "period": [d.isoformat(), d.isoformat()], "note": None}
        note = f"요청 날짜 {d}는 조회 가능 범위({lo}~{hi})를 벗어나 최근 {RECENT_DAYS}일 데이터로 대체했다."

    start, end = today - timedelta(days=RECENT_DAYS), today - timedelta(days=1)
    res = get_forecast(lat, lon, start.isoformat(), end.isoformat())
    return {**res, "basis": f"recent_{RECENT_DAYS}_days", "period": [start.isoformat(), end.isoformat()], "note": note}


MAX_REGIONS = 10


def filter_regions(regions: list[dict], target_date: str | None = None) -> dict:
    """좌표별 날씨를 조회해 맑은 날이 하나도 없는 지역을 추천에서 제외한다.

    regions: [{"name": str, "latitude": float, "longitude": float}, ...] (최대 MAX_REGIONS개)
    날씨 근거는 get_weather와 같다 (날짜가 있으면 그날, 없으면 최근 7일).
    반환: {recommended(맑은 정도 높은 순), excluded(맑은 날 없음), failed(조회 실패, 판단 불가)}
    """
    if not regions:
        raise ValueError("regions는 1개 이상이어야 합니다.")
    if target_date:
        _parse_date(target_date, "date")  # 형식 오류는 지역별 실패가 아니라 입력 오류로 처리

    recommended, excluded, failed = [], [], []
    for r in regions[:MAX_REGIONS]:
        try:
            name, lat, lon = str(r["name"]), float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            failed.append({"region": r, "error": "name, latitude, longitude가 필요합니다."})
            continue
        try:
            res = get_weather(lat, lon, target_date)
        except (WeatherError, ValueError) as e:  # 조회 실패는 '맑은 날 없음'과 구분해 따로 알린다
            failed.append({"name": name, "latitude": lat, "longitude": lon, "error": str(e)})
            continue
        item = {
            "name": name, "latitude": lat, "longitude": lon,
            "basis": res["basis"], "period": res["period"], "note": res["note"],
            "average_clearness": res["average_clearness"],
            "clear_days": res["clear_days"], "cloudy_days": res["cloudy_days"],
            "total_days": len(res["days"]),
        }
        (recommended if res["recommendable"] else excluded).append(item)

    recommended.sort(key=lambda x: x["average_clearness"] or 0, reverse=True)
    return {"recommended": recommended, "excluded": excluded, "failed": failed}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Open-Meteo 날씨와 맑은 정도 (날짜가 있으면 그날, 없으면 최근 7일)")
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD (생략하면 최근 7일)")
    a = ap.parse_args()
    print(json.dumps(get_weather(a.lat, a.lon, a.date), ensure_ascii=False, indent=2))
