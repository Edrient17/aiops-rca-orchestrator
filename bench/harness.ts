import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Ask the collector's model for the next action, given a frozen trajectory.
 *
 * The agent's real loop is a tool-calling loop, so a plan step is observable as
 * a tool call: name plus arguments. That is what this reads back, which keeps
 * the benchmark measuring the same decision the deployed agent makes rather
 * than a prose description of it.
 */

const BENCH = resolve(import.meta.dirname);
const PROMPTS = resolve(BENCH, "..", "prompts");

export type ToolSpec = {
  name: string;
  bare_name: string;
  description: string;
  parameters: Record<string, unknown>;
};

type Snapshot = { servers: Record<string, ToolSpec[]> };

const snapshot = JSON.parse(
  readFileSync(resolve(BENCH, "tools.snapshot.json"), "utf8"),
) as Snapshot;

export const ALL_TOOLS: ToolSpec[] = Object.values(snapshot.servers).flat();

export const collectorPrompt = (): string =>
  readFileSync(resolve(PROMPTS, "evidence-collector.system.md"), "utf8");

/** One turn of the frozen trajectory. */
export type Turn =
  | { role: "user"; content: string }
  | { role: "assistant"; content?: string; call: { name: string; args: unknown } }
  | { role: "tool"; forCall: string; content: string };

export type PredictedAction = {
  /** Bare tool name, node prefix stripped. Null when the model answered in prose. */
  tool: string | null;
  args: Record<string, unknown>;
  /** Whatever the model said alongside, which is where a refusal shows up. */
  text: string;
};

export type BenchCase = {
  id: string;
  /** APB setting this case instantiates. */
  kind: "step-wise" | "tool-broken" | "tool-extraneous" | "unsolvable";
  /** APB error category the case guards against. */
  guards: "E1" | "E2" | "E3" | "E4" | "E5" | "E6";
  /** The real incident this was distilled from, so a failure can be read. */
  origin: string;
  trajectory: Turn[];
  /**
   * Extra tools to inject for the tool-extraneous setting, by bare name. They
   * come from the ten removed from the Wazuh router: real schemas, genuinely
   * irrelevant to an outage.
   */
  noise?: ToolSpec[];
  /** Return null to pass, or the reason it failed. */
  check: (action: PredictedAction) => string | null;
};

/**
 * Bare tool names a case needs in the snapshot, read from its own trajectory.
 * A case whose prior calls no longer exist is asking the model about a tool it
 * cannot see, and would fail for a reason that has nothing to do with planning.
 */
export function requiredTools(testCase: BenchCase): string[] {
  return [...new Set(
    testCase.trajectory
      .filter((turn): turn is Extract<Turn, { role: "assistant" }> => turn.role === "assistant")
      .map((turn) => bare(turn.call.name)),
  )];
}

const available = new Set(ALL_TOOLS.map((t) => t.bare_name));

/** Which of a case's tools are missing, so a skip can say why. */
export const missingTools = (testCase: BenchCase): string[] =>
  requiredTools(testCase).filter((name) => !available.has(name));

const MODEL = process.env.BENCH_MODEL ?? "gpt-5.6-terra";
// Matches the Investigation Model node. Reasoning effort changes which plan the
// model produces, so a benchmark run at a different effort is not measuring the
// deployed agent.
const EFFORT = process.env.BENCH_EFFORT ?? "medium";
const KEY = process.env.OPENAI_API_KEY;

export const benchEnabled = Boolean(KEY);

/** n8n exposes tools prefixed with the node name; cases are written bare. */
const bare = (name: string): string =>
  name.replace(/^(Zabbix_MCP_Tools_|Log_MCP_Tools_|Elasticsearch_Query_Tools_|Wazuh_MCP_Tools_)/, "");

const toolsFor = (testCase: BenchCase) =>
  [...ALL_TOOLS, ...(testCase.noise ?? [])].map((tool) => ({
    type: "function" as const,
    name: tool.name,
    description: tool.description,
    parameters: tool.parameters,
  }));

/**
 * The trajectory as Responses API input items. Prior tool calls are their own
 * item type here rather than a field on an assistant message, which is the
 * shape n8n's agent sends.
 */
function inputFor(testCase: BenchCase) {
  const input: Record<string, unknown>[] = [];
  for (const turn of testCase.trajectory) {
    if (turn.role === "user") {
      input.push({ role: "user", content: turn.content });
    } else if (turn.role === "assistant") {
      if (turn.content) input.push({ role: "assistant", content: turn.content });
      input.push({
        type: "function_call",
        call_id: turn.call.name,
        name: turn.call.name,
        arguments: JSON.stringify(turn.call.args),
      });
    } else {
      input.push({ type: "function_call_output", call_id: turn.forCall, output: turn.content });
    }
  }
  return input;
}

export async function predictNextAction(testCase: BenchCase): Promise<PredictedAction> {
  // /v1/responses, not /v1/chat/completions. These models refuse function tools
  // together with a reasoning effort on the older endpoint, and n8n's agent
  // node uses the Responses API -- so measuring on chat/completions would score
  // a configuration the deployed agent never runs.
  const response = await fetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${KEY}`,
    },
    body: JSON.stringify({
      model: MODEL,
      instructions: collectorPrompt(),
      input: inputFor(testCase),
      tools: toolsFor(testCase),
      tool_choice: "auto",
      reasoning: { effort: EFFORT },
    }),
  });

  if (!response.ok) {
    throw new Error(`${MODEL} returned ${response.status}: ${(await response.text()).slice(0, 300)}`);
  }

  const body = await response.json() as {
    output?: ({ type: string; name?: string; arguments?: string;
                content?: { type: string; text?: string }[] })[];
  };
  const items = body.output ?? [];
  const call = items.find((item) => item.type === "function_call");
  const said = items
    .filter((item) => item.type === "message")
    .flatMap((item) => item.content ?? [])
    .map((part) => part.text ?? "")
    .join(" ");

  let args: Record<string, unknown> = {};
  if (call) {
    try {
      args = JSON.parse(call.arguments || "{}") as Record<string, unknown>;
    } catch {
      // A model that emits unparsable arguments has made a tool-use error, and
      // the case's own check should be the thing that says so.
      args = { __unparsable: call.arguments };
    }
  }

  return {
    tool: call?.name ? bare(call.name) : null,
    args,
    text: said,
  };
}
