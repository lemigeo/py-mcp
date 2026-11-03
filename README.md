# py-mcp

## 로컬 MySQL 실행

```bash
docker compose up -d
docker compose ps    # healthy 확인 (호스트 포트 4406 → 컨테이너 3306)
```

## 스키마 적용

```bash
docker exec -i py-mcp-mysql mysql -uroot -proot mcp < sql/schema.sql
```

## 읽기 전용 계정(mcp_reader) 생성

MCP 서버는 root가 아닌 SELECT 권한만 가진 계정으로 접속하는 것을 권장한다.
컨테이너가 healthy 상태가 된 뒤 아래 스크립트를 실행한다.

```bash
docker exec -i py-mcp-mysql mysql -uroot -proot <<'SQL'
CREATE USER IF NOT EXISTS 'mcp_reader'@'%' IDENTIFIED BY 'mcp_reader_pw';

-- 추천 기능용 테이블만 SELECT 허용
GRANT SELECT ON mcp.product TO 'mcp_reader'@'%';
GRANT SELECT ON mcp.category TO 'mcp_reader'@'%';
GRANT SELECT ON mcp.product_category TO 'mcp_reader'@'%';
GRANT SELECT ON mcp.product_score TO 'mcp_reader'@'%';

FLUSH PRIVILEGES;
SHOW GRANTS FOR 'mcp_reader'@'%';
SQL
```

- 위 GRANT는 테이블이 이미 존재해야 성공한다. 테이블을 먼저 만든 뒤 실행할 것.
- 비밀번호 `mcp_reader_pw`는 로컬 개발용 예시이다. 바꿨다면 `.env`도 함께 수정한다.
- 개인정보 컬럼이 있으면 해당 컬럼을 뺀 뷰(view)를 만들고 뷰에만 SELECT를 부여한다.

계정 생성 후 `.env`를 읽기 전용 계정으로 바꾼다.

```
MYSQL_USER=mcp_reader
MYSQL_PASSWORD=mcp_reader_pw
```

동작 확인:

```bash
docker exec -i py-mcp-mysql mysql -umcp_reader -pmcp_reader_pw mcp -e "SELECT 1; INSERT INTO users (id) VALUES (1);"
# SELECT는 성공, INSERT는 권한 오류(ERROR 1142)가 나와야 정상
```

## 상품 추천 기능

사용자 텍스트 → AI가 카테고리 1개 이상을 유추 → 해당 카테고리(하위 포함)의 상품을 `score` 내림차순으로 5개씩 반환한다.

```bash
pip install -r requirements.txt
# .env에서 LLM_PROVIDER와 해당 설정을 지정한다 (아래 표 참고)
python recommend.py "출출한데 먹을 거 추천해줘"
python recommend.py "출출한데 먹을 거 추천해줘" --page 2
```

- 점수는 `product_score.score`(상품당 1행)이며 없으면 0으로 본다.
- 응답의 `has_more`가 `true`면 `--page`를 올려 다음 5개를 조회한다.
- MCP 도구: `list_categories`, `get_products_by_categories`(호출 모델이 직접 카테고리를 고름), `recommend_products`(서버가 AI로 유추).

### LLM 선택 (Claude / Ollama)

`.env`의 `LLM_PROVIDER`로 카테고리 유추에 쓸 모델을 고른다. 코드는 `llm_client.py`에 있다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `LLM_PROVIDER` | `claude` | `claude` 또는 `ollama` |
| `ANTHROPIC_API_KEY` | - | claude 사용 시 필수 |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | claude 모델 |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | ollama 서버 주소 |
| `OLLAMA_MODEL` | `llama3.1:8b` | `ollama pull <모델>`로 먼저 받아 둘 것 |
| `OLLAMA_TIMEOUT` | `60` | 초 |

```bash
# Ollama 예
ollama pull llama3.1:8b
LLM_PROVIDER=ollama python recommend.py "출출한데 먹을 거 추천해줘"
```

- Ollama는 structured outputs(JSON Schema)로 출력 형식을 강제하며, 소형 모델은 카테고리를 덜 정확하게 고를 수 있다.
- 결과의 카테고리 id는 두 방식 모두 DB의 카테고리 목록으로 다시 검증한다.

## 문제 해결: `Public Key Retrieval is not allowed`

MySQL 8의 기본 인증(`caching_sha2_password`)을 SSL 없이 처음 쓰는 JDBC 클라이언트(DBeaver, DataGrip 등)에서 나는 오류이다. 아래 중 하나로 해결한다.

1. 클라이언트 접속 설정(드라이버 속성)에 `allowPublicKeyRetrieval=true`, `useSSL=false`를 추가한다. 로컬 개발에는 이 방법이 가장 간단하다.
2. 계정의 인증 방식을 바꾼다 (MySQL 8.0 한정, 8.4부터는 기본 비활성).

```bash
docker exec -i py-mcp-mysql mysql -uroot -proot -e "ALTER USER 'mcp_reader'@'%' IDENTIFIED WITH mysql_native_password BY 'mcp_reader_pw'; FLUSH PRIVILEGES;"
```

Python(PyMySQL)은 같은 상황에서 `cryptography` 패키지가 필요하다 (`requirements.txt`에 포함).
