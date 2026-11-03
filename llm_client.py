"""LLM 클라이언트 (Claude / Ollama 선택).

환경변수
  LLM_PROVIDER     claude(기본) | ollama
  ANTHROPIC_API_KEY, ANTHROPIC_MODEL (기본 claude-haiku-5-5)           - claude
  OLLAMA_HOST (기본 http://127.0.0.1:11434, `0.0.0.0` 같은 스킴 없는 값도 허용), OLLAMA_MODEL (기본 llama3.1:8b) - ollama

두 클라이언트 모두 같은 인터페이스를 가진다.
  classify(system, text, schema) -> dict   # schema(JSON Schema)에 맞는 구조화된 결과
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import db  # .env 로드 및 로깅 설정


class LLMError(RuntimeError):
    pass


def normalize_ollama_host(value: str | None) -> str:
    """OLLAMA_HOST를 호출 가능한 URL로 정규화한다.

    Ollama 자체는 OLLAMA_HOST를 `0.0.0.0`, `127.0.0.1:11434`처럼 스킴 없이 쓰므로 같은 값도 받아 준다.
    - 스킴이 없으면 http://를 붙이고, 포트가 없으면 11434를 쓴다.
    - 서버 바인드용 주소(0.0.0.0, [::])는 접속용 127.0.0.1로 바꾼다.
    """
    value = (value or "").strip() or "127.0.0.1"
    if "://" not in value:
        value = "http://" + value
    u = urllib.parse.urlsplit(value)
    host = u.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:  # IPv6 리터럴
        host = f"[{host}]"
    return f"{u.scheme}://{host}:{u.port or 11434}"


class ClaudeClient:
    name = "claude"

    def __init__(self):
        import anthropic

        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
        self._client = anthropic.Anthropic()

    def classify(self, system: str, text: str, schema: dict) -> dict:
        tool = {
            "name": "submit_result",
            "description": "분류 결과를 기록한다.",
            "input_schema": schema,
        }
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=512,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_result"},
            messages=[{"role": "user", "content": text}],
        )
        return next((b.input for b in resp.content if b.type == "tool_use"), {})


class OllamaClient:
    name = "ollama"

    def __init__(self):
        self.host = normalize_ollama_host(os.environ.get("OLLAMA_HOST"))
        self.model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
        self.timeout = float(os.environ.get("OLLAMA_TIMEOUT", "60"))

    def classify(self, system: str, text: str, schema: dict) -> dict:
        # Ollama의 structured outputs(format=JSON Schema)로 출력 형식을 강제한다.
        body = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        }
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.load(r)
        except urllib.error.URLError as e:
            raise LLMError(f"Ollama({self.host}) 호출 실패: {e}") from e
        try:
            return json.loads(payload["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise LLMError(f"Ollama 응답을 해석할 수 없습니다: {payload!r:.300}") from e


_PROVIDERS = {"claude": ClaudeClient, "ollama": OllamaClient}


def get_client():
    provider = os.environ.get("LLM_PROVIDER", "claude").strip().lower()
    if provider not in _PROVIDERS:
        raise LLMError(f"지원하지 않는 LLM_PROVIDER: {provider!r} (claude | ollama)")
    return _PROVIDERS[provider]()
