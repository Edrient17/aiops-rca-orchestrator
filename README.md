# AIOps RCA Orchestrator

Slack으로 들어온 운영 질문을 조사해 RCA 보고서로 돌려주는 서비스다. `rca-api`가 Zabbix, Wazuh, Elasticsearch MCP에서 근거를 수집하고 LangGraph로 조사 과정을 진행한다. `ingress`는 Slack 연동과 작업 큐, 결과 저장을 담당한다.

전체 프로젝트 구성은 상위 [PROJECT_HOME.md](../PROJECT_HOME.md)에서 확인할 수 있다. 이 문서는 이 저장소의 배포와 운영 방법을 설명한다.

## 구성

```text
Slack
  └─ ingress ── PostgreSQL
       └─ rca-api
            ├─ Zabbix MCP
            ├─ Wazuh MCP
            └─ Elasticsearch MCP
```

| 서비스 | 역할 | 호스트 노출 |
| --- | --- | --- |
| `ingress` | Slack 서명 검증, 요청 저장, 작업 배분, 결과 게시, 템플릿 동기화 | `127.0.0.1:8080` |
| `rca-api` | 질문 분석, MCP 조사, RCA 보고서 작성 | 없음 (컨테이너 내부 8090) |
| `postgres` | 요청, 조사 실행 기록, 보고서, 평가 데이터 저장 | 없음 |
| `db-migrate` | DB 마이그레이션 적용 후 종료 | 없음 |
| `caddy` | 선택 사항. HTTPS reverse proxy | 80/443 (`proxy` 프로필) |

Slack 요청은 DB에 저장한 뒤 바로 응답한다. 실제 조사는 별도 작업으로 실행되며, 실패한 작업은 큐에서 최대 5분 간격으로 다시 시도한다. 모델과 MCP 호출은 모두 `rca-api`에서 처리한다.

## 시작하기

### 환경 변수

예제 파일을 복사하고 토큰, 도메인, 채널 ID를 현재 환경에 맞게 채운다.

```powershell
Copy-Item .env.example .env
```

토큰이나 비밀번호는 URL 인코딩 문제가 없는 64자리 hex 문자열을 권장한다.

```powershell
[Convert]::ToHexString(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
).ToLower()
```

주요 설정은 다음과 같다. 전체 목록과 기본값은 [.env.example](.env.example)을 참고한다.

| 변수 | 설명 |
| --- | --- |
| `POSTGRES_*` | PostgreSQL DB, 사용자, 비밀번호 |
| `AIOPS_DOMAIN` | Caddy가 사용할 공개 도메인. `proxy` 프로필에서만 필요 |
| `AIOPS_INTERNAL_TOKEN` | `ingress`와 `rca-api` 사이의 내부 인증 토큰 |
| `SLACK_*` | Slack 앱 자격 증명과 질문·답변 채널 ID |
| `OPENAI_API_KEY` | 모델 호출에 사용할 API 키 |
| `RCA_MODEL`, `RCA_MODEL_*` | 기본 모델과 단계별 모델 |
| `ZABBIX_MCP_*` | Zabbix MCP 주소와 Bearer 토큰 |
| `WAZUH_MCP_*` | Wazuh MCP 주소와 Bearer 토큰 |
| `OSS_ES_MCP_URL` | Elasticsearch MCP 주소 |
| `AIOPS_MONTHLY_HOST_GROUP_ID` | 월간 용량 보고서에 사용할 Zabbix 호스트 그룹 ID |
| `ZABBIX_FRONTEND_URL` | 보고서의 Zabbix 근거 링크에 사용할 주소. 선택 사항 |
| `KIBANA_URL`, `KIBANA_DATA_VIEW_ID` | 로그 근거를 Kibana Discover로 연결할 때 사용. 선택 사항 |
| `LANGSMITH_*` | LangSmith 추적 설정. 선택 사항 |

`RCA_MODEL`을 기본값으로 사용하되 특정 단계만 다른 모델로 지정할 수 있다.

```dotenv
RCA_MODEL=gpt-5.6-luna
RCA_MODEL_OBSERVATION_PLANNER=gpt-5.6-terra
```

단계별 변수의 접미사는 아래 값 중 하나여야 한다.

```text
QUESTION_ANALYZER
RESOLVE_HOSTS
ESTABLISH_PHENOMENON
HYPOTHESIS_PLANNER
OBSERVATION_PLANNER
HYPOTHESIS_UPDATER
REPORT_WRITER
```

시간 제한은 모두 기본값이 있으므로 변경할 때만 `.env`에 추가한다.

| 변수 | 기본값 | 설명 |
| --- | ---: | --- |
| `DISPATCH_INTERVAL_MS` | 1000 | 대기 중인 작업을 확인하는 주기 |
| `RCA_TIMEOUT_MS` | 900000 | 조사 한 건의 최대 실행 시간 |
| `SLACK_POST_TIMEOUT_MS` | 30000 | Slack 메시지 한 건의 게시 제한 시간 |
| `MCP_TIMEOUT_SECONDS` | 120 | MCP 도구 호출 한 건의 제한 시간 |
| `MODEL_TIMEOUT_SECONDS` | 180 | 모델 호출 한 건의 제한 시간 |

`RCA_TIMEOUT_MS`는 큐 작업의 점유 시간에도 반영된다.

### Slack 앱 설정

| 항목 | 값 |
| --- | --- |
| Bot Token Scopes | `app_mentions:read`, `chat:write` |
| 채널의 모든 메시지를 받을 때 | `channels:history` 추가 |
| 보고서 평가를 받을 때 | `reactions:read`, `channels:history` 추가 |
| Request URL | `https://<AIOPS_DOMAIN>/slack/events` |
| Bot events | `app_mention` |
| 선택 이벤트 | `message.channels`, `reaction_added`, `reaction_removed` |

봇을 질문 채널과 답변 채널에 초대한 뒤 각 채널 ID를 `.env`에 입력한다. 질문자를 제한하려면 `SLACK_ALLOWED_USER_IDS`에 Slack User ID를 쉼표로 구분해 적는다. 이 제한은 질문과 보고서 평가에 모두 적용된다.

### 실행

외부 reverse proxy가 있으면 기본 프로필을 사용한다.

```powershell
docker compose up -d --build
```

포함된 Caddy로 HTTPS까지 구성하려면 `proxy` 프로필을 사용한다.

```powershell
docker compose --profile proxy up -d --build
```

기본 구성에서 `ingress`는 `127.0.0.1:8080`에만 바인딩된다. 외부 proxy에는 `/slack/events`만 연결한다. `rca-api`와 PostgreSQL은 호스트에 포트를 공개하지 않는다.

상태 확인:

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:8080/readyz
```

## 운영

### 배포

애플리케이션만 다시 빌드할 때는 다음 명령을 사용한다.

```bash
docker compose up -d --build rca-api ingress
docker compose ps rca-api ingress
docker compose logs --tail=100 rca-api ingress
```

> 컨테이너를 재시작하면 진행 중인 조사가 중단된다. 작업은 큐에 남아 다시 실행되지만 이미 수행한 모델 호출 비용은 복구되지 않는다.

배포 전에 진행 중인 요청을 확인하려면 PostgreSQL에서 아래 쿼리를 실행한다.

```sql
SELECT request_id, status
FROM aiops_requests
WHERE status NOT IN ('completed', 'failed', 'needs_clarification', 'unsupported');
```

### DB 마이그레이션

`db-migrate`는 컨테이너가 시작될 때 `database/migrations/*.sql`을 파일명 순서대로 적용한다. 마이그레이션이 완료되어야 `ingress`가 시작된다.

```powershell
docker compose logs db-migrate
```

각 SQL 파일은 여러 번 실행해도 안전해야 한다. 테이블과 컬럼에는 `IF NOT EXISTS`를 사용하고, 뷰는 `DROP VIEW IF EXISTS` 후 다시 만든다. 별도 마이그레이션 원장은 사용하지 않는다.

기존 DB에 처음 적용할 때 `003_message_dedup.sql`이 중복 요청을 정리한다. 적용 전에 아래 쿼리로 대상을 확인할 수 있다.

```sql
SELECT channel_id, message_ts, count(*)
FROM aiops_requests
GROUP BY 1, 2
HAVING count(*) > 1;
```

### 보안 경계

외부에 공개할 경로는 `/slack/events`뿐이다. `/internal/*`는 운영용 API이므로 reverse proxy에 연결하지 않는다.

- PostgreSQL과 `rca-api`는 Docker 내부 네트워크에서만 접근한다.
- MCP 서버에는 조회 전용 도구만 노출한다.
- Zabbix MCP는 Bearer 인증과 허용 호스트 그룹을 함께 사용한다.
- Slack 토큰, OpenAI API 키, MCP 토큰은 저장소에 커밋하지 않는다.
- 인증 기능이 없는 Elasticsearch MCP는 사설 네트워크 안에서만 운영한다.

## 보고서 템플릿

보고서 종류와 조사 범위는 `templates/*.json`으로 관리한다. `ingress`가 시작될 때 파일을 읽어 DB의 템플릿 목록을 동기화한다.

| 작업 | 방법 |
| --- | --- |
| 추가 | JSON 파일을 추가하고 재배포 |
| 수정 | 파일을 수정하고 재배포. 실제 내용이 달라질 때만 버전 증가 |
| 삭제 | 파일을 삭제하고 재배포. 이전 버전 기록은 DB에 유지 |

동기화 결과는 로그에서 확인한다.

```powershell
docker compose logs ingress | Select-String "Report templates synced"
```

템플릿 파일에는 `template_id`를 직접 지정한다. 파일명이 바뀌어도 같은 템플릿으로 인식하기 위해서다. 잘못된 파일이 하나라도 있으면 `ingress`가 시작되지 않는다. 디렉터리가 비어 있거나 마운트되지 않은 경우에는 기존 템플릿을 일괄 삭제하지 않고 동기화를 중단한다.

### 주요 필드

| 필드 | 설명 |
| --- | --- |
| `description` | 이 템플릿을 선택해야 하는 질문의 범위 |
| `collection.guidance` | 해당 보고서를 위해 근거를 수집하는 방법 |
| `sections[].id` | 작성 결과와 연결되는 키. 기존 템플릿에서는 변경하지 않는다 |
| `sections[].required` | 결과가 없어도 섹션을 유지할지 여부 |
| `sections[].requires_problem_event` | 실제 Zabbix 문제 이벤트가 있을 때만 섹션을 출력할지 여부 |
| `sections[].instruction` | 섹션에 작성할 내용 |

### API로 임시 변경

`PUT /internal/templates/:id`를 사용하면 재배포 없이 템플릿을 시험할 수 있다. 다만 다음 배포 때 `templates/`의 파일 내용으로 다시 동기화된다.

```bash
curl -X PUT http://127.0.0.1:8080/internal/templates/monthly_capacity_report \
  -H "X-AIOPS-Internal-Token: $AIOPS_INTERNAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d @templates/monthly-capacity-report.json
```

| API | 설명 |
| --- | --- |
| `GET /internal/templates` | 활성 템플릿 목록. `?all=true`로 비활성 템플릿 포함 |
| `PUT /internal/templates/:id` | 템플릿 생성 또는 수정 |
| `DELETE /internal/templates/:id` | 템플릿 삭제 |

내용이 같으면 `PUT` 응답의 `changed` 값은 `false`다. 임시로 사용을 중단할 때는 삭제보다 `enabled: false`를 권장한다.

## 보고서 평가

사용자가 보고서 메시지에 남긴 반응은 조사 결과의 평가 데이터로 저장된다.

| 반응 | 판정 |
| --- | --- |
| ✅ `white_check_mark`, ✔️ `heavy_check_mark` | `correct` |
| 🤔 `thinking_face` | `partial` |
| ❌ `x` | `incorrect` |

매핑은 `SLACK_LABEL_REACTIONS`에서 `이모지=판정` 형식으로 바꿀 수 있다. 목록에 없는 반응은 무시하고, 반응을 취소하면 판정도 삭제한다.

`partial` 또는 `incorrect`가 처음 등록되면 봇이 스레드에서 실제 원인을 묻는다. 답변은 `aiops_report_notes`에 저장된다. 이 기능을 사용하려면 Slack 앱에 평가용 scope와 이벤트를 추가해야 한다.

## MCP 서버 추가

새 근거 출처는 `rca-api`에 등록한다. 아래 네 곳을 수정한다.

1. `.env.example`, `.env`, `docker-compose.yml`

   MCP URL과 필요한 인증 토큰을 추가하고 `rca-api` 컨테이너에 전달한다.

   ```dotenv
   PROM_MCP_URL=http://10.0.0.9:3005/mcp
   PROM_MCP_AUTH_TOKEN=<bearer-token>
   ```

2. `rca-api/src/aiops_rca/config/settings.py`

   같은 이름의 설정 필드를 추가한다. 필수 토큰은 `reject_empty_secrets` 검사에도 포함한다.

   ```python
   prom_mcp_url: str
   prom_mcp_auth_token: SecretStr
   ```

3. `rca-api/src/aiops_rca/sources.py`

   `SourceProfile`을 추가하고 `ToolSource` 타입에도 소스 이름을 등록한다.

   ```python
   "prometheus": SourceProfile(
       name="prometheus",
       url_setting="prom_mcp_url",
       token_setting="prom_mcp_auth_token",
       generic_prefix="prom:object",
       generic_evidence_type="observation",
       evidence_prefixes=("prom:series", "prom:object"),
   ),
   ```

   인증이 필요 없으면 `token_setting`은 `None`으로 둔다. `generic_prefix`는 전용 normalizer가 없는 도구의 결과 ID에 사용되므로 비워 둘 수 없다.

4. `rca-api/src/aiops_rca/tools/registry.py`

   모델이 호출할 수 있는 도구를 레지스트리에 추가한다.

   ```python
   _tool(
       "get_prom_range",
       "prometheus",
       requires=("query", "time_from", "time_to"),
       priority=20,
       result_list_fields=("series",),
   ),
   ```

주요 옵션:

| 옵션 | 설명 |
| --- | --- |
| `requires`, `requires_any` | 호출 전에 확인할 필수 인자 |
| `kind="generic"` | 범용 도구로 분류. 정형 도구로 조사할 수 없을 때만 사용 |
| `result_list_fields` | 결과에서 행 목록을 담는 필드명 |
| `window_policy_argument` | 긴 조회 구간에 적용할 정책 인자 |
| `blocked_reason` | 등록은 유지하되 호출을 막는 사유 |

`temporal_scope`는 MCP 서버가 도구 메타데이터로 제공한다. 레지스트리에는 따로 적지 않는다.

등록 후 단위 테스트와 실제 연결을 확인한다.

```powershell
Set-Location rca-api
.\.venv\Scripts\python.exe -m pytest -q
```

```bash
docker compose up -d --build rca-api
docker compose logs rca-api | grep -i "tool_catalog"
```

## 데이터와 추적

PostgreSQL 콘솔 접속:

```powershell
docker compose exec postgres psql -U aiops -d aiops
```

| 테이블 | 내용 |
| --- | --- |
| `aiops_requests`, `aiops_dispatch_queue` | 요청과 작업 큐 |
| `aiops_agent_runs` | 단계별 모델, 소요 시간, 출력 |
| `aiops_tool_calls` | MCP 도구 호출 기록 |
| `aiops_reports` | 최종 보고서 |
| `aiops_system_errors` | 조사 실행 오류 |
| `aiops_report_feedback` | 보고서 반응 평가 |
| `aiops_report_notes` | 평가 스레드에 기록된 실제 원인 |
| `aiops_report_templates`, `aiops_report_template_versions` | 템플릿과 변경 이력 |

`aiops_labeled_dataset` 뷰는 질문, 근거, 결론, 판정을 한 행으로 묶는다. 한 보고서에 판정이 여러 개면 가장 낮은 판정을 사용한다.

```sql
SELECT request_id, question, label, notes
FROM aiops_labeled_dataset
WHERE label IS NOT NULL;
```

요청 상태는 내부 API에서도 확인할 수 있다.

```text
GET /internal/requests/:requestId
X-AIOPS-Internal-Token: <AIOPS_INTERNAL_TOKEN>
```

`LANGSMITH_*`를 설정하면 질문 분석, 조사, 보고서 작성 과정이 LangSmith에 기록된다. 조사 흐름은 `investigation <request_id>` trace의 Details 뷰에서 확인한다.

## 개발

Ingress:

```powershell
Set-Location ingress
npm ci
npm run typecheck
npm test
```

RCA API:

```powershell
Set-Location ..\rca-api
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

로그 저장소의 필드와 인덱스 구성이 프롬프트의 가정과 맞는지는 실제 클러스터를 대상으로 확인한다. 이 검사는 CI에서 실행하지 않는다.

```bash
python -m aiops_rca.evals.log_store check
```

## 저장소 구조

```text
.
├── database/migrations/    PostgreSQL 마이그레이션
├── ingress/                Slack 연동, 큐 처리, 결과 게시
├── rca-api/                질문 분석, 조사 그래프, 보고서 작성
├── schemas/                서비스 간 데이터 계약
├── templates/              보고서 템플릿
├── Caddyfile               선택형 HTTPS reverse proxy 설정
├── docker-compose.yml
└── .env.example
```

MCP 서버와는 `.env`에 지정한 URL과 토큰으로만 연결한다. 이 저장소는 상위 디렉터리나 다른 MCP 저장소의 파일을 직접 참조하지 않는다.
