"""브라우저용 HTTP 게이트웨이.

브라우저는 stdio MCP 서버와 직접 통신할 수 없으므로, 이 프로세스가 MCP 클라이언트가 되어 중간에서 이어준다.

  브라우저 --HTTP--> web.py (MCP 클라이언트) --stdio--> server.py (MCP 서버) --> MySQL / Open-Meteo / LLM

실행:  python web.py   → http://127.0.0.1:8000

환경변수 (모두 선택)
  WEB_HOST (기본 127.0.0.1)   외부에 열면 LLM 호출 비용이 노출되므로 인증/프록시 없이 0.0.0.0으로 열지 말 것
  WEB_PORT (기본 8000)
  WEB_RATE_PER_MIN (기본 20)  IP당 분당 검색 횟수 제한
  MCP_SERVER_CMD              MCP 서버 실행 명령 (기본: 현재 python + server.py)
"""
import contextlib
import json
import os
import shlex
import sys
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
MAX_QUERY_CHARS = 200
MAX_PAGE = 50
RATE_PER_MIN = int(os.environ.get("WEB_RATE_PER_MIN", "20"))

# 모드 → (MCP 도구 이름, 검색어 인자 이름)
TOOLS = {"recommend": ("recommend_products", "text"), "keyword": ("search_products", "keyword")}

_hits: dict[str, deque] = defaultdict(deque)


def _server_params() -> StdioServerParameters:
    cmd = shlex.split(os.environ.get("MCP_SERVER_CMD", "")) or [sys.executable, str(HERE / "server.py")]
    # 자식 프로세스에 현재 환경변수(LLM_PROVIDER, API 키 등)를 그대로 넘긴다. DB 접속 정보는 server가 .env에서도 읽는다.
    return StdioServerParameters(command=cmd[0], args=cmd[1:], env=dict(os.environ), cwd=str(HERE))


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async with contextlib.AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(_server_params()))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        app.state.session = session
        app.state.tools = {t.name for t in (await session.list_tools()).tools}  # tools/list
        missing = {name for name, _ in TOOLS.values()} - app.state.tools
        if missing:
            raise RuntimeError(f"MCP 서버에 필요한 도구가 없습니다: {sorted(missing)}")
        yield


def _rate_limited(ip: str) -> bool:
    now, q = time.monotonic(), _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        return True
    q.append(now)
    return False


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def index(request: Request) -> HTMLResponse:
    return HTMLResponse((HERE / "static" / "index.html").read_text(encoding="utf-8"))


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "tools": sorted(request.app.state.tools)})


async def search(request: Request) -> JSONResponse:
    p = request.query_params
    q, mode = p.get("q", "").strip(), p.get("mode", "recommend")
    date = p.get("date", "").strip()
    if mode not in TOOLS:
        return _error(400, "mode는 recommend 또는 keyword 입니다.")
    if not q or len(q) > MAX_QUERY_CHARS:
        return _error(400, f"검색어는 1~{MAX_QUERY_CHARS}자로 입력하세요.")
    try:
        page = int(p.get("page", "1"))
    except ValueError:
        return _error(400, "page는 숫자여야 합니다.")
    if not 1 <= page <= MAX_PAGE:
        return _error(400, f"page는 1~{MAX_PAGE} 범위여야 합니다.")
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return _error(400, "date는 YYYY-MM-DD 형식이어야 합니다.")
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        return _error(429, "요청이 너무 많습니다. 잠시 후 다시 시도하세요.")

    tool, arg = TOOLS[mode]
    try:
        result = await request.app.state.session.call_tool(tool, {arg: q, "page": page, "date": date})  # tools/call
    except Exception as e:  # MCP 연결 문제 등
        return _error(502, f"MCP 서버 호출 실패: {e}")

    text = "".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))
    if getattr(result, "isError", False):
        return _error(422, text or "도구 실행 중 오류가 발생했습니다.")
    try:
        return JSONResponse(json.loads(text))
    except json.JSONDecodeError:
        return _error(502, "MCP 응답을 해석할 수 없습니다.")


app = Starlette(
    routes=[Route("/", index), Route("/api/health", health), Route("/api/search", search)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("WEB_HOST", "127.0.0.1"), port=int(os.environ.get("WEB_PORT", "8000")))
