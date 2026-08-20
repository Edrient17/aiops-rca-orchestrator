# AIOps RCA Orchestrator

Slack 질문을 안전하게 접수하고, LangGraph 조사 서비스가 MCP로 증거를 모아 RCA
보고서를 작성하는 독립 배포 단위입니다.

## 구성 요소

- `ingress`: Slack 서명 검증, 이벤트 중복 방지, 즉시 ACK, 조사 수행과 게시,
  기동 시 보고서 템플릿 동기화
- `postgres`: AIOps 요청·Agent 실행·보고서 감사 데이터
- `db-migrate`: 기동마다 `database/migrations/*.sql`을 다시 적용하는 일회성 서비스
- `rca-api`: 질문 분석 → LangGraph 조사 → 보고서 작성. 모델 호출과 MCP 세션을
  모두 여기서 소유합니다
- `caddy`: 선택 사항인 공개 HTTPS reverse proxy

Ingress가 요청을 먼저 Postgres에 저장한 뒤 Slack에 200을 응답합니다. AI 실행은
이 응답과 분리되며, 조사가 실패하면 outbox가 최대 5분 간격으로 계속
재시도합니다.

**추론은 전부 `rca-api`에 있습니다.** ingress는 Slack 입출력과 감사 기록만
담당하며 모델이나 MCP를 직접 호출하지 않습니다.

## 1. 환경 변수

```powershell
Copy-Item .env.example .env
```

`.env`에서 모든 `replace-...` 값을 변경합니다. 비밀번호와 키는 URL 인코딩 문제가
없는 64자리 hex 문자열을 권장합니다.

```powershell
[Convert]::ToHexString(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
).ToLower()
```

중요 값:

- `AIOPS_DOMAIN`, `AIOPS_PUBLIC_URL`: Orchestrator의 공개 HTTPS 주소
- `AIOPS_INTERNAL_TOKEN`: ingress와 rca-api만 공유하는 내부 토큰
- `SLACK_*`: Slack App 자격 증명과 질문·답변·오류 채널 ID
- `ZABBIX_MCP_URL`, `ZABBIX_MCP_AUTH_TOKEN`: Zabbix Investigation MCP
- `OSS_ES_MCP_URL`: 공식 Elasticsearch MCP (인증 없음)
- `WAZUH_MCP_URL`, `WAZUH_MCP_AUTH_TOKEN`: Wazuh MCP
- `OPENAI_API_KEY`, `RCA_*_MODEL`: 세 스테이지의 모델 연결
- `LANGSMITH_*`: 선택 사항인 추적. 키가 없으면 추적 없이 그대로 동작합니다

## 2. Slack App

Bot Token Scopes:

- `app_mentions:read`
- `chat:write`
- 질문 채널의 모든 메시지를 받을 경우 `channels:history`
- 보고서 라벨링을 사용할 경우 `reactions:read`, 답변 채널 스레드 답글 수집을 위한
  `channels:history`

Event Subscriptions:

- Request URL: `https://<AIOPS_DOMAIN>/slack/events`
- Bot event: `app_mention`
- 멘션 없이 질문 채널의 모든 메시지를 받을 경우 `message.channels`
- 보고서 라벨링을 사용할 경우 `reaction_added`, `reaction_removed`

Bot을 질문·답변·오류 채널에 초대하고 각 채널 ID를 `.env`에 입력합니다. 특정
사용자만 허용하려면 `SLACK_ALLOWED_USER_IDS`에 쉼표로 구분한 User ID를 넣습니다.
이 목록은 질문뿐 아니라 라벨링에도 그대로 적용됩니다.

### 보고서 라벨링

발행된 RCA 보고서에 남긴 이모지 반응이 그 조사에 대한 판정으로 기록됩니다.
보고서를 게시한 채널과 메시지 ts가 이미 `aiops_reports`에 있으므로 별도의 상관
관계 식별자 없이 반응 이벤트만으로 어느 요청인지 특정됩니다.

| 반응 | 판정 |
| --- | --- |
| ✅ `white_check_mark`, ✔️ `heavy_check_mark` | `correct` |
| 🤔 `thinking_face` | `partial` |
| ❌ `x` | `incorrect` |

목록에 없는 이모지는 무시하며, 이때 DB 조회도 하지 않습니다. 매핑은
`SLACK_LABEL_REACTIONS`에서 `이모지=판정` 형식으로 바꿀 수 있습니다. 반응을
취소하면 해당 판정도 삭제됩니다.

반응만으로는 결론이 틀렸다는 사실은 남아도 실제 원인은 남지 않습니다. `correct`가
아닌 판정이 처음 달리면 봇이 보고서 스레드에 실제 원인을 물어보고, 스레드에 달린
답글이 `aiops_report_notes`에 기록됩니다.

질문 채널의 스레드 답글은 종전대로 명확화 답변으로 처리됩니다.

## 3. 실행

호스트에 이미 HTTPS reverse proxy가 있다면 기본 실행:

```powershell
docker compose up -d --build
```

포함된 Caddy로 인증서를 발급하고 80/443을 공개하려면:

```powershell
docker compose --profile proxy up -d --build
```

기본 실행에서는 다음 포트가 loopback에만 바인딩됩니다.

- ingress: `127.0.0.1:8080`

외부 reverse proxy는 `/slack/events`만 ingress 8080으로 전달하면 됩니다.
`rca-api`의 8090 포트는 호스트에 공개하지 않고 Docker 네트워크 안에서
ingress만 호출합니다.

포함된 Caddyfile은 `/slack/events` 외의 모든 경로에 404를 반환합니다.

```powershell
ssh -L 5678:127.0.0.1:5678 <orchestrator-host>
```

editor를 공개하면 Code 노드의 임의 코드 실행이 owner 로그인 하나에만 의존하게
됩니다. 특히 owner 계정을 만들기 전에 공개하면 먼저 접근한 사람이 owner를
선점할 수 있으므로, `--profile proxy`로 공개하기 전에 터널로 owner 계정을 먼저
만드십시오.

상태 확인:

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:8080/readyz
Invoke-RestMethod http://127.0.0.1:5678/healthz
```

### 스키마 마이그레이션

`database/migrations/*.sql`은 `db-migrate` 서비스가 매 기동마다 파일 이름 순서로
다시 적용합니다. ingress는 이 서비스가 성공해야 시작하므로, 컬럼을 추가하는
배포가 그 컬럼이 없는 데이터베이스를 상대로 ingress를 띄우는 일은 없습니다.
별도 명령은 없고 `docker compose up -d`가 전부입니다.

```powershell
docker compose logs db-migrate
```

새 파일을 추가하든 기존 파일을 고치든 **다시 실행해도 안전해야 합니다**.
`CREATE ... IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, 뷰는 `DROP VIEW IF
EXISTS` 후 재생성을 사용합니다. 적용된 파일 이름을 기록하는 원장은 두지
않았습니다. 원장이 있으면 기존 파일을 제자리에서 고쳤을 때 그 변경이 조용히
건너뛰어집니다.

> 이미 운영 중인 데이터베이스에서 처음 실행하면 `003_message_dedup.sql`이
> 중복 요청 행을 실제로 삭제합니다. 먼저 확인하려면:
>
> ```sql
> SELECT channel_id, message_ts, count(*)
> FROM aiops_requests GROUP BY 1, 2 HAVING count(*) > 1;
> ```

### 배포

조사와 보고서 작성은 `rca-api`가, Slack 입출력과 게시는 `ingress`가 합니다.
둘 다 healthy가 된 것을 확인하고 로그를 봅니다.

```bash
docker compose up -d --build rca-api ingress
docker compose ps rca-api ingress
docker compose exec rca-api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/readyz').read().decode())"
docker compose logs --tail=100 rca-api ingress
```

재시작은 **진행 중인 조사를 중단시킵니다.** 중단된 조사는 큐에 남아 재시도되지만,
먼저 확인하는 편이 낫습니다.

```sql
SELECT request_id, status FROM aiops_requests
WHERE status NOT IN ('completed', 'failed', 'needs_clarification', 'unsupported');
```

Agent 실행은 `aiops_agent_runs`, MCP 호출은 증거 패키지를 통해
`aiops_tool_calls`, 최종 결과는 `aiops_reports`에 저장됩니다.

## 4. 보안 경계

- 공개되어야 하는 ingress 경로는 `/slack/events` 하나뿐입니다.
- ingress의 `/internal/*`는 외부 reverse proxy에 연결하지 않습니다.
- Postgres와 `rca-api`는 Docker 내부 네트워크에만 존재합니다.
- Zabbix Investigation MCP는 Bearer 인증과 허용 호스트 그룹을 함께 설정합니다.
- MCP는 읽기 전용 도구만 노출합니다. Zabbix는 `.get` 계열만 도달 가능합니다.
- Slack Signing Secret, Bot Token, OpenAI key, MCP token은 저장소에 커밋하지
  않습니다.

## 보고서 템플릿

문서 종류마다 **무엇을 수집하고 어떤 구성으로 쓸지**를 DB 행 하나로 정의합니다.
장애 RCA와 월말 용량 보고서는 볼 메트릭도, 기간도, 문서 구성도 다른데, 이를
워크플로 분기로 넣으면 종류를 늘릴 때마다 재배포해야 합니다.

### 어떤 보고서가 있는지는 `templates/`가 정합니다

`templates/*.json`이 진실이고 DB는 그것을 따라갑니다. ingress가 **기동할 때마다**
디렉터리를 읽어 DB에 맞춥니다 — 배포할 때마다 돈다는 뜻입니다.

- **추가** — 파일을 만들고 배포합니다.
- **수정** — 파일을 고치고 배포합니다. 내용이 실제로 달라졌을 때만 `version`이 오릅니다.
- **삭제** — 파일을 지우고 배포합니다. DB 행이 사라지고,
  `aiops_report_template_versions`에는 남아 과거 보고서를 설명할 수 있습니다.

`template_id`는 파일 안에 적습니다. 파일명에서 유추하지 않는 이유는, 파일명은
실수로 바뀌기 쉬운데 그 실수가 조용히 새 템플릿을 만들고 원래 것을 고아로
만들기 때문입니다.

잘못된 파일이 하나라도 있으면 **ingress가 뜨지 않습니다.** 템플릿은 조사 도중
프롬프트가 되므로, 런타임에 발견되면 이미 접수·응답까지 끝난 질문이 실패합니다.
이때 이전 컨테이너가 그대로 남으므로 서비스는 계속됩니다.

디렉터리에서 파일을 **하나도 못 읽으면 삭제를 건너뜁니다.** 볼륨 마운트가
빠진 것과 의도적으로 비운 것이 여기서는 구분되지 않는데, 전자라면 폴백
템플릿까지 지워져 모든 질문이 실패하기 때문입니다.

```powershell
docker compose logs ingress | Select-String "Report templates synced"
```

### 섹션은 자기가 무엇으로 쓰이는지 선언합니다

`output.sections[].requires_effects`가 그 섹션을 채우는 관측을 도구 레지스트리의
어휘로 적습니다. 이 선언 하나가 세 시점에서 검사됩니다.

| 시점 | 검사 |
| --- | --- |
| 로드 | 만들 수 없는 관측을 요구하는 섹션은 테스트에서 걸립니다 |
| 수집 | 선언됐는데 관측되지 않은 것을 결정론적으로 수집합니다. 남으면 조사를 끝내지 않습니다 |
| 작성 | 끝내 못 모은 섹션은 이유를 밝히고, 인용되지 않은 이벤트는 한계 항목으로 내려갑니다 |

```json
{ "id": "capacity_trend", "heading": "용량 추세",
  "requires_effects": ["metric_change"],
  "instruction": "호스트별 디스크 사용률의 월초 대비 월말 변화" }
```

이 선언이 없으면 수집이 그 섹션을 채웠는지 아무도 확인하지 않습니다. 실제로
월말 보고서의 용량 섹션이 조용히 비어 나온 적이 있고, 출력을 읽어야만 알 수
있었습니다.

**서술형 섹션은 아무것도 선언하지 않습니다.** 요약이나 한계처럼 조사 전체를
읽어 쓰는 칸은 특정 관측 하나에 의존해서는 안 됩니다. 같은 이유로 장애 RCA
템플릿은 선언이 비어 있습니다 — 그 섹션들은 측정치가 아니라 결론이고, 선언하면
모든 장애 조사에 메트릭 스윕 비용이 붙습니다.

사용 가능한 관측 이름은 `rca-api/src/aiops_rca/tools/registry.py`의 도구별
`effects`이고, 그중 스윕이 자동으로 모을 수 있는 것은
`tools/coverage.py`의 레시피가 정합니다.

### 그 밖의 섹션 규칙

`id`로 작성 Agent의 출력과 짝지어지므로 제목은 바꿔도 되지만 `id`는 바꾸지
마십시오. `required: false`인 칸은 쓸 내용이 없으면 보고서에서 빠지고, `true`면
"해당 없음"으로 남습니다. `requires_problem_event: true`인 칸은 **실제 Zabbix
문제 이벤트가 있을 때만** 나옵니다 — 멀쩡한 호스트에 없던 장애를 지어내는 것을
막습니다.

`description`은 질문 분석 Agent가 읽습니다. 문서에 무엇이 담기는지가 아니라
**어떤 질문일 때 이걸 고르는지**를 적으십시오.

### 급할 때만: API로 직접 고치기

`PUT /internal/templates/:id`로 재배포 없이 바꿀 수 있습니다. 문구를 빠르게
시험할 때 쓰되, **다음 배포에서 파일 기준으로 되돌아갑니다.** 남기려면 파일에
반영하십시오.

```bash
curl -X PUT http://127.0.0.1:8080/internal/templates/monthly_capacity_report \
  -H "X-AIOPS-Internal-Token: $AIOPS_INTERNAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d @templates/monthly-capacity-report.json
```

- `GET /internal/templates`가 사용 가능한 목록, `?all=true`면 비활성 포함입니다.
- `DELETE /internal/templates/:id`로 제거합니다. 잠시 내리기만 할 때는
  `enabled: false`가 낫습니다 — 그 템플릿으로 발행된 보고서가 가리키는 대상이
  사라지지 않습니다.

같은 내용을 다시 보내면 `{"changed": false}`가 돌아옵니다. 모든 판이
`aiops_report_template_versions`에 남으므로 템플릿을 지웠다 다시 만들어도 버전
번호는 이어집니다.

## MCP 서버 추가하기

증거 출처를 하나 붙이는 일은 전부 `rca-api` 안에서 끝납니다.

건드릴 곳은 네 군데이고, 나머지는 표에서 따라옵니다.

### 1. 환경 변수

`.env.example`과 `.env`에 URL과 토큰을 넣고, `docker-compose.yml`의 `rca-api`
환경에 전달합니다. 인증이 없는 서버라면 URL만 넣습니다.

```dotenv
PROM_MCP_URL=http://10.0.0.9:3005/mcp
PROM_MCP_AUTH_TOKEN=<bearer>
```

### 2. `rca-api/src/aiops_rca/config/settings.py`

같은 이름의 필드를 추가합니다. 토큰이 있으면 `reject_empty_secrets`의 목록에도
넣어 빈 값으로 기동하지 않게 합니다.

```python
prom_mcp_url: str
prom_mcp_auth_token: SecretStr
```

### 3. `rca-api/src/aiops_rca/sources.py`

**여기가 중심입니다.** 표에 항목 하나를 넣으면 transport, adapter, 도구 카탈로그
조회, 증거 접두사, `evidence_id` 정규식이 전부 따라옵니다.

```python
"prometheus": SourceProfile(
    name="prometheus",
    url_setting="prom_mcp_url",
    token_setting="prom_mcp_auth_token",   # 인증이 없으면 None
    generic_prefix="prom:object",
    generic_evidence_type="observation",
    evidence_prefixes=("prom:series", "prom:object"),
),
```

같은 파일의 `ToolSource`에도 이름을 추가합니다. 타입 검사기가 읽어야 해서 생성할
수 없는 값인데, 표와 어긋나면 테스트가 잡습니다.

`generic_prefix`는 전용 normalizer가 없는 도구의 결과가 기록될 이름입니다.
**비워 둘 수 없습니다** — 없으면 조사 도중 `KeyError`로 죽습니다.

### 4. `rca-api/src/aiops_rca/tools/registry.py`

도구마다 `ToolPolicy`를 등록합니다. `effects`가 그 도구로 무엇을 알 수 있는지를
나타내는 이름이고, 보고서 섹션의 `requires_effects`가 이 어휘를 씁니다.

```python
_tool(
    "get_prom_range",
    "prometheus",
    ("metric_level", "metric_change"),
    requires=("query", "time_from", "time_to"),
    priority=20,
    result_list_fields=("series",),
),
```

- `requires` / `requires_any`: 없으면 호출 전에 거절되는 인자
- `temporal_scope="current_only"`: 현재 상태만 답하는 도구. 과거를 묻는 관측에
  배정되지 않습니다
- `kind="generic"`: 정형 도구로 부족하다는 근거가 있을 때만 열리는 범용 도구
- `window_policy_argument="policy"`: 긴 구간에 다른 정책 인자를 받는 도구

### 선택: 커버리지 레시피

보고서 섹션이 이 서버의 `effects`를 `requires_effects`로 선언하게 하려면
`tools/coverage.py`에 레시피를 추가합니다. 레시피가 없는 effect는 계획 단계에서
모델이 요청할 때만 수집되고, 섹션이 선언하면 템플릿 검증에서 거절됩니다.

### 확인

```powershell
Set-Location rca-api
.venv/Scripts/python.exe -m pytest -q
```

빠뜨린 것은 대부분 여기서 걸립니다 — 표와 `ToolSource`의 불일치, 존재하지 않는
설정 필드 이름, 스키마가 모르는 증거 타입, 두 소스가 같은 접두사를 주장하는
경우, 그리고 등록된 도구의 소스가 표에 없는 경우입니다.

실제 서버에 붙는지는 기동 후 도구 카탈로그로 확인합니다.

```bash
docker compose up -d --build rca-api
docker compose logs rca-api | grep -i "tool_catalog"
```

## 데이터 확인

```powershell
docker compose exec postgres psql -U aiops -d aiops
```

주요 테이블:

- `aiops_requests`, `aiops_dispatch_queue`
- `aiops_agent_runs`: 스테이지별 모델·소요 시간·출력
- `aiops_tool_calls`, `aiops_reports`
- `aiops_system_errors`: 실행 오류. 조사를 포기한 디스패처가 요청 ID와 함께
  직접 기록합니다. 이미 `completed`인 요청은 덮어쓰지 않습니다.
- `aiops_report_feedback`: 보고서에 달린 반응 판정
- `aiops_report_notes`: 보고서 스레드에 적힌 실제 원인
- `aiops_report_templates`, `aiops_report_template_versions`

`aiops_labeled_dataset` 뷰가 질문·증거·결론·판정을 한 행으로 묶어 줍니다. 한
보고서에 판정이 여러 개면 가장 나쁜 판정을 채택합니다.

```sql
SELECT request_id, question, label, notes
FROM aiops_labeled_dataset
WHERE label IS NOT NULL;
```

요청 상태 조회용 내부 API도 제공하지만 외부 공개용은 아닙니다.

```text
GET /internal/requests/:requestId
X-AIOPS-Internal-Token: <AIOPS_INTERNAL_TOKEN>
```

### 조사 과정 보기

`LANGSMITH_*`를 설정하면 조사가 LangSmith에 기록됩니다. 요청 하나가 세 개의
독립 trace를 남깁니다 — 질문 분석, `investigation <request_id>`, 보고서 작성.
가운데 것을 **Details(트리) 뷰**로 열면 노드 경계·루프·각 모델 호출이 그대로
보입니다. Turns 뷰는 그래프를 한 턴으로 눌러 버려 흐름이 보이지 않습니다.

## 개발 검증

```powershell
Set-Location ingress
npm ci; npm run typecheck; npm test
```

```powershell
Set-Location ../rca-api
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check src tests
```

워크플로 재생성:

```powershell
Set-Location ..
node scripts/generate-workflows.mjs
```

## 저장소 구조

```text
.
├── database/migrations/    스키마. 매 기동 재적용, 멱등이어야 함
├── ingress/                Slack 수신, 템플릿 동기화
├── rca-api/                질문 분석 · LangGraph 조사 · 보고서 작성
├── schemas/                단계 간 계약. rca-api 모델이 이 파일로 검증됨
├── scripts/                워크플로 생성·재배포
├── templates/              보고서 종류. DB의 원본
├── workflows/              생성물. 직접 고치지 말 것
├── Caddyfile
├── docker-compose.yml
└── .env.example
```

`workflows/*.json`은 `scripts/generate-workflows.mjs`의 출력입니다. 워크플로를
바꿀 때는 생성기를 고치고 다시 생성하십시오.

이 폴더는 MCP 저장소의 파일이나 상위 디렉터리를 참조하지 않습니다. 연결 계약은
`.env`의 MCP URL과 토큰입니다.
