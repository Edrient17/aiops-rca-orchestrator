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

const MODEL = process.env.BENCH_MODEL ?? "gpt-5.6-terra";
const KEY = process.env.OPENAI_API_KEY;

export const benchEnabled = Boolean(KEY);

/** n8n exposes tools prefixed with the node name; cases are written bare. */
const bare = (name: string): string =>
  name.replace(/^(Zabbix_MCP_Tools_|Log_MCP_Tools_|Elasticsearch_Query_Tools_|Wazuh_MCP_Tools_)/, "");

const toolsFor = (testCase: BenchCase) =>
  [...ALL_TOOLS, ...(testCase.noise ?? [])].map((tool) => ({
    type: "function" as const,
    function: {
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters,
    },
  }));

function messagesFor(testCase: BenchCase) {
  const messages: Record<string, unknown>[] = [
    { role: "system", content: collectorPrompt() },
  ];
  for (const turn of testCase.trajectory) {
    if (turn.role === "user") {
      messages.push({ role: "user", content: turn.content });
    } else if (turn.role === "assistant") {
      messages.push({
        role: "assistant",
        content: turn.content ?? null,
        tool_calls: [{
          id: turn.call.name,
          type: "function",
          function: { name: turn.call.name, arguments: JSON.stringify(turn.call.args) },
        }],
      });
    } else {
      messages.push({ role: "tool", tool_call_id: turn.forCall, content: turn.content });
    }
  }
  return messages;
}

export async function predictNextAction(testCase: BenchCase): Promise<PredictedAction> {
  const response = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${KEY}`,
    },
    body: JSON.stringify({
      model: MODEL,
      messages: messagesFor(testCase),
      tools: toolsFor(testCase),
      tool_choice: "auto",
    }),
  });

  if (!response.ok) {
    throw new Error(`${MODEL} returned ${response.status}: ${(await response.text()).slice(0, 300)}`);
  }

  const body = await response.json() as {
    choices: { message: { content: string | null; tool_calls?: { function: { name: string; arguments: string } }[] } }[];
  };
  const message = body.choices[0]?.message;
  const call = message?.tool_calls?.[0];

  let args: Record<string, unknown> = {};
  if (call) {
    try {
      args = JSON.parse(call.function.arguments || "{}") as Record<string, unknown>;
    } catch {
      // A model that emits unparsable arguments has made a tool-use error, and
      // the case's own check should be the thing that says so.
      args = { __unparsable: call.function.arguments };
    }
  }

  return {
    tool: call ? bare(call.function.name) : null,
    args,
    text: message?.content ?? "",
  };
}
