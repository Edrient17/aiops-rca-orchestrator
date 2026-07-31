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

Event Subscriptions:

- Request URL: `https://<AIOPS_DOMAIN>/slack/events`
- Bot event: `app_mention`
- 멘션 없이 질문 채널의 모든 메시지를 받을 경우 `message.channels`

Bot을 질문·답변·오류 채널에 초대하고 각 채널 ID를 `.env`에 입력합니다. 특정
사용자만 허용하려면 `SLACK_ALLOWED_USER_IDS`에 쉼표로 구분한 User ID를 넣습니다.

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

외부 reverse proxy는 `/slack/events`를 ingress 8080으로, 나머지 n8n 경로를
5678로 전달해야 합니다.

상태 확인:

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:8080/readyz
Invoke-RestMethod http://127.0.0.1:5678/healthz
```

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

워크플로 파일은 컨테이너 시작 시 DB에 해당 ID가 없을 때만 import됩니다. 따라서
n8n UI에서 수정한 내용은 재시작으로 덮어쓰지 않습니다. 파일의 새 버전을
강제로 반영할 때는 UI에서 import하거나 n8n CLI를 명시적으로 실행합니다.

## 5. 보안 경계

- 공개되어야 하는 ingress 경로는 `/slack/events` 하나뿐입니다.
- ingress의 `/internal/*`는 외부 reverse proxy에 연결하지 않습니다.
- Postgres는 Docker 내부 네트워크에만 존재합니다.
- Zabbix Investigation MCP는 Bearer 인증과 허용 호스트 그룹을 함께 설정합니다.
- n8n editor는 owner 계정과 HTTPS로 보호합니다.
- Slack Signing Secret, Bot Token, OpenAI key, MCP token은 저장소에 커밋하지
  않습니다.

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
- `aiops_system_errors`

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
│   └── init/
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
