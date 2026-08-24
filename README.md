# AIOps RCA Orchestrator

Slack 질문을 접수하고, LangGraph 조사 서비스가 MCP로 근거를 모아 RCA 보고서를 작성해 회신하는 독립 배포 단위.

시스템 전체 개요는 상위 [`PROJECT_HOME.md`](../PROJECT_HOME.md) 참조. 본 문서는 설치·운영·확장 절차를 다룬다.

---

## 1. 구성

| 서비스 | 역할 | 노출 |
| --- | --- | --- |
| `ingress` | Slack 서명 검증, 중복 방지, 즉시 ACK, 조사 요청·게시, 템플릿 동기화 | `127.0.0.1:8080` |
| `rca-api` | 질문 분석 → LangGraph 조사 → 보고서 작성. 모델·MCP 세션 소유 | 내부 네트워크만 (8090) |
| `postgres` | 요청·조사 실행·보고서·평가 감사 데이터 | 내부 네트워크만 |
| `db-migrate` | 기동마다 `database/migrations/*.sql` 재적용 (일회성) | — |
| `caddy` | 선택. 공개 HTTPS reverse proxy | 80/443 (`--profile proxy`) |

- **추론은 전부 `rca-api`에 있다.** ingress는 Slack 입출력과 감사 기록만 담당하며 모델이나 MCP를 직접 호출하지 않는다.
- ingress는 요청을 Postgres에 저장한 뒤 Slack에 200을 응답한다. AI 실행은 이 응답과 분리되며, 조사가 실패하면 큐가 최대 5분 간격으로 재시도한다.

## 2. 환경 변수

```powershell
Copy-Item .env.example .env
```

`.env`의 모든 `replace-...` 값을 교체한다. 비밀번호와 키는 URL 인코딩 문제가 없는 64자리 hex 권장.

```powershell
[Convert]::ToHexString(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
).ToLower()
```

| 변수 | 용도 |
| --- | --- |
| `AIOPS_DOMAIN`, `AIOPS_PUBLIC_URL` | 공개 HTTPS 주소 |
| `AIOPS_INTERNAL_TOKEN` | ingress ↔ rca-api 내부 인증 |
| `SLACK_*` | Slack 자격 증명, 질문·답변·오류 채널 ID |
| `ZABBIX_MCP_URL`, `ZABBIX_MCP_AUTH_TOKEN` | Zabbix Investigation MCP |
| `WAZUH_MCP_URL`, `WAZUH_MCP_AUTH_TOKEN` | Wazuh MCP |
| `OSS_ES_MCP_URL` | Elasticsearch MCP (인증 없음) |
| `OPENAI_API_KEY` | 모델 호출 |
| `RCA_MODEL`, `RCA_MODEL_*` | 단계별 모델 지정 (§2.1) |
| `LANGSMITH_*` | 선택. 추적. 키가 없으면 추적 없이 동작 |

### 2.1 단계별 모델

기본은 `RCA_MODEL` 하나이며, 특정 단계만 다른 모델을 쓰려면 그 줄만 추가한다. 빈 값은 기본값을 따른다.

```dotenv
RCA_MODEL=gpt-5.6-luna
RCA_MODEL_OBSERVATION_PLANNER=gpt-5.6-terra   # 이 단계만 상향
```

지정 가능한 단계: `QUESTION_ANALYZER`, `RESOLVE_HOSTS`, `ESTABLISH_PHENOMENON`, `HYPOTHESIS_PLANNER`, `OBSERVATION_PLANNER`, `HYPOTHESIS_UPDATER`, `REPORT_WRITER`

없는 단계명을 쓰면 기동 시 실패한다. 조용히 기본값으로 도는 것보다 낫기 때문이다.

## 3. Slack App

| 구분 | 필요 항목 |
| --- | --- |
| Bot Token Scopes | `app_mentions:read`, `chat:write` |
| — 채널 전체 메시지 수신 시 | `channels:history` |
| — 보고서 평가 사용 시 | `reactions:read`, `channels:history` |
| Request URL | `https://<AIOPS_DOMAIN>/slack/events` |
| Bot events | `app_mention` (+ 선택: `message.channels`, `reaction_added`, `reaction_removed`) |

봇을 질문·답변·오류 채널에 초대하고 채널 ID를 `.env`에 넣는다. 특정 사용자만 허용하려면 `SLACK_ALLOWED_USER_IDS`에 User ID를 쉼표로 나열한다. 이 목록은 질문과 평가 양쪽에 적용된다.

<details>
<summary><b>보고서 평가 동작</b></summary>

발행된 보고서에 달린 이모지가 그 조사에 대한 판정으로 기록된다. 게시 채널과 메시지 ts가 `aiops_reports`에 있으므로 별도 식별자 없이 반응 이벤트만으로 요청이 특정된다.

| 반응 | 판정 |
| --- | --- |
| ✅ `white_check_mark`, ✔️ `heavy_check_mark` | `correct` |
| 🤔 `thinking_face` | `partial` |
| ❌ `x` | `incorrect` |

- 목록에 없는 이모지는 무시하며, 이때 DB 조회도 하지 않는다.
- 매핑은 `SLACK_LABEL_REACTIONS`에서 `이모지=판정` 형식으로 변경 가능.
- 반응을 취소하면 판정도 삭제된다.
- 반응만으로는 "틀렸다"는 사실만 남고 실제 원인은 남지 않는다. `correct`가 아닌 판정이 처음 달리면 봇이 스레드에서 실제 원인을 질의하고, 답글이 `aiops_report_notes`에 기록된다.
- 질문 채널의 스레드 답글은 종전대로 명확화 답변으로 처리된다.

</details>

## 4. 실행

```powershell
docker compose up -d --build                    # 외부 reverse proxy가 있는 경우
docker compose --profile proxy up -d --build    # 포함된 Caddy로 80/443 공개
```

- 기본 실행에서 ingress는 `127.0.0.1:8080`에만 바인딩된다.
- 외부 proxy는 `/slack/events`만 8080으로 전달하면 된다.
- `rca-api`의 8090은 호스트에 공개하지 않으며 Docker 네트워크 안에서 ingress만 호출한다.
- 포함된 Caddyfile은 `/slack/events` 외 모든 경로에 404를 반환한다.

상태 확인:

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:8080/readyz
```

### 4.1 스키마 마이그레이션

`db-migrate`가 매 기동마다 `database/migrations/*.sql`을 파일명 순서로 재적용한다. ingress는 이 서비스가 성공해야 시작하므로, 컬럼을 추가하는 배포가 그 컬럼 없는 DB를 상대로 뜨는 일은 없다. 별도 명령은 없고 `docker compose up -d`가 전부다.

```powershell
docker compose logs db-migrate
```

**모든 마이그레이션은 반복 실행에 안전해야 한다.** `CREATE ... IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, 뷰는 `DROP VIEW IF EXISTS` 후 재생성.

적용 이력 원장은 두지 않았다. 원장이 있으면 기존 파일을 제자리에서 수정했을 때 그 변경이 조용히 건너뛰어지기 때문이다.

> 운영 중인 DB에서 처음 실행하면 `003_message_dedup.sql`이 중복 요청 행을 실제로 삭제한다. 사전 확인:
>
> ```sql
> SELECT channel_id, message_ts, count(*)
> FROM aiops_requests GROUP BY 1, 2 HAVING count(*) > 1;
> ```

### 4.2 배포

```bash
docker compose up -d --build rca-api ingress
docker compose ps rca-api ingress
docker compose logs --tail=100 rca-api ingress
```

**재시작은 진행 중인 조사를 중단시킨다.** 큐에 남아 재시도되지만 AI 호출 비용이 다시 발생하므로 사전 확인이 필요하다.

```sql
SELECT request_id, status FROM aiops_requests
WHERE status NOT IN ('completed', 'failed', 'needs_clarification', 'unsupported');
```

## 5. 보안 경계

| 항목 | 조치 |
| --- | --- |
| 공개 경로 | `/slack/events` 단일 경로. `/internal/*`는 외부 proxy에 연결하지 않는다 |
| 내부 서비스 | Postgres와 `rca-api`는 Docker 내부 네트워크에만 존재 |
| Zabbix MCP | Bearer 인증 + 허용 호스트 그룹. `.get` 계열만 도달 가능 |
| 도구 권한 | MCP는 읽기 전용 도구만 노출 |
| 비밀 값 | Slack Signing Secret, Bot Token, OpenAI key, MCP token은 저장소에 커밋하지 않는다 |

---

## 6. 보고서 템플릿

보고서 종류마다 **무엇을 수집하고 어떤 구성으로 쓸지**를 DB 행 하나로 정의한다. 장애 RCA와 월말 용량 보고서는 볼 지표도, 기간도, 문서 구성도 다른데, 이를 코드 분기로 넣으면 종류를 늘릴 때마다 재배포해야 한다.

### 6.1 `templates/`가 원본

`templates/*.json`이 진실이고 DB가 그것을 따라간다. ingress가 **기동할 때마다** 디렉터리를 읽어 DB에 맞춘다 — 배포할 때마다 돈다는 뜻이다.

| 작업 | 방법 |
| --- | --- |
| 추가 | 파일 생성 후 배포 |
| 수정 | 파일 수정 후 배포. 내용이 실제로 달라졌을 때만 `version` 증가 |
| 삭제 | 파일 삭제 후 배포. DB 행은 사라지고 `aiops_report_template_versions`에는 남아 과거 보고서를 설명 |

```powershell
docker compose logs ingress | Select-String "Report templates synced"
```

<details>
<summary><b>동기화 안전장치</b></summary>

- **`template_id`는 파일 안에 적는다.** 파일명에서 유추하지 않는 이유는, 파일명은 실수로 바뀌기 쉬운데 그 실수가 조용히 새 템플릿을 만들고 원래 것을 고아로 만들기 때문이다.
- **잘못된 파일이 하나라도 있으면 ingress가 뜨지 않는다.** 템플릿은 조사 도중 프롬프트가 되므로, 런타임에 발견되면 이미 접수·응답까지 끝난 질문이 실패한다. 이때 이전 컨테이너가 남으므로 서비스는 계속된다.
- **디렉터리에서 파일을 하나도 못 읽으면 삭제를 건너뛴다.** 볼륨 마운트 누락과 의도적으로 비운 것은 구분되지 않는데, 전자라면 모든 템플릿이 지워져 전 질문이 실패한다.

</details>

### 6.2 섹션 규칙

| 필드 | 규칙 |
| --- | --- |
| `id` | 작성 결과와 짝짓는 키. 제목은 바꿔도 되지만 `id`는 바꾸지 않는다 |
| `required` | `false`면 내용이 없을 때 보고서에서 빠지고, `true`면 "해당 없음"으로 남는다 |
| `requires_problem_event` | `true`인 섹션은 실제 Zabbix 문제 이벤트가 있을 때만 출력. 멀쩡한 호스트에 없던 장애를 지어내는 것을 막는다 |
| `instruction` | 그 칸에 무엇을 쓸지. 작성 단계가 읽는다 |
| `description` (템플릿 단위) | **어떤 질문일 때 이 템플릿을 고르는지.** 질문 분석 단계가 읽는다. 문서 내용 설명이 아니다 |

`collection.guidance`에는 **조사 방법**을 적는다. 예를 들어 `log_review`는 "하루치 로그는 십만 건 단위이므로 집계로 훑고, 직전 24시간을 기준선으로 함께 조회하라"고 지시한다.

### 6.3 API로 임시 수정

`PUT /internal/templates/:id`로 재배포 없이 변경할 수 있다. 문구를 빠르게 시험할 때 쓰되 **다음 배포에서 파일 기준으로 되돌아간다.** 남기려면 파일에 반영한다.

```bash
curl -X PUT http://127.0.0.1:8080/internal/templates/monthly_capacity_report \
  -H "X-AIOPS-Internal-Token: $AIOPS_INTERNAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d @templates/monthly-capacity-report.json
```

- `GET /internal/templates` — 사용 가능한 목록. `?all=true`면 비활성 포함
- `DELETE /internal/templates/:id` — 제거. 잠시 내리기만 할 때는 `enabled: false`가 낫다. 그 템플릿으로 발행된 보고서가 가리키는 대상이 사라지지 않는다
- 같은 내용을 다시 보내면 `{"changed": false}`. 모든 판이 `aiops_report_template_versions`에 남으므로 지웠다 다시 만들어도 버전 번호는 이어진다

---

## 7. MCP 서버 추가

근거 출처를 하나 붙이는 작업은 전부 `rca-api` 안에서 끝난다. 건드릴 곳은 네 군데이고 나머지는 따라온다.

**① 환경 변수** — `.env.example`과 `.env`에 URL·토큰을 넣고 `docker-compose.yml`의 `rca-api` 환경에 전달한다.

```dotenv
PROM_MCP_URL=http://10.0.0.9:3005/mcp
PROM_MCP_AUTH_TOKEN=<bearer>
```

**② `config/settings.py`** — 같은 이름의 필드를 추가한다. 토큰이 있으면 `reject_empty_secrets` 목록에도 넣어 빈 값으로 기동하지 않게 한다.

```python
prom_mcp_url: str
prom_mcp_auth_token: SecretStr
```

**③ `sources.py`** — 여기가 중심이다. 표에 항목 하나를 넣으면 transport, adapter, 도구 카탈로그 조회, 근거 접두사, `evidence_id` 정규식이 전부 따라온다.

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

같은 파일의 `ToolSource`에도 이름을 추가한다. 타입 검사기가 읽어야 해서 생성할 수 없는 값인데, 표와 어긋나면 테스트가 잡는다.

`generic_prefix`는 전용 normalizer가 없는 도구의 결과가 기록될 이름이다. **비워 둘 수 없다** — 없으면 조사 도중 `KeyError`로 죽는다.

**④ `tools/registry.py`** — 부를 수 있는 도구를 등록한다. 여기 없는 도구는 카탈로그에서 제외되어 모델에게 보이지 않는다.

```python
_tool(
    "get_prom_range",
    "prometheus",
    requires=("query", "time_from", "time_to"),
    priority=20,
    result_list_fields=("series",),
),
```

| 옵션 | 의미 |
| --- | --- |
| `requires` / `requires_any` | 없으면 호출 전에 거절되는 인자 |
| `kind="generic"` | 정형 도구로 부족하다는 근거가 있을 때만 열리는 범용 도구 |
| `result_list_fields` | 결과에서 행 목록이 담긴 필드명 |
| `window_policy_argument` | 긴 구간에 다른 정책 인자를 받는 도구 |
| `blocked_reason` | 등록은 하되 호출은 막을 때 |

> `temporal_scope`(현재 상태만 답하는 도구인지)는 **도구 서버가 스스로 선언**하며 카탈로그로 전달된다. 레지스트리에 적지 않는다.

### 7.1 확인

```powershell
Set-Location rca-api
.venv/Scripts/python.exe -m pytest -q
```

빠뜨린 것은 대부분 여기서 걸린다 — 표와 `ToolSource`의 불일치, 존재하지 않는 설정 필드명, 스키마가 모르는 근거 타입, 두 소스가 같은 접두사를 주장하는 경우, 등록된 도구의 소스가 표에 없는 경우.

실제 서버 연결은 기동 후 확인한다.

```bash
docker compose up -d --build rca-api
docker compose logs rca-api | grep -i "tool_catalog"
```

---

## 8. 데이터 확인

```powershell
docker compose exec postgres psql -U aiops -d aiops
```

| 테이블 | 내용 |
| --- | --- |
| `aiops_requests`, `aiops_dispatch_queue` | 요청과 작업 큐 |
| `aiops_agent_runs` | 단계별 모델·소요 시간·출력 |
| `aiops_tool_calls` | 개별 도구 호출 |
| `aiops_reports` | 최종 보고서 |
| `aiops_system_errors` | 실행 오류. 조사를 포기한 디스패처가 직접 기록. 이미 `completed`인 요청은 덮어쓰지 않는다 |
| `aiops_report_feedback` | 보고서에 달린 반응 판정 |
| `aiops_report_notes` | 보고서 스레드에 적힌 실제 원인 |
| `aiops_report_templates`, `..._versions` | 템플릿과 변경 이력 |

`aiops_labeled_dataset` 뷰가 질문·근거·결론·판정을 한 행으로 묶는다. 한 보고서에 판정이 여러 개면 가장 나쁜 판정을 채택한다.

```sql
SELECT request_id, question, label, notes
FROM aiops_labeled_dataset
WHERE label IS NOT NULL;
```

요청 상태 조회용 내부 API도 있으나 외부 공개용은 아니다.

```text
GET /internal/requests/:requestId
X-AIOPS-Internal-Token: <AIOPS_INTERNAL_TOKEN>
```

### 8.1 조사 과정 추적

`LANGSMITH_*`를 설정하면 조사가 LangSmith에 기록된다. 요청 하나가 **세 개의 독립 trace**를 남긴다 — 질문 분석, `investigation <request_id>`, 보고서 작성.

가운데 것을 **Details(트리) 뷰**로 열면 노드 경계·루프·각 모델 호출이 그대로 보인다. Turns 뷰는 그래프를 한 턴으로 눌러 버려 흐름이 보이지 않는다.

## 9. 개발 검증

```powershell
Set-Location ingress
npm ci; npm run typecheck; npm test
```

```powershell
Set-Location ../rca-api
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check src tests
```

로그 저장소에 대한 프롬프트의 주장이 아직 유효한지 검사한다. 실제 클러스터가 필요하므로 CI에서는 돌지 않는다.

```bash
python -m aiops_rca.evals.log_store check
```

## 10. 저장소 구조

```text
.
├── database/migrations/    스키마. 매 기동 재적용, 반복 실행에 안전해야 함
├── ingress/                Slack 수신, 게시, 템플릿 동기화, 평가 수집
├── rca-api/                질문 분석 · LangGraph 조사 · 보고서 작성
├── schemas/                단계 간 계약. rca-api 모델이 이 파일로 검증됨
├── templates/              보고서 종류. DB의 원본
├── Caddyfile
├── docker-compose.yml
└── .env.example
```

이 폴더는 MCP 저장소의 파일이나 상위 디렉터리를 참조하지 않는다. 연결 계약은 `.env`의 MCP URL과 토큰뿐이다.
