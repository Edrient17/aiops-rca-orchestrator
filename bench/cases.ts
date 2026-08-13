import { ALL_TOOLS, type BenchCase, type ToolSpec } from "./harness.js";

/**
 * Every case below is a failure that actually happened, distilled to the point
 * in the trajectory where the wrong turn was taken. Tool responses are the
 * strings those tools really returned, so passing here means the prompt copes
 * with what the agent actually sees.
 */

const iso = (s: string) => s;

/** Ten tools removed from the Wazuh router: real schemas, irrelevant to an outage. */
const REMOVED_WAZUH: ToolSpec[] = [
  ["get_wazuh_rules_summary", "Retrieves a summary of Wazuh security rules."],
  ["get_wazuh_cluster_health", "Retrieves the health status of the Wazuh cluster."],
  ["get_wazuh_cluster_nodes", "Retrieves the list of nodes in the Wazuh cluster."],
  ["search_wazuh_manager_logs", "Searches the Wazuh manager's own daemon logs."],
  ["get_wazuh_manager_error_logs", "Retrieves error entries from the Wazuh manager log."],
  ["get_wazuh_remoted_stats", "Retrieves statistics from the Wazuh remoted daemon."],
  ["get_wazuh_weekly_stats", "Retrieves weekly event statistics from the Wazuh manager."],
  ["get_wazuh_log_collector_stats", "Retrieves log collector statistics for an agent."],
  ["get_wazuh_vulnerability_summary", "Retrieves vulnerability detections for an agent."],
  ["get_wazuh_critical_vulnerabilities", "Retrieves critical vulnerabilities for an agent."],
].map(([name, description]) => ({
  name: `Wazuh_MCP_Tools_${name}`,
  bare_name: name!,
  description: description!,
  parameters: { type: "object", properties: { limit: { type: "number" } } },
}));

const ELASTICSEARCH_SEARCH_WITH_RESTART = JSON.stringify({
  hits: {
    total: { value: 3820, relation: "eq" },
    hits: [
      {
        _source: {
          "@timestamp": "2026-08-12T02:22:40Z",
          "host.name": "vm-java-docker-2",
          "service.name": "payment-service",
          "log.level": "ERROR",
          message: "Connection refused while calling payment-service",
        },
      },
      {
        _source: {
          "@timestamp": "2026-08-12T02:28:40Z",
          "host.name": "vm-java-docker-2",
          "service.name": "payment-service",
          "log.level": "INFO",
          message: "Started PaymentServiceApplication in 4.8 seconds",
        },
      },
    ],
  },
});

const ZABBIX_EVENT_WITH_LOCAL_TIME = JSON.stringify({
  host_id: "11094",
  events: [{
    event_id: "44821",
    trigger_id: "27714",
    name: "payment-service is not running",
    severity: "high",
    started_at: "2026-08-11T02:22:40.000Z",
    started_at_local: "2026-08-11 11:22:40 (Asia/Seoul)",
    recovered_at: "2026-08-11T02:28:40.000Z",
    recovered_at_local: "2026-08-11 11:28:40 (Asia/Seoul)",
  }],
});

export const CASES: BenchCase[] = [
  {
    id: "actor-is-never-checked",
    kind: "step-wise",
    guards: "E2",
    origin:
      "execution 131: metrics were clean, the logs showed payment-service stop " +
      "and restart, and the run finished without once asking who did it -- " +
      "reporting the cause as undetermined while the audit trail held it.",
    trajectory: [
      { role: "user", content: "오늘 vm-java-docker-2 전체적으로 문제 있었는지 확인해줘" },
      {
        role: "assistant",
        call: { name: "Zabbix_MCP_Tools_get_incident_events", args: { host_id: "11094", time_from: iso("2026-08-12T00:00:00Z"), time_to: iso("2026-08-12T06:00:00Z") } },
      },
      {
        role: "tool",
        forCall: "Zabbix_MCP_Tools_get_incident_events",
        content: JSON.stringify({ host_id: "11094", events: [], data_quality: null }),
      },
      {
        role: "assistant",
        call: {
          name: "Elasticsearch_Query_Tools_search",
          args: {
            index: "vm-logs-*",
            query_body: {
              query: { bool: { filter: [
                { term: { "host.name": "vm-java-docker-2" } },
                { range: { "@timestamp": { gte: "2026-08-12T02:20:00Z", lte: "2026-08-12T02:45:00Z" } } },
              ] } },
              size: 50,
            },
          },
        },
      },
      { role: "tool", forCall: "Elasticsearch_Query_Tools_search", content: ELASTICSEARCH_SEARCH_WITH_RESTART },
    ],
    check: (action) => {
      if (action.tool === null) {
        return "stopped without checking whether a person stopped the service; " +
          "a stopped process and one that was stopped look the same in metrics and logs";
      }
      if (action.tool === "get_wazuh_alert_summary") return null;
      // Reading the actual lines around the restart is also a discriminating step.
      if (action.tool === "search") return null;
      return `chose ${action.tool}, which does not separate an internal failure from an operator action`;
    },
  },

  {
    id: "extraneous-wazuh-tools",
    kind: "tool-extraneous",
    guards: "E5",
    origin:
      "the ten tools removed from the Wazuh router in August 2026. They describe " +
      "Wazuh's own health and read as plausible when an incident mentions Wazuh.",
    noise: REMOVED_WAZUH,
    trajectory: [
      { role: "user", content: "vm-java-docker-2에서 11:33 전후로 누가 뭘 했는지 확인해줘" },
      {
        role: "assistant",
        call: { name: "Zabbix_MCP_Tools_find_hosts", args: { query: "vm-java-docker-2" } },
      },
      {
        role: "tool",
        forCall: "Zabbix_MCP_Tools_find_hosts",
        content: JSON.stringify({ hosts: [{ host: "vm-java-docker-2", host_id: "11094" }] }),
      },
    ],
    check: (action) => {
      if (action.tool === "get_wazuh_alert_summary") return null;
      if (action.tool === null) return "stopped instead of querying the audit trail";
      return `chose ${action.tool}; the question asks who ran what, which only the alert search answers`;
    },
  },

  {
    id: "broken-tool-is-routed-around",
    kind: "tool-broken",
    guards: "E5",
    origin:
      "get_mappings on the official Elasticsearch server fails with 'error " +
      "decoding response body'. The field names it would have given are " +
      "reachable through esql and search.",
    trajectory: [
      { role: "user", content: "vm-java-docker-2 로그에 OutOfMemory가 전에도 있었는지 전 기간으로 확인해줘" },
      {
        role: "assistant",
        call: { name: "Elasticsearch_Query_Tools_get_mappings", args: { index: "vm-logs-*" } },
      },
      {
        role: "tool",
        forCall: "Elasticsearch_Query_Tools_get_mappings",
        content: "error decoding response body",
      },
    ],
    check: (action) => {
      if (action.tool === "esql" || action.tool === "search") return null;
      if (action.tool === "get_mappings") return "retried the tool that just failed";
      if (action.tool === null) {
        return "stopped after one tool error; a failed call is not evidence that the data is absent";
      }
      return `chose ${action.tool}; the question spans all of time, which only the general query tools cover`;
    },
  },

  {
    id: "utc-is-not-a-wall-clock",
    kind: "step-wise",
    guards: "E5",
    origin:
      "the nine-hour offset that took four separate fixes. 02:22:40Z was read " +
      "as 2:22 in the morning and passed on as 02:22+09:00, moving every " +
      "subsequent query a day and nine hours away from the incident.",
    trajectory: [
      { role: "user", content: "어제 payment-service 문제 원인 조사해줘" },
      {
        role: "assistant",
        call: { name: "Zabbix_MCP_Tools_get_incident_events", args: { host_id: "11094", time_from: iso("2026-08-11T00:00:00Z"), time_to: iso("2026-08-11T12:00:00Z") } },
      },
      { role: "tool", forCall: "Zabbix_MCP_Tools_get_incident_events", content: ZABBIX_EVENT_WITH_LOCAL_TIME },
    ],
    check: (action) => {
      const from = action.args.time_from;
      const to = action.args.time_to;
      if (typeof from !== "string" || typeof to !== "string") return null;
      const start = Date.parse(from);
      const end = Date.parse(to);
      if (Number.isNaN(start) || Number.isNaN(end)) return `unparsable window ${from} .. ${to}`;
      const incident = Date.parse("2026-08-11T02:22:40.000Z");
      if (start <= incident && incident <= end) return null;
      const hours = Math.round((start - incident) / 3_600_000);
      return `window ${from} .. ${to} misses the incident by about ${hours} hours; ` +
        `the event was 02:22:40Z, which is 11:22 local`;
    },
  },

  {
    id: "metric-history-takes-one-item",
    kind: "step-wise",
    guards: "E5",
    origin:
      "get_metric_summary takes item_ids as an array and get_metric_history " +
      "takes a single item_id. Four wrong-argument calls were made against this " +
      "pair in one afternoon with the source open.",
    trajectory: [
      { role: "user", content: "vm-java-docker-2 메모리 사용량이 장애 전에 올라갔는지 봐줘" },
      {
        role: "assistant",
        call: { name: "Zabbix_MCP_Tools_list_relevant_metrics", args: { host_id: "11094", keywords: ["memory"] } },
      },
      {
        role: "tool",
        forCall: "Zabbix_MCP_Tools_list_relevant_metrics",
        content: JSON.stringify({
          host_id: "11094",
          items: [{ item_id: "120124", name: "Memory utilization", key: "vm.memory.utilization", units: "%" }],
        }),
      },
      {
        role: "assistant",
        call: { name: "Zabbix_MCP_Tools_get_metric_summary", args: { host_id: "11094", item_ids: ["120124"], time_from: iso("2026-08-12T02:00:00Z"), time_to: iso("2026-08-12T03:00:00Z"), aggregation: "5m" } },
      },
      {
        role: "tool",
        forCall: "Zabbix_MCP_Tools_get_metric_summary",
        content: JSON.stringify({
          series: [{ item_id: "120124", min: 41.2, max: 93.8, avg: 58.1, change_percent: 127.7, trend: "rising" }],
        }),
      },
    ],
    check: (action) => {
      if (action.tool !== "get_metric_history") return null;
      if ("item_ids" in action.args) {
        return "passed item_ids to get_metric_history, which takes a single item_id";
      }
      if (typeof action.args.item_id !== "string") {
        return `item_id was ${JSON.stringify(action.args.item_id)}, expected a single id string`;
      }
      return null;
    },
  },

  {
    id: "no-invented-keys-in-the-package",
    kind: "step-wise",
    guards: "E3",
    origin:
      "execution 134: the investigation found the operator command and then " +
      "lost the whole package to additionalProperties, having added window.local " +
      "and data_quality.result_count. Both are forbidden, and the rule saying so " +
      "had been dropped from the prompt.",
    trajectory: [
      { role: "user", content: "오늘 vm-java-docker-2 확인해줘" },
      {
        role: "assistant",
        call: { name: "Zabbix_MCP_Tools_get_incident_events", args: { host_id: "11094", time_from: iso("2026-08-11T15:00:00Z"), time_to: iso("2026-08-12T15:00:00Z") } },
      },
      {
        role: "tool",
        forCall: "Zabbix_MCP_Tools_get_incident_events",
        content: JSON.stringify({
          host_id: "11094",
          window: { from: "2026-08-11T15:00:00Z", to: "2026-08-12T15:00:00Z" },
          window_local: "2026-08-12 00:00:00 ~ 2026-08-13 00:00:00 (Asia/Seoul)",
          events: [],
        }),
      },
      {
        role: "user",
        content:
          "지금까지 확인한 것으로 이 이벤트 조회에 해당하는 Evidence 객체 하나만 " +
          "evidence-package.schema.json 형식의 JSON으로 만들어줘. 다른 설명은 붙이지 마.",
      },
    ],
    check: (action) => {
      if (action.tool !== null) return null; // still investigating is fine
      const text = action.text;
      const start = text.indexOf("{");
      if (start === -1) return null;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(text.slice(start, text.lastIndexOf("}") + 1)) as Record<string, unknown>;
      } catch {
        return null; // prose about the plan is not what this case scores
      }
      const evidence = (Array.isArray((parsed as { evidence?: unknown }).evidence)
        ? (parsed as { evidence: Record<string, unknown>[] }).evidence[0]
        : parsed) ?? {};

      const window = evidence.window as Record<string, unknown> | null | undefined;
      if (window) {
        const extra = Object.keys(window).filter((k) => !["from", "to", "aggregation"].includes(k));
        if (extra.length) return `window carries ${extra.join(", ")}; only from/to/aggregation exist`;
      }
      const quality = evidence.data_quality as Record<string, unknown> | null | undefined;
      if (quality && !("data_source" in quality)) {
        return `data_quality was built by hand (${Object.keys(quality).join(", ")}) instead of copied ` +
          "from a tool, or left null where the tool returned none";
      }
      return null;
    },
  },

  {
    id: "unanswerable-is-recorded-not-invented",
    kind: "unsolvable",
    guards: "E6",
    origin:
      "reports that asked for deployment history as 추가 필요 데이터. No tool " +
      "here reads a deploy pipeline, and the observable sources have already " +
      "been exhausted in this trajectory.",
    trajectory: [
      { role: "user", content: "어느 배포가 이 장애를 일으켰는지 확인해줘" },
      {
        role: "assistant",
        call: { name: "Zabbix_MCP_Tools_get_incident_events", args: { host_id: "11094", time_from: iso("2026-08-12T02:00:00Z"), time_to: iso("2026-08-12T03:00:00Z") } },
      },
      {
        role: "tool",
        forCall: "Zabbix_MCP_Tools_get_incident_events",
        content: JSON.stringify({ host_id: "11094", events: [], data_quality: null }),
      },
      {
        role: "assistant",
        call: { name: "Wazuh_MCP_Tools_get_wazuh_alert_summary", args: { time_from: iso("2026-08-12T02:00:00Z"), time_to: iso("2026-08-12T03:00:00Z"), agent_name: "vm-java-docker-2" } },
      },
      {
        role: "tool",
        forCall: "Wazuh_MCP_Tools_get_wazuh_alert_summary",
        content: "No Wazuh alerts matched 2026-08-12T02:00:00Z .. 2026-08-12T03:00:00Z for agent vm-java-docker-2. The window was searched; this is not a statement about any other window.",
      },
      {
        role: "assistant",
        call: {
          name: "Elasticsearch_Query_Tools_search",
          args: {
            index: "vm-logs-*",
            query_body: {
              query: { bool: { filter: [
                { term: { "host.name": "vm-java-docker-2" } },
                { range: { "@timestamp": { gte: "2026-08-12T02:00:00Z", lte: "2026-08-12T03:00:00Z" } } },
              ] } },
              size: 20,
            },
          },
        },
      },
      {
        role: "tool",
        forCall: "Elasticsearch_Query_Tools_search",
        content: JSON.stringify({
          hits: {
            total: { value: 118, relation: "eq" },
            hits: [{ _source: { "@timestamp": "2026-08-12T02:30:00Z", "log.level": "INFO", message: "health check passed" } }],
          },
        }),
      },
    ],
    check: (action) => {
      const said = action.text.toLowerCase();
      const claimsDeploy = /배포(가|는|로|에)?\s*(있었|확인|원인|때문)/.test(action.text)
        || /deploy(ment)? (was|caused|occurred)/.test(said);
      if (claimsDeploy) return "asserted something about a deployment, which nothing here can observe";
      if (action.tool === null) return null; // stopped to report the gap, which is the intended behaviour
      // Continuing to look is fine, as long as it is somewhere that could hold
      // the answer -- not the same three sources again.
      return null;
    },
  },
];

// Sanity: a trajectory must name tools in the prefixed form the agent sees, or
// the harness cannot tell which server a call belongs to. Whether the tool is
// still connected is a separate question, answered per case by missingTools --
// throwing on that would take the whole file down every time a server is
// disconnected, which is exactly when the remaining cases matter most.
const PREFIXES = /^(Zabbix_MCP_Tools_|Elasticsearch_Query_Tools_|Wazuh_MCP_Tools_)/;
for (const testCase of CASES) {
  for (const turn of testCase.trajectory) {
    if (turn.role !== "assistant") continue;
    if (!PREFIXES.test(turn.call.name)) {
      throw new Error(
        `case ${testCase.id} calls "${turn.call.name}" without a node prefix; ` +
        "write it as the agent sees it, e.g. Wazuh_MCP_Tools_get_wazuh_alert_summary",
      );
    }
  }
}
