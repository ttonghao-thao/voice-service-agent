import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Input,
  Slider,
  Spin,
  Tag,
} from "antd";
import {
  AudioOutlined,
  AudioMutedOutlined,
  SendOutlined,
  StopOutlined,
  CustomerServiceOutlined,
  FileTextOutlined,
  LinkOutlined,
  MenuOutlined,
  CheckCircleOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import {
  Answer,
  api,
  Capabilities,
  Conversation,
  PortalEvent,
  setCallAccessToken,
  streamEvents,
  Turn,
} from "./api";
import { VoiceClient, VoiceState } from "./audio/VoiceClient";

const stateLabels: Record<VoiceState, string> = {
  closed: "Voice disconnected",
  connecting: "Connecting",
  ready: "Voice ready",
  reconnecting: "Reconnecting",
  error: "Voice unavailable",
};
const statuses: Record<string, string> = {
  answered: "Answered",
  needs_clarification: "Clarification needed",
  insufficient_evidence: "Insufficient evidence",
  failed: "Search failed",
  canceled: "Canceled",
  superseded: "Superseded",
  expired: "Expired",
  running: "Searching",
};

export default function App() {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [cid, setCid] = useState(""),
    [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState(""),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [sending, setSending] = useState(false),
    [progress, setProgress] = useState(""),
    [callEnded, setCallEnded] = useState(false);
  const [voiceState, setVoiceState] = useState<VoiceState>("closed"),
    [muted, setMuted] = useState(false),
    [volume, setVolume] = useState(0.8),
    [inputState, setInputState] = useState("quiet");
  const [selected, setSelected] = useState<Answer | null>(null),
    [sidebar, setSidebar] = useState(false),
    [sourcesOpen, setSourcesOpen] = useState(false),
    [before, setBefore] = useState<string | null>(null);
  const [transcripts, setTranscripts] = useState<
    Record<string, { kind: string; text: string; done: boolean }>
  >({});
  const epoch = useRef(0),
    requestRevision = useRef(0),
    active = useRef(""),
    voice = useRef<VoiceClient | null>(null),
    stopEvents = useRef<(() => void) | null>(null),
    lastEvent = useRef(new Set<string>()),
    end = useRef<HTMLDivElement>(null);
  const onError = useCallback((message: string) => setError(message), []);
  const refresh = useCallback(async (id: string, older?: string) => {
    const data = await api<{
      items: Turn[];
      epoch: number;
      request_revision: number;
      next_before: string | null;
    }>(`/conversations/${id}/messages${older ? "?before=" + older : ""}`);
    if (active.current !== id) return;
    epoch.current = Math.max(epoch.current, data.epoch);
    requestRevision.current = Math.max(
      requestRevision.current,
      data.request_revision || 0,
    );
    setBefore(data.next_before);
    setTurns((previous) =>
      older
        ? [
            ...data.items,
            ...previous.filter((t) => !data.items.some((x) => x.id === t.id)),
          ]
        : data.items,
    );
    const answer = [...data.items].reverse().find((t) => t.answer)?.answer;
    if (answer && !older) setSelected(answer);
  }, []);
  const handleEvent = useCallback(
    (event: PortalEvent) => {
      if (
        event.conversation_id !== active.current ||
        event.epoch < epoch.current
      )
        return;
      if (lastEvent.current.has(event.event_id)) return;
      lastEvent.current.add(event.event_id);
      if (lastEvent.current.size > 2048)
        lastEvent.current.delete(lastEvent.current.values().next().value!);
      epoch.current = event.epoch;
      requestRevision.current = Math.max(
        requestRevision.current,
        event.request_revision || 0,
      );
      if (event.type === "portal.tool.started") {
        setProgress(String(event.payload.message || "Searching"));
        void refresh(event.conversation_id).catch((e) => setError(e.message));
      }
      if (event.type === "portal.answer.final") {
        setProgress("");
        setSelected(event.payload as unknown as Answer);
        void refresh(event.conversation_id).catch((e) => setError(e.message));
      }
      if (event.type === "portal.playback.clear") {
        voice.current?.clearForEpoch(event.epoch);
        setProgress("");
        void refresh(event.conversation_id).catch((e) => setError(e.message));
      }
      if (event.type === "portal.input.state")
        setInputState(String(event.payload.state));
      if (
        event.type.includes("transcript.") ||
        event.type.includes("speech_text.")
      ) {
        const key = `${event.epoch}:${event.type.includes("speech_text") ? "voice" : "user"}:${event.payload.item_id || event.payload.response_id}`;
        setTranscripts((old) => ({
          ...old,
          [key]: {
            kind: event.type.includes("speech_text") ? "Spoken reply" : "Your transcript",
            text: event.type.endsWith(".done")
              ? String(event.payload.text)
              : (old[key]?.text || "") + String(event.payload.text),
            done: event.type.endsWith(".done"),
          },
        }));
      }
    },
    [refresh],
  );
  useEffect(() => {
    voice.current = new VoiceClient(handleEvent, setVoiceState, onError);
    return () => {
      void voice.current?.stop();
    };
  }, [handleEvent, onError]);
  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const capabilities = await api<Capabilities>("/capabilities");
        if (current) setCaps(capabilities);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        if (current) setLoading(false);
      }
    })();
    return () => {
      current = false;
    };
  }, []);
  useEffect(() => {
    if (!cid) return;
    void refresh(cid).catch((e) => setError(e.message));
    const stop = streamEvents(
      `/conversations/${cid}/events`,
      handleEvent,
      (message) => setError(message),
    );
    stopEvents.current = stop;
    return () => {
      stop();
      if (stopEvents.current === stop) stopEvents.current = null;
    };
  }, [cid, refresh, handleEvent]);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, progress, transcripts]);
  async function closeCurrentCall() {
    stopEvents.current?.();
    stopEvents.current = null;
    await voice.current?.stop();
    const id = active.current;
    active.current = "";
    if (id) await api(`/conversations/${id}`, { method: "DELETE" }).catch(() => {});
    setCallAccessToken("");
  }
  async function newConversation() {
    try {
      await closeCurrentCall();
      const c = await api<Conversation>("/conversations", {
        method: "POST",
        body: JSON.stringify({ title: "New conversation", locale: "en-US" }),
      });
      if (!c.access_token) throw new Error("The server did not issue call access");
      setCallAccessToken(c.access_token);
      active.current = c.id;
      epoch.current = 0;
      requestRevision.current = 0;
      setTurns([]);
      setSelected(null);
      setTranscripts({});
      setProgress("");
      setCallEnded(false);
      lastEvent.current.clear();
      setCid(c.id);
      setSidebar(false);
      return c.id;
    } catch (e) {
      setError((e as Error).message);
      return "";
    }
  }
  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setError("");
    try {
      voice.current?.clear();
      await voice.current?.stop();
      const id = !cid || callEnded ? await newConversation() : cid;
      if (!id) return;
      const result = await api<{
        turn_id: string;
        epoch: number;
        request_revision: number;
      }>(
        `/conversations/${id}/messages`,
        {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({ text }),
        },
      );
      if (active.current === id) {
        epoch.current = Math.max(epoch.current, result.epoch);
        requestRevision.current = Math.max(
          requestRevision.current,
          result.request_revision,
        );
        setDraft("");
        setProgress("Processing");
        await refresh(id);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  }
  async function startCall() {
    setError("");
    const id = await newConversation();
    if (id) await voice.current?.start(id);
  }
  async function endCall() {
    if (!active.current || callEnded) return;
    await closeCurrentCall();
    setCallEnded(true);
    setProgress("");
  }
  async function cancelCurrent() {
    const id = cid;
    if (!id) return;
    voice.current?.stopPlayback();
    setProgress("");
    try {
      const result = await api<{ request_revision: number }>(
        `/conversations/${id}/tasks/current/cancel`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_epoch: epoch.current,
            expected_revision: requestRevision.current,
          }),
        },
      );
      requestRevision.current = result.request_revision;
      await refresh(id);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const nav = (
    <>
      <div className="brand">
        <span className="brand-mark">
          <CustomerServiceOutlined />
        </span>
        <div>
          VoiceBridge<small>SERVICE DESK</small>
        </div>
      </div>
      <div className="call-isolation">
        <strong>Private test call</strong>
        <p>Each tab creates its own conversation when you start a call. No history is shared with other tabs.</p>
      </div>
      <div className="sidebar-foot">
        <span className="avatar">
          <AudioOutlined />
        </span>
        <div>
          <strong>{cid && !callEnded ? "Call in this tab" : "No active call"}</strong>
          <small>{stateLabels[voiceState]}</small>
        </div>
      </div>
    </>
  );
  const sources = (
    <div className="evidence">
      <div className="evidence-heading">
        <FileTextOutlined />
        <h3>Answer sources</h3>
        {selected && <span>{selected.citations.length} sources</span>}
      </div>
      {!selected ? (
        <div className="evidence-placeholder">
          <FileTextOutlined />
          <p>Answers backed by sources</p>
          <small>After a search, view knowledge sources, versions, and locations here.</small>
        </div>
      ) : (
        <>
          <Tag color={selected.status === "answered" ? "green" : "orange"}>
            {statuses[selected.status] || selected.status}
          </Tag>
          {selected.is_mock && <Tag color="orange">Synthetic integration data</Tag>}
          {selected.citations.map((c) => (
            <article className="citation" key={c.citation_id}>
              <div className="citation-title">
                <span>{c.citation_id}</span>
                <strong>{c.title}</strong>
              </div>
              <p>{c.content}</p>
              {c.context_parts?.length > 0 && (
                <details className="citation-context">
                  <summary>Context and source locations ({c.context_parts.length})</summary>
                  {c.context_parts.map((part) => (
                    <div key={part.chunk_id}>
                      <strong>{part.title_path.join(" / ") || "Source excerpt"}</strong>
                      {part.anchor.page ? ` · page ${part.anchor.page}` : ""}
                      <p>{part.source_text}</p>
                    </div>
                  ))}
                </details>
              )}
              {c.relations?.length > 0 && (
                <div className="citation-relations">
                  {c.relations.map((relation) => (
                    <span key={relation.relation_id}>
                      Relationship evidence: {relation.relation_type} ({relation.stance})
                      {Object.entries(relation.conditions).map(([key, value]) => ` · ${key}: ${value}`).join("")}
                    </span>
                  ))}
                  <small>These relations are source claims; their truth has not been established.</small>
                </div>
              )}
              {(c.context_truncated || c.context_omitted) && (
                <div className="citation-warning">
                  {c.context_omitted ? "Some context was omitted by this service." : "CueKB limited the returned context."}
                </div>
              )}
              {c.hits_omitted > 0 && (
                <div className="citation-warning">
                  {c.hits_omitted} matching source(s) were omitted by this service's evidence budget.
                </div>
              )}
              <div className="citation-meta">
                Content version {c.business_version || c.version_id}
                {c.anchor.page ? ` · page ${c.anchor.page}` : ""}
                {c.updated_at && (
                  <>
                    <br />
                    {new Date(c.updated_at).toLocaleString("en-US")}
                  </>
                )}
              </div>
              {(c.scope_limited || c.retrieval_status === "degraded") && (
                <div className="citation-warning">This source came from a limited or degraded search.</div>
              )}
              {c.source_uri && (
                <a
                  href={c.source_uri}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <LinkOutlined /> View authorized source
                </a>
              )}
            </article>
          ))}
          {selected.cards.map((card, i) => (
            <WeatherCard key={i} card={card} />
          ))}
          {!selected.citations.length && !selected.cards.length && (
            <p className="muted">
              No evidence is available to show. Please clarify your question or contact a representative.
            </p>
          )}
          <div className="evidence-note">
            <CheckCircleOutlined />{" "}
            The server validates source access; the conclusion still needs business review.
          </div>
        </>
      )}
    </div>
  );
  if (loading)
    return (
      <div className="login-screen">
        <Spin size="large" tip="Opening customer support" />
      </div>
    );
  return (
    <div className="app-shell">
      <aside className="sidebar">{nav}</aside>
      <main className="workspace">
        <header className="topbar">
          <div>
            <Button
              className="mobile-menu"
              type="text"
              icon={<MenuOutlined />}
              aria-label="Call information"
              onClick={() => setSidebar(true)}
            />
            <span className="breadcrumb">
              Customer support <span>/</span> Online help
            </span>
          </div>
          <div className="top-actions">
            <Tag color={caps?.is_mock ? "orange" : "green"}>
              {caps?.is_mock ? "Demo environment" : "Integration environment"}
            </Tag>
          </div>
        </header>
        <div className="workspace-heading">
          <div>
            <div className="eyebrow">CUSTOMER SUPPORT</div>
            <h1>Support that responds</h1>
            <p>Speak naturally, keep a written record, and review the sources behind each answer.</p>
          </div>
          <Button
            className="sources-toggle"
            icon={<FileTextOutlined />}
            onClick={() => setSourcesOpen(true)}
          >
            Answer sources
          </Button>
        </div>
        {caps?.is_mock && (
          <div className="mode-notice">
            <span>Integration mode</span>{" "}
            This environment includes demo services. Unconfigured real models and tools cannot produce real business answers.
            {caps.provider === "mock"
              ? "Voice only verifies capture and transport; it does not recognize or synthesize speech."
              : ""}
          </div>
        )}
        {error && (
          <Alert
            className="error-banner"
            type="error"
            showIcon
            closable
            onClose={() => setError("")}
            message={error}
          />
        )}
        <div className="work-columns">
          <section className="chat-panel">
            <div className="chat-header">
              <div>
                <span className="status-dot" />
                <strong>Support conversation</strong>
                <span className="locale">English</span>
              </div>
              <span className="small muted">{stateLabels[voiceState]}</span>
            </div>
            <div className="messages">
              {before && (
                <Button
                  type="link"
                  onClick={() =>
                    void refresh(cid, before).catch((e) => setError(e.message))
                  }
                >
                  View older messages
                </Button>
              )}
              {!turns.length && !Object.keys(transcripts).length ? (
                <div className="welcome">
                  <div className="welcome-icon">
                    <CustomerServiceOutlined />
                  </div>
                  <h2>Hello. How can I help today?</h2>
                  <p>
                    Speak or type your question. I will search company knowledge you are allowed to access.
                    <br />I will ask for clarification when the evidence is insufficient.
                  </p>
                  <div className="suggestions">
                    <button
                      onClick={() => setDraft("Find the product troubleshooting procedure")}
                    >
                      <FileTextOutlined />
                      <strong>Search knowledge</strong>
                      <span>Products, services, and procedures</span>
                    </button>
                    <button onClick={() => setDraft("What should I prepare before a product upgrade?")}>
                      <AudioOutlined />
                      <strong>Ask naturally</strong>
                      <span>Procedures, versions, and precautions</span>
                    </button>
                  </div>
                  {caps?.is_mock && (
                    <button
                      className="fixture-link"
                      onClick={() => setDraft("Find the integration sample")}
                    >
                      View synthetic integration sample →
                    </button>
                  )}
                </div>
              ) : null}
              {Object.entries(transcripts).map(([key, t]) => (
                <article
                  className={t.kind === "Spoken reply" ? "assistant-message live-transcript" : "user-message live-transcript"}
                  key={key}
                >
                  <span className="message-label">
                    {t.kind === "Spoken reply" ? "Assistant · Voice output" : "You · Voice input"}
                    {!t.done ? " · Live" : ""}
                  </span>
                  <p>{t.text || "…"}</p>
                </article>
              ))}
              {turns.filter((t) => t.channel !== "voice").map((t) => (
                <article className="turn" key={t.id}>
                  <div className="user-message">
                    <span className="message-label">
                      You · {t.channel === "voice" ? "Voice request" : "Text"}
                    </span>
                    <p>{t.user_text}</p>
                  </div>
                  <div className="assistant-message">
                    <div className="assistant-label">
                      <span className="mini-brand">
                        <CustomerServiceOutlined />
                      </span>
                      <strong>Answer</strong>
                      <Tag
                        color={
                          t.status === "answered"
                            ? "green"
                            : t.status === "running"
                              ? "processing"
                              : "default"
                        }
                      >
                        {statuses[t.status]}
                      </Tag>
                    </div>
                    {t.answer ? (
                      <>
                        <p>{t.answer.display_text}</p>
                        {(t.answer.citations.length > 0 ||
                          t.answer.cards.length > 0) && (
                          <button
                            className="source-button"
                            onClick={() => {
                              setSelected(t.answer);
                              setSourcesOpen(true);
                            }}
                          >
                            <FileTextOutlined /> View sources <span>↗</span>
                          </button>
                        )}
                      </>
                    ) : t.status === "running" ? (
                      <div className="processing">
                        <LoadingOutlined /> {progress || "Processing your question"}
                      </div>
                    ) : (
                      <p className="muted">This request has stopped. No further result will be submitted.</p>
                    )}
                    {t.status === "failed" && (
                      <Button
                        size="small"
                        onClick={() => setDraft(t.user_text)}
                      >
                        Ask again
                      </Button>
                    )}
                  </div>
                </article>
              ))}
              {selected && (selected.citations.length > 0 || selected.cards.length > 0) && (
                <button className="source-button voice-source-button" onClick={() => setSourcesOpen(true)}>
                  <FileTextOutlined /> View verified answer sources <span>↗</span>
                </button>
              )}
              <div ref={end} />
            </div>
            <div className="composer">
              <div className="voice-controls">
                <Button
                  aria-label="Start call"
                  type="primary"
                  icon={<AudioOutlined />}
                  disabled={
                    !caps?.voice_available || voiceState === "connecting" || voiceState === "ready" || voiceState === "reconnecting"
                  }
                  onClick={() => void startCall()}
                >
                  Start call
                </Button>
                <Button
                  aria-label={muted ? "Unmute microphone" : "Mute microphone"}
                  disabled={voiceState !== "ready"}
                  icon={muted ? <AudioMutedOutlined /> : <AudioOutlined />}
                  onClick={() => {
                    setMuted(!muted);
                    voice.current?.setMuted(!muted);
                  }}
                >
                  {muted ? "Unmute" : "Mute"}
                </Button>
                <Button
                  aria-label="Stop playback"
                  icon={<StopOutlined />}
                  disabled={voiceState !== "ready"}
                  onClick={() => voice.current?.stopPlayback()}
                >
                  Stop playback
                </Button>
                <Button
                  danger
                  disabled={!turns.some((turn) => turn.status === "running")}
                  onClick={() => void cancelCurrent()}
                >
                  Cancel search
                </Button>
                <Button
                  type="text"
                  disabled={!cid || callEnded}
                  onClick={() => void endCall()}
                >
                  End call
                </Button>
              </div>
              {voiceState === "ready" && (
                <div className="voice-live">
                  <span className="pulse-bars">
                    <i />
                    <i />
                    <i />
                    <i />
                  </span>
                  <span>
                    {muted
                      ? "Microphone muted"
                      : inputState === "speaking"
                        ? "Listening"
                        : "Voice is ready. You can ask a question."}
                    {progress ? " · " + progress : ""}
                  </span>
                  <label>
                    Volume
                    <Slider
                      value={volume}
                      min={0}
                      max={1}
                      step={0.05}
                      onChange={(v) => {
                        setVolume(v);
                        voice.current?.setVolume(v);
                      }}
                    />
                  </label>
                </div>
              )}
              <div className="text-composer">
                <Input.TextArea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  maxLength={2000}
                  autoSize={{ minRows: 2, maxRows: 5 }}
                  placeholder="Type your question; Shift + Enter for a new line"
                  aria-label="Your question"
                  onKeyDown={(e) => {
                    if (
                      e.key === "Enter" &&
                      !e.shiftKey &&
                      !e.nativeEvent.isComposing
                    ) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                />
                <Button
                  type="primary"
                  icon={<SendOutlined />}
                  aria-label="Send question"
                  loading={sending}
                  disabled={!draft.trim()}
                  onClick={() => void send()}
                />
              </div>
              <div className="composer-foot">
                <span>Sending a text question ends the current voice connection.</span>
                <span>{draft.length} / 2000</span>
              </div>
            </div>
          </section>
          <aside className="evidence-panel">{sources}</aside>
        </div>
        <footer className="page-footer">
          <span>VoiceBridge · Customer support portal</span>
          <span>Read-only tools · raw recordings are not stored by default</span>
        </footer>
      </main>
      <Drawer
        title="Call information"
        placement="left"
        open={sidebar}
        onClose={() => setSidebar(false)}
      >
        {nav}
      </Drawer>
      <Drawer
        title="Answer sources"
        open={sourcesOpen}
        onClose={() => setSourcesOpen(false)}
        width={400}
      >
        {sources}
      </Drawer>
    </div>
  );
}
function WeatherCard({ card }: { card: Record<string, unknown> }) {
  if (card.type !== "weather") return null;
  const c = card as unknown as {
    place: { name: string; timezone: string };
    kind: string;
    temperature: { value: number; unit: string };
    condition: string;
    valid_at: string;
    fetched_at: string;
    source: { provider: string; is_mock: boolean };
    stale: boolean;
  };
  return (
    <article className="weather-card">
      <span>
        {c.kind === "current" ? "Current weather" : "Weather forecast"}
        {c.stale ? " · Stale data" : ""}
      </span>
      <h3>{c.place.name}</h3>
      <div className="temperature">
        {c.temperature.value}
        <small>°{c.temperature.unit}</small>
      </div>
      <p>{c.condition}</p>
      <small>
        Valid at{" "}
        {new Date(c.valid_at).toLocaleString("en-US", {
          timeZone: c.place.timezone,
        })}
        <br />
        Time zone {c.place.timezone}
        <br />
        Source {c.source.provider}
        {c.source.is_mock ? " (synthetic)" : ""}
      </small>
    </article>
  );
}
