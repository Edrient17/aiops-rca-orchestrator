# AIOps RCA Orchestrator

Slack 요청을 안전하게 접수하고 n8n에서 Zabbix 증거 조사와 RCA 작성을 실행하는
독립 배포 단위입니다.

## 구성 요소

- `ingress`: Slack 서명 검증, 이벤트 중복 방지, 즉시 ACK, n8n 전달 재시도
- `postgres`: n8n DB와 AIOps 요청·Agent 실행·보고서 감사 데이터
- `n8n-import`: 워크플로가 없을 때만 최초 자동 import하는 일회성 서비스
- `n8n`: 질문 분석 → Zabbix MCP 조사 → RCA 작성 → Slack 게시
- `caddy`: 선택 사항인 공개 HTTPS reverse proxy

Ingress가 요청을 먼저 Postgres에 저장한 뒤 Slack에 200을 응답합니다. AI 실행은
이 응답과 분리되며, n8n이 일시적으로 내려가 있으면 outbox가 최대 5분 간격으로
계속 재시도합니다.

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
- `AIOPS_INTERNAL_TOKEN`: ingress와 n8n만 공유하는 내부 토큰
- `SLACK_*`: Slack App 자격 증명과 질문·답변·오류 채널 ID
- `ZABBIX_MCP_URL`: Zabbix Investigation MCP의 공개 또는 사설 `/mcp` URL
- `N8N_ENCRYPTION_KEY`: 최초 설정 후 절대 변경하지 않는 n8n credential 암호화 키

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
답글이 `aiops_report_notes`에 기록됩니다. 이 되묻기에는 `SLACK_BOT_TOKEN`이
필요하며, 없으면 라벨링은 그대로 동작하고 되묻기만 생략됩니다.

질문 채널의 스레드 답글은 종전대로 명확화 답변으로 처리되며 이 경로의 영향을
받지 않습니다.

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
- n8n: `127.0.0.1:5678`

외부 reverse proxy는 `/slack/events`만 ingress 8080으로 전달하면 됩니다.

n8n editor는 공개하지 않습니다. 포함된 Caddyfile도 `/slack/events` 외의 모든
경로에 404를 반환합니다. editor에는 SSH 터널로 접근합니다.

```powershell
ssh -L 5678:127.0.0.1:5678 <orchestrator-host>
```

editor를 공개하면 Slack Bot Token, MCP Bearer Token, OpenAI credential과 Code
노드의 임의 코드 실행이 owner 로그인 하나에만 의존하게 됩니다. 특히 owner
계정을 만들기 전에 공개하면 먼저 접근한 사람이 owner를 선점할 수 있으므로,
`--profile proxy`로 공개하기 전에 터널로 owner 계정을 먼저 만드십시오.

상태 확인:

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:8080/readyz
Invoke-RestMethod http://127.0.0.1:5678/healthz
```

### 스키마 마이그레이션

`database/migrations/*.sql`은 `db-migrate` 서비스가 매 기동마다 파일 이름
순서로 다시 적용합니다. ingress는 이 서비스가 성공해야 시작하므로, 컬럼을
추가하는 배포가 그 컬럼이 없는 데이터베이스를 상대로 ingress를 띄우는 일은
없습니다. 별도로 실행할 명령은 없고 `docker compose up -d`가 전부입니다.

```powershell
docker compose logs db-migrate
```

새 파일을 추가하든 기존 파일을 고치든 **다시 실행해도 안전해야 합니다**.
`CREATE ... IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, 뷰는 `DROP VIEW IF
EXISTS` 후 재생성을 사용합니다. 적용된 파일 이름을 기록하는 원장은 두지
않았습니다. 원장이 있으면 기존 파일을 제자리에서 고쳤을 때 그 변경이 조용히
건너뛰어집니다 — 실제로 `002_feedback.sql`은 인덱스가 나중에 덧붙는 식으로
수정된 적이 있습니다.

이 파일들은 예전에 `/docker-entrypoint-initdb.d`로 마운트됐습니다. postgres
이미지는 데이터 디렉터리가 비어 있을 때만 그 경로를 실행하므로, 최초 기동
이후 추가된 파일은 이미 존재하는 배포에 한 번도 적용되지 않았습니다.

> 이미 운영 중인 데이터베이스에서 처음 실행하면 `003_message_dedup.sql`이
> 중복 요청 행을 실제로 삭제합니다. 보고서가 달린 행을 남기고 나머지를
> 지우며, 삭제된 행의 보고서는 함께 지워집니다. 먼저 확인하려면:
>
> ```sql
> SELECT channel_id, message_ts, count(*)
> FROM aiops_requests GROUP BY 1, 2 HAVING count(*) > 1;
> ```

## 4. n8n 최초 설정

`AIOPS_PUBLIC_URL`을 열고 owner 계정을 만든 다음 import된 두 워크플로를
확인합니다.

`AIOps - Slack to Zabbix RCA`에서:

1. OpenAI API credential을 만들고 `Question Model`, `Investigation Model`,
   `RCA Model`에 지정합니다.
2. HTTP Bearer Auth credential을 만들고 토큰 값에 Zabbix Investigation MCP의
   `ZABBIX_MCP_AUTH_TOKEN`을 입력합니다.
3. 이 credential을 `Zabbix MCP Tools` 노드에 지정합니다.
4. 각 Agent의 수동 테스트를 실행합니다.
5. `AIOps - Error Handler`와 메인 워크플로를 저장하고 Publish합니다.

워크플로 파일은 최초 1회만 import됩니다. `n8n-import`가 n8n 데이터 볼륨의
`/home/node/.n8n/.aiops-workflows-imported` 마커를 확인해, 마커가 있으면 아무
것도 하지 않고 종료합니다. 따라서 재시작이나 `.env` 변경으로 컨테이너가
재생성되어도 UI에서 수정한 내용(특히 credential 연결)을 덮어쓰지 않습니다.

파일의 새 버전을 반영할 때는 orchestrator 호스트에서 재배포 스크립트를
사용합니다.

```bash
python3 scripts/redeploy-workflow.py
```

import는 워크플로의 노드 목록을 통째로 교체하므로 UI에서 지정한 credential
연결이 사라지고, import 직후 워크플로가 비활성화되며, 변경은 n8n 재시작 후에야
반영됩니다. 스크립트가 이 세 가지를 순서대로 처리합니다 — 현재 credential
연결을 새 파일에 옮겨 담고, import한 뒤, 재활성화하고 재시작합니다.

n8n을 재시작하면 **실행 중이던 조사가 중단됩니다.** 중단된 실행은 자신의 실패를
보고하지도 못합니다. n8n이 이미 닫은 DB 커넥션으로 오류 워크플로를 호출하려다
실패하므로, 해당 요청은 Slack에 아무 설명도 남기지 못한 채 방치됩니다. 그래서
스크립트는 진행 중인 실행이 있으면 중단합니다.

```bash
python3 scripts/redeploy-workflow.py --wait 300   # 끝날 때까지 최대 5분 대기
python3 scripts/redeploy-workflow.py --force      # 손실을 감수하고 강행
```

UI에서 직접 import해도 되지만, 그 경우 credential을 다시 지정해야 합니다.

## 5. 보안 경계

- 공개되어야 하는 ingress 경로는 `/slack/events` 하나뿐입니다.
- ingress의 `/internal/*`는 외부 reverse proxy에 연결하지 않습니다.
- Postgres는 Docker 내부 네트워크에만 존재합니다.
- Zabbix Investigation MCP는 Bearer 인증과 허용 호스트 그룹을 함께 설정합니다.
- n8n editor는 owner 계정과 HTTPS로 보호합니다.
- Slack Signing Secret, Bot Token, OpenAI key, MCP token은 저장소에 커밋하지
  않습니다.

## 보고서 템플릿

문서 종류마다 **무엇을 수집하고 어떤 구성으로 쓸지**를 DB 행 하나로 정의합니다.
장애 RCA와 월말 용량 보고서는 볼 메트릭도, 기간도, 문서 구성도 다른데, 이를
워크플로 분기로 넣으면 종류를 늘릴 때마다 재배포해야 합니다.

장애 RCA는 `incident_rca` 템플릿으로 시드되어 있습니다. 질문이 어떤 템플릿에도
맞지 않으면 여기로 떨어지므로 이 행이 없으면 조사가 실패합니다. 마이그레이션이
매 배포마다 `ON CONFLICT DO NOTHING`으로 복원하니, 실수로 지워도 다음 배포에서
원래 문구로 돌아옵니다. 수정한 내용은 덮어쓰지 않습니다.

추가와 수정은 같은 요청입니다. 오케스트레이터 호스트에서:

```bash
curl -X PUT http://127.0.0.1:8080/internal/templates/monthly_capacity_report \
  -H "X-AIOPS-Internal-Token: $AIOPS_INTERNAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "월말 용량 보고서",
    "description": "월말이나 정기 용량·가용성 요약을 요청할 때 고른다",
    "collection": {
      "host_selector": { "mode": "host_group", "group_ids": ["10"] },
      "window": { "policy": "long_term_capacity", "range": "last_calendar_month" },
      "aggregation": "1d",
      "metric_keywords": ["disk", "cpu", "memory"],
      "guidance": "호스트별 이벤트를 먼저 훑고 사건이 있는 곳만 깊게 본다."
    },
    "output": {
      "sections": [
        { "id": "summary", "heading": "요약",
          "instruction": "한 달간 전반 상태를 3문장 이내로" },
        { "id": "capacity_trend", "heading": "용량 추세",
          "instruction": "호스트별 디스크 증가율" }
      ],
      "guidance": "존댓말은 요약에만 쓴다."
    }
  }'
```

- `description`은 질문 분석 Agent가 읽습니다. 문서에 무엇이 담기는지가 아니라
  **어떤 질문일 때 이걸 고르는지**를 적으십시오.
- `GET /internal/templates`가 사용 가능한 목록, `?all=true`면 비활성 포함입니다.
- `DELETE /internal/templates/:id`로 제거합니다. 잠시 내리기만 할 때는
  `enabled: false`로 저장하는 편이 낫습니다 — 그 템플릿으로 발행된 보고서가
  가리키는 대상이 사라지지 않습니다.

내용이 실제로 달라졌을 때만 `version`이 오릅니다. 같은 내용을 다시 보내면
`{"changed": false}`가 돌아옵니다. 모든 판이 `aiops_report_template_versions`에
남으므로, 템플릿을 지웠다 다시 만들어도 버전 번호는 이어집니다. 과거 보고서가
어떤 양식으로 만들어졌는지 되짚을 수 있어야 하기 때문입니다.

`sections`가 문서의 구성입니다. `id`로 작성 Agent의 출력과 짝지어지므로 제목을
바꿔도 무방하지만 `id`는 바꾸지 마십시오. `required: false`인 칸은 쓸 내용이
없으면 보고서에서 빠지고, `true`면 "해당 없음"으로 남습니다.
`requires_problem_event: true`인 칸은 **실제 Zabbix 문제 이벤트가 있을 때만**
나옵니다 — 장애 시각처럼 사건이 없으면 존재할 수 없는 내용을 위한 것으로,
멀쩡한 호스트에 없던 장애를 지어내는 것을 막습니다.

잘못된 템플릿은 저장 시점에 거절됩니다. 템플릿은 조사 도중 Agent의 프롬프트가
되므로, 런타임에 발견되면 이미 접수·응답까지 끝난 질문이 실패합니다.

## 데이터 확인

```powershell
docker compose exec postgres psql -U aiops -d aiops
```

주요 테이블:

- `aiops_requests`
- `aiops_dispatch_queue`
- `aiops_agent_runs`
- `aiops_tool_calls`
- `aiops_reports`
- `aiops_system_errors`: 실행 오류. n8n은 오류 워크플로에 실행 ID만 넘기므로,
  메인 워크플로가 시작 직후 기록해 둔 `aiops_requests.n8n_execution_id`로
  어느 요청이었는지 되찾습니다. 그래야 실패한 요청이 진행 중 상태에 남지 않고
  `failed`로 정리됩니다. 이미 `completed`인 요청은 덮어쓰지 않습니다.
- `aiops_report_feedback`: 보고서에 달린 반응 판정
- `aiops_report_notes`: 보고서 스레드에 적힌 실제 원인
- `aiops_report_templates`: 문서 종류별 수집 범위와 출력 구성
- `aiops_report_template_versions`: 템플릿의 모든 판. 템플릿을 지워도 남습니다

`aiops_labeled_dataset` 뷰가 질문·증거·결론·판정을 한 행으로 묶어 줍니다. 학습
데이터나 RAG 색인의 입력으로 그대로 쓸 수 있습니다. 한 보고서에 판정이 여러 개면
가장 나쁜 판정을 채택합니다. 한 명이 틀렸다고 하면 다른 사람이 맞다고 해도 틀린
쪽으로 기록하는 편이 데이터셋에서는 안전합니다.

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

## 개발 검증

```powershell
Set-Location ingress
npm ci
npm run typecheck
npm test
npm run build
```

워크플로 재생성:

```powershell
Set-Location ..
node scripts/generate-workflows.mjs
```

## 저장소 구조

```text
.
├── database/
│   └── migrations/
├── ingress/
│   ├── src/
│   └── tests/
├── prompts/
├── schemas/
├── scripts/
├── workflows/
├── Caddyfile
├── docker-compose.yml
└── .env.example
```

이 폴더는 Zabbix Investigation MCP 저장소의 파일이나 상위 디렉터리를 참조하지
않습니다. 연결 계약은 `.env`의 `ZABBIX_MCP_URL`과 n8n의 MCP Bearer
credential입니다.
