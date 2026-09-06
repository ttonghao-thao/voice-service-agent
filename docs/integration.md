# 接口与工具接入

## 配置模式

默认 `development` + 显式 mock；`integration` 允许未通过验收的真实 VoiceChat 连接，用于 P0 联调，能力表仍区分未验证。`production` 禁止开发身份、mock、自动建表和外部 SDK tracing，要求 PostgreSQL/Redis、真实工具和文本模型。真实语音生产开放需要配置 API 版本并在完成验收后设置 `VOICECHAT_INTEGRATION_VERIFIED=true`。该开关是部署者基于验收报告的声明，不是程序自行测得的中文质量证明。

文本模型设置 `AGENT_PROVIDER=openai`、明确的 `AGENT_MODEL` 与 `OPENAI_API_KEY`；使用 Responses API。兼容供应商设置 `AGENT_PROVIDER=compatible` 与 `AGENT_BASE_URL`，使用 Chat Completions；必须另验工具、结构化输出、streaming 和错误语义。绝不把 VoiceChat WS 当成文本模型地址。

语言默认为 `zh-CN`，目前门户仅公开已要求的中文选项。原生 `session.update` 不额外添加语言字段，实际云端有明确版本化语言字段时集中修改 voice adapter。

## 身份与组织

- 本期单组织部署：`TENANT_ID` 固定服务组织，签名 JWT 的 `tenant_id` 必须一致。
- OIDC：配置 issuer、JWKS、audience/client ID。仅接受 RS256、校验 issuer/audience/exp/iat/sub；角色声明 `roles` 必须包含 `operator` 或 `admin`。`admin` 具有工具启停和 drain 权限。
- JWT 的 `knowledge_base_ids` 与部署配置 `KNOWLEDGE_BASE_IDS` 取交集；上游知识库再次校验租户、用户和知识库 ACL。
- 浏览器登录使用 Authorization Code + PKCE/S256 + nonce/state，经后端兑换；需要 authorization/token URL、client secret（若 IdP 要求）及至少 32 字符 `AUTH_COOKIE_SECRET`。回调地址为 `${PUBLIC_ORIGIN}/api/v1/auth/callback`。
- ID token 置于 HttpOnly、生产 Secure、SameSite=Strict Cookie；模拟账户不会暴露为匿名生产 admin。现有客服系统也可发送已签名 Bearer JWT。
- 身份/会话 API 不接受浏览器自己指定租户、scope 或知识库。不是完整的任意 IdP claim 自动映射：供应商字段差异集中修改 `api/auth.py`，不要绕过签名。

## 工具

注册在 `config/tools.yaml`。新增工具只需受信任适配器（参数模型、返回 TypeAdapter、invoke）、注册工厂、ToolSpec、测试；无需修改门户聊天核心、原生协议或 SDK 循环。API 连接测试使用注册项的固定 endpoint `/health`，返回“健康端点可达”不表示业务查询通过。

ToolSpec 的实际 input/output schema 从受信任 adapter 的 Pydantic 模型产生。输入和输出均校验；JSON 单次最多 32 KB，RAG 模型证据预算 6000 字符。只读 HTTP 在总预算内对网络/5xx 最多重试一次；429、4xx、错误 JSON、不符合 schema 和过大正文不重试。默认天气总预算 4 秒，RAG 5 秒，Agent 12 秒；最大 8 次 SDK turn。工具修订号在 run 开始固定；停用或配置修订后，旧 run 不得继续执行该工具，重新启用只对新 run 生效。

### RAG

详见 `contracts/rag-openapi.yaml`。默认代理路径 `POST /v1/retrieve`，是本项目契约而非厂商通用标准。header 携带服务凭据及已认证 tenant/user。返回 request_id 必须对应请求，空命中或同一文档多版本冲突不生成有依据答案。

来源元数据和引用 ID 由服务端创建；外链只允许 `RAG_SOURCE_HOSTS` 列表内的 HTTPS 主机，无授权地址则仅展示片段。上游来源内容仅作为资料传给模型，不作为工具 URL/指令执行。引用存在性检查不能证明每个结论都被片段充分支持，仍须业务样本评测。

### 天气

详见 `contracts/weather-openapi.yaml`。因为尚未提供实际供应商，本实现对接**配置的供应商代理契约**：`/v1/places/resolve` → `/v1/weather`。已有厂商路径不同就在 WeatherAdapter 做映射，不虚构特定厂商的成功对接。

唯一地名命中后根据 IANA timezone 解析“今天/明天”或 ISO 日期；多个或零个地点要求澄清。校验返回地点/单位/日期及实况/预报，拒绝 real 结果标为 mock。缓存 5 分钟、最多 512 个键，包含租户/地点ID/日期/单位/provider。返回原有效时间和 stale。卡片数字直接来自校验后的结构化结果。已确认地点、单位和最后查询日期单独保存为 slots，历史裁剪后仍可解析“那明天呢”；不缓存历史天气数字为新事实。

## 会话与流

完整 HTTP 契约见 `contracts/openapi.json`；开发文档地址 `/api/docs`。

- 文字 POST 必须有 `Idempotency-Key`，返回 202 + turn_id；同键不同正文返回 409。
- SSE 仅重放持久业务事件，`Last-Event-ID` 或 `after` 是单会话序号。门户按 event_id 去重。不重放音频，不显示未经验证的 SDK delta。
- WebSocket ticket 有效 60 秒、一次性、绑定已认证用户/会话/epoch/Origin，日志不得保存 query string。媒体连接不能跨网关迁移。
- `portal.audio.append` 固定 PCM16 LE、24 kHz mono、80 ms，即 1920 samples/3840 bytes。连续发送，包括静音；seq 严格递增，重复帧丢弃，缺帧/过快/停顿触发显式错误。
- 原生工具通过 `consult_service_agent` → 同一 BusinessRuntime → 单个原 call_id 结果。调用键包含 conversation/epoch/call_id；完成转写不再触发一次业务处理。
- 硬打断先清播放器，然后 invalidate epoch、取消业务和关闭旧连接；“打断并重新提问”会建立新连接并提示等待就绪。文字提交先结束语音。
- playback_ack 只记录浏览器估计播放采样数，校验不能超出已发送量；不推断用户确实听到，也不把 Agent 原文冒充实际语音字幕。
- 网页后台/离线/采集停顿终止旧连接。应用语音轮换初值 105 秒，是应用资源策略，不是宣称云端 API 会在此时断开。会话结束时需要再次开始语音并重说，不能宣称隐藏状态无缝恢复。

## 保留与诊断

默认不存原始录音。SQL 记录会话、轮次、事件、实际字幕、播放确认、工具证据、管理员变更；工具运行记录只保存结构化证据和错误 code，不保存密钥或上游异常正文。会话关闭不删除历史；`RETENTION_DAYS` 控制过期会话和审计清理，启动及每小时执行。外部 tracing 明确关闭。
