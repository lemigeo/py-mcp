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
        db.log.debug("llm[claude] model=%s system_chars=%d input=%r", self.model, len(system), text[:200])
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=512,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_result"},
            messages=[{"role": "user", "content": text}],
        )
        out = next((b.input for b in resp.content if b.type == "tool_use"), {})
        db.log.debug("llm[claude] usage=%s output=%s", getattr(resp, "usage", None), out)
        return out


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
            "options": {"temperature": 0, **({"num_ctx": int(os.environ["OLLAMA_NUM_CTX"])} if os.environ.get("OLLAMA_NUM_CTX") else {})},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
        }
        db.log.debug("llm[ollama] host=%s model=%s system_chars=%d input=%r options=%s",
                     self.host, self.model, len(system), text[:200], body["options"])
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.load(r)
        except TimeoutError as e:  # 모델 로딩/긴 프롬프트로 응답이 늦을 때 (urlopen 타임아웃은 URLError가 아니다)
            raise LLMError(
                f"Ollama({self.host}, model={self.model}) 응답 시간 초과({self.timeout:.0f}s). "
                "모델이 로딩 중이거나 프롬프트가 길 수 있습니다. OLLAMA_TIMEOUT을 늘리거나 `ollama run <model>`로 미리 로드하세요."
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"Ollama({self.host}) 호출 실패: {e}") from e
        # prompt_eval_count가 num_ctx(기본 2048~4096)에 가깝거나 같으면 프롬프트가 잘렸을 가능성이 크다
        db.log.debug("llm[ollama] prompt_eval_count=%s eval_count=%s content=%r",
                     payload.get("prompt_eval_count"), payload.get("eval_count"), (payload.get("message") or {}).get("content"))
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
