# n8n workflows

`generate-workflows.mjs`가 기존 `prompts/`와 `schemas/`를 읽어 다음 파일을
생성합니다.

- `01-aiops-main.json`: Slack 접수 이후 질문 분석 → MCP 조사 → RCA 게시
- `99-aiops-error-handler.json`: 실행 오류 기록 및 오류 채널 통지

Schema 원본의 로컬 `$ref`는 n8n Structured Output Parser가 올바르게 처리하지
못하므로 생성 시 인라인으로 평탄화합니다. 원본 Schema는 변경하지 않습니다.

재생성:

```powershell
node ..\scripts\generate-workflows.mjs
```

워크플로를 import한 뒤 다음 자격 증명을 직접 지정해야 합니다.

1. `Question Model`, `Investigation Model`, `RCA Model`: 같은 OpenAI API credential
2. `Zabbix MCP Tools`: Zabbix Investigation MCP의 `ZABBIX_MCP_AUTH_TOKEN`을
   담은 HTTP Bearer Auth

오류 처리 워크플로를 먼저 활성화한 뒤 메인 워크플로를 활성화합니다.
