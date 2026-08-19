import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const rootDir = resolve(scriptDir, "..");
const outputDir = resolve(scriptDir, "..", "workflows");

// The prompts and JSON schemas this used to read were the reasoning contract of
// the n8n agents. Those agents are gone; the same contract now lives beside the
// LangGraph nodes in rca-api, and schemas/ stays as the canonical definition
// that rca-api's Pydantic models are tested against.
async function main() {
  const mainWorkflowId = "aiops-main-v010";
  const errorWorkflowId = "aiops-error-v010";

  const mainWorkflow = buildMainWorkflow({
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
    // First thing after the payload is validated, and deliberately ahead of the
    // Slack ACK: everything below can fail, and a failure is reported by the
    // error workflow, which is given an execution id and no request id. This
    // call is what lets that id be turned back into a request. Registering it
    // any later would leave the nodes before it unattributable.
    httpNode(
      "Register Execution",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/execution' }}",
      "={{ JSON.stringify({ execution_id: String($execution.id) }) }}",
      [360, 0],
      "internal",
    ),
    codeNode("Format ACK", [480, 0], formatAckCode),
    httpNode(
      "Post Business ACK",
      "POST",
      "https://slack.com/api/chat.postMessage",
      // A continuation posts into the thread its parent already owns, so one
      // investigation occupies one thread however many clarifications it took.
      // Assert ACK Posted works out which thread that was.
      "={{ JSON.stringify({ channel: $env.SLACK_ANSWER_CHANNEL_ID, text: $('Format ACK').first().json.text, ...($('Normalize Request').first().json.parent_ack_ts ? { thread_ts: $('Normalize Request').first().json.parent_ack_ts } : {}) }) }}",
      [600, 0],
      "slack",
    ),
    codeNode("Assert ACK Posted", [720, 0], assertAckAndResolveAnchorCode),
    // Which kinds of report exist is a database question, and it has to be
    // asked before the analyzer classifies rather than after: the analyzer is
    // what picks one, so it needs the list in front of it. Placed after the
    // acknowledgement so a database that is briefly unreachable delays the
    // investigation rather than leaving the asker with no reply at all.
    httpNode(
      "Fetch Template Catalog",
      "GET",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/templates' }}",
      null,
      [840, 0],
      "internal",
    ),
    httpNode(
      "Mark Analyzing",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/status' }}",
      // Record the anchor while it is at hand, so a reply written under this
      // investigation's report can be traced back to it.
      "={{ JSON.stringify({ status: 'analyzing_question', slack_ack_ts: $('Assert ACK Posted').first().json.thread_anchor_ts }) }}",
      [960, 0],
      "internal",
    ),
    httpNode(
      "Call LangGraph RCA",
      "POST",
      "={{ $env.RCA_API_URL + '/v1/investigations' }}",
      "={{ JSON.stringify({ request: { request_id: $('Normalize Request').first().json.request_id, source: 'slack', received_at: $('Normalize Request').first().json.received_at, timezone: 'Asia/Seoul', question: $('Normalize Request').first().json.question, metadata: { channel_id: $('Normalize Request').first().json.channel_id, user_id: $('Normalize Request').first().json.user_id, message_ts: $('Normalize Request').first().json.message_ts, thread_ts: $('Normalize Request').first().json.thread_ts || null, parent_request_id: $('Normalize Request').first().json.parent_request_id || null } }, prior_question: $('Normalize Request').first().json.prior_question || null, templates: $('Fetch Template Catalog').first().json.templates || [] }) }}",
      [1320, -520],
      "internal",
      {},
      900_000,
    ),
    codeNode("Prepare LangGraph Audit Runs", [1560, -520], prepareLangGraphAuditCode),
    httpNode(
      "Persist LangGraph Audit Runs",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/agent-runs' }}",
      "={{ JSON.stringify({ stage: $json.stage, status: $json.status, model: $json.model, duration_ms: $json.duration_ms, output: $json.output }) }}",
      [1800, -520],
      "internal",
    ),
    codeNode("Collapse LangGraph Audit Runs", [2040, -520], collapseItemsCode),
    ifNode(
      "LangGraph Completed?",
      "={{ $('Call LangGraph RCA').first().json.status }}",
      "completed",
      [2280, -520],
    ),
    codeNode("Format LangGraph RCA", [2520, -660], langGraphRcaFormatter()),
    httpNode(
      "Post LangGraph RCA Report",
      "POST",
      "https://slack.com/api/chat.postMessage",
      "={{ JSON.stringify({ channel: $env.SLACK_ANSWER_CHANNEL_ID, thread_ts: $('Assert ACK Posted').first().json.thread_anchor_ts, text: $('Format LangGraph RCA').first().json.slack_markdown }) }}",
      [2760, -660],
      "slack",
    ),
    codeNode("Assert LangGraph RCA Posted", [3000, -660], assertSlackCode),
    httpNode(
      "Save LangGraph Completed Report",
      "PUT",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/report' }}",
      "={{ JSON.stringify({ parsed_request: $('Call LangGraph RCA').first().json.parsed_request, evidence_package: $('Call LangGraph RCA').first().json.evidence_package, rca_report: $('Call LangGraph RCA').first().json.report, slack_markdown: $('Format LangGraph RCA').first().json.slack_markdown, slack_channel_id: $env.SLACK_ANSWER_CHANNEL_ID, slack_message_ts: $('Post LangGraph RCA Report').first().json.ts }) }}",
      [3240, -660],
      "internal",
    ),
    codeNode(
      "Format LangGraph Clarification",
      [2520, -360],
      langGraphClarificationFormatter(),
    ),
    httpNode(
      "Post LangGraph Clarification",
      "POST",
      "https://slack.com/api/chat.postMessage",
      "={{ JSON.stringify({ channel: $('Normalize Request').first().json.channel_id, thread_ts: $('Normalize Request').first().json.thread_ts || $('Normalize Request').first().json.message_ts, text: $('Format LangGraph Clarification').first().json.text }) }}",
      [2760, -360],
      "slack",
    ),
    codeNode("Assert LangGraph Clarification Posted", [3000, -360], assertSlackCode),
    httpNode(
      "Mark LangGraph Needs Clarification",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/status' }}",
      "={{ JSON.stringify({ status: $('Call LangGraph RCA').first().json.status }) }}",
      [3240, -360],
      "internal",
    ),
  ];

  const connections = {};
  connectMain(connections, "AIOps Internal Webhook", "Normalize Request");
  connectMain(connections, "Normalize Request", "Register Execution");
  connectMain(connections, "Register Execution", "Format ACK");
  connectMain(connections, "Format ACK", "Post Business ACK");
  connectMain(connections, "Post Business ACK", "Assert ACK Posted");
  connectMain(connections, "Assert ACK Posted", "Fetch Template Catalog");
  connectMain(connections, "Fetch Template Catalog", "Mark Analyzing");
  // The gate that chose between this path and the n8n agent chain went with
  // the chain, so analysis runs straight into the API call.
  connectMain(connections, "Mark Analyzing", "Call LangGraph RCA");
  connectMain(connections, "Call LangGraph RCA", "Prepare LangGraph Audit Runs");
  connectMain(connections, "Prepare LangGraph Audit Runs", "Persist LangGraph Audit Runs");
  connectMain(connections, "Persist LangGraph Audit Runs", "Collapse LangGraph Audit Runs");
  connectMain(connections, "Collapse LangGraph Audit Runs", "LangGraph Completed?");
  connectMain(connections, "LangGraph Completed?", "Format LangGraph RCA", 0);
  connectMain(
    connections,
    "LangGraph Completed?",
    "Format LangGraph Clarification",
    1,
  );
  connectMain(connections, "Format LangGraph RCA", "Post LangGraph RCA Report");
  connectMain(
    connections,
    "Post LangGraph RCA Report",
    "Assert LangGraph RCA Posted",
  );
  connectMain(
    connections,
    "Assert LangGraph RCA Posted",
    "Save LangGraph Completed Report",
  );
  connectMain(
    connections,
    "Format LangGraph Clarification",
    "Post LangGraph Clarification",
  );
  connectMain(
    connections,
    "Post LangGraph Clarification",
    "Assert LangGraph Clarification Posted",
  );
  connectMain(
    connections,
    "Assert LangGraph Clarification Posted",
    "Mark LangGraph Needs Clarification",
  );

  // autoFix on the parsed-request parser requires its own model connection.
  // autoFix on the evidence parser requires its own model connection.
  // autoFix on the report parser requires its own model connection.

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

// Reporting a failure has two halves -- tell a human, and file it against the
// request -- and neither may be able to suppress the other.
//
// They used to hang off the same output as parallel branches. n8n runs sibling
// branches in position order and aborts the execution at the first unhandled
// node error, so a failing `/internal/errors` call took the Slack alert down
// with it. That is precisely the situation where the alert matters most: ingress
// being unreachable is a likely reason the run died in the first place, and the
// failure would then reach nobody at all.
//
// Chained instead, so both always run. Slack goes first because it is the half a
// human sees and the half least likely to be broken by whatever broke the run,
// and it swallows its own errors so it cannot block the record behind it. The
// record stays unguarded on purpose: it is last, nothing follows it, and letting
// it fail keeps the failed execution visible in n8n instead of reporting success
// after silently dropping the row. Both bodies reference Format Workflow Error
// by name, so neither depends on being fed by it directly.
function buildErrorWorkflow(workflowId) {
  const nodes = [
    node("Error Trigger", "n8n-nodes-base.errorTrigger", 1, [0, 0], {}),
    codeNode("Format Workflow Error", [260, 0], formatErrorCode),
    httpNode(
      "Post Error Alert",
      "POST",
      "https://slack.com/api/chat.postMessage",
      "={{ JSON.stringify({ channel: $env.SLACK_ERROR_CHANNEL_ID, text: $('Format Workflow Error').first().json.slack_text }) }}",
      [540, 0],
      "slack",
      { onError: "continueRegularOutput" },
    ),
    httpNode(
      "Record Workflow Error",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/errors' }}",
      "={{ JSON.stringify($('Format Workflow Error').first().json.error_record) }}",
      [800, 0],
      "internal",
    ),
  ];

  const connections = {};
  connectMain(connections, "Error Trigger", "Format Workflow Error");
  connectMain(connections, "Format Workflow Error", "Post Error Alert");
  connectMain(connections, "Post Error Alert", "Record Workflow Error");

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

// `extras` carries node-level settings rather than parameters -- onError above
// all. Main-workflow calls leave it empty: a step that fails there must abort so
// the error workflow fires, which is the whole reporting path.
// jsonBody of null sends no body at all, for the GET that reads the catalog.
function httpNode(
  name,
  method,
  url,
  jsonBody,
  position,
  authKind,
  extras = {},
  timeoutMs = null,
) {
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
    ...(jsonBody === null
      ? { sendBody: false }
      : {
          sendBody: true,
          contentType: "raw",
          rawContentType: "application/json",
          body: jsonBody,
        }),
    options: {
      timeout: timeoutMs ?? (authKind === "slack" ? 30_000 : 10_000),
    },
  }, extras);
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

function resolvePointer(root, pointer) {
  return pointer
    .slice(2)
    .split("/")
    .map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"))
    .reduce((current, part) => current[part], root);
}

/**
 * Parses every Code node's body before the workflow is written.
 *
 * These bodies are JavaScript assembled inside template literals, so an escape
 * that is one character off produces a file that looks fine and a node that
 * throws "Invalid or unexpected token" -- in production, at the end of an
 * investigation, after every token has already been spent. A `\n` written
 * without doubling became a real newline inside a string literal and did
 * exactly that.
 *
 * new Function parses without running, which is all that is needed: the code
 * never executes here, and n8n's own globals ($env, $input) are never touched.
 */
function assertCodeNodesParse(workflow) {
  for (const node of workflow.nodes) {
    const code = node.parameters?.jsCode;
    if (!code) continue;
    try {
      new Function(code);
    } catch (error) {
      throw new Error(
        `Code node "${node.name}" does not parse: ${error.message}\n` +
          "The body is built from a template literal, so check the escaping " +
          "of \\n and backticks first.",
      );
    }
  }
}

async function writeWorkflow(filename, workflow) {
  assertCodeNodesParse(workflow);
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

// Asserts the ACK went out and, in the same step, settles which thread this
// investigation lives in. Two nodes need that answer -- the status record and
// the report -- and they must not derive it separately: they already did, with
// the same wrong expression, and only one of the two showed symptoms.
//
// The anchor is deliberately not read back from the Slack response.
// chat.postMessage returns thread_ts nested in `message`, not at the top level,
// so `response.thread_ts` is always undefined; a continuation fell through to
// its own reply ts and recorded that as the thread root. Slack still put the
// report in the right place, because a thread_ts pointing at a reply resolves
// to its parent, so the damage was invisible in Slack and confined to the
// database -- findReportByThread matches the stored anchor against the root ts
// a reply carries, missed, and dropped the written correction on the floor.
//
// Slack does not have to be asked. A continuation was posted into the parent's
// thread, so the parent's anchor is the answer; a new request starts its own
// thread and is its own anchor. That holds however long the clarification chain
// gets, because each link copies the anchor rather than deriving a new one.
const assertAckAndResolveAnchorCode = String.raw`
const response = $input.first().json;
if (response.ok !== true) {
  throw new Error('Slack API error: ' + (response.error ?? 'unknown_error'));
}
const parentAnchor = $('Normalize Request').first().json.parent_ack_ts;
const anchor = parentAnchor || response.ts;
if (!anchor) {
  throw new Error('Slack ACK returned no ts to anchor this investigation to');
}
return [{ json: { ...response, thread_anchor_ts: anchor } }];
`.trim();

const prepareLangGraphAuditCode = String.raw`
const response = $input.first().json;
const runs = Array.isArray(response.agent_runs) ? response.agent_runs : [];
if (runs.length === 0) {
  throw new Error('LangGraph RCA returned no agent audit records');
}
return runs.map((run) => ({ json: run }));
`.trim();

const collapseItemsCode = String.raw`
return [{ json: { persisted: $input.all().length } }];
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

// Ping the asker. Slack notifies thread participants only weakly, and this
// message is a question addressed to them -- it is waiting on their reply.
const mention = request.user_id ? '<@' + request.user_id + '> ' : '';

const header = unsupported
  ? '⛔ ' + mention + '*지원 범위를 벗어난 요청입니다*'
  : '❓ ' + mention + '*조사에 필요한 정보가 더 있습니다*';

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

function langGraphClarificationFormatter() {
  return formatClarificationCode.replace(
    "const parsed = $('Question Analyzer').first().json.output;",
    "const parsed = $('Call LangGraph RCA').first().json.parsed_request;",
  );
}

const formatRcaCode = String.raw`
const request = $('Normalize Request').first().json;
const selection = $('Select Template').first().json;
const evidence = $('Evidence Collector').first().json.output;
const report = $('RCA Writer').first().json.output;

// The template owns the layout: it names the sections, in order, with their
// headings. The writer only says what goes in each, keyed by id, so a reworded
// heading or a section the writer invented cannot change the document.
const spec = (selection.output && selection.output.sections) || [];
const filled = new Map(
  (report.sections || []).map((section) => [section.id, section]),
);

// A real Zabbix event id is numeric. The agent also emits synthetic ones such
// as zbx:event:no-problem-events-... to record that it looked and found
// nothing, and those must not license a timeline: the writer has been observed
// copying the investigation window into incident timing on a healthy host,
// which renders as an hour-long outage that never happened.
const backedByEvent = (evidence.evidence || []).some(
  (item) => /^zbx:event:\d+$/.test(item.evidence_id || ''),
);

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

// After reading the report the operator's next move is to open the graph, so
// the footnotes carry a way back into Zabbix. Everything needed is already on
// the evidence entry: resource_ids identifies what to open, window scopes it to
// the interval that was actually examined.
const evidenceById = new Map(
  (evidence.evidence || []).map((item) => [item.evidence_id, item]),
);
const zabbixBase = ($env.ZABBIX_FRONTEND_URL || '').replace(/\/+$/, '');

// Footnote numbering only ever reached items[].evidence_refs, so an evidence id
// written into prose printed raw -- a summary paragraph interrupted by
// zbx:metric:55052:1785855600-1785942000-1h stops being readable. The writer is
// told not to, but that is a request; this makes it not matter. Converted
// rather than deleted, so the citation survives as the marker it should have
// been, and only ids the investigation actually produced are touched.
const escapeRe = (value) => value.replace(/[.*+?^\${}()|[\]\\]/g, '\\$&');
const knownIds = [...evidenceById.keys()]
  .filter(Boolean)
  .sort((left, right) => right.length - left.length);
const idPattern = knownIds.length > 0
  ? new RegExp(knownIds.map(escapeRe).join('|'), 'g')
  : null;
const citeInline = (text) => {
  if (!text || !idPattern) return text;
  // replace() walks left to right, so markers number in reading order.
  return text
    .replace(idPattern, (id) => '[' + refNumber(id) + ']')
    // A bracket that held nothing but ids now holds nothing but markers.
    .replace(/\(\s*((?:\[\d+\]\s*[,;]?\s*)+)\)/g, (_, marks) => marks.replace(/[\s,;]+/g, ''))
    .replace(/\s+([.,)])/g, '$1');
};

const asTime = (value) => {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? String(value)
    : parsed.toLocaleString('sv-SE', { timeZone: 'Asia/Seoul' });
};

const kibanaBase = ($env.KIBANA_URL || '').replace(/\/+$/, '');
const kibanaDataView = $env.KIBANA_DATA_VIEW_ID || '';

// Which host each piece of evidence belongs to. resource_ids carries the Zabbix
// id, and Kibana has never heard of that -- the name is what the log index
// stores, and query_context is where the two were put side by side.
const hostNameById = new Map(
  ((evidence.query_context && evidence.query_context.hosts) || [])
    .map((entry) => [entry.host_id, entry.host]),
);

// A log citation quotes a line out of a window. The link opens Discover on that
// window, for that host, so the reader lands where the quote came from rather
// than at the top of the index.
//
// Both settings are required because neither works alone: without the data view
// id Discover opens with no index selected, and the id means nothing without
// the address it lives at. Absent either, log footnotes simply carry no link,
// which is what they did before.
const kibanaLink = (id) => {
  if (!kibanaBase || !kibanaDataView) return null;
  const item = evidenceById.get(id);
  const window = (item && item.window) || {};
  if (!window.from || !window.to) return null;

  const host = hostNameById.get((item.resource_ids || {}).host_id);
  // The search the evidence came from, when the collector carried it across.
  // Falling back to the host alone opens everything that host logged in the
  // window, which is a different thing from what was cited.
  const q = "'";
  const query = item.search_query || (host ? 'host.name:"' + host + '"' : "*");
  const time = "(time:(from:" + q + window.from + q + ",to:" + q + window.to + q + "))";
  const app = "(index:" + q + kibanaDataView + q +
    ",query:(language:kuery,query:" + q + query + q + "))";

  return {
    url: kibanaBase + '/app/discover#/?_g=' + encodeURIComponent(time) +
      '&_a=' + encodeURIComponent(app),
    label: '로그',
  };
};

const zabbixLink = (id) => {
  if (!zabbixBase) return null;
  // Only Zabbix ids get Zabbix links, stated as a whitelist rather than a list
  // of exclusions. Evidence from anywhere else has no Zabbix object behind it,
  // but it usually carries a host_id, so the fallback at the bottom would
  // happily produce a "최근 데이터" link -- sending the reader to metrics under
  // a citation that quoted an audit record or a log line. When a new source is
  // added, its footnote should degrade to a bare id, not to a wrong link.
  if (!id.startsWith('zbx:')) return null;
  const item = evidenceById.get(id);
  const ids = (item && item.resource_ids) || {};
  const window = (item && item.window) || {};

  let range = '';
  const from = asTime(window.from);
  const to = asTime(window.to);
  if (from && to) {
    range = '&from=' + encodeURIComponent(from) + '&to=' + encodeURIComponent(to);
  }

  // Follow what the evidence actually is, so the link matches the id beside it.
  // An event footnote that opened a metric graph would send the reader
  // somewhere the citation never claimed.
  const eventLink = ids.event_id && ids.trigger_id
    ? {
        url: zabbixBase + '/tr_events.php?triggerid=' + ids.trigger_id + '&eventid=' + ids.event_id,
        label: '이벤트',
      }
    : null;
  const graphLink = ids.item_id
    ? {
        url: zabbixBase + '/history.php?action=showgraph&itemids%5B%5D=' + ids.item_id + range,
        label: '그래프',
      }
    : null;

  if (id.startsWith('zbx:event:') || id.startsWith('zbx:trigger:')) {
    if (eventLink) return eventLink;
    if (graphLink) return graphLink;
  } else if (graphLink) {
    return graphLink;
  }

  if (ids.host_id) {
    return {
      url: zabbixBase + '/zabbix.php?action=latest.view&filter_hostids%5B%5D=' + ids.host_id + '&filter_set=1',
      label: '최근 데이터',
    };
  }
  return null;
};

const renderItem = (item) => {
  const label = item.label ? '*[' + String(item.label).toUpperCase() + ']* ' : '';
  const lines = ['• ' + label + citeInline(item.text) + cite(item.evidence_refs)];
  const against = cite(item.counter_evidence_refs);
  if (against) lines.push('    ↳ 반박' + against);
  return lines.join('\n');
};

// The hosts come from what the investigation resolved rather than from the
// report, so the header cannot claim coverage the evidence does not show.
const hosts = ((evidence.query_context && evidence.query_context.hosts) || [])
  .map((entry) => entry.host)
  .filter(Boolean);
const hostLabel = hosts.length === 0
  ? '확인 불가'
  : hosts.length <= 8
    ? hosts.join(', ')
    : hosts.slice(0, 8).join(', ') + ' 외 ' + (hosts.length - 8) + '대';

const meta = ['• 요청 ID: \`' + request.request_id + '\`'];
if (request.user_id) meta.push('• 요청자: <@' + request.user_id + '>');
meta.push('• 호스트: ' + hostLabel);

const sections = ['📋 *' + report.title + '*', meta.join('\n')];

for (const declared of spec) {
  if (declared.requires_problem_event && !backedByEvent) continue;

  const section = filled.get(declared.id);
  const body = section && section.body ? citeInline(String(section.body).trim()) : '';
  const items = (section && section.items) || [];
  if (!body && items.length === 0) {
    // A section the template insists on is reported as empty rather than
    // dropped, so its absence reads as a finding instead of an oversight.
    if (declared.required) {
      sections.push('*' + declared.heading + '*\n• _해당 없음_');
    }
    continue;
  }

  const parts = [];
  if (body) parts.push(body);
  if (items.length > 0) parts.push(items.map(renderItem).join('\n'));
  sections.push('*' + declared.heading + '*\n' + parts.join('\n\n'));
}

// Built last so every marker above has already been numbered.
if (refs.length > 0) {
  sections.push('*근거*\n' + refs.map((id, index) => {
    // The query goes into the link, not onto the page. A collector that names a
    // dozen services produces a KQL string longer than the finding it supports,
    // and the citation list stops being readable. Clicking still arrives at the
    // same lines, which is what the query was for.
    const link = id.startsWith('log:') ? kibanaLink(id) : zabbixLink(id);
    const marker = '\`[' + (index + 1) + ']\`';
    // The id is the collector's own bookkeeping and says nothing to a reader
    // that the number and the link do not. It stays only when there is no link,
    // so the line still identifies what it cites.
    return link
      ? marker + ' <' + link.url + '|' + link.label + '>'
      : marker + ' \`' + id + '\`';
  }).join('\n'));
}

const slackMarkdown = sections.join('\n\n').slice(0, 39000);
return [{
  json: {
    request_id: request.request_id,
    slack_markdown: slackMarkdown,
    evidence_ref_count: refs.length,
    template_id: selection.template_id,
    template_version: selection.template_version,
  },
}];
`.trim();

function langGraphRcaFormatter() {
  return formatRcaCode
    .replace(
      "const selection = $('Select Template').first().json;",
      "const selectedTemplate = $('Call LangGraph RCA').first().json.template;\nconst selection = { ...selectedTemplate, template_version: selectedTemplate.version };",
    )
    .replace(
      "const evidence = $('Evidence Collector').first().json.output;",
      "const evidence = $('Call LangGraph RCA').first().json.evidence_package;",
    )
    .replace(
      "const report = $('RCA Writer').first().json.output;",
      "const report = $('Call LangGraph RCA').first().json.report;",
    );
}

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
