"""MySQL 접속 공용 모듈 (읽기 전용 세션).

접속 정보는 환경변수로 주입한다 (프로젝트 루트의 .env가 있으면 자동 로드).
  MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB
"""
import logging
import os
import sys

import pymysql
import pymysql.cursors

try:  # python-dotenv가 설치되어 있으면 .env를 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다.
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# stdio MCP 서버는 stdout이 프로토콜 채널이므로 로그는 반드시 stderr로 보낸다.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("py-mcp")

MAX_ROWS = 100
QUERY_TIMEOUT_MS = 5000


def get_conn():
    conn = pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "4406")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DB"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        read_timeout=10,
        autocommit=True,
    )
    with conn.cursor() as cur:
        # 세션 단위로 읽기 전용 + 쿼리 실행 시간 제한 (이중 안전장치)
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute(f"SET SESSION MAX_EXECUTION_TIME={QUERY_TIMEOUT_MS}")
    return conn


def query(sql: str, params: tuple = (), limit: int = MAX_ROWS) -> list[dict]:
    log.info("query: %s | params=%s", " ".join(sql.split()), params)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchmany(limit))
    finally:
        conn.close()
