import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  Modal,
  Slider,
  Spin,
  Switch,
  Tag,
  Tooltip,
} from "antd";
import {
  AudioOutlined,
  AudioMutedOutlined,
  PlusOutlined,
  SendOutlined,
  StopOutlined,
  CustomerServiceOutlined,
  FileTextOutlined,
  SettingOutlined,
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
    [adminOpen, setAdminOpen] = useState(false),
    [sidebar, setSidebar] = useState(false),
    [sourcesOpen, setSourcesOpen] = useState(false),
    [before, setBefore] = useState<string | null>(null);
  const [transcripts, setTranscripts] = useState<
    Record<string, { kind: string; text: string; done: boolean }>
  >({});
  const epoch = useRef(0),
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
      next_before: string | null;
    }>(`/conversations/${id}/messages${older ? "?before=" + older : ""}`);
    if (active.current !== id) return;
    epoch.current = Math.max(epoch.current, data.epoch);
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
      const result = await api<{ turn_id: string; epoch: number }>(
        `/conversations/${id}/messages`,
        {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({ text }),
        },
      );
      if (active.current === id) {
        epoch.current = Math.max(epoch.current, result.epoch);
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
  async function interrupt() {
    const id = cid;
    if (!id) return;
    const oldEpoch = epoch.current,
      wasVoice = voiceState === "ready" || voiceState === "connecting";
    voice.current?.clear();
    setProgress("");
    try {
      const result = await api<{ epoch: number }>(
        `/conversations/${id}/interrupt`,
        { method: "POST", body: JSON.stringify({ expected_epoch: oldEpoch }) },
      );
      epoch.current = result.epoch;
      await voice.current?.stop();
      await refresh(id);
      if (wasVoice) {
        setTranscripts({});
        await voice.current?.start(id);
      }
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
          <strong>{me?.user_id || "客服工作台"}</strong>
          <small>
            {me?.auth_mode === "dev" ? "开发身份" : "已通过组织认证"}
          </small>
        </div>
        {me?.auth_mode === "oidc" && (
          <Tooltip title="退出登录">
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
          </Tooltip>
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
          <small>查询完成后，在这里查看知识来源、版本和天气详情。</small>
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
              <div className="citation-meta">
                版本 {c.version}
                <br />
                {new Date(c.updated_at).toLocaleString("zh-CN")}
              </div>
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
        <Spin size="large" tip="正在进入工作台" />
      </div>
    );
  if (login)
    return (
      <div className="login-screen">
        <div className="login-card">
          <CustomerServiceOutlined />
          <h1>声桥客服工作台</h1>
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
              客服中心 <span>/</span> 工作台
            </span>
          </div>
          <div className="top-actions">
            <Tag color={caps?.is_mock ? "orange" : "green"}>
              {caps?.is_mock ? "演示环境" : "服务集成环境"}
            </Tag>
            {me?.scopes.includes("tools:admin") && (
              <Button
                icon={<SettingOutlined />}
                onClick={() => setAdminOpen(true)}
              >
                工具管理
              </Button>
            )}
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
                    您可以查询公司知识，也可以询问指定地点的天气。
                    <br />
                    我会在需要时请您补充信息。
                  </p>
                  <div className="suggestions">
                    <button
                      onClick={() => setDraft("请查询产品的故障排查流程")}
                    >
                      <FileTextOutlined />
                      <strong>查询知识</strong>
                      <span>产品、服务与处理流程</span>
                    </button>
                    <button onClick={() => setDraft("请查询北京今天的天气")}>
                      <AudioOutlined />
                      <strong>自然提问</strong>
                      <span>确认地点，获取天气信息</span>
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
                  icon={<StopOutlined />}
                  disabled={!cid}
                  onClick={() => void interrupt()}
                >
                  打断并重新提问
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
        title="我的工作台"
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
      <Admin open={adminOpen} close={() => setAdminOpen(false)} />
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
function Admin({ open, close }: { open: boolean; close: () => void }) {
  const [tools, setTools] = useState<
      {
        name: string;
        description: string;
        display_name: string;
        enabled: boolean;
        timeout_ms: number;
      }[]
    >([]),
    [errors, setErrors] = useState<{ name: string; code: string }[]>([]),
    [notice, setNotice] = useState("");
  const [services, setServices] = useState<{
    voice_health: string;
    text_model: string;
    active_voice_sessions: number;
    active_business_runs: number;
  } | null>(null);
  const load = () =>
    Promise.all([
      api<{ items: typeof tools; recent_errors: typeof errors }>(
        "/admin/tools",
      ),
      api<NonNullable<typeof services>>("/admin/services"),
    ])
      .then(([x, status]) => {
        setTools(x.items);
        setErrors(x.recent_errors);
        setServices(status);
      })
      .catch((e) => setNotice(e.message));
  useEffect(() => {
    if (open) void load();
  }, [open]);
  return (
    <Modal
      title="只读工具管理"
      open={open}
      onCancel={close}
      footer={null}
      width={620}
    >
      {notice && <Alert message={notice} type="info" showIcon />}
      {services && (
        <div className="service-health">
          <p>
            语音服务：
            {(
              {
                mock: "演示模式",
                healthy: "健康端点可达",
                unavailable: "暂不可用",
                unconfigured: "尚未配置",
              } as Record<string, string>
            )[services.voice_health] || services.voice_health}
          </p>
          <p>
            文本模型：
            {services.text_model === "mock"
              ? "演示模式"
              : services.text_model === "configured"
                ? "已配置，实际推理需联调"
                : "尚未配置"}
          </p>
          <small>
            本副本活跃语音 {services.active_voice_sessions} · 正在处理{" "}
            {services.active_business_runs}
          </small>
        </div>
      )}
      {tools.map((t) => (
        <div className="admin-tool" key={t.name}>
          <div>
            <strong>{t.display_name || t.name}</strong>
            <p>{t.description}</p>
            <small>超时预算 {t.timeout_ms / 1000} 秒</small>
          </div>
          <Switch
            aria-label={`启用${t.name}`}
            checked={t.enabled}
            onChange={(enabled) =>
              void api(`/admin/tools/${t.name}`, {
                method: "PATCH",
                body: JSON.stringify({ enabled }),
              })
                .then(load)
                .catch((e) => setNotice(e.message))
            }
          />
          <Button
            onClick={() =>
              void api<{ message: string }>(`/admin/tools/${t.name}/test`, {
                method: "POST",
                body: "{}",
              })
                .then((x) => setNotice(x.message))
                .catch((e) => setNotice(e.message))
            }
          >
            连接测试
          </Button>
        </div>
      ))}
      <h4>近期错误</h4>
      {errors.length ? (
        errors.map((e, i) => (
          <p key={i}>
            {e.name} · {e.code}
          </p>
        ))
      ) : (
        <Empty
          description="暂无错误记录"
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      )}
    </Modal>
  );
}
