export interface SourceAnchor {
  page?: number | null;
  heading_path?: string[];
  start_offset?: number | null;
  end_offset?: number | null;
  bbox?: Record<string, number | string> | null;
}
export interface SourceSpan {
  label: string;
  text: string;
  anchor: SourceAnchor;
  parser_item_id?: string | null;
  span_id?: string | null;
  row_index?: number | null;
  column_index?: number | null;
}
export interface CitationApplicability {
  status: "resolved" | "unknown" | "ambiguous";
  reason_codes?: ("missing_binding" | "no_reviewed_match" | "structure_missing" | "scope_conflict")[];
  subjects: {
    entity_id?: string | null;
    display_name: string;
    product_model?: string | null;
    software_version?: string | null;
    source_kind: "curated" | "reviewed_alias" | "parser";
    provenance_id?: string | null;
    source_spans?: SourceSpan[];
  }[];
  conditions: {
    text: string;
    source_kind: "curated" | "parser";
    provenance_id?: string | null;
    source_spans?: SourceSpan[];
  }[];
}
export interface Citation {
  citation_id: string;
  title: string;
  content: string;
  context: string | null;
  context_parts: {
    chunk_id: string;
    source_text: string;
    anchor: SourceAnchor;
    title_path: string[];
    source_spans?: SourceSpan[];
    required?: boolean;
    role?: "attribution" | "table_header" | "condition" | "exception" | "continuation" | null;
    applicability?: CitationApplicability;
    link_provenance?: {
      kind: "parser_reference" | "curated" | "parser_continuation";
      source_unit_id: string;
      target_unit_id: string;
      provenance_id: string;
      reference_text?: string | null;
    } | null;
  }[];
  context_truncated: boolean;
  fact_kind?: "statement" | "specification" | "comparison" | "bundle" | "general";
  dependency_issues?: ("unmatched_reference" | "ambiguous_reference" | "dependency_limit_exceeded" | "dependency_cycle" | "unsafe_dependency")[];
  context_omitted: boolean;
  hits_omitted: number;
  relations: {
    relation_id: string;
    relation_type: string;
    stance: "supports" | "refutes";
    conditions: Record<string, unknown>;
  }[];
  version_id: string;
  business_version: string | null;
  updated_at: string | null;
  trace_id: string;
  retrieval_status: string;
  evidence_status: string;
  degraded_reasons: string[];
  scope_limited: boolean;
  rank: number;
  title_path: string[];
  anchor: Record<string, unknown>;
  source_uri: string | null;
  is_mock: boolean;
  // Optional for previously saved references that predate CueKB V2.
  cuekb_schema_version?: "1" | "2";
  document_name?: string | null;
  content_sha256?: string | null;
  applicability?: CitationApplicability;
  source_spans?: SourceSpan[];
  processing_profile?: string | null;
  source_unit_id?: string | null;
  block_kind?: "text" | "list" | "table" | "heading";
  context_state?: "disabled" | "complete" | "truncated" | "unresolved" | null;
  context_omissions?: string[];
  score?: number | null;
  score_type?: "rrf" | "rerank" | null;
  stage_scores?: Record<string, number>;
  ranking_method?: string | null;
  ranking_version?: string | null;
}
export interface Answer {
  answer_id: string;
  status: string;
  display_text: string;
  speech_text: string;
  citations: Citation[];
  cards: Record<string, unknown>[];
  is_mock: boolean;
  reason_code: string | null;
  answer_kind?: string;
  composition?: string;
  validation_level?: string;
  verification_timing?: string;
}
export interface Turn {
  id: string;
  input_item_id?: string | null;
  created_at?: string;
  user_text: string;
  channel: string;
  status: string;
  answer: Answer | null;
  epoch: number;
  request_revision: number;
  parent_task_id: string | null;
  cancellation_reason: string | null;
  delivery_status: string;
  execution_phase?: string | null;
  output_suppressed: boolean;
}
export interface RecordItem {
  kind: string;
  epoch: number;
  source_id: string;
  created_at: string;
  payload: {
    text?: string;
    response_id?: string;
    turn_id?: string;
    input_item_id?: string;
    related_input_item_ids?: string[];
    answer_kind?: string;
    phase?: string;
    segment_index?: number;
    played_samples?: number;
    status?: string;
    mode?: string;
    reason_code?: string | null;
  };
}
export interface Conversation {
  id: string;
  title: string;
  epoch: number;
  request_revision: number;
  access_token?: string;
}
export interface PortalEvent {
  type: string;
  event_id: string;
  conversation_id: string;
  epoch: number;
  request_revision: number;
  server_seq: number;
  turn_id: string | null;
  payload: Record<string, unknown>;
}
export interface Capabilities {
  is_mock: boolean;
  voice_available: boolean;
  text_configured: boolean;
  agent_provider: string;
  cuekb_mode: string;
  weather_mode: string;
  provider: string;
  voice_session_max_seconds: number;
  qa_execution_mode?: string;
  native_tools?: string[];
  native_tool_revision?: number;
  portal_task_progress?: boolean;
}
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

let callAccessToken = "";

export function setCallAccessToken(token: string) {
  callAccessToken = token;
}

function requestHeaders(init: RequestInit) {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body !== undefined)
    headers.set("Content-Type", "application/json");
  if (callAccessToken)
    headers.set("Authorization", `Bearer ${callAccessToken}`);
  return headers;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch("/api/v1" + path, {
    ...init,
    headers: requestHeaders(init),
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      data.code || "REQUEST_FAILED",
      data.message || "Request failed",
      response.status,
    );
  return data;
}

export function streamEvents(
  path: string,
  onEvent: (event: PortalEvent) => void,
  onError: (message: string) => void,
) {
  const controller = new AbortController();
  const token = callAccessToken;
  let cursor = 0;

  const pause = () =>
    new Promise<void>((resolve) => {
      const timer = window.setTimeout(resolve, 500);
      controller.signal.addEventListener(
        "abort",
        () => {
          window.clearTimeout(timer);
          resolve();
        },
        { once: true },
      );
    });

  void (async () => {
    while (!controller.signal.aborted) {
      try {
        const headers = new Headers({ Accept: "text/event-stream" });
        if (token) headers.set("Authorization", `Bearer ${token}`);
        if (cursor) headers.set("Last-Event-ID", String(cursor));
        const response = await fetch("/api/v1" + path, {
          headers,
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok || !response.body)
          throw new Error(`Event stream failed (${response.status})`);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder
            .decode(value, { stream: true })
            .replace(/\r\n/g, "\n");
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            let data = "";
            for (const line of block.split("\n")) {
              if (line.startsWith("id:"))
                cursor = Number(line.slice(3).trim()) || cursor;
              if (line.startsWith("data:")) data += line.slice(5).trimStart();
            }
            if (data) onEvent(JSON.parse(data));
          }
        }
      } catch (error) {
        if (controller.signal.aborted) break;
        onError(
          error instanceof Error ? error.message : "Event stream disconnected",
        );
      }
      await pause();
    }
  })();

  return () => controller.abort();
}
