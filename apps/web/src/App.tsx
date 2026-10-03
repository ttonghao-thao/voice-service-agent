import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Drawer, Input, Slider, Spin, Tag } from "antd";
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
  KnowledgeResult,
  PortalEvent,
  RecordItem,
  setCallAccessToken,
  streamEvents,
  Turn,
} from "./api";
import {
  MicrophoneDiagnostics,
  VoiceClient,
  VoiceState,
} from "./audio/VoiceClient";

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
  awaiting_voice: "Preparing voice reply",
  voice_completed: "Response completed",
};

function voiceDeliveryFailed(answer: Answer) {
  return (
    answer.answer_origin === "voicechat" &&
    answer.status === "failed" &&
    /^(VOICE_|AGENT_|FORBIDDEN$|KB_ACCESS_REVOKED$)/.test(
      answer.reason_code || "",
    )
  );
}

function visibleTurnStatus(turn: Turn) {
  return turn.answer?.status || turn.status;
}

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
    [inputState, setInputState] = useState("quiet"),
    [microphone, setMicrophone] = useState<MicrophoneDiagnostics | null>(null);
  const [selected, setSelected] = useState<Answer | KnowledgeResult | null>(
      null,
    ),
    [sidebar, setSidebar] = useState(false),
    [sourcesOpen, setSourcesOpen] = useState(false),
    [before, setBefore] = useState<string | null>(null);
  const [inputs, setInputs] = useState<
    Record<string, { text: string; done: boolean }>
  >({});
  const [spoken, setSpoken] = useState<
    Record<string, { turnId: string; text: string; done: boolean }>
  >({});
  const [timeline, setTimeline] = useState<string[]>([]);
  const [atBottom, setAtBottom] = useState(true);
  const [finishedTurns, setFinishedTurns] = useState<Set<string>>(new Set());
  const epoch = useRef(0),
    requestRevision = useRef(0),
    active = useRef(""),
    voice = useRef<VoiceClient | null>(null),
    stopEvents = useRef<(() => void) | null>(null),
    lastEvent = useRef(new Set<string>()),
    end = useRef<HTMLDivElement>(null),
    messages = useRef<HTMLDivElement>(null),
    followBottom = useRef(true);
  const knownTurns = useRef(new Set<string>());
  const revokedTurns = useRef(new Set<string>());
  const finalAnswers = useRef(
    new Map<string, { epoch: number; revision: number; answer: Answer }>(),
  );
  const knowledgeResults = useRef(
    new Map<
      string,
      { epoch: number; revision: number; knowledge: KnowledgeResult }
    >(),
  );
  const onError = useCallback((message: string) => setError(message), []);
  const refresh = useCallback(async (id: string, older?: string) => {
    const data = await api<{
      items: Turn[];
      records: RecordItem[];
      epoch: number;
      request_revision: number;
      next_before: string | null;
    }>(`/conversations/${id}/messages${older ? "?before=" + older : ""}`);
    if (active.current !== id) return;
    if (
      data.epoch < epoch.current ||
      data.request_revision < requestRevision.current
    )
      return;
    epoch.current = Math.max(epoch.current, data.epoch);
    requestRevision.current = Math.max(
      requestRevision.current,
      data.request_revision || 0,
    );
    setBefore(data.next_before);
    data.items = data.items.map((turn) => {
      knownTurns.current.add(turn.id);
      if (
        turn.knowledge_result?.reason_code === "KB_ACCESS_REVOKED" ||
        turn.answer?.reason_code === "KB_ACCESS_REVOKED"
      )
        revokedTurns.current.add(turn.id);
      if (revokedTurns.current.has(turn.id)) {
        knowledgeResults.current.delete(turn.id);
        finalAnswers.current.delete(turn.id);
        return {
          ...turn,
          status: "failed",
          knowledge_result: null,
          answer: turn.answer
            ? {
                ...turn.answer,
                display_text: "Knowledge access changed. Please ask again.",
                speech_text: "",
                citations: [],
                reason_code: "KB_ACCESS_REVOKED",
              }
            : null,
        };
      }
      const cached = knowledgeResults.current.get(turn.id);
      if (
        cached &&
        cached.epoch === turn.epoch &&
        cached.revision === turn.request_revision &&
        !turn.knowledge_result
      ) {
        turn = {
          ...turn,
          knowledge_result: cached.knowledge,
          status: ["running", "awaiting_voice"].includes(turn.status)
            ? "awaiting_voice"
            : turn.status,
        };
      }
      const final = finalAnswers.current.get(turn.id);
      return final &&
        final.epoch === turn.epoch &&
        final.revision === turn.request_revision &&
        ["running", "awaiting_voice"].includes(turn.status)
        ? {
            ...turn,
            status: final.answer.status,
            answer: final.answer,
            delivery_status: voiceDeliveryFailed(final.answer)
              ? "voice_failed"
              : "accepted",
          }
        : turn;
    });
    setTurns((previous) => {
      const monotonic = data.items.map((turn) => {
        const prior = previous.find((p) => p.id === turn.id);
        return prior &&
          prior.epoch === turn.epoch &&
          prior.request_revision === turn.request_revision &&
          !["running", "awaiting_voice"].includes(prior.status) &&
          ["running", "awaiting_voice"].includes(turn.status) &&
          !revokedTurns.current.has(turn.id)
          ? {
              ...prior,
              knowledge_result: prior.knowledge_result || turn.knowledge_result,
            }
          : turn;
      });
      return older
        ? [
            ...monotonic,
            ...previous.filter((t) => !data.items.some((x) => x.id === t.id)),
          ]
        : monotonic;
    });
    const records = data.records || [];
    setInputs((previous) => {
      const next = { ...previous };
      for (const record of records) {
        if (record.kind !== "user_transcript" || !record.payload.text) continue;
        const key = `${record.epoch}:${record.source_id}`;
        next[key] = { text: record.payload.text, done: true };
      }
      return next;
    });
    setSpoken((previous) => {
      const next = { ...previous };
      const revoked = new Set(
        data.items
          .filter((turn) => revokedTurns.current.has(turn.id))
          .map((turn) => turn.id),
      );
      for (const id of revoked) revokedTurns.current.add(id);
      for (const [key, part] of Object.entries(next)) {
        if (revoked.has(part.turnId)) delete next[key];
      }
      for (const record of records) {
        if (
          record.kind !== "voicechat_transcript" ||
          !record.payload.turn_id ||
          !record.payload.text ||
          revokedTurns.current.has(record.payload.turn_id)
        )
          continue;
        const key = `${record.epoch}:${record.source_id}`;
        next[key] = {
          turnId: record.payload.turn_id,
          text: record.payload.text,
          done: true,
        };
      }
      return next;
    });
    const entries = [
      ...data.items.map((turn) => ({
        key:
          turn.channel === "voice" && turn.input_item_id
            ? `input:${turn.epoch}:${turn.input_item_id}`
            : `turn:${turn.id}`,
        time: Date.parse(turn.created_at || "") || 0,
      })),
      ...records
        .filter((record) => record.kind === "user_transcript")
        .map((record) => ({
          key: `input:${record.epoch}:${record.source_id}`,
          time: Date.parse(record.created_at || "") || 0,
        })),
    ].sort((a, b) => a.time - b.time);
    setTimeline((previous) => {
      const added = entries
        .map((entry) => entry.key)
        .filter(
          (key, index, all) =>
            all.indexOf(key) === index && !previous.includes(key),
        );
      return older ? [...added, ...previous] : [...previous, ...added];
    });
    const answer = [...data.items].reverse().find((t) => t.answer)?.answer;
    if (!older)
      setSelected(
        answer ||
          [...data.items].reverse().find((t) => t.knowledge_result)
            ?.knowledge_result ||
          null,
      );
  }, []);
  const handleEvent = useCallback(
    (event: PortalEvent) => {
      if (
        event.conversation_id !== active.current ||
        event.epoch < epoch.current ||
        event.request_revision < requestRevision.current
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
      if (event.type === "portal.knowledge.ready" && event.turn_id) {
        if (event.request_revision < requestRevision.current) return;
        const knowledge = event.payload as unknown as KnowledgeResult;
        if (knowledge.reason_code === "KB_ACCESS_REVOKED") {
          revokedTurns.current.add(event.turn_id);
          finalAnswers.current.delete(event.turn_id);
          setSpoken((old) =>
            Object.fromEntries(
              Object.entries(old).filter(([, p]) => p.turnId !== event.turn_id),
            ),
          );
          setSelected(null);
          setTurns((old) =>
            old.map((t) =>
              t.id === event.turn_id
                ? {
                    ...t,
                    knowledge_result: null,
                    answer: null,
                    status: "failed",
                  }
                : t,
            ),
          );
          return;
        }
        if (revokedTurns.current.has(event.turn_id)) return;
        knowledgeResults.current.set(event.turn_id, {
          epoch: event.epoch,
          revision: event.request_revision,
          knowledge,
        });
        if (knowledgeResults.current.size > 100)
          knowledgeResults.current.delete(
            knowledgeResults.current.keys().next().value!,
          );
        setTurns((old) =>
          old.map((t) =>
            t.id === event.turn_id &&
            t.epoch === event.epoch &&
            t.request_revision === event.request_revision
              ? {
                  ...t,
                  knowledge_result:
                    t.knowledge_result &&
                    t.knowledge_result.result_id !== knowledge.result_id
                      ? t.knowledge_result
                      : knowledge,
                  status: t.status === "running" ? "awaiting_voice" : t.status,
                }
              : t,
          ),
        );
        if (!finalAnswers.current.has(event.turn_id)) {
          setSelected(knowledge);
          setProgress("Preparing voice reply");
        }
        if (!knownTurns.current.has(event.turn_id))
          void refresh(event.conversation_id).catch((e) => setError(e.message));
      }
      if (event.type === "portal.answer.final") {
        if (event.request_revision < requestRevision.current) return;
        setProgress("");
        const answer = event.payload as unknown as Answer;
        if (
          event.turn_id &&
          revokedTurns.current.has(event.turn_id) &&
          answer.reason_code !== "KB_ACCESS_REVOKED"
        )
          return;
        if (answer.reason_code === "KB_ACCESS_REVOKED" && event.turn_id)
          revokedTurns.current.add(event.turn_id);
        if (answer.reason_code === "KB_ACCESS_REVOKED" && event.turn_id)
          setSpoken((old) =>
            Object.fromEntries(
              Object.entries(old).filter(
                ([, part]) => part.turnId !== event.turn_id,
              ),
            ),
          );
        setSelected(answer);
        if (event.turn_id) {
          finalAnswers.current.set(event.turn_id, {
            epoch: event.epoch,
            revision: event.request_revision,
            answer,
          });
          if (finalAnswers.current.size > 100)
            finalAnswers.current.delete(
              finalAnswers.current.keys().next().value!,
            );
          setTurns((previous) =>
            previous.map((turn) =>
              turn.id === event.turn_id &&
              turn.epoch === event.epoch &&
              turn.request_revision === event.request_revision &&
              ["running", "awaiting_voice"].includes(turn.status)
                ? {
                    ...turn,
                    answer,
                    status: answer.status,
                    delivery_status: voiceDeliveryFailed(answer)
                      ? "voice_failed"
                      : "accepted",
                  }
                : turn,
            ),
          );
        }
        // The event contains a validated, principal-filtered answer. Fetch only
        // when its turn is not loaded (e.g. voice or reconnect), never to gate
        // rendering a final answer for an existing bubble.
        if (!event.turn_id || !knownTurns.current.has(event.turn_id))
          void refresh(event.conversation_id).catch((e) => setError(e.message));
      }
      if (event.type === "portal.playback.clear") {
        voice.current?.clearForEpoch(event.epoch);
        setProgress("");
        void refresh(event.conversation_id).catch((e) => setError(e.message));
      }
      if (event.type === "portal.input.state") {
        setInputState(String(event.payload.state));
      }
      if (
        event.type === "portal.audio.done" &&
        event.turn_id &&
        event.payload.phase !== "status"
      ) {
        setFinishedTurns((old) => new Set(old).add(event.turn_id!));
      }
      if (event.type.startsWith("portal.transcript.")) {
        const item = String(event.payload.item_id || "");
        if (!item || !String(event.payload.text || "").trim()) return;
        const key = `${event.epoch}:${item}`;
        const done = event.type.endsWith(".done");
        setInputs((old) => ({
          ...old,
          [key]: {
            text: done
              ? String(event.payload.text || "")
              : old[key]?.done
                ? old[key].text
                : (old[key]?.text || "") + String(event.payload.text || ""),
            done: done || !!old[key]?.done,
          },
        }));
        setTimeline((old) =>
          old.includes(`input:${key}`) ? old : [...old, `input:${key}`],
        );
      }
      if (event.type.startsWith("portal.speech_text.")) {
        if (event.request_revision < requestRevision.current) return;
        if (event.payload.phase === "status") {
          setProgress("Checking knowledge");
          return;
        }
        if (
          !event.turn_id ||
          !event.payload.response_id ||
          revokedTurns.current.has(event.turn_id)
        )
          return;
        const key = `${event.epoch}:${event.payload.response_id}:${event.payload.segment_index || 0}`;
        const done = event.type.endsWith(".done");
        setSpoken((old) => ({
          ...old,
          [key]: {
            turnId: event.turn_id!,
            text: done
              ? String(event.payload.text || "")
              : old[key]?.done
                ? old[key].text
                : (old[key]?.text || "") + String(event.payload.text || ""),
            done: done || !!old[key]?.done,
          },
        }));
        if (!knownTurns.current.has(event.turn_id))
          void refresh(event.conversation_id).catch((e) => setError(e.message));
      }
    },
    [refresh],
  );
  useEffect(() => {
    voice.current = new VoiceClient(
      handleEvent,
      setVoiceState,
      onError,
      setMicrophone,
    );
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
    if (followBottom.current) end.current?.scrollIntoView({ block: "end" });
  }, [turns, progress, inputs, spoken, timeline]);
  async function closeCurrentCall() {
    stopEvents.current?.();
    stopEvents.current = null;
    await voice.current?.stop();
    const id = active.current;
    active.current = "";
    if (id)
      await api(`/conversations/${id}`, { method: "DELETE" }).catch(() => {});
    setCallAccessToken("");
  }
  async function newConversation() {
    try {
      await closeCurrentCall();
      const c = await api<Conversation>("/conversations", {
        method: "POST",
        body: JSON.stringify({ title: "New conversation", locale: "en-US" }),
      });
      if (!c.access_token)
        throw new Error("The server did not issue call access");
      setCallAccessToken(c.access_token);
      active.current = c.id;
      epoch.current = 0;
      requestRevision.current = 0;
      setTurns([]);
      setSelected(null);
      setInputs({});
      setSpoken({});
      setTimeline([]);
      setFinishedTurns(new Set());
      followBottom.current = true;
      setAtBottom(true);
      setProgress("");
      setCallEnded(false);
      lastEvent.current.clear();
      knownTurns.current.clear();
      revokedTurns.current.clear();
      finalAnswers.current.clear();
      knowledgeResults.current.clear();
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
    if (!caps?.text_available || !text || sending) return;
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
      }>(`/conversations/${id}/messages`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ text }),
      });
      if (active.current === id) {
        epoch.current = Math.max(epoch.current, result.epoch);
        requestRevision.current = Math.max(
          requestRevision.current,
          result.request_revision,
        );
        setDraft("");
        setProgress(
          finalAnswers.current.has(result.turn_id) ? "" : "Processing",
        );
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
  const turnsByKey = new Map(
    turns.map((turn) => [
      turn.channel === "voice" && turn.input_item_id
        ? `input:${turn.epoch}:${turn.input_item_id}`
        : `turn:${turn.id}`,
      turn,
    ]),
  );
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
        <p>
          Each tab creates its own conversation when you start a call. No
          history is shared with other tabs.
        </p>
      </div>
      <div className="sidebar-foot">
        <span className="avatar">
          <AudioOutlined />
        </span>
        <div>
          <strong>
            {cid && !callEnded ? "Call in this tab" : "No active call"}
          </strong>
          <small>{stateLabels[voiceState]}</small>
        </div>
      </div>
    </>
  );
  const sources = (
    <div className="evidence">
      <div className="evidence-heading">
        <FileTextOutlined />
        <h3>
          {selected &&
          ("kind" in selected || selected.evidence_role === "retrieved_context")
            ? "Sources consulted"
            : "Answer sources"}
        </h3>
        {selected && <span>{selected.citations.length} sources</span>}
      </div>
      {!selected ? (
        <div className="evidence-placeholder">
          <FileTextOutlined />
          <p>Answers backed by sources</p>
          <small>
            After a search, view knowledge sources, versions, and locations
            here.
          </small>
        </div>
      ) : (
        <>
          {("kind" in selected ||
            selected.evidence_role === "retrieved_context") && (
            <p className="muted">
              Retrieved context supplied for the spoken reply; sources are not
              individually verified citations.
            </p>
          )}
          <Tag
            color={
              "status" in selected && selected.status === "answered"
                ? "green"
                : "orange"
            }
          >
            {"status" in selected
              ? statuses[selected.status] || selected.status
              : "Preparing voice reply"}
          </Tag>
          {selected.is_mock && (
            <Tag color="orange">Synthetic integration data</Tag>
          )}
          {selected.citations.map((c) => (
            <article className="citation" key={c.citation_id}>
              <div className="citation-title">
                <span>{c.citation_id}</span>
                <strong>{c.title}</strong>
              </div>
              <p>{c.content}</p>
              {c.context_parts?.length > 0 && (
                <details className="citation-context">
                  <summary>
                    Context and source locations ({c.context_parts.length})
                  </summary>
                  {c.context_parts.map((part) => (
                    <div key={part.chunk_id}>
                      <strong>
                        {part.title_path.join(" / ") || "Source excerpt"}
                      </strong>
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
                      Relationship evidence: {relation.relation_type} (
                      {relation.stance})
                      {Object.entries(relation.conditions)
                        .map(([key, value]) => ` · ${key}: ${value}`)
                        .join("")}
                    </span>
                  ))}
                  <small>
                    These relations are source claims; their truth has not been
                    established.
                  </small>
                </div>
              )}
              {(c.context_truncated || c.context_omitted) && (
                <div className="citation-warning">
                  {c.context_omitted
                    ? "Some context was omitted by this service."
                    : "CueKB limited the returned context."}
                </div>
              )}
              {c.hits_omitted > 0 && (
                <div className="citation-warning">
                  {c.hits_omitted} matching source(s) were omitted by this
                  service's evidence budget.
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
                <div className="citation-warning">
                  This source came from a limited or degraded search.
                </div>
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
          {("cards" in selected ? selected.cards : []).map((card, i) => (
            <WeatherCard key={i} card={card} />
          ))}
          {!selected.citations.length &&
            !("cards" in selected && selected.cards.length) && (
              <p className="muted">
                No evidence is available to show. Please clarify your question
                or contact a representative.
              </p>
            )}
          <div className="evidence-note">
            <CheckCircleOutlined /> The server validates source access; the
            conclusion still needs business review.
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
            <p>
              Speak naturally, keep a written record, and review the sources
              behind each answer.
            </p>
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
            <span>Integration mode</span> This environment includes demo
            services. Unconfigured real models and tools cannot produce real
            business answers.
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
            <div
              className="messages"
              ref={messages}
              onScroll={() => {
                const node = messages.current;
                if (!node) return;
                const bottom =
                  node.scrollHeight - node.scrollTop - node.clientHeight < 60;
                followBottom.current = bottom;
                setAtBottom(bottom);
              }}
            >
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
              {!timeline.length ? (
                <div className="welcome">
                  <div className="welcome-icon">
                    <CustomerServiceOutlined />
                  </div>
                  <h2>Hello. How can I help today?</h2>
                  <p>
                    Speak or type your question. I will search company knowledge
                    you are allowed to access.
                    <br />I will ask for clarification when the evidence is
                    insufficient.
                  </p>
                  <div className="suggestions">
                    <button
                      onClick={() =>
                        setDraft("Find the product troubleshooting procedure")
                      }
                    >
                      <FileTextOutlined />
                      <strong>Search knowledge</strong>
                      <span>Products, services, and procedures</span>
                    </button>
                    <button
                      onClick={() =>
                        setDraft(
                          "What should I prepare before a product upgrade?",
                        )
                      }
                    >
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
              {timeline.map((key) => {
                const t = turnsByKey.get(key);
                const input = key.startsWith("input:")
                  ? inputs[key.slice(6)]
                  : null;
                if (!t && !input) return null;
                const speechParts = t
                  ? Object.values(spoken).filter((part) => part.turnId === t.id)
                  : [];
                const speech =
                  speechParts
                    .map((part) => part.text)
                    .filter(Boolean)
                    .join(" ") ||
                  (t?.execution_mode === "direct" &&
                  t.answer?.answer_origin === "voicechat" &&
                  t.delivery_status !== "voice_failed"
                    ? t.answer.display_text
                    : "");
                return (
                  <article className="turn" key={key}>
                    <div className="user-message">
                      <span className="message-label">
                        You · {t?.channel === "text" ? "Text" : "Voice request"}
                      </span>
                      <p aria-live={t ? undefined : "polite"}>
                        {t?.user_text || input?.text || "…"}
                      </p>
                      {!t && (
                        <small className="input-status">
                          {input?.done
                            ? voiceState === "ready"
                              ? "Waiting for request"
                              : "Request not accepted"
                            : voiceState === "ready"
                              ? "Listening"
                              : "Transcription interrupted"}
                        </small>
                      )}
                    </div>
                    {t && (
                      <div className="assistant-message">
                        <div className="assistant-label">
                          <span className="mini-brand">
                            <CustomerServiceOutlined />
                          </span>
                          <strong>Answer</strong>
                          <Tag
                            color={
                              visibleTurnStatus(t) === "answered"
                                ? "green"
                                : visibleTurnStatus(t) === "running"
                                  ? "processing"
                                  : "default"
                            }
                          >
                            {t.delivery_status === "voice_failed"
                              ? "Voice reply unavailable"
                              : statuses[visibleTurnStatus(t)] ||
                                visibleTurnStatus(t)}
                          </Tag>
                        </div>
                        {t.channel === "voice" ? (
                          <>
                            {t.delivery_status === "voice_failed" && (
                              <small className="input-status">
                                Voice reply unavailable · Spoken reply
                                interrupted
                              </small>
                            )}
                            {speech ? (
                              <>
                                <p aria-live="polite">{speech}</p>
                                {voiceState !== "ready" &&
                                  speechParts.some((part) => !part.done) && (
                                    <small className="input-status">
                                      Spoken reply interrupted
                                    </small>
                                  )}
                              </>
                            ) : t.execution_mode === "direct" && t.answer ? (
                              <p className="muted">{t.answer.display_text}</p>
                            ) : t.answer ? (
                              <p className="muted">
                                {voiceState === "ready" &&
                                !finishedTurns.has(t.id) &&
                                t.answer.status !== "failed"
                                  ? "Answer ready. Waiting for spoken reply."
                                  : "Voice reply unavailable. The full answer is below."}
                              </p>
                            ) : ["running", "awaiting_voice"].includes(
                                visibleTurnStatus(t),
                              ) ? (
                              <div className="processing">
                                <LoadingOutlined />{" "}
                                {visibleTurnStatus(t) === "awaiting_voice"
                                  ? "Preparing voice reply"
                                  : progress || "Processing your question"}
                              </div>
                            ) : (
                              <p className="muted">
                                This request has stopped. No further result will
                                be submitted.
                              </p>
                            )}
                            {t.execution_mode === "direct" &&
                            (t.knowledge_result || t.answer)?.citations
                              .length ? (
                              <button
                                className="source-button"
                                onClick={() => {
                                  setSelected(
                                    t.answer || t.knowledge_result || null,
                                  );
                                  setSourcesOpen(true);
                                }}
                              >
                                <FileTextOutlined /> Sources consulted{" "}
                                <span>↗</span>
                              </button>
                            ) : null}
                            {t.answer && t.execution_mode !== "direct" && (
                              <details className="full-answer">
                                <summary>View full answer / Sources</summary>
                                <p>{t.answer.display_text}</p>
                                {t.answer.citations.length > 0 && (
                                  <button
                                    className="source-button"
                                    onClick={() => {
                                      setSelected(t.answer);
                                      setSourcesOpen(true);
                                    }}
                                  >
                                    <FileTextOutlined /> View sources{" "}
                                    <span>↗</span>
                                  </button>
                                )}
                              </details>
                            )}
                          </>
                        ) : t.answer ? (
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
                        ) : visibleTurnStatus(t) === "running" ? (
                          <div className="processing">
                            <LoadingOutlined />{" "}
                            {progress || "Processing your question"}
                          </div>
                        ) : (
                          <p className="muted">
                            This request has stopped. No further result will be
                            submitted.
                          </p>
                        )}
                        {visibleTurnStatus(t) === "failed" &&
                          caps?.text_available && (
                            <Button
                              size="small"
                              onClick={() => setDraft(t.user_text)}
                            >
                              Ask again
                            </Button>
                          )}
                      </div>
                    )}
                  </article>
                );
              })}
              <div ref={end} />
            </div>
            {!atBottom && (
              <button
                className="latest-button"
                onClick={() => {
                  followBottom.current = true;
                  setAtBottom(true);
                  end.current?.scrollIntoView({
                    behavior: "smooth",
                    block: "end",
                  });
                }}
              >
                Latest messages ↓
              </button>
            )}
            <div className="composer">
              <div className="voice-controls">
                <Button
                  aria-label="Start call"
                  type="primary"
                  icon={<AudioOutlined />}
                  disabled={
                    !caps?.voice_available ||
                    voiceState === "connecting" ||
                    voiceState === "ready" ||
                    voiceState === "reconnecting"
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
                  disabled={
                    !turns.some((turn) =>
                      ["running", "awaiting_voice"].includes(turn.status),
                    )
                  }
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
                <>
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
                  {microphone && (
                    <div
                      className={`microphone-status ${microphone.echoCancellation === false ? "warning" : ""}`}
                    >
                      Microphone: {microphone.label}
                      {microphone.sampleRate
                        ? ` · ${microphone.sampleRate} Hz capture`
                        : ""}
                      {" · 24000 Hz sent"}
                      {microphone.channelCount
                        ? ` · ${microphone.channelCount} channel`
                        : ""}
                      {microphone.echoCancellation === false
                        ? " · echo cancellation unavailable; use a headset"
                        : microphone.echoCancellation
                          ? " · echo cancellation on"
                          : " · echo cancellation not reported"}
                      {microphone.noiseSuppression === true
                        ? " · noise suppression on"
                        : ""}
                      {microphone.autoGainControl === true
                        ? " · auto gain on"
                        : ""}
                    </div>
                  )}
                </>
              )}
              <div className="text-composer">
                <Input.TextArea
                  disabled={!caps?.text_available}
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
                  disabled={!caps?.text_available || !draft.trim()}
                  onClick={() => void send()}
                />
              </div>
              <div className="composer-foot">
                <span>
                  {caps?.text_available
                    ? "Sending a text question ends the current voice connection."
                    : "Text input is unavailable in this deployment. Please use voice."}
                </span>
                <span>{draft.length} / 2000</span>
              </div>
            </div>
          </section>
          <aside className="evidence-panel">{sources}</aside>
        </div>
        <footer className="page-footer">
          <span>VoiceBridge · Customer support portal</span>
          <span>
            Read-only tools · raw recordings are not stored by default
          </span>
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
