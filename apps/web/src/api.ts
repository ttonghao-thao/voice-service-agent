export interface Citation {
  citation_id: string;
  title: string;
  content: string;
  context: string | null;
  context_parts: {
    chunk_id: string;
    source_text: string;
    anchor: { page?: number | null; heading_path?: string[] };
    title_path: string[];
  }[];
  context_truncated: boolean;
  context_omitted: boolean;
  hits_omitted: number;
  relations: {
    relation_id: string;
    relation_type: string;
    stance: "supports" | "refutes";
    conditions: Record<string, string>;
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
}
export interface Turn {
  id: string;
  user_text: string;
  channel: string;
  status: string;
  answer: Answer | null;
  epoch: number;
  request_revision: number;
  parent_task_id: string | null;
  cancellation_reason: string | null;
  delivery_status: string;
  output_suppressed: boolean;
}
export interface RecordItem {
  kind: string;
  epoch: number;
  source_id: string;
  payload: { text?: string; response_id?: string; played_samples?: number };
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
  if (callAccessToken) headers.set("Authorization", `Bearer ${callAccessToken}`);
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
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            let data = "";
            for (const line of block.split("\n")) {
              if (line.startsWith("id:")) cursor = Number(line.slice(3).trim()) || cursor;
              if (line.startsWith("data:")) data += line.slice(5).trimStart();
            }
            if (data) onEvent(JSON.parse(data));
          }
        }
      } catch (error) {
        if (controller.signal.aborted) break;
        onError(error instanceof Error ? error.message : "Event stream disconnected");
      }
      await pause();
    }
  })();

  return () => controller.abort();
}
