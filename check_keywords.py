"""키워드별 검색 결과가 정상인지 확인하는 점검 스크립트 (실제 DB/날씨/LLM을 사용한다).

사용 예
  python check_keywords.py 오사카 도쿄                     # 키워드 모드로 점검
  python check_keywords.py "오사카=오사카" "도쿄=도쿄"      # '키워드=기대 카테고리명' (이름/영문명 일치)
  python check_keywords.py --file tests/keywords.txt        # 파일: 한 줄에 '키워드' 또는 '키워드=기대 카테고리'
  python check_keywords.py --mode recommend 오사카=오사카   # 추천 모드 (LLM 호출)
  python check_keywords.py --mode both --date 2026-11-10 오사카
  python check_keywords.py --url http://127.0.0.1:8000 오사카   # 실행 중인 web.py(HTTP)를 통해 점검

점검 항목: 예외 없음 / 카테고리 매칭(+기대값) / 상품 존재 / 점수 내림차순 / 상품 id 중복 없음 / 페이지 크기 / 
          최상위(parent_id NULL) 카테고리 미사용 / (추천) nearby 구조
종료 코드: 모두 통과 0, 하나라도 실패 1.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def parse_specs(args) -> list[tuple[str, str | None]]:
    raw = list(args.keywords)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            raw += [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    specs = []
    for r in raw:
        kw, _, exp = r.partition("=")
        specs.append((kw.strip(), exp.strip() or None))
    return [s for s in specs if s[0]]


def run_local(mode: str, kw: str, date: str | None, page: int) -> dict:
    import recommend as rec  # 지연 import: --url 모드에서는 DB 설정이 필요 없다
    if mode == "keyword":
        return rec.search_by_keyword(kw, page, date)
    return rec.recommend(kw, page, date)


def run_http(base: str, mode: str, kw: str, date: str | None, page: int) -> dict:
    q = {"q": kw, "mode": mode, "page": str(page)}
    if date:
        q["date"] = date
    url = f"{base.rstrip('/')}/api/search?{urllib.parse.urlencode(q)}"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from e


def validate(res: dict, expected: str | None, page_size: int = 5) -> list[str]:
    """실패 사유 목록 (비어 있으면 통과)."""
    errs = []
    matched = res.get("matched_categories") or []
    items = res.get("items") or []
    if not matched:
        errs.append("매칭된 카테고리가 없음: " + "; ".join(res.get("messages") or []))
    names = {n.casefold() for c in matched for n in (c.get("name", ""), c.get("en_name", ""))}
    if expected and expected.casefold() not in names:
        errs.append(f"기대 카테고리 '{expected}' 없음 (실제: {[c.get('name') for c in matched]})")
    if matched and not items:
        errs.append("상품이 없음: " + "; ".join(res.get("messages") or []))
    scores = [i.get("score") for i in items]
    if any(s is None for s in scores):
        errs.append("score 누락")
    elif scores != sorted(scores, reverse=True):
        errs.append(f"점수 내림차순 아님: {scores}")
    ids = [i.get("id") for i in items]
    if len(ids) != len(set(ids)):
        errs.append(f"상품 id 중복: {ids}")
    if len(items) > page_size:
        errs.append(f"페이지 크기 초과: {len(items)} > {page_size}")
    for c in matched:
        if c.get("parent_id", 1) is None:  # 방어적 검사 (공개 필드에는 보통 parent_id가 없다)
            errs.append(f"최상위 카테고리 사용됨: {c.get('name')}")
    if res.get("mode") == "recommend":
        nb = res.get("nearby")
        if res.get("page", 1) <= 1 and (not isinstance(nb, dict) or "regions" not in nb):
            errs.append("추천 1페이지에 nearby 구조 없음")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keywords", nargs="*", help="'키워드' 또는 '키워드=기대 카테고리명'")
    ap.add_argument("--file", help="키워드 목록 파일 (# 주석 가능)")
    ap.add_argument("--mode", choices=["keyword", "recommend", "both"], default="keyword")
    ap.add_argument("--date", help="YYYY-MM-DD (없으면 최근 7일 날씨 기준)")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--url", help="web.py 주소. 지정하면 HTTP로 점검한다")
    ap.add_argument("--verbose", "-v", action="store_true", help="상품 목록과 메시지까지 출력")
    args = ap.parse_args()

    specs = parse_specs(args)
    if not specs:
        ap.error("키워드를 지정하세요.")
    modes = ["keyword", "recommend"] if args.mode == "both" else [args.mode]

    failed = 0
    for mode in modes:
        for kw, expected in specs:
            try:
                res = run_http(args.url, mode, kw, args.date, args.page) if args.url else run_local(mode, kw, args.date, args.page)
                errs = validate(res, expected)
            except Exception as e:  # noqa: BLE001 - 점검 도구이므로 모든 예외를 실패로 기록
                res, errs = {}, [f"예외: {type(e).__name__}: {e}"]
            ok = not errs
            failed += not ok
            cats = ",".join(f"{c['name']}({c['id']})" for c in res.get("matched_categories", [])) or "-"
            top = (res.get("items") or [{}])[0]
            print(f"[{'PASS' if ok else 'FAIL'}] {mode:<9} {kw!r:<16} 카테고리={cats} 상품={len(res.get('items') or [])}개"
                  + (f" 1위={top.get('name', '')[:30]!r}({top.get('score')})" if top else ""))
            for e in errs:
                print(f"        - {e}")
            if args.verbose and res:
                for i in res.get("items", []):
                    print(f"        · {i['id']} {i['score']:>8} {i['name'][:50]}")
                for m in res.get("messages", []):
                    print(f"        ! {m}")
                nb = res.get("nearby")
                if nb:
                    print(f"        ~ nearby: {[(r['category']['name'], r['category']['distance_km']) for r in nb.get('regions', [])]} {nb.get('message', '')}")
    total = len(specs) * len(modes)
    print(f"\n{total - failed}/{total} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
