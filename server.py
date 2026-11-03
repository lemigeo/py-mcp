"""py-mcp: 상품 추천용 MySQL 읽기 전용 MCP 서버

설치:  pip install -r requirements.txt
실행:  mcp dev server.py   (MCP Inspector로 테스트)

접속 정보는 .env 또는 환경변수로 주입한다 (db.py 참고).
"""
import re

from mcp.server.fastmcp import FastMCP

import db
import recommend as rec

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
    """상품 카테고리 전체(id, parent_id, name, en_name)를 반환한다. 사용자 요청에 맞는 카테고리를 고를 때 먼저 호출할 것."""
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
def recommend_products(text: str, page: int = 1) -> dict:
    """사용자 텍스트에서 AI가 카테고리(1개 이상)를 유추해 상품을 score 내림차순으로 5개씩 추천한다.

    서버에 LLM 설정(LLM_PROVIDER 등)이 되어 있어야 한다. 호출하는 쪽 모델이 직접 카테고리를 고를 수 있으면
    list_categories + get_products_by_categories 조합을 쓰는 편이 더 저렴하다.
    """
    return rec.recommend(text, page)


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
