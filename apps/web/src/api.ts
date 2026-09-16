export interface Citation {
  citation_id: string;
  title: string;
  content: string;
  context: string | null;
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
export interface Me {
  user_id: string;
  tenant_id: string;
  scopes: string[];
  auth_mode: string;
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
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch("/api/v1" + path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      data.code || "REQUEST_FAILED",
      data.message || "请求失败",
      response.status,
    );
  return data;
}
