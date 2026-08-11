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
    reportSchema,
  ] = await Promise.all([
    readFile(resolve(rootDir, "prompts", "question-analyzer.system.md"), "utf8"),
    readFile(resolve(rootDir, "prompts", "evidence-collector.system.md"), "utf8"),
    readFile(resolve(rootDir, "prompts", "rca-writer.system.md"), "utf8"),
    readJson(resolve(rootDir, "schemas", "parsed-request.schema.json")),
    readJson(resolve(rootDir, "schemas", "evidence-package.schema.json")),
    readJson(resolve(rootDir, "schemas", "report.schema.json")),
  ]);

  const mainWorkflowId = "aiops-main-v010";
  const errorWorkflowId = "aiops-error-v010";

  const mainWorkflow = buildMainWorkflow({
    questionPrompt,
    evidencePrompt,
    rcaPrompt,
    parsedSchema: flattenLocalRefs(parsedSchema),
    evidenceSchema: flattenLocalRefs(evidenceSchema),
    reportSchema: flattenLocalRefs(reportSchema),
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

// Each model id appears twice: once on the model node, once in the audit record
// the stage writes to aiops_agent_runs. Defining them here keeps the two in step
// -- editing only the node would leave the audit table attributing timings and
// output to whichever model used to be there, which is exactly the data you
// consult when deciding whether a cheaper model was good enough.
const MODELS = {
  question: { id: "gpt-5.4-mini", reasoningEffort: "low" },
  investigation: { id: "gpt-5.4-mini", reasoningEffort: "medium" },
  rca: { id: "gpt-5.4-mini", reasoningEffort: "medium" },
};

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
    codeNode("Stamp Question Start", [1080, 0], stampCode),
    agentNode(
      "Question Analyzer",
      [1200, 0],
      input.questionPrompt,
      // The catalog goes in as input rather than into the system prompt: it is
      // per-request data read from a table, not part of this agent's role.
      // supplies_hosts is derived here so the analyzer can tell that a kind
      // which brings its own hosts does not need one named in the question.
      "={{ JSON.stringify({ request_id: $('Normalize Request').first().json.request_id, question: $('Normalize Request').first().json.question, slack_received_at: $('Normalize Request').first().json.received_at, default_timezone: 'Asia/Seoul', prior_question: $('Normalize Request').first().json.prior_question, answers_clarification: Boolean($('Normalize Request').first().json.parent_request_id), report_catalog: ($('Fetch Template Catalog').first().json.templates || []).map(entry => ({ id: entry.template_id, title: entry.title, when_to_use: entry.description, supplies_hosts: entry.collection.host_selector.mode !== 'from_question', supplies_window: entry.collection.window.range !== 'anchor_relative' })) }, null, 2) }}",
      3,
    ),
    modelNode("Question Model", [1120, 260], MODELS.question.id, MODELS.question.reasoningEffort),
    // The last strict parser, until a monthly question rejected the whole
    // output over a field that kind of report does not even use. It is the
    // cheapest stage to retry and the earliest to fail, so a rejection here
    // costs a question that was already acknowledged.
    parserNode("Parsed Request Parser", [1320, 260], input.parsedSchema, true),
    httpNode(
      "Persist Question Result",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/agent-runs' }}",
      "={{ JSON.stringify({ stage: 'question_analyzer', status: 'succeeded', model: '" + MODELS.question.id + "', duration_ms: Date.now() - $('Stamp Question Start').first().json.stage_started_ms, output: $('Question Analyzer').first().json.output }) }}",
      [1440, 0],
      "internal",
    ),
    codeNode("Select Template", [1560, 0], selectTemplateCode),
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
      // collection is null when no template matched, which is how this keeps
      // behaving as it did before templates existed. Select Template settled
      // the budget already, including the per-host scaling.
      "={{ JSON.stringify({ parsed_request: $('Question Analyzer').first().json.output, slack_context: $('Normalize Request').first().json, collection: $('Select Template').first().json.collection, window: $('Select Template').first().json.window, limits: $('Select Template').first().json.limits }, null, 2) }}",
      12,
    ),
    modelNode("Investigation Model", [1840, 140], MODELS.investigation.id, MODELS.investigation.reasoningEffort),
    // The only stage observed to fail schema validation in operation, so it is
    // the only one given a retry. The other two parsers stay strict.
    parserNode("Evidence Package Parser", [2040, 140], input.evidenceSchema, true),
    mcpNode("Zabbix MCP Tools", [2240, 140], "ZABBIX_MCP_URL"),
    // Metrics say when a host went wrong; logs say what failed. Both are
    // reached by the same host string, which is what lets one report cite them
    // side by side.
    mcpNode("Log MCP Tools", [2240, 320], "ES_MCP_URL"),
    // The general client, beside the two shaped ones. They answer the questions
    // an investigation usually asks, already aggregated; this answers the rest
    // -- across all of time, by any field, without a window. It authenticates
    // with nothing because the server offers nothing to authenticate with; it
    // is reachable only from the private network.
    mcpNode("Elasticsearch Query Tools", [2240, 500], "OSS_ES_MCP_URL", "none"),
    httpNode(
      "Persist Evidence Result",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/agent-runs' }}",
      "={{ JSON.stringify({ stage: 'evidence_collector', status: 'succeeded', model: '" + MODELS.investigation.id + "', duration_ms: Date.now() - $('Stamp Evidence Start').first().json.stage_started_ms, output: $('Evidence Collector').first().json.output }) }}",
      [2160, -140],
      "internal",
    ),
    codeNode("Stamp RCA Start", [2280, -140], stampCode),
    agentNode(
      "RCA Writer",
      [2400, -140],
      input.rcaPrompt,
      // The section list is the writer's assignment: it fills the sections it
      // is given, by id, and cannot invent or rename one. Sections gated on a
      // problem event are withheld here as well as at render time, so the
      // writer is never asked to produce timing that would be dropped anyway.
      "={{ JSON.stringify({ parsed_request: $('Question Analyzer').first().json.output, evidence_package: $('Evidence Collector').first().json.output, report_guidance: ($('Select Template').first().json.output || {}).guidance || '', sections: (($('Select Template').first().json.output || {}).sections || []).filter(section => !section.requires_problem_event || ($('Evidence Collector').first().json.output.evidence || []).some(item => /^zbx:event:\\d+$/.test(item.evidence_id || ''))).map(section => ({ id: section.id, heading: section.heading, instruction: section.instruction, required: section.required })) }, null, 2) }}",
      3,
    ),
    modelNode("RCA Model", [2360, 140], MODELS.rca.id, MODELS.rca.reasoningEffort),
    // The writer joined the list of stages observed to fail schema validation
    // in operation: a month of evidence in, six sections out, and one item
    // missing a field rejects the whole report after the investigation has
    // already been paid for. autoFix hands it back to be corrected instead.
    parserNode("Report Parser", [2560, 140], input.reportSchema, true),
    httpNode(
      "Persist RCA Result",
      "POST",
      "={{ $env.AIOPS_CONTROL_URL + '/internal/requests/' + encodeURIComponent($('Normalize Request').first().json.request_id) + '/agent-runs' }}",
      "={{ JSON.stringify({ stage: 'rca_writer', status: 'succeeded', model: '" + MODELS.rca.id + "', duration_ms: Date.now() - $('Stamp RCA Start').first().json.stage_started_ms, output: $('RCA Writer').first().json.output }) }}",
      [2640, -140],
      "internal",
    ),
    codeNode("Format RCA for Slack", [2880, -140], formatRcaCode),
    httpNode(
      "Post RCA Report",
      "POST",
      "https://slack.com/api/chat.postMessage",
      // The same anchor the status record holds, so the report lands in the
      // thread that aiops_requests.slack_ack_ts says it is in.
      "={{ JSON.stringify({ channel: $env.SLACK_ANSWER_CHANNEL_ID, thread_ts: $('Assert ACK Posted').first().json.thread_anchor_ts, text: $('Format RCA for Slack').first().json.slack_markdown }) }}",
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
  connectMain(connections, "Normalize Request", "Register Execution");
  connectMain(connections, "Register Execution", "Format ACK");
  connectMain(connections, "Format ACK", "Post Business ACK");
  connectMain(connections, "Post Business ACK", "Assert ACK Posted");
  connectMain(connections, "Assert ACK Posted", "Fetch Template Catalog");
  connectMain(connections, "Fetch Template Catalog", "Mark Analyzing");
  connectMain(connections, "Mark Analyzing", "Stamp Question Start");
  connectMain(connections, "Stamp Question Start", "Question Analyzer");
  connectMain(connections, "Question Analyzer", "Persist Question Result");
  connectMain(connections, "Persist Question Result", "Select Template");
  connectMain(connections, "Select Template", "Request Ready?");
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
  // autoFix on the parsed-request parser requires its own model connection.
  connectAi(connections, "Question Model", "Parsed Request Parser", "ai_languageModel");
  connectAi(connections, "Investigation Model", "Evidence Collector", "ai_languageModel");
  connectAi(connections, "Evidence Package Parser", "Evidence Collector", "ai_outputParser");
  // autoFix on the evidence parser requires its own model connection.
  connectAi(connections, "Investigation Model", "Evidence Package Parser", "ai_languageModel");
  connectAi(connections, "Zabbix MCP Tools", "Evidence Collector", "ai_tool");
  connectAi(connections, "Log MCP Tools", "Evidence Collector", "ai_tool");
  connectAi(connections, "Elasticsearch Query Tools", "Evidence Collector", "ai_tool");
  connectAi(connections, "RCA Model", "RCA Writer", "ai_languageModel");
  connectAi(connections, "Report Parser", "RCA Writer", "ai_outputParser");
  // autoFix on the report parser requires its own model connection.
  connectAi(connections, "RCA Model", "Report Parser", "ai_languageModel");

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

// autoFix lets the parser hand a rejected output back to a model to be
// corrected instead of failing the run. Enabling it adds a *required* model
// input to the parser node, so any caller passing true must also connect one.
function parserNode(name, position, schema, autoFix = false) {
  return node(
    name,
    "@n8n/n8n-nodes-langchain.outputParserStructured",
    1.3,
    position,
    {
      schemaType: "manual",
      inputSchema: JSON.stringify(schema, null, 2),
      autoFix,
    },
  );
}

// n8n prefixes every tool it exposes with the node name, so two MCP servers on
// one agent cannot collide however they name their tools.
function mcpNode(name, position, urlVariable, authentication = "bearerAuth") {
  return node(name, "@n8n/n8n-nodes-langchain.mcpClientTool", 1.4, position, {
    endpointUrl: "={{ $env." + urlVariable + " }}",
    serverTransport: "httpStreamable",
    authentication,
    include: "all",
    options: {
      timeout: 120_000,
    },
  });
}

// `extras` carries node-level settings rather than parameters -- onError above
// all. Main-workflow calls leave it empty: a step that fails there must abort so
// the error workflow fires, which is the whole reporting path.
// jsonBody of null sends no body at all, for the GET that reads the catalog.
function httpNode(name, method, url, jsonBody, position, authKind, extras = {}) {
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
      timeout: authKind === "slack" ? 30_000 : 10_000,
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

// Appends rather than replaces: one model node feeds both an agent and that
// agent's auto-fixing parser, so the same source can have several targets on
// the same connection type.
function connectAi(connections, from, to, type) {
  connections[from] ??= {};
  connections[from][type] ??= [[]];
  connections[from][type][0].push({
    node: to,
    type,
    index: 0,
  });
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

// Records when a stage begins so the persist call after it can report how long
// the stage took. Without this the agent_runs.duration_ms column stays empty and
// per-stage latency can only be recovered by parsing n8n's internal run data.
const stampCode = String.raw`
return [{ json: { ...$input.first().json, stage_started_ms: Date.now() } }];
`.trim();

// Turns the kind the analyzer named into the template that defines it, and
// settles the investigation budget while it is at it.
//
// The matching lives here rather than in the output parser because n8n's parser
// takes a static schema, so the set of valid kinds cannot be an enum that grows
// with the table. Checking it here costs nothing and fails softer: a model that
// names a kind nobody defined falls back to the built-in behaviour instead of
// failing a question that has already been acknowledged.
//
// Merging the limits here rather than in the agent's expression is the same
// reasoning as the anchor -- a Code node is real JavaScript, while an n8n
// expression is one expression, and this is a lookup with defaults.
const selectTemplateCode = String.raw`
const catalog = $('Fetch Template Catalog').first().json.templates || [];
const parsed = $('Question Analyzer').first().json.output;
const requested = parsed.request_type;

// Falls back to the incident RCA, which is seeded by migration for exactly this
// reason: the report cannot be laid out without a section list, so there has to
// be one to land on. Failing here rather than rendering something shapeless is
// the right end if even that is gone -- it means the table was emptied, which
// is a configuration problem and not something to paper over.
const template = catalog.find((entry) => entry.template_id === requested)
  || catalog.find((entry) => entry.template_id === 'incident_rca')
  || null;
if (!template) {
  throw new Error(
    'No report template matched \'' + requested + '\' and the incident_rca fallback is missing. '
    + 'Re-run the migrations to restore it.',
  );
}
const collection = template.collection;
const limits = collection && collection.limits ? collection.limits : {};

// Without a template this is exactly the budget the workflow used before there
// were any: 30 calls for one host, scaled by the number asked about.
const hostCount = Math.max(1, (parsed.host_queries || []).length);

// Turn the template's range into real timestamps here rather than letting the
// writer work them out. Asking a model for the bounds of last month is the same
// kind of arithmetic this system keeps away from it everywhere else, and it is
// the input to every query that follows -- a month off by a day is a month of
// wrong data, reported confidently.
//
// KST is UTC+9 with no daylight saving, so shifting by nine hours makes the UTC
// getters read local calendar fields, and shifting back gives the instant.
const KST_OFFSET_MS = 9 * 60 * 60 * 1000;
const asked = new Date($('Normalize Request').first().json.received_at);
const local = new Date(asked.getTime() + KST_OFFSET_MS);
const instant = (wallMs) => new Date(wallMs - KST_OFFSET_MS).toISOString();
const daysBack = (days) => ({
  from: new Date(asked.getTime() - days * 24 * 60 * 60 * 1000).toISOString(),
  to: asked.toISOString(),
});

const range = (collection.window && collection.window.range) || 'anchor_relative';
let window = null;
if (range === 'last_calendar_month') {
  // Date.UTC normalises month -1 into the previous December on its own.
  window = {
    from: instant(Date.UTC(local.getUTCFullYear(), local.getUTCMonth() - 1, 1)),
    to: instant(Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), 1)),
  };
} else if (range === 'last_7_days') {
  window = daysBack(7);
} else if (range === 'last_30_days') {
  window = daysBack(30);
}
// anchor_relative leaves this null: the window comes from the anchor time the
// analyzer read out of the question, which is how an incident has always worked.

return [{ json: {
  requested_template_id: requested,
  template_id: template.template_id,
  template_version: template.version,
  matched: template.template_id === requested,
  collection,
  window,
  output: template.output,
  limits: {
    max_iterations: limits.max_iterations || 10,
    max_tool_calls: limits.max_tool_calls || Math.min(60, 10 + 20 * hostCount),
    // Was a flat 24 whatever the template asked for, so a monthly report was
    // handed a month-long window and told in the same breath not to look past a
    // day. When the range produced a window, that window is the bound; without
    // one it follows the policy, which is what decides the ceiling the MCP will
    // actually enforce.
    max_window_hours: window
      ? Math.ceil((Date.parse(window.to) - Date.parse(window.from)) / 3600000)
      : (collection.window && collection.window.policy === 'long_term_capacity'
          ? 24 * 31
          : 24),
  },
}}];
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
  // Log evidence has no Zabbix object behind it. The host fallback below would
  // still produce a link, and a footnote reading "최근 데이터" under a quoted log
  // line sends the reader to metrics that were never what was cited.
  if (id.startsWith('log:')) return null;
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
    const link = id.startsWith('log:') ? kibanaLink(id) : zabbixLink(id);
    const marker = '\`[' + (index + 1) + ']\`';
    const item = evidenceById.get(id) || {};
    const shown = item.search_query
      ? '
     검색: \`' + item.search_query + '\`'
      : '';
    return (link
      ? marker + ' <' + link.url + '|' + link.label + '>  \`' + id + '\`'
      : marker + ' \`' + id + '\`') + shown;
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
