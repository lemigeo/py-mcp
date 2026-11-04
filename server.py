"""py-mcp: 상품 추천용 MySQL 읽기 전용 MCP 서버

설치:  pip install -r requirements.txt
실행:  mcp dev server.py   (MCP Inspector로 테스트)

접속 정보는 .env 또는 환경변수로 주입한다 (db.py 참고).
"""
import re

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

import db
import recommend as rec
import weather

mcp = FastMCP("py-mcp")

# 모델에 노출할 테이블 화이트리스트
ALLOWED_TABLES = {"product", "category", "product_category", "product_score"}


@mcp.tool()
def list_tables() -> list[str]:
    """조회 가능한 테이블 이름 목록을 반환한다."""
    return sorted(ALLOWED_TABLES)


@mcp.tool()
def describe_table(table: str) -> list[dict]:
    """테이블의 컬럼 이름, 타입, 널 허용 여부를 반환한다. 쿼리 작성 전에 먼저 호출할 것."""
    if table not in ALLOWED_TABLES:
        raise ValueError(f"허용되지 않은 테이블: {table}")
    return db.query(
        "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "ORDER BY ORDINAL_POSITION",
        (table,),
    )


@mcp.tool()
def list_categories() -> list[dict]:
    """사용 가능한 카테고리(id, parent_id, name, en_name, latitude, longitude)를 반환한다. 최상위(parent_id가 NULL) 카테고리는 제외된다. 사용자 요청에 맞는 카테고리를 고를 때 먼저 호출할 것."""
    return rec.list_categories()


@mcp.tool()
def get_products_by_categories(category_ids: list[int], page: int = 1) -> dict:
    """카테고리 id(1개 이상, 하위 카테고리 포함)에 속한 상품을 score 내림차순으로 5개씩 반환한다.

    page는 1부터 시작하며 결과의 has_more가 true면 다음 페이지가 있다.
    """
    if not category_ids:
        raise ValueError("category_ids는 1개 이상이어야 합니다.")
    return rec.find_products(category_ids[:10], page)


@mcp.tool()
def search_products(keyword: str, page: int = 1, date: str = "") -> dict:
    """키워드 검색: 이름(한글/영문)에 키워드가 포함된 카테고리의 상품을 score 내림차순으로 5개씩 반환한다. AI 호출 없음.

    카테고리에 상품이 있으면 보여주고, 그 지역에 맑은 날이 없으면 messages에 안내가 들어간다.
    date(YYYY-MM-DD)가 있으면 그날, 없으면 최근 7일의 날씨가 근거다. 최상위 카테고리는 쓰지 않는다.
    """
    return rec.search_by_keyword(keyword, page, date.strip() or None)


@mcp.tool()
def recommend_products(text: str, page: int = 1, date: str = "") -> dict:
    """추천 검색: 사용자 텍스트에서 AI가 카테고리(1개 이상)를 유추해 상품을 score 내림차순으로 5개씩 반환한다.

    1페이지에는 nearby가 함께 온다: 찾은 지역과 좌표가 가까운 다른 카테고리 중 맑은 날이 있는 지역의 상품이며
    본 결과와 구분해서 보여줄 것. 찾은 지역에 맑은 날이 없으면 messages에 안내가 들어간다.
    서버에 LLM 설정(LLM_PROVIDER 등)이 되어 있어야 한다. 호출하는 쪽 모델이 직접 카테고리를 고를 수 있으면
    list_categories + get_products_by_categories 조합을 쓰는 편이 더 저렴하다.
    """
    return rec.recommend(text, page, date.strip() or None)


@mcp.tool()
def get_weather(latitude: float, longitude: float, date: str = "") -> dict:
    """좌표의 날씨와 '맑은 정도'(0~1, 높을수록 맑음)를 반환한다.

    date(YYYY-MM-DD)를 주면 그날의 날씨를 근거로 하고, 비우면 최근 7일(어제까지)의 데이터를 근거로 한다.
    date가 조회 가능 범위(과거 92일~오늘 포함 16일) 밖이면 최근 7일로 대체하며 note에 이유가 적힌다.
    basis/period로 어떤 기간을 근거로 했는지 확인할 수 있다. average_clearness는 서버가 일조 비율과
    강수확률로 계산한 값이므로 모델이 다시 계산하지 말고 그대로 사용할 것.
    """
    return weather.get_weather(latitude, longitude, date.strip() or None)


@mcp.tool()
def filter_regions_by_weather(regions: list[dict], date: str = "") -> dict:
    """지역 후보를 날씨로 걸러낸다. 맑은 날이 하나도 없는 지역은 excluded로 빠지고 추천에서 제외해야 한다.

    regions: [{"name": "Osaka", "latitude": 34.69, "longitude": 135.50}, ...] (최대 10개).
    date(YYYY-MM-DD)가 있으면 그날, 없으면 최근 7일의 데이터가 근거다 (get_weather와 동일).
    '맑은 날'은 강수확률 70% 미만이고 비/눈/뇌우/안개가 아닌 날이다.
    recommended는 맑은 정도 높은 순, failed는 조회 실패라 판단 불가인 지역이므로 추천하지 말고 별도로 알릴 것.
    """
    return weather.filter_regions(regions, date.strip() or None)


_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"into\s+outfile|load_file|sleep|benchmark)\b",
    re.IGNORECASE,
)


@mcp.tool()
def run_select(sql: str) -> list[dict]:
    """SELECT 쿼리를 직접 실행한다(최대 100행). 전용 도구로 해결되지 않을 때만 사용."""
    stmt = sql.strip().rstrip(";")
    if not stmt.lower().startswith("select") or ";" in stmt:
        raise ValueError("단일 SELECT 문만 허용됩니다.")
    if _FORBIDDEN.search(stmt):
        raise ValueError("허용되지 않는 키워드가 포함되어 있습니다.")
    return db.query(stmt)


if __name__ == "__main__":
    mcp.run()  # 기본 stdio 전송
