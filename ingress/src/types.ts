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

export interface SystemErrorInput {
  requestId?: string;
  workflowName?: string;
  executionId?: string;
  lastNode?: string;
  message: string;
  details?: unknown;
}

export interface RequestRepository {
  ping(): Promise<void>;
  saveSlackRequest(request: AcceptedSlackRequest): Promise<SaveRequestResult>;
  findPendingClarification(
    channelId: string,
    threadTs: string,
  ): Promise<PendingClarification | null>;
  claimDispatch(): Promise<DispatchJob | null>;
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
}
