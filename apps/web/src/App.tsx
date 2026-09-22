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
  PlusOutlined,
  SendOutlined,
  StopOutlined,
  CustomerServiceOutlined,
  FileTextOutlined,
  LinkOutlined,
  MenuOutlined,
  CheckCircleOutlined,
  LoadingOutlined,
  MessageOutlined,
  LogoutOutlined,
} from "@ant-design/icons";
import {
  Answer,
  api,
  ApiError,
  Capabilities,
  Conversation,
  Me,
  PortalEvent,
  RecordItem,
  Turn,
} from "./api";
import { VoiceClient, VoiceState } from "./audio/VoiceClient";

const stateLabels: Record<VoiceState, string> = {
  closed: "语音未连接",
  connecting: "正在连接",
  ready: "语音已就绪",
  reconnecting: "正在重新连接",
  error: "语音不可用",
};
const statuses: Record<string, string> = {
  answered: "查询完成",
  needs_clarification: "需要补充信息",
  insufficient_evidence: "依据不足",
  failed: "查询失败",
  canceled: "已取消",
  superseded: "已改问",
  expired: "已过期",
  running: "正在查询",
};

export default function App() {
  const [me, setMe] = useState<Me | null>(null),
    [login, setLogin] = useState(false),
    [caps, setCaps] = useState<Capabilities | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]),
    [cid, setCid] = useState(""),
    [turns, setTurns] = useState<Turn[]>([]),
    [records, setRecords] = useState<RecordItem[]>([]);
  const [draft, setDraft] = useState(""),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [sending, setSending] = useState(false),
    [progress, setProgress] = useState("");
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
    lastEvent = useRef(new Set<string>()),
    end = useRef<HTMLDivElement>(null);
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
    setRecords(data.records);
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
        setProgress(String(event.payload.message || "正在查询"));
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
            kind: event.type.includes("speech_text") ? "语音字幕" : "用户转写",
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
  const loadList = useCallback(async () => {
    const data = await api<{ items: Conversation[] }>("/conversations");
    setConversations(data.items);
    return data.items;
  }, []);
  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const user = await api<Me>("/auth/me");
        if (!current) return;
        setMe(user);
        setCaps(await api<Capabilities>("/capabilities"));
        const list = await loadList();
        if (current && list[0]) setCid(list[0].id);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) setLogin(true);
        else setError((e as Error).message);
      } finally {
        if (current) setLoading(false);
      }
    })();
    return () => {
      current = false;
    };
  }, [loadList]);
  useEffect(() => {
    active.current = cid;
    epoch.current = 0;
    requestRevision.current = 0;
    setTurns([]);
    setRecords([]);
    setSelected(null);
    setTranscripts({});
    setProgress("");
    lastEvent.current.clear();
    if (!cid) return;
    void voice.current?.stop();
    void refresh(cid).catch((e) => setError(e.message));
    const events = new EventSource(`/api/v1/conversations/${cid}/events`);
    events.onmessage = ({ data }) => {
      try {
        handleEvent(JSON.parse(data));
      } catch {
        setError("收到无法识别的会话事件");
      }
    };
    return () => events.close();
  }, [cid, refresh, handleEvent]);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, progress, transcripts]);
  async function newConversation() {
    try {
      await voice.current?.stop();
      const c = await api<Conversation>("/conversations", {
        method: "POST",
        body: JSON.stringify({ title: "新会话", locale: "zh-CN" }),
      });
      setCid(c.id);
      await loadList();
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
      const id = cid || (await newConversation());
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
        setProgress("正在处理");
        await refresh(id);
      }
      await loadList();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  }
  async function startVoice() {
    setError("");
    const id = cid || (await newConversation());
    if (id) await voice.current?.start(id);
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
          声桥<small>SERVICE DESK</small>
        </div>
      </div>
      <Button
        type="primary"
        size="large"
        icon={<PlusOutlined />}
        block
        onClick={() => void newConversation()}
      >
        新建会话
      </Button>
      <div className="section-label">
        我的会话 <span>{conversations.length}</span>
      </div>
      <nav className="conversations">
        {conversations.map((c) => (
          <button
            className={"conversation " + (c.id === cid ? "active" : "")}
            key={c.id}
            onClick={() => {
              setCid(c.id);
              setSidebar(false);
            }}
          >
            <MessageOutlined />
            <span>{c.title}</span>
            {c.id === cid && <i />}
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <span className="avatar">
          {me?.user_id.slice(0, 1).toUpperCase() || "客"}
        </span>
        <div>
          <strong>{me?.user_id || "客户服务"}</strong>
          <small>
            {me?.auth_mode === "dev" ? "开发身份" : "已通过组织认证"}
          </small>
        </div>
        {me?.auth_mode === "oidc" && (
          <Button
            type="text"
            aria-label="退出登录"
            icon={<LogoutOutlined />}
            onClick={() =>
              void fetch("/api/v1/auth/logout", { method: "POST" }).then(() =>
                location.reload(),
              )
            }
          />
        )}
      </div>
    </>
  );
  const sources = (
    <div className="evidence">
      <div className="evidence-heading">
        <FileTextOutlined />
        <h3>答案依据</h3>
        {selected && <span>{selected.citations.length} 条来源</span>}
      </div>
      {!selected ? (
        <div className="evidence-placeholder">
          <FileTextOutlined />
          <p>每个答案，都有据可查</p>
          <small>查询完成后，在这里查看知识来源、版本和适用位置。</small>
        </div>
      ) : (
        <>
          <Tag color={selected.status === "answered" ? "green" : "orange"}>
            {statuses[selected.status] || selected.status}
          </Tag>
          {selected.is_mock && <Tag color="orange">合成联调资料</Tag>}
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
                内容版本 {c.business_version || c.version_id}
                {c.anchor.page ? ` · 第 ${c.anchor.page} 页` : ""}
                {c.updated_at && (
                  <>
                    <br />
                    {new Date(c.updated_at).toLocaleString("zh-CN")}
                  </>
                )}
              </div>
              {(c.scope_limited || c.retrieval_status === "degraded") && (
                <div className="citation-warning">本条来源来自受限或降级检索</div>
              )}
              {c.source_uri && (
                <a
                  href={c.source_uri}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <LinkOutlined /> 查看授权来源
                </a>
              )}
            </article>
          ))}
          {selected.cards.map((card, i) => (
            <WeatherCard key={i} card={card} />
          ))}
          {!selected.citations.length && !selected.cards.length && (
            <p className="muted">
              本次没有可展示的证据。请补充问题，或交由人工进一步确认。
            </p>
          )}
          <div className="evidence-note">
            <CheckCircleOutlined />{" "}
            来源由服务端校验；引用是否充分支持结论仍需业务核实。
          </div>
        </>
      )}
    </div>
  );
  if (loading)
    return (
      <div className="login-screen">
        <Spin size="large" tip="正在进入客户服务" />
      </div>
    );
  if (login)
    return (
      <div className="login-screen">
        <div className="login-card">
          <CustomerServiceOutlined />
          <h1>声桥客户服务</h1>
          <p>使用组织账号登录，安全访问您的会话与知识库。</p>
          <Button type="primary" size="large" href="/api/v1/auth/login">
            使用组织账号登录
          </Button>
        </div>
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
              aria-label="会话列表"
              onClick={() => setSidebar(true)}
            />
            <span className="breadcrumb">
              客户服务 <span>/</span> 在线咨询
            </span>
          </div>
          <div className="top-actions">
            <Tag color={caps?.is_mock ? "orange" : "green"}>
              {caps?.is_mock ? "演示环境" : "服务集成环境"}
            </Tag>
          </div>
        </header>
        <div className="workspace-heading">
          <div>
            <div className="eyebrow">CUSTOMER SUPPORT</div>
            <h1>让每一次对话，都有回应</h1>
            <p>语音沟通，文字留痕，业务答案有据可查。</p>
          </div>
          <Button
            className="sources-toggle"
            icon={<FileTextOutlined />}
            onClick={() => setSourcesOpen(true)}
          >
            答案依据
          </Button>
        </div>
        {caps?.is_mock && (
          <div className="mode-notice">
            <span>联调模式</span>{" "}
            当前包含演示服务，未配置的真实模型和工具不会生成真实业务结果。
            {caps.provider === "mock"
              ? "语音仅验证采集与传输，不进行识别或合成。"
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
                <strong>服务对话</strong>
                <span className="locale">简体中文</span>
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
                  查看更早的对话
                </Button>
              )}
              {!turns.length && !Object.keys(transcripts).length ? (
                <div className="welcome">
                  <div className="welcome-icon">
                    <CustomerServiceOutlined />
                  </div>
                  <h2>您好，今天有什么可以帮您？</h2>
                  <p>
                    您可以直接说出或输入问题，我会查询您有权访问的公司知识。
                    <br />我会在依据不足时请您补充信息。
                  </p>
                  <div className="suggestions">
                    <button
                      onClick={() => setDraft("请查询产品的故障排查流程")}
                    >
                      <FileTextOutlined />
                      <strong>查询知识</strong>
                      <span>产品、服务与处理流程</span>
                    </button>
                    <button onClick={() => setDraft("请说明产品升级前的准备事项")}>
                      <AudioOutlined />
                      <strong>自然提问</strong>
                      <span>流程、版本与注意事项</span>
                    </button>
                  </div>
                  {caps?.is_mock && (
                    <button
                      className="fixture-link"
                      onClick={() => setDraft("请查询联调示例")}
                    >
                      查看合成联调示例 →
                    </button>
                  )}
                </div>
              ) : null}
              {turns.map((t) => (
                <article className="turn" key={t.id}>
                  <div className="user-message">
                    <span className="message-label">
                      您 · {t.channel === "voice" ? "语音请求" : "文字"}
                    </span>
                    <p>{t.user_text}</p>
                  </div>
                  <div className="assistant-message">
                    <div className="assistant-label">
                      <span className="mini-brand">
                        <CustomerServiceOutlined />
                      </span>
                      <strong>查询答案</strong>
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
                            <FileTextOutlined /> 查看本次依据 <span>↗</span>
                          </button>
                        )}
                      </>
                    ) : t.status === "running" ? (
                      <div className="processing">
                        <LoadingOutlined /> {progress || "正在处理您的问题"}
                      </div>
                    ) : (
                      <p className="muted">本轮已停止，不会继续提交结果。</p>
                    )}
                    {t.status === "failed" && (
                      <Button
                        size="small"
                        onClick={() => setDraft(t.user_text)}
                      >
                        重新提问
                      </Button>
                    )}
                  </div>
                </article>
              ))}
              {Object.entries(transcripts).map(([key, t]) => (
                <div className="transcript" key={key}>
                  <span>
                    {t.kind}
                    {!t.done ? " · 转写中" : ""}
                  </span>
                  <p>{t.text}</p>
                </div>
              ))}
              {records.filter((r) => r.kind === "voicechat_transcript").length >
                0 && (
                <details className="voice-records">
                  <summary>已保存的实际语音字幕 · 与查询答案分别记录</summary>
                  {records
                    .filter((r) => r.kind === "voicechat_transcript")
                    .map((r) => (
                      <p key={r.epoch + r.source_id}>{r.payload.text}</p>
                    ))}
                  <small>
                    播放记录是浏览器估计进度，不表示用户已经听到完整答案。
                  </small>
                </details>
              )}
              <div ref={end} />
            </div>
            <div className="composer">
              <div className="voice-controls">
                <Button
                  aria-label={
                    voiceState === "ready" ? "重新开始语音" : "开始语音"
                  }
                  type={voiceState === "ready" ? "default" : "primary"}
                  icon={<AudioOutlined />}
                  disabled={
                    !caps?.voice_available || voiceState === "connecting"
                  }
                  onClick={() => void startVoice()}
                >
                  {voiceState === "ready" ? "重新开始语音" : "开始语音"}
                </Button>
                <Button
                  aria-label={muted ? "取消麦克风静音" : "麦克风静音"}
                  disabled={voiceState !== "ready"}
                  icon={muted ? <AudioMutedOutlined /> : <AudioOutlined />}
                  onClick={() => {
                    setMuted(!muted);
                    voice.current?.setMuted(!muted);
                  }}
                >
                  {muted ? "取消静音" : "静音"}
                </Button>
                <Button
                  aria-label="停止播报"
                  icon={<StopOutlined />}
                  disabled={voiceState !== "ready"}
                  onClick={() => voice.current?.stopPlayback()}
                >
                  停止播报
                </Button>
                <Button
                  danger
                  disabled={!turns.some((turn) => turn.status === "running")}
                  onClick={() => void cancelCurrent()}
                >
                  取消查询
                </Button>
                <Button
                  type="text"
                  disabled={voiceState === "closed"}
                  onClick={() => void voice.current?.stop()}
                >
                  结束语音
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
                      ? "麦克风已静音"
                      : inputState === "speaking"
                        ? "正在听您说话"
                        : "语音就绪，可以开始提问"}
                    {progress ? " · " + progress : ""}
                  </span>
                  <label>
                    音量
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
                  placeholder="输入您的问题，Shift + Enter 换行"
                  aria-label="输入您的问题"
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
                  aria-label="发送问题"
                  loading={sending}
                  disabled={!draft.trim()}
                  onClick={() => void send()}
                />
              </div>
              <div className="composer-foot">
                <span>文字提问将结束当前语音连接</span>
                <span>{draft.length} / 2000</span>
              </div>
            </div>
          </section>
          <aside className="evidence-panel">{sources}</aside>
        </div>
        <footer className="page-footer">
          <span>声桥 · 客服智能体门户</span>
          <span>工具仅供查询 · 原始录音默认不保存</span>
        </footer>
      </main>
      <Drawer
        title="我的会话"
        placement="left"
        open={sidebar}
        onClose={() => setSidebar(false)}
      >
        {nav}
      </Drawer>
      <Drawer
        title="答案依据"
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
        {c.kind === "current" ? "天气实况" : "天气预报"}
        {c.stale ? " · 过期数据" : ""}
      </span>
      <h3>{c.place.name}</h3>
      <div className="temperature">
        {c.temperature.value}
        <small>°{c.temperature.unit}</small>
      </div>
      <p>{c.condition}</p>
      <small>
        有效时间{" "}
        {new Date(c.valid_at).toLocaleString("zh-CN", {
          timeZone: c.place.timezone,
        })}
        <br />
        时区 {c.place.timezone}
        <br />
        来源 {c.source.provider}
        {c.source.is_mock ? "（合成）" : ""}
      </small>
    </article>
  );
}
