import type {
  ReportTemplate,
  ReportTemplateBody,
  ReportTemplateFile,
  SaveTemplateResult,
} from "./templates.js";

export type {
  ReportTemplate,
  ReportTemplateBody,
  ReportTemplateFile,
  SaveTemplateResult,
};

export interface SlackEventEnvelope {
  type: "event_callback";
  event_id: string;
  event_time?: number;
  team_id?: string;
  api_app_id?: string;
  event: {
    type: string;
    channel?: string;
    user?: string;
    text?: string;
    ts?: string;
    thread_ts?: string;
    bot_id?: string;
    subtype?: string;
    /** Reaction events name the emoji here rather than carrying text. */
    reaction?: string;
    /** Reaction events point at their target here, not at event.channel. */
    item?: {
      type?: string;
      channel?: string;
      ts?: string;
    };
  };
}

export interface AcceptedSlackRequest {
  requestId: string;
  slackEventId: string;
  teamId: string | null;
  channelId: string;
  userId: string;
  messageTs: string;
  threadTs: string | null;
  question: string;
  receivedAt: string;
  rawPayload: unknown;
  /** Set when this message answers a clarification the bot asked for. */
  parentRequestId?: string | null;
}

/** A request that stopped at needs_clarification and is awaiting an answer. */
export interface PendingClarification {
  requestId: string;
  question: string;
}

export interface SaveRequestResult {
  created: boolean;
  requestId: string;
}

export interface DispatchJob {
  id: number;
  requestId: string;
  attempts: number;
  payload: {
    request_id: string;
    slack_event_id: string;
    team_id: string | null;
    channel_id: string;
    user_id: string;
    message_ts: string;
    thread_ts: string | null;
    question: string;
    received_at: string;
    /** Present only when this request answers an earlier clarification. */
    parent_request_id: string | null;
    prior_question: string | null;
    /**
     * The answer-channel message the parent request is anchored to. A
     * continuation posts into this thread instead of starting a new one.
     */
    parent_ack_ts: string | null;
    /** Set once acknowledged, so a retry rejoins that thread. */
    slack_ack_ts: string | null;
  };
}

export interface AgentRunInput {
  stage: "question_analyzer" | "evidence_collector" | "rca_writer";
  status: "succeeded" | "failed";
  model?: string;
  durationMs?: number;
  output?: unknown;
  error?: string;
}

export interface ReportInput {
  parsedRequest: unknown;
  evidencePackage: unknown;
  rcaReport: unknown;
  slackMarkdown: string;
  slackChannelId: string;
  slackMessageTs?: string;
}

/** The verdict an operator can pass on a published report. */
export type FeedbackLabel = "correct" | "partial" | "incorrect";

/** A published report located from the Slack message it was posted as. */
export interface ReportRef {
  requestId: string;
  /**
   * Root of the thread the report lives in. The report is itself a reply under
   * the acknowledgement, and Slack threads are one level deep, so a reply to
   * the report carries this ts rather than the report's own.
   */
  threadTs: string;
}

export interface ReportFeedbackInput {
  requestId: string;
  userId: string;
  reaction: string;
  label: FeedbackLabel;
}

export interface SaveFeedbackResult {
  created: boolean;
  /**
   * True only for the first negative verdict on a report that has no written
   * correction yet, so repeated reactions do not each ask the same question.
   */
  shouldAskForCorrection: boolean;
}

export interface ReportNoteInput {
  requestId: string;
  userId: string;
  slackMessageTs: string;
  note: string;
}

export interface SystemErrorInput {
  requestId?: string;
  workflowName?: string;
  message: string;
}

export interface RequestRepository {
  ping(): Promise<void>;
  /**
   * The catalog the question analyzer classifies against. Disabled templates
   * are excluded so retiring one takes it out of circulation without deleting
   * the rows that explain past reports.
   */
  listTemplates(includeDisabled: boolean): Promise<ReportTemplate[]>;
  getTemplate(templateId: string): Promise<ReportTemplate | null>;
  /** Upsert. Bumps the version only when the content actually differs. */
  saveTemplate(
    templateId: string,
    body: ReportTemplateBody,
  ): Promise<SaveTemplateResult>;
  deleteTemplate(templateId: string): Promise<boolean>;
  saveSlackRequest(request: AcceptedSlackRequest): Promise<SaveRequestResult>;
  findPendingClarification(
    channelId: string,
    threadTs: string,
  ): Promise<PendingClarification | null>;
  /**
   * Takes the next due job and holds it for `lockSeconds`. The caller sets the
   * hold because only it knows how long it may keep the job in flight; the lock
   * has to outlast that, or a second dispatcher can claim a job still being
   * delivered and investigate the same request twice.
   */
  claimDispatch(lockSeconds: number): Promise<DispatchJob | null>;
  completeDispatch(jobId: number): Promise<void>;
  retryDispatch(jobId: number, delaySeconds: number, error: string): Promise<void>;
  updateRequestStatus(
    requestId: string,
    status: string,
    error?: string,
    slackAckTs?: string,
  ): Promise<boolean>;
  recordAgentRun(requestId: string, input: AgentRunInput): Promise<boolean>;
  saveReport(requestId: string, input: ReportInput): Promise<boolean>;
  recordSystemError(input: SystemErrorInput): Promise<void>;
  getRequest(requestId: string): Promise<unknown | null>;
  /** Finds the report published as this exact message, for reaction events. */
  findReportByMessage(channelId: string, messageTs: string): Promise<ReportRef | null>;
  /** Finds the report a thread belongs to, for replies written under it. */
  findReportByThread(channelId: string, threadTs: string): Promise<ReportRef | null>;
  saveReportFeedback(input: ReportFeedbackInput): Promise<SaveFeedbackResult>;
  removeReportFeedback(input: {
    requestId: string;
    userId: string;
    reaction: string;
  }): Promise<boolean>;
  saveReportNote(input: ReportNoteInput): Promise<boolean>;
}
