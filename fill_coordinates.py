"""category.latitude / longitude를 채우기 위한 UPDATE SQL 생성기 (Open-Meteo Geocoding API).

이 스크립트는 DB에 쓰지 않는다 (프로젝트의 읽기 전용 원칙). 읽기 전용 계정으로 대상을 조회하고,
확실하게 매칭된 행만 UPDATE 문으로 파일에 저장한다. 내용을 확인한 뒤 root 계정으로 직접 적용할 것.

사용 (대상을 반드시 지정):
  python fill_coordinates.py --parent-id 10          # id 10의 직계 하위 카테고리 (예: 일본의 도시들)
  python fill_coordinates.py --ids 58,61             # 특정 카테고리
  python fill_coordinates.py --all                   # 좌표가 비어 있는 전체 (최상위 제외, 음식 같은 비장소 카테고리는 '찾을 수 없음'으로 걸러짐)

적용:
  docker exec -i py-mcp-mysql mysql -uroot -proot mcp < sql/fill_coordinates.sql

매칭 규칙
  - en_name과 이름이 정확히 같은 지명만 후보로 본다 (대소문자 무시, 퍼지 매칭 안 함).
  - 도시급(feature_code PPL*)을 우선하고, 없으면 행정구역(ADM*)을 쓴다.
  - 부모 카테고리의 en_name이 후보의 국가/행정구역과 같으면 그 후보로 좁힌다.
  - 후보가 하나이거나, 1순위 인구가 2순위의 2배 이상이면 확정(ok). 아니면 애매함(ambiguous)이라 SQL에 넣지 않는다.
위치 데이터 출처: GeoNames (Open-Meteo Geocoding). 무료 API는 비상업적 용도 전용.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import db

FREE_URL = "https://geocoding-api.open-meteo.com/v1/search"
COMMERCIAL_URL = "https://customer-geocoding-api.open-meteo.com/v1/search"
REQUEST_INTERVAL = 0.3  # 초. 호출 간격


def _endpoint() -> tuple[str, dict]:
    url = os.environ.get("OPEN_METEO_GEOCODING_URL")
    key = os.environ.get("OPEN_METEO_API_KEY")
    if url:
        return url, ({"apikey": key} if key else {})
    if key:
        return COMMERCIAL_URL, {"apikey": key}
    return FREE_URL, {}


def search(name: str, count: int = 10) -> list[dict]:
    base, extra = _endpoint()
    params = {"name": name, "count": count, "language": "en", "format": "json", **extra}
    req = urllib.request.Request(f"{base}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "py-mcp/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("OPEN_METEO_TIMEOUT", "10"))) as r:
            return json.load(r).get("results") or []
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(str(e)) from e


def pick(en_name: str, parent_name: str | None, results: list[dict]) -> tuple[str, list[dict]]:
    """(status, 후보들). status: ok | ambiguous | not_found. ok면 후보의 첫 번째가 선택된 값."""
    n = en_name.strip().casefold()
    exact = [r for r in results if str(r.get("name", "")).casefold() == n and r.get("latitude") is not None]
    places = [r for r in exact if str(r.get("feature_code", "")).startswith("PPL")]
    cands = places or [r for r in exact if str(r.get("feature_code", "")).startswith("ADM")]

    if parent_name and cands:
        p = parent_name.strip().casefold()
        scoped = [r for r in cands if p in {str(r.get(k, "")).casefold() for k in ("country", "country_code", "admin1", "admin2")}]
        cands = scoped or cands  # 부모가 국가가 아니면(예: 대륙) 좁히지 않는다

    if not cands:
        return "not_found", []
    cands = sorted(cands, key=lambda r: r.get("population") or 0, reverse=True)
    if len(cands) == 1:
        return "ok", cands
    top, second = cands[0].get("population") or 0, cands[1].get("population") or 0
    return ("ok" if top > 0 and top >= 2 * second else "ambiguous"), cands


def load_targets(ids: list[int] | None, parent_id: int | None) -> list[dict]:
    sql = (
        "SELECT c.category_id AS id, c.name, c.en_name, p.en_name AS parent_en_name "
        "FROM category c JOIN category p ON p.category_id = c.parent_id "
        "WHERE c.parent_id IS NOT NULL AND (c.latitude IS NULL OR c.longitude IS NULL) AND c.en_name <> ''"  # 최상위는 사용하지 않으므로 제외
    )
    params: list = []
    if ids:
        sql += f" AND c.category_id IN ({','.join(['%s'] * len(ids))})"
        params += ids
    if parent_id is not None:
        sql += " AND c.parent_id = %s"
        params.append(parent_id)
    return db.query(sql + " ORDER BY c.category_id", tuple(params), limit=5000)


def describe(r: dict) -> str:
    return f"{r.get('name')} / {r.get('admin1', '-')} / {r.get('country_code', '-')} / pop={r.get('population', '-')}"


def main() -> int:
    ap = argparse.ArgumentParser(description="category 좌표 채우기용 UPDATE SQL 생성")
    ap.add_argument("--ids", help="쉼표로 구분한 category id")
    ap.add_argument("--parent-id", type=int, help="이 id의 직계 하위 카테고리")
    ap.add_argument("--all", action="store_true", help="좌표가 비어 있는 전체 카테고리")
    ap.add_argument("--out", default="sql/fill_coordinates.sql")
    a = ap.parse_args()

    ids = [int(x) for x in a.ids.split(",") if x.strip()] if a.ids else None
    if not (ids or a.parent_id is not None or a.all):
        ap.error("--ids, --parent-id, --all 중 하나를 지정하세요 (비장소 카테고리를 실수로 채우지 않기 위해).")

    targets = load_targets(ids, a.parent_id)
    if not targets:
        print("대상이 없습니다 (좌표가 이미 채워졌거나 조건에 맞는 카테고리가 없음).")
        return 0

    lines, counts = [], {"ok": 0, "ambiguous": 0, "not_found": 0, "error": 0}
    for i, t in enumerate(targets):
        label = f"[{t['id']}] {t['en_name']} ({t['name']})"
        try:
            status, cands = pick(t["en_name"], t["parent_en_name"], search(t["en_name"]))
        except RuntimeError as e:
            counts["error"] += 1
            print(f"ERROR      {label}: {e}")
            continue
        counts[status] += 1
        if status == "ok":
            c = cands[0]
            print(f"OK         {label} -> {c['latitude']:.4f}, {c['longitude']:.4f}  ({describe(c)})")
            lines.append(
                f"UPDATE category SET latitude = {c['latitude']:.7f}, longitude = {c['longitude']:.7f} "
                f"WHERE category_id = {int(t['id'])} AND latitude IS NULL AND longitude IS NULL;  -- {t['en_name']} ({describe(c)})"
            )
        elif status == "ambiguous":
            print(f"AMBIGUOUS  {label}: 직접 확인 필요")
            for c in cands[:5]:
                print(f"             - {c['latitude']:.4f}, {c['longitude']:.4f}  ({describe(c)})")
        else:
            print(f"NOT_FOUND  {label}")
        if i < len(targets) - 1:
            time.sleep(REQUEST_INTERVAL)

    if lines:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("-- Location data based on GeoNames (Open-Meteo Geocoding API). 내용을 확인한 뒤 적용할 것.\n")
            f.write("-- 적용: docker exec -i py-mcp-mysql mysql -uroot -proot mcp < " + a.out + "\n\n")
            f.write("\n".join(lines) + "\n")
    print(f"\n확정 {counts['ok']} / 애매함 {counts['ambiguous']} / 못 찾음 {counts['not_found']} / 오류 {counts['error']}")
    if lines:
        print(f"UPDATE {len(lines)}건을 {a.out}에 저장했습니다. 내용을 확인한 뒤 적용하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
