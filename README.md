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

## 상품 검색 / 추천 기능

카테고리에 맞는 상품을 `product_score.score` 내림차순으로 **5개씩** 보여준다 (`has_more`가 true면 `--page`를 올려 다음 5개). 점수가 없으면 0.

```bash
pip install -r requirements.txt
python recommend.py "출출한데 먹을 거 추천해줘"                 # 추천 검색 (AI가 카테고리 유추)
python recommend.py "Osaka" --mode keyword                    # 키워드 검색 (이름 포함 매칭, AI 호출 없음)
python recommend.py "오사카" --mode keyword --date 2026-11-06   # 그날의 날씨 기준
```

**규칙**
- **`parent_id`가 NULL인 카테고리(최상위)는 사용하지 않는다.** AI 후보, 키워드 매칭, 상품 조회, 근처 검색 모두에서 제외한다.
- **키워드/추천으로 찾은 카테고리에 상품이 있으면 노출**한다. 그 지역에 맑은 날이 없으면 상품은 그대로 보여주고 `messages`에 안내를 넣는다 (예: "오사카 지역은 최근 7일(…) 동안 맑은 날이 없습니다."). 날씨를 못 가져오면 상품은 노출하고 그 사실만 안내한다.
- **추천 검색에서는** 찾은 지역과 좌표가 가까운 다른 카테고리(반경 `NEARBY_RADIUS_KM`, 기본 150km, 가까운 순 최대 10곳) 중 **맑은 날이 있는 지역의 상품을 `nearby`로 구분해서** 준다. 지역마다 상위 5개. 찾은 카테고리와 그 하위는 제외하고, 1페이지에서만 준다. 키워드 검색에는 `nearby`가 없다.
- 맑은 날 판단과 날씨 근거(날짜가 있으면 그날, 없으면 최근 7일)는 아래 "날씨 연동" 참고.
- MCP 도구: `list_categories`, `get_products_by_categories`(호출 모델이 직접 카테고리를 고름), `search_products`(키워드), `recommend_products`(AI 유추 + nearby).

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

## 날씨 연동 (Open-Meteo)

`weather.py`가 [Open-Meteo 예보 API](https://open-meteo.com/en/docs)로 일별 예보를 가져와 **맑은 정도(0~1)**를 계산한다. MCP 도구는 `get_weather(latitude, longitude, date)`.

```bash
python weather.py 35.68 139.69 2026-11-06   # 도쿄, 그날의 날씨
python weather.py 35.68 139.69              # 날짜 생략 → 최근 7일
```

- **흐림 기준**: 하루 최대 강수확률이 **70% 이상**이면 흐림(`is_cloudy: true`, 응답의 `cloudy_days`는 흐린 날 수). 강수확률이 없으면 날씨 코드(흐림/비/눈)로 대신 판단한다. 기준값은 `weather.py`의 `CLOUDY_PRECIP_PROB`.
- 맑은 정도 = 일조 비율 60% + (1 − 강수확률) 40%. 흐림이거나 비/눈 코드(WMO 51 이상)면 최대 0.4, 뇌우(95 이상)면 최대 0.2. 가중치와 상한은 `weather.py` 상단 상수.
- **근거 데이터 정책**: 날짜가 있으면 그날의 날씨, 없으면 최근 7일(어제까지)의 데이터를 쓴다. 응답의 `basis`/`period`에 어떤 기간을 썼는지 나온다.
- 날짜가 조회 가능 범위(과거 92일 ~ 오늘 포함 16일) 밖이면 오류 대신 최근 7일로 대체하고 `note`에 이유를 적는다. 날짜 형식이 틀리면 오류.
- **맑은 날 / 추천 제외**: 맑은 날 = 흐리지 않고(강수확률 70% 미만) 비·눈·뇌우·안개 코드가 아닌 날. 근거 기간에 맑은 날이 하나도 없으면 `recommendable: false`이며 그 좌표의 지역은 추천에서 제외한다.
- MCP 도구 `filter_regions_by_weather(regions, date)`: 지역 후보(`name`, `latitude`, `longitude`, 최대 10개)를 한 번에 걸러 `recommended`(맑은 정도 높은 순) / `excluded`(맑은 날 없음) / `failed`(조회 실패, 판단 불가)로 나눈다.
- 같은 좌표·기간은 1시간 캐시한다 (`OPEN_METEO_TTL`).
- 무료 API는 **비상업적 용도 전용**이다. 상업적으로 쓰려면 `OPEN_METEO_API_KEY`를 설정한다(`customer-api.open-meteo.com`으로 호출). 약관은 Open-Meteo의 Licence/Pricing 페이지에서 확인할 것.

## category 좌표 채우기 (fill_coordinates.py)

`category.latitude/longitude`를 Open-Meteo Geocoding API(데이터 출처 GeoNames)로 채울 UPDATE SQL을 만든다. 스크립트는 DB에 쓰지 않고 SQL 파일만 생성한다.

```bash
python fill_coordinates.py --parent-id 10      # 특정 카테고리의 직계 하위(예: 일본의 도시들)
python fill_coordinates.py --ids 58,61         # 특정 id
python fill_coordinates.py --all               # 좌표가 빈 전체 (비장소 카테고리는 '못 찾음'으로 걸러짐)
# 결과를 확인한 뒤 적용
docker exec -i py-mcp-mysql mysql -uroot -proot mcp < sql/fill_coordinates.sql
```

- `en_name`과 이름이 정확히 같은 지명만 후보로 삼고, 도시급(PPL)을 우선하며 부모 카테고리의 `en_name`이 국가/행정구역과 같으면 그쪽으로 좁힌다.
- 확정(OK)만 SQL에 들어간다. `AMBIGUOUS`(동명 지명 다수)는 후보가 출력되니 직접 골라 UPDATE를 작성하고, `NOT_FOUND`는 `en_name` 철자를 확인한다.
- 이미 좌표가 있는 행은 건드리지 않는다 (`WHERE ... IS NULL`).

## 브라우저에서 사용하기 (web.py)

브라우저는 stdio MCP 서버와 직접 통신할 수 없으므로 `web.py`가 MCP 클라이언트로서 중간에서 이어준다.

```bash
python web.py        # http://127.0.0.1:8000
```

- `/api/search?q=오사카&mode=recommend|keyword&page=1&date=YYYY-MM-DD` → MCP `recommend_products` / `search_products` 호출
- `/api/health` → MCP 서버의 `tools/list` 결과
- 환경변수: `WEB_HOST`(기본 127.0.0.1), `WEB_PORT`(8000), `WEB_RATE_PER_MIN`(20), `MCP_SERVER_CMD`
- 주의: 추천 검색은 LLM을 호출하므로 인증 없이 `0.0.0.0`으로 열지 말 것.

요청 흐름도: `docs/browser-flow.png` (원본 `docs/browser-flow.mmd`)

![browser flow](docs/browser-flow.png)

## 디버그
```bash
LOG_LEVEL=DEBUG python recommend.py "오사카" --mode recommend   # 또는 .env에 LOG_LEVEL=DEBUG
LOG_LEVEL=DEBUG python web.py                                  # 서버 로그는 터미널(stderr)에 출력
```
- `infer:` 줄: LLM에 준 카테고리 수/프롬프트 길이, LLM 원본 응답, 최종 선택(id, 이름)
- Ollama: `prompt_eval_count`가 컨텍스트 한도(기본 2048~4096)에 가까우면 프롬프트가 잘린 것이므로 `OLLAMA_NUM_CTX=8192` 등으로 늘린다.
- SQL은 INFO 레벨에서도 항상 로그에 남는다.

## 테스트
```bash
python -m unittest discover -s tests -v   # DB/LLM/네트워크 없이 infer, 상품 조회 SQL 검증
```

## 키워드 점검 (실제 DB/날씨/LLM)
```bash
python check_keywords.py --file tests/keywords.txt -v        # 키워드 모드
python check_keywords.py --mode recommend "오사카=오사카" -v  # 추천 모드 (LLM)
python check_keywords.py --url http://127.0.0.1:8000 오사카   # web.py를 통해 HTTP로 점검
```
`키워드=기대 카테고리` 형식으로 기대값을 주면 매칭 결과에 포함되는지 검사한다. 실패가 있으면 종료 코드 1.
