import { z } from "zod";

/**
 * The shape of a report template, validated when an operator writes one.
 *
 * These are edited at runtime rather than deployed, so a malformed one would
 * otherwise surface as a failed investigation: the template reaches the agents
 * as prompt text, and a missing section list or an absurd tool budget is only
 * discovered once a question has already been asked. Everything here is
 * therefore bounded and checked at write time, where the operator is still
 * present to read the error.
 */

/** Plain identifier: it is interpolated into prompts and request paths. */
export const TEMPLATE_ID = /^[a-z][a-z0-9_]{2,63}$/;

export const templateIdSchema = z
  .string()
  .regex(TEMPLATE_ID, "template_id must be lower-case letters, digits and underscores, 3-64 chars");

const hostSelectorSchema = z.discriminatedUnion("mode", [
  /** Hosts come from the question, as they do for an incident. */
  z.object({ mode: z.literal("from_question") }),
  /**
   * Hosts come from Zabbix host groups. A periodic report is about an estate
   * rather than about whatever the asker happened to name, and this is what
   * lets "월말 보고서 만들어줘" resolve to hosts at all.
   */
  z.object({
    mode: z.literal("host_group"),
    group_ids: z.array(z.string().regex(/^\d+$/)).min(1).max(20),
  }),
]);

const collectionSchema = z.object({
  host_selector: hostSelectorSchema,
  window: z.object({
    policy: z.enum(["standard", "long_term_capacity"]),
    /**
     * Which span to investigate when the question names none. Kept to a closed
     * set so the workflow can turn it into concrete timestamps; free text would
     * leave that to the model, which is the arithmetic this system deliberately
     * keeps away from it.
     */
    range: z
      .enum(["anchor_relative", "last_7_days", "last_30_days", "last_calendar_month"])
      .default("anchor_relative"),
  }),
  /** Null lets the Evidence Collector choose, as it does today. */
  aggregation: z
    .enum(["raw", "1m", "5m", "15m", "1h", "6h", "1d"])
    .nullable()
    .default(null),
  /** Seed keywords for list_relevant_metrics. */
  metric_keywords: z.array(z.string().min(1).max(100)).max(30).default([]),
  limits: z
    .object({
      max_iterations: z.number().int().min(1).max(20).optional(),
      max_tool_calls: z.number().int().min(1).max(200).optional(),
    })
    .default({}),
  /** Appended to the Evidence Collector's input. */
  guidance: z.string().max(4_000).default(""),
});

const outputSchema = z.object({
  /**
   * The document's outline. The agents' own output contract is fixed, so these
   * do not describe a JSON shape -- they tell the writer what the report is
   * made of, and the renderer walks whatever comes back.
   */
  sections: z
    .array(
      z.object({
        /**
         * How the writer's output is matched back to this section. Matching on
         * an id rather than on the heading means the writer never reproduces a
         * heading, so it cannot quietly rename one, and the operator can retitle
         * a section without invalidating anything.
         */
        id: z
          .string()
          .regex(TEMPLATE_ID, "section id must be lower-case letters, digits and underscores, 3-64 chars"),
        heading: z.string().min(1).max(120),
        instruction: z.string().min(1).max(2_000),
        /** A section with nothing to say is dropped rather than left empty. */
        required: z.boolean().default(true),
        /**
         * Withholds the section unless the investigation found a real Zabbix
         * problem event.
         *
         * Incident timing is the case this exists for. The writer has been seen
         * copying the investigation window into "started at" and "duration" on a
         * host that was fine, which reads as an hour-long outage that never
         * happened. Judgement in a prompt did not stop that; dropping the
         * section when nothing backs it does.
         */
        requires_problem_event: z.boolean().default(false),
      }),
    )
    .min(1)
    .max(30)
    .refine(
      (sections) => new Set(sections.map((s) => s.id)).size === sections.length,
      "section ids must be unique within a template",
    ),
  /** Appended to the RCA Writer's input. */
  guidance: z.string().max(4_000).default(""),
});

export const reportTemplateSchema = z.object({
  title: z.string().min(1).max(200),
  /** Why the question analyzer would pick this one. */
  description: z.string().min(1).max(1_000),
  enabled: z.boolean().default(true),
  collection: collectionSchema,
  output: outputSchema,
});

export type ReportTemplateBody = z.infer<typeof reportTemplateSchema>;

export interface ReportTemplate extends ReportTemplateBody {
  template_id: string;
  version: number;
}

export interface SaveTemplateResult {
  version: number;
  /** False when the submitted template matched what was already stored. */
  changed: boolean;
  created: boolean;
}
