import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outputDir = resolve(scriptDir, "..", "workflows");

async function main() {
  const [
    questionPrompt,
    evidencePrompt,
    rcaPrompt,
    parsedSchema,
    evidenceSchema,
    rcaSchema,
  ] = await Promise.all([
    readFile(resolve(rootDir, "prompts", "question-analyzer.system.md"), "utf8"),
    readFile(resolve(rootDir, "prompts", "evidence-collector.system.md"), "utf8"),
    readFile(resolve(rootDir, "prompts", "rca-writer.system.md"), "utf8"),
    readJson(resolve(rootDir, "schemas", "parsed-request.schema.json")),
    readJson(resolve(rootDir, "schemas", "evidence-package.schema.json")),
    readJson(resolve(rootDir, "schemas", "rca-report.schema.json")),
  ]);

  const mainWorkflowId = "aiops-main-v010";
  const errorWorkflowId = "aiops-error-v010";

  const mainWorkflow = buildMainWorkflow({
    questionPrompt,
    evidencePrompt,
    rcaPrompt,
    parsedSchema: flattenLocalRefs(parsedSchema),
    evidenceSchema: flattenLocalRefs(evidenceSchema),
    rcaSchema: flattenLocalRefs(rcaSchema),
    mainWorkflowId,
    errorWorkflowId,
  });
  const errorWorkflow = buildErrorWorkflow(errorWorkflowId);

  await mkdir(outputDir, { recursive: true });
  await Promise.all([
    writeWorkflow("01-aiops-main.json", mainWorkflow),
    writeWorkflow("99-aiops-error-handler.json", errorWorkflow),
  ]);

  console.log(`Generated 2 workflows in ${outputDir}`);
}

function buildMainWorkflow(input) {
  const nodes = [
    node(
      "AIOps Internal Webhook",
      "n8n-nodes-base.webhook",
      2.1,
      [0, 0],
      {
        httpMethod: "POST",
        path: "aiops-process",
        responseMode: "onReceived",
        responseData: "allEntries",
        options: {
          responseCode: 202,
        },
      },
      { webhookId: "aiops-process-v010" },
    ),
    codeNode("Normalize Request", [240, 0], normalizeRequestCode),
    codeNode("Format ACK", [360, 0], formatAckCode),
    httpNode(
      "Post Business ACK",
      "POST",
      "https://slack.com/api/chat.postMessage",
      // A continuation posts into the thread its parent already owns, so one
      // investigation occupies one thread however many clarifications it took.
      // Slack echoes thread_ts back, which is what the report then anchors to.
      "={{ JSON.stringify({ channel: $env.SLACK_ANSWER_CHANNEL_ID, text: $('Format ACK').first().json.text, ...($('Normalize Request').first().json.parent_ack_ts ? { thread_ts: $('Normalize Request').first().json.parent_ack_ts } : {}) }) }}",
      [480, 0],
      "slack",
    ),
    codeNode("Assert ACK Posted", [720, 0], assertSlackCode),
    httpNode(
      "Mark Analyzing",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/status' }}",
      // Record the anchor while it is at hand. A continuation reuses its
      // parent's, so store that rather than this reply's own ts.
      "={{ JSON.stringify({ status: 'analyzing_question', slack_ack_ts: $('Post Business ACK').first().json.thread_ts || $('Post Business ACK').first().json.ts }) }}",
      [960, 0],
      "internal",
    ),
    codeNode("Stamp Question Start", [1080, 0], stampCode),
    agentNode(
      "Question Analyzer",
      [1200, 0],
      input.questionPrompt,
      "={{ JSON.stringify({ request_id: $('Normalize Request').first().json.request_id, question: $('Normalize Request').first().json.question, slack_received_at: $('Normalize Request').first().json.received_at, default_timezone: 'Asia/Seoul', prior_question: $('Normalize Request').first().json.prior_question, answers_clarification: Boolean($('Normalize Request').first().json.parent_request_id) }, null, 2) }}",
      3,
    ),
    modelNode("Question Model", [1120, 260], "gpt-5.4-mini", "low"),
    parserNode("Parsed Request Parser", [1320, 260], input.parsedSchema),
    httpNode(
      "Persist Question Result",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/agent-runs' }}",
      "={{ JSON.stringify({ stage: 'question_analyzer', status: 'succeeded', model: 'gpt-5.4-mini', duration_ms: Date.now() - $('Stamp Question Start').first().json.stage_started_ms, output: $('Question Analyzer').first().json.output }) }}",
      [1440, 0],
      "internal",
    ),
    ifNode(
      "Request Ready?",
      "={{ $('Question Analyzer').first().json.output.parse_status }}",
      "ready",
      [1680, 0],
    ),
    codeNode("Stamp Evidence Start", [1800, -140], stampCode),
    agentNode(
      "Evidence Collector",
      [1920, -140],
      input.evidencePrompt,
      "={{ JSON.stringify({ parsed_request: $('Question Analyzer').first().json.output, slack_context: $('Normalize Request').first().json, limits: { max_iterations: 8, max_tool_calls: 30, max_window_hours: 24 } }, null, 2) }}",
      8,
    ),
    modelNode("Investigation Model", [1840, 140], "gpt-5.4", "medium"),
    parserNode("Evidence Package Parser", [2040, 140], input.evidenceSchema),
    mcpNode("Zabbix MCP Tools", [2240, 140]),
    httpNode(
      "Persist Evidence Result",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/agent-runs' }}",
      "={{ JSON.stringify({ stage: 'evidence_collector', status: 'succeeded', model: 'gpt-5.4', duration_ms: Date.now() - $('Stamp Evidence Start').first().json.stage_started_ms, output: $('Evidence Collector').first().json.output }) }}",
      [2160, -140],
      "internal",
    ),
    codeNode("Stamp RCA Start", [2280, -140], stampCode),
    agentNode(
      "RCA Writer",
      [2400, -140],
      input.rcaPrompt,
      "={{ JSON.stringify({ parsed_request: $('Question Analyzer').first().json.output, evidence_package: $('Evidence Collector').first().json.output }, null, 2) }}",
      3,
    ),
    modelNode("RCA Model", [2360, 140], "gpt-5.4-mini", "medium"),
    parserNode("RCA Report Parser", [2560, 140], input.rcaSchema),
    httpNode(
      "Persist RCA Result",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/agent-runs' }}",
      "={{ JSON.stringify({ stage: 'rca_writer', status: 'succeeded', model: 'gpt-5.4-mini', duration_ms: Date.now() - $('Stamp RCA Start').first().json.stage_started_ms, output: $('RCA Writer').first().json.output }) }}",
      [2640, -140],
      "internal",
    ),
    codeNode("Format RCA for Slack", [2880, -140], formatRcaCode),
    httpNode(
      "Post RCA Report",
      "POST",
      "https://slack.com/api/chat.postMessage",
      // thread_ts when the ACK was itself a reply, ts when it started the thread.
      "={{ JSON.stringify({ channel: $env.SLACK_ANSWER_CHANNEL_ID, thread_ts: $('Post Business ACK').first().json.thread_ts || $('Post Business ACK').first().json.ts, text: $('Format RCA for Slack').first().json.slack_markdown }) }}",
      [3120, -140],
      "slack",
    ),
    codeNode("Assert RCA Posted", [3360, -140], assertSlackCode),
    httpNode(
      "Save Completed Report",
      "PUT",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/report' }}",
      "={{ JSON.stringify({ parsed_request: $('Question Analyzer').first().json.output, evidence_package: $('Evidence Collector').first().json.output, rca_report: $('RCA Writer').first().json.output, slack_markdown: $('Format RCA for Slack').first().json.slack_markdown, slack_channel_id: $env.SLACK_ANSWER_CHANNEL_ID, slack_message_ts: $('Post RCA Report').first().json.ts }) }}",
      [3600, -140],
      "internal",
    ),
    codeNode("Format Clarification", [1920, 360], formatClarificationCode),
    httpNode(
      "Post Clarification",
      "POST",
      "https://slack.com/api/chat.postMessage",
      // Asked in the thread of the user's own message in the question channel:
      // the answer has to land somewhere ingress listens, and a reply there
      // carries thread_ts, which is what links it back to this request.
      "={{ JSON.stringify({ channel: $('Normalize Request').first().json.channel_id, thread_ts: $('Normalize Request').first().json.thread_ts || $('Normalize Request').first().json.message_ts, text: $('Format Clarification').first().json.text }) }}",
      [2160, 360],
      "slack",
    ),
    codeNode("Assert Clarification Posted", [2400, 360], assertSlackCode),
    httpNode(
      "Mark Needs Clarification",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/status' }}",
      "={{ JSON.stringify({ status: $('Question Analyzer').first().json.output.parse_status }) }}",
      [2640, 360],
      "internal",
    ),
  ];

  const connections = {};
  connectMain(connections, "AIOps Internal Webhook", "Normalize Request");
  connectMain(connections, "Normalize Request", "Format ACK");
  connectMain(connections, "Format ACK", "Post Business ACK");
  connectMain(connections, "Post Business ACK", "Assert ACK Posted");
  connectMain(connections, "Assert ACK Posted", "Mark Analyzing");
  connectMain(connections, "Mark Analyzing", "Stamp Question Start");
  connectMain(connections, "Stamp Question Start", "Question Analyzer");
  connectMain(connections, "Question Analyzer", "Persist Question Result");
  connectMain(connections, "Persist Question Result", "Request Ready?");
  connectMain(connections, "Request Ready?", "Stamp Evidence Start", 0);
  connectMain(connections, "Request Ready?", "Format Clarification", 1);
  connectMain(connections, "Stamp Evidence Start", "Evidence Collector");
  connectMain(connections, "Evidence Collector", "Persist Evidence Result");
  connectMain(connections, "Persist Evidence Result", "Stamp RCA Start");
  connectMain(connections, "Stamp RCA Start", "RCA Writer");
  connectMain(connections, "RCA Writer", "Persist RCA Result");
  connectMain(connections, "Persist RCA Result", "Format RCA for Slack");
  connectMain(connections, "Format RCA for Slack", "Post RCA Report");
  connectMain(connections, "Post RCA Report", "Assert RCA Posted");
  connectMain(connections, "Assert RCA Posted", "Save Completed Report");
  connectMain(connections, "Format Clarification", "Post Clarification");
  connectMain(connections, "Post Clarification", "Assert Clarification Posted");
  connectMain(connections, "Assert Clarification Posted", "Mark Needs Clarification");

  connectAi(connections, "Question Model", "Question Analyzer", "ai_languageModel");
  connectAi(connections, "Parsed Request Parser", "Question Analyzer", "ai_outputParser");
  connectAi(connections, "Investigation Model", "Evidence Collector", "ai_languageModel");
  connectAi(connections, "Evidence Package Parser", "Evidence Collector", "ai_outputParser");
  connectAi(connections, "Zabbix MCP Tools", "Evidence Collector", "ai_tool");
  connectAi(connections, "RCA Model", "RCA Writer", "ai_languageModel");
  connectAi(connections, "RCA Report Parser", "RCA Writer", "ai_outputParser");

  return {
    id: input.mainWorkflowId,
    name: "AIOps - Slack to Zabbix RCA",
    active: false,
    nodes,
    connections,
    settings: {
      executionOrder: "v1",
      errorWorkflow: input.errorWorkflowId,
      saveDataErrorExecution: "all",
      saveDataSuccessExecution: "all",
      saveManualExecutions: true,
      callerPolicy: "workflowsFromSameOwner",
      // A hard ceiling on the whole run. maxIterations bounds each agent, but
      // only this bounds the request as a whole, so a stuck model or a slow
      // Zabbix cannot burn tokens indefinitely. A healthy run takes ~2 minutes.
      executionTimeout: 900,
    },
    staticData: null,
    pinData: {},
    versionId: stableUuid("main-workflow-version"),
    meta: {
      templateCredsSetupCompleted: false,
    },
    tags: [],
  };
}

function buildErrorWorkflow(workflowId) {
  const nodes = [
    node("Error Trigger", "n8n-nodes-base.errorTrigger", 1, [0, 0], {}),
    codeNode("Format Workflow Error", [260, 0], formatErrorCode),
    httpNode(
      "Record Workflow Error",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/errors' }}",
      "={{ JSON.stringify($('Format Workflow Error').first().json.error_record) }}",
      [540, -100],
      "internal",
    ),
    httpNode(
      "Post Error Alert",
      "POST",
      "https://slack.com/api/chat.postMessage",
      "={{ JSON.stringify({ channel: $env.SLACK_ERROR_CHANNEL_ID, text: $('Format Workflow Error').first().json.slack_text }) }}",
      [540, 120],
      "slack",
    ),
  ];

  const connections = {};
  connectMain(connections, "Error Trigger", "Format Workflow Error");
  connectMain(connections, "Format Workflow Error", "Record Workflow Error");
  connectMain(connections, "Format Workflow Error", "Post Error Alert");

  return {
    id: workflowId,
    name: "AIOps - Error Handler",
    active: false,
    nodes,
    connections,
    settings: {
      executionOrder: "v1",
      saveDataErrorExecution: "all",
      saveDataSuccessExecution: "all",
      saveManualExecutions: true,
    },
    staticData: null,
    pinData: {},
    versionId: stableUuid("error-workflow-version"),
    meta: {
      templateCredsSetupCompleted: false,
    },
    tags: [],
  };
}

function node(name, type, typeVersion, position, parameters, extras = {}) {
  return {
    parameters,
    id: stableUuid(`node:${name}`),
    name,
    type,
    typeVersion,
    position,
    ...extras,
  };
}

function codeNode(name, position, jsCode) {
  return node(name, "n8n-nodes-base.code", 2, position, {
    mode: "runOnceForAllItems",
    jsCode,
  });
}

// maxIterations is the only hard ceiling n8n enforces on an agent, so it is set
// per agent rather than left at a single default: the investigator needs room to
// iterate over tools, while the two agents that call no tools should never loop.
function agentNode(name, position, systemMessage, text, maxIterations) {
  return node(name, "@n8n/n8n-nodes-langchain.agent", 3.1, position, {
    promptType: "define",
    text,
    hasOutputParser: true,
    needsFallback: false,
    options: {
      systemMessage,
      maxIterations,
      returnIntermediateSteps: false,
    },
  });
}

function modelNode(name, position, model, reasoningEffort) {
  return node(name, "@n8n/n8n-nodes-langchain.lmChatOpenAi", 1.3, position, {
    model: {
      __rl: true,
      mode: "id",
      value: model,
    },
    responsesApiEnabled: true,
    builtInTools: {},
    options: {
      reasoningEffort,
    },
  });
}

function parserNode(name, position, schema) {
  return node(
    name,
    "@n8n/n8n-nodes-langchain.outputParserStructured",
    1.3,
    position,
    {
      schemaType: "manual",
      inputSchema: JSON.stringify(schema, null, 2),
      autoFix: false,
    },
  );
}

function mcpNode(name, position) {
  return node(name, "@n8n/n8n-nodes-langchain.mcpClientTool", 1.4, position, {
    endpointUrl: "={{ $env.ZABBIX_MCP_URL }}",
    serverTransport: "httpStreamable",
    authentication: "bearerAuth",
    include: "all",
    options: {
      timeout: 120_000,
    },
  });
}

function httpNode(name, method, url, jsonBody, position, authKind) {
  const headers =
    authKind === "slack"
      ? [
          {
            name: "Authorization",
            value: "=Bearer {{ $env.SLACK_BOT_TOKEN }}",
          },
          {
            name: "Content-Type",
            value: "application/json; charset=utf-8",
          },
        ]
      : [
          {
            name: "X-AIOPS-Internal-Token",
            value: "={{ $env.AIOPS_INTERNAL_TOKEN }}",
          },
          {
            name: "Content-Type",
            value: "application/json",
          },
        ];

  return node(name, "n8n-nodes-base.httpRequest", 4.2, position, {
    method,
    url,
    sendHeaders: true,
    headerParameters: {
      parameters: headers,
    },
    sendBody: true,
    contentType: "raw",
    rawContentType: "application/json",
    body: jsonBody,
    options: {
      timeout: authKind === "slack" ? 30_000 : 10_000,
    },
  });
}

function ifNode(name, leftValue, rightValue, position) {
  return node(name, "n8n-nodes-base.if", 2.2, position, {
    conditions: {
      options: {
        caseSensitive: true,
        leftValue: "",
        typeValidation: "strict",
        version: 2,
      },
      conditions: [
        {
          id: stableUuid(`condition:${name}`),
          leftValue,
          rightValue,
          operator: {
            type: "string",
            operation: "equals",
          },
        },
      ],
      combinator: "and",
    },
    options: {},
  });
}

function connectMain(connections, from, to, outputIndex = 0) {
  connections[from] ??= {};
  connections[from].main ??= [];
  while (connections[from].main.length <= outputIndex) {
    connections[from].main.push([]);
  }
  connections[from].main[outputIndex].push({
    node: to,
    type: "main",
    index: 0,
  });
}

function connectAi(connections, from, to, type) {
  connections[from] = {
    ...(connections[from] ?? {}),
    [type]: [
      [
        {
          node: to,
          type,
          index: 0,
        },
      ],
    ],
  };
}

function flattenLocalRefs(schema) {
  const root = structuredClone(schema);

  function visit(value) {
    if (Array.isArray(value)) {
      return value.map(visit);
    }
    if (typeof value !== "object" || value === null) {
      return value;
    }
    if (typeof value.$ref === "string" && value.$ref.startsWith("#/")) {
      const resolved = resolvePointer(root, value.$ref);
      const siblings = Object.fromEntries(
        Object.entries(value).filter(([key]) => key !== "$ref"),
      );
      return visit({ ...structuredClone(resolved), ...siblings });
    }

    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !["$schema", "$id", "$defs"].includes(key))
        .map(([key, child]) => [key, visit(child)]),
    );
  }

  return visit(root);
}

function resolvePointer(root, pointer) {
  return pointer
    .slice(2)
    .split("/")
    .map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"))
    .reduce((current, part) => current[part], root);
}

async function readJson(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

async function writeWorkflow(filename, workflow) {
  await writeFile(
    resolve(outputDir, filename),
    `${JSON.stringify(workflow, null, 2)}\n`,
    "utf8",
  );
}

function stableUuid(value) {
  const hash = createHash("sha256").update(value).digest("hex").slice(0, 32).split("");
  hash[12] = "5";
  hash[16] = "8";
  return `${hash.slice(0, 8).join("")}-${hash.slice(8, 12).join("")}-${hash
    .slice(12, 16)
    .join("")}-${hash.slice(16, 20).join("")}-${hash.slice(20).join("")}`;
}

const normalizeRequestCode = String.raw`
const envelope = $input.first().json;
const headers = envelope.headers ?? {};
if (headers['x-aiops-internal-token'] !== $env.AIOPS_INTERNAL_TOKEN) {
  throw new Error('Unauthorized internal webhook request');
}
const body = envelope.body ?? envelope;
const required = ['request_id', 'slack_event_id', 'channel_id', 'user_id', 'message_ts', 'question', 'received_at'];
for (const field of required) {
  if (typeof body[field] !== 'string' || body[field].length === 0) {
    throw new Error('Invalid normalized request: missing ' + field);
  }
}
return [{ json: body }];
`.trim();

const assertSlackCode = String.raw`
const response = $input.first().json;
if (response.ok !== true) {
  throw new Error('Slack API error: ' + (response.error ?? 'unknown_error'));
}
return $input.all();
`.trim();

// Records when a stage begins so the persist call after it can report how long
// the stage took. Without this the agent_runs.duration_ms column stays empty and
// per-stage latency can only be recovered by parsing n8n's internal run data.
const stampCode = String.raw`
return [{ json: { ...$input.first().json, stage_started_ms: Date.now() } }];
`.trim();

// A request that answers a clarification is a continuation, not a new question.
// Announcing it with the reply text alone ("아 midibus-docker-ftp03에서") reads
// as nonsense in the answer channel, where the original question is not visible.
const formatAckCode = String.raw`
const request = $('Normalize Request').first().json;
const continues = Boolean(request.parent_request_id);

// A continuation lands inside the parent's thread, where the original question
// is already on screen, so it only needs to report what was added. Without a
// parent anchor it starts its own thread and must carry the question itself.
if (continues && request.parent_ack_ts) {
  return [{ json: { text: [
    '🔎 *조사 재개* — 보충된 정보로 이어서 조사합니다.',
    '• 요청 ID: \`' + request.request_id + '\`',
    '• 보충된 정보: ' + request.question,
  ].join('\n') } }];
}

const lines = ['🔎 *AIOps 조사 접수*', '• 요청 ID: \`' + request.request_id + '\`'];
if (continues && request.prior_question) {
  lines.push('• 원래 질문: ' + request.prior_question);
  lines.push('• 보충된 정보: ' + request.question);
} else {
  lines.push('• 질문: ' + request.question);
}
return [{ json: { text: lines.join('\n') } }];
`.trim();

const formatClarificationCode = String.raw`
const request = $('Normalize Request').first().json;
const parsed = $('Question Analyzer').first().json.output;
const ambiguities = Array.isArray(parsed.ambiguities) ? parsed.ambiguities : [];
const unsupported = parsed.parse_status === 'unsupported';

const lines = ambiguities.length > 0
  ? ambiguities.map((item) => '• ' + item)
  : ['• 조사할 호스트와 기준 시각을 알려주세요.'];

const header = unsupported
  ? '⛔ *지원 범위를 벗어난 요청*'
  : '❓ *조사에 필요한 정보가 더 있습니다*';

const sections = [
  header,
  '• 요청 ID: \`' + request.request_id + '\`',
  lines.join('\n'),
];

// Only invite a reply when one would actually help. An unsupported request
// will not become supported by answering.
if (!unsupported) {
  sections.push('_이 스레드에 저를 멘션해서 답해주시면 원래 질문과 함께 이어서 조사합니다._');
}

return [{ json: { text: sections.join('\n\n') } }];
`.trim();

const formatRcaCode = String.raw`
const request = $('Normalize Request').first().json;
const parsed = $('Question Analyzer').first().json.output;
const evidence = $('Evidence Collector').first().json.output;
const rca = $('RCA Writer').first().json.output;

// Evidence IDs are long enough to swamp the prose they support, so cite them as
// footnote markers and print the mapping once at the end. Traceability survives;
// the reader is not asked to parse zbx:metric:118168:1785479458-... mid-sentence.
const refs = [];
const refNumber = (id) => {
  const seen = refs.indexOf(id);
  if (seen >= 0) return seen + 1;
  refs.push(id);
  return refs.length;
};
const cite = (ids) => (Array.isArray(ids) && ids.length > 0)
  ? ' ' + ids.map((id) => '[' + refNumber(id) + ']').join('')
  : '';

const bullets = (items, emptyText) => {
  if (!Array.isArray(items) || items.length === 0) return '• _' + emptyText + '_';
  return items
    .map((item) => '• ' + (typeof item === 'string' ? item : JSON.stringify(item)))
    .join('\n');
};

const asTime = (value) => {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return String(value);
  return parsed.toLocaleString('sv-SE', { timeZone: 'Asia/Seoul' });
};

const section = (heading, body) => '*' + heading + '*\n' + body;

// --- 장애 개요 ------------------------------------------------------------
// Incident timing is only meaningful when a problem event was actually found.
// The writer has been observed copying the investigation window into these
// fields on a healthy host, which renders as an hour-long outage that never
// happened. A real Zabbix event id is numeric; the agent also emits synthetic
// ids such as zbx:event:no-problem-events-... to record that it looked and
// found nothing, and those must not license a timeline.
const incident = rca.incident || {};
const backedByEvent = (evidence.evidence || []).some(
  (item) => /^zbx:event:\d+$/.test(item.evidence_id || ''),
);
const overview = ['• 관측된 형태: ' + (incident.observed_failure_mode || '확인 불가')];
if (backedByEvent) {
  if (incident.started_at) overview.push('• 발생: ' + asTime(incident.started_at));
  if (incident.recovered_at) overview.push('• 복구: ' + asTime(incident.recovered_at));
  if (typeof incident.duration_seconds === 'number') {
    overview.push('• 지속: ' + Math.round(incident.duration_seconds / 60) + '분');
  }
}

// --- 영향 ----------------------------------------------------------------
const impact = rca.impact || {};
const impactLines = [];
for (const item of impact.confirmed || []) impactLines.push('• *확인됨* ' + item);
for (const item of impact.unconfirmed || []) impactLines.push('• _미확인_ ' + item);
if (impactLines.length === 0) impactLines.push('• _영향이 확인되지 않음_');

// --- 조사 범위 ------------------------------------------------------------
const finalWindow = evidence.investigation && evidence.investigation.final_window;
const windowLine = finalWindow
  ? '• ' + asTime(finalWindow.from) + ' ~ ' + asTime(finalWindow.to) + ' (KST)'
  : '• _확인 불가_';

// --- 확인된 사실 ----------------------------------------------------------
const facts = (rca.confirmed_facts || []).map(
  (item) => '• ' + item.fact + cite(item.evidence_refs),
);

// --- 타임라인 -------------------------------------------------------------
const timeline = (rca.timeline || []).map(
  (item) => '• \`' + asTime(item.time) + '\`  ' + item.description + cite(item.evidence_refs),
);

// --- 관련 신호 ------------------------------------------------------------
const signals = (rca.related_signals || []).map((item) => {
  const relation = item.relationship ? ' _(' + item.relationship + ')_' : '';
  return '• ' + item.description + relation + cite(item.evidence_refs);
});

// --- 원인 후보 ------------------------------------------------------------
const candidates = (rca.root_cause_candidates || []).map((item) => {
  const lines = ['• *[' + String(item.confidence || '?').toUpperCase() + ']* ' + item.description];
  const support = cite(item.supporting_evidence_refs);
  const against = cite(item.contradicting_evidence_refs);
  if (support) lines.push('    ↳ 근거' + support);
  if (against) lines.push('    ↳ 반박' + against);
  return lines.join('\n');
});

const sections = [
  '📋 *' + rca.title + '*',
  '• 요청 ID: \`' + request.request_id + '\`\n• 호스트: ' + (incident.host || '확인 불가') +
    '\n• 심각도: ' + (incident.severity || '확인 불가'),
  section('요약', rca.executive_summary),
  section('장애 개요', overview.join('\n')),
  section('영향', impactLines.join('\n')),
  section('조사 범위', windowLine),
  section('확인된 사실', facts.length ? facts.join('\n') : '• _확인된 사실 없음_'),
  section('타임라인', timeline.length ? timeline.join('\n') : '• _기록된 사건 없음_'),
  section('관련 신호', signals.length ? signals.join('\n') : '• _관련 신호 없음_'),
  section('원인 후보 — 확정 원인 아님',
    candidates.length ? candidates.join('\n') : '• _현재 증거로 평가 가능한 원인 후보 없음_'),
  section('복구', bullets(rca.recovery, '복구 조치가 확인되지 않음')),
  section('즉시 권고', bullets(rca.immediate_actions, '권고 없음')),
  section('예방 권고', bullets(rca.preventive_actions, '권고 없음')),
  section('추가 필요 데이터', bullets(rca.additional_data_required, '없음')),
  section('분석 한계', bullets(rca.limitations, '명시된 한계 없음')),
];

// Built last so every marker above has already been numbered.
if (refs.length > 0) {
  sections.push(section('근거',
    refs.map((id, index) => '\`[' + (index + 1) + ']\` \`' + id + '\`').join('\n')));
}

const slackMarkdown = sections.join('\n\n').slice(0, 39000);
return [{
  json: {
    request_id: request.request_id,
    slack_markdown: slackMarkdown,
    evidence_ref_count: refs.length,
  },
}];
`.trim();

const formatErrorCode = String.raw`
const payload = $input.first().json;
const execution = payload.execution ?? {};
const workflow = payload.workflow ?? {};
const error = execution.error ?? {};
const message = error.message ?? 'Unknown workflow error';
const executionId = execution.id ? String(execution.id) : undefined;
const workflowName = workflow.name ?? 'Unknown workflow';
const lastNode = execution.lastNodeExecuted ?? undefined;
const requestId = error.context?.request_id ?? undefined;
const details = {
  stack: error.stack,
  execution_url: execution.url,
  mode: execution.mode,
};
return [{
  json: {
    error_record: {
      ...(requestId ? { request_id: requestId } : {}),
      workflow_name: workflowName,
      ...(executionId ? { execution_id: executionId } : {}),
      ...(lastNode ? { last_node: lastNode } : {}),
      message,
      details,
    },
    slack_text:
      '🚨 *AIOps 워크플로 오류*\n' +
      'Workflow: ' + workflowName + '\n' +
      (executionId ? 'Execution: ' + executionId + '\n' : '') +
      (lastNode ? 'Last node: ' + lastNode + '\n' : '') +
      'Error: ' + message +
      (execution.url ? '\n' + execution.url : ''),
  },
}]; 
`.trim();

await main();
