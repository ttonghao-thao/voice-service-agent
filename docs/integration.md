# 后端服务与工具接入

更新：2026-09-16。按主题读取。架构决策见 [architecture.md](architecture.md)，当前差距见 [任务板](TASK_BOARD.md)。

## 1. 运行依赖与配置状态

| 依赖 | 最终职责 | 当前实现状态 |
| --- | --- | --- |
| VoiceChat | 独立实时语音服务，原生工具调用 | 有 NVIDIA WebSocket adapter；真实部署未验收 |
| CueKB | 知识检索与版本来源 | 专用 adapter、契约和受控测试已实现；真实服务/ACL 待 D07 |
| 文本模型 | BusinessRuntime 的推理与业务回答 | 已有 openai / compatible adapter；真实模型未验收 |
| 第三方工具 | 按实际接入选择启用 | 现有天气代理示例；生产强制天气配置尚待解除 |
| 身份服务 | 客户身份、组织及知识范围 | OIDC/JWT 已支持 customer/operator/admin；真实 IdP 与 CueKB ACL 待验收 |

当前环境变量名和启动校验以 `apps/api/app/config.py`、`.env.example`、`.env.production.example` 为准。实现读取 `CUEKB_MODE`、`CUEKB_BASE_URL`、`CUEKB_API_KEY`、`CUEKB_SEARCH_MODE`、`CUEKB_TOP_K` 与 `CUEKB_API_REVISION`；旧 `RAG_*` 配置和 `/v1/retrieve` 契约已退出活动实现。

development 使用显式 mock；integration 用于真实联调；production 禁止开发身份、mock、自动建表和外部 SDK tracing。真实语音生产开放需要固定 `VOICECHAT_API_VERSION`、`VOICECHAT_IMAGE_DIGEST`、经人工复核的 `VOICECHAT_CAPABILITY_MODE=basic|enhanced` 和 `VOICECHAT_INTEGRATION_VERIFIED=true`。配置校验会拒绝缺少版本证据的“已验证”声明，但布尔值本身仍是部署者声明，必须附探测报告和授权音频证据。

## 2. CueKB 目标契约（D03）

核对本地 CueKB revision：`22c74a335bf88ea50f0df3b171d0b2125c14c77a`；来源为其 `src/cuekb/schemas.py`、API routes/dependencies 与检索实现。部署前核对目标服务 OpenAPI，不把该 revision 当作所有部署版本。

```http
POST {server-configured-cuekb-base-url}/v1/search
Authorization: Bearer <server-side-scoped-key>
Content-Type: application/json
```

```json
{
  "query": "用户问题与已确认条件",
  "kb_ids": ["00000000-0000-4000-8000-000000000001"],
  "mode": "auto",
  "top_k": 5,
  "filters": {},
  "include_context": true
}
```

示例 UUID 仅说明类型。实际 KB UUID 来自服务端授权映射；旧 `kb_support` 不能直接传入。当前 CueKB query 上限 2000 字符，top_k 支持 1–20；默认选 5 是本应用建议。filters 仅映射其支持的 document_ids、product_model、software_version；条件必须来自明确输入或已确认上下文。

不向上游发送自造 request_id/locale/deadline 并假定生效。HTTP 超时与应用 deadline 自行执行，取消本地等待不证明 CueKB 后台已停止。

### 2.1 结果映射

| CueKB | 内部证据与回答处理 |
| --- | --- |
| trace_id | 关联本项目 task/run，不能要求回显不存在的 request_id |
| retrieval_status | 保留 ok/degraded/not_found，与最终 answer status 分开 |
| evidence_status | 保留 unassessed 等原值，不能转换成“证据充分” |
| degraded_reasons、scope_limited | 审计并参与回答决策，必要时告知限制 |
| content_revisions | 保留知识内容版本线索，不宣称跨系统事务一致性 |
| hits.document_id / chunk_id / version_id | 引用真实身份与版本；本项目另生成 citation_id |
| source_text、context | 保留证据原文与上下文，内容相同不重复占预算 |
| title_path、anchor、metadata | 来源定位及适用条件；缺失标题用“来源片段”，不伪造 |
| rank、retrieval_sources | 检索排序/来源，不当作事实置信度 |
| timings_ms、retrieval_path、executed_stages、skipped_stages | 内部诊断，不要求普通客户理解 |

Citation 的 updated_at 已改为可选，未填当前时间冒充文档更新时间；不再生成 score。`version_id` 与 metadata 中受控的 `business_version` 分开，旧历史 JSON 在读取时仍按原数据兼容，新增任务字段由 Alembic 0004 迁移。

当前 CueKB 的 context 可能等于 source_text，include_context=true 不保证已有相邻段、表头和完整步骤。回答模块仍需检查证据充分性。原件查看需经本项目重新鉴权并固定检索版本；这是待实现入口，不向浏览器暴露服务 Key 或私有下载 URL。

### 2.2 身份和错误

有效范围取客户授权、部署允许、CueKB Key 可读范围的交集。CueKB 当前鉴权主体是 API Key；自定义 X-Tenant-ID/X-User-ID 不代表它已执行客户级 ACL。不同隔离范围由服务端映射受限 Key/KB，不由工具参数指定。

not_found 表示本次未命中，不能推导事实不存在；degraded 有 hits 时保留原因并判断可用性；401/403 为授权或配置问题，422 为契约问题，429 为负载限制，5xx/超时为服务故障。禁止统一降为“查无资料”。返回答案、读历史证据和原件时都需覆盖撤权策略。

当前传输 JSON 32 KiB 与模型证据 6000 字符是不同预算，需对中文及 context/anchor 实测。原 5 秒知识工具、12 秒业务预算作为初始值，不能证明真实端到端时延已达标。

## 3. VoiceChat 接入与能力门槛

保留 `session.created → session.update → session.updated`；在首次配置注册 `consult_service_agent(user_request)`，只允许既定函数和 schema。

原生事件 `response.function_call_arguments.done` 进入网关后调用后台业务任务；结果经 `conversation.item.create` / `function_call_output` 以同一有效 call_id 回传。当前网关只支持一个 pending 原生调用；转写完成不能再次触发相同任务。

适配器明确校验在线 API 的 24 kHz PCM16 输入/输出，门户 80 ms 上行。模型卡内部音频采样率不能直接替换在线接口格式；变更须以服务契约及握手为准。

目标部署必须记录容器 digest、服务/API revision、语言、事件样例和验收时间。`scripts/probe_voicechat.py` 固定目标版本，支持工具结果延迟 5 秒、等待期间发送不同的第二段录音、记录无正文的事件时间线，并可选择保存授权输出 WAV 供人工复核。等待提示语、持续收音、工具等待时自由回答、停止播报、取消推理仍是分别验证的能力；脚本不自动提升模式。已核对模型卡与限制页，但尚无目标容器的真实验证。

- 基础：同 call 工具往返、中文音频、硬打断/关闭重连及旧连接隔离。
- 增强：延迟工具 5 秒，期间新问题在旧结果返回前得到实际回答；改问后旧答案不交付，原 call 安全结清并可继续新调用。ACK 不计为新问题回答。
- 不发送未证实的 response.cancel、动态 instructions、任意文本 TTS 或后台结果推送事件。失败不意味着服务支持的全部功能都不存在，只表示本部署未建立契约和证据。

来源：[在线 API](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/api-reference.md)、[部署](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/deploy.md)、[模型卡](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-VoiceChat-11B/blob/main/README.md)。当前 adapter 最初依据 NVIDIA revision `097dfe9e2f55baf653b83035868bdc89849f1b47`（API blob `06252330444f0a81679fdeb1f25c8ee067ac8c90`）；2026-09-16 模型卡页面显示 README 修订 `bd32b9997858b0acd9af64f26e4306cf91ad1c82`。这些均不是云端镜像版本。

## 4. 文本模型与业务 Agent

当前 `AGENT_PROVIDER=openai` 使用 Responses API，明确配置模型/API key；`compatible` 配置独立 HTTP base URL 并使用 Chat Completions。这些是本仓库适配器行为，真实供应商必须另验工具调用、结构化输出、streaming 和错误语义。VoiceChat WSS 地址不能代替文本模型 endpoint。

Runtime 复用授权工具和证据校验，输出 display_text、短 speech_text、引用和业务状态；SDK 的工具循环与耗时受整轮 deadline 约束。复杂知识子 agent 若启用，权限/预算继承并缩小，独立临时上下文，只返回候选结果。

## 5. 第三方扩展（D06）

沿用 ToolSpec + trusted adapter + ToolRegistry：受信任代码定义参数/返回类型、调用实现、固定 endpoint/凭据引用、权限、预算、版本及错误映射。工具启停/修订后旧 run 不得继续使用失效工具。

目标可用工具集合为部署启用 `ENABLED_TOOLS` ∩ 管理员当前启用 ∩ 用户授权；必需依赖只检查部署启用项。未配置天气/股票时不得暴露或调用，也不影响 CueKB-only 启动。部署白名单不能由管理员 API 重新开启；管理员只能在白名单内临时启停，运行中的旧任务会因版本或可用性变化被拒绝。`/capabilities` 返回部署集合和当前身份的 `available_tools`，`/health/ready` 返回非敏感部署集合。

已有天气示例契约仍见 [weather-openapi.yaml](../contracts/weather-openapi.yaml)，路径为 `/v1/places/resolve` 和 `/v1/weather`。这是应用供应商代理契约，没有证明已接通任何厂商；保留当前代码/schema 避免文档清理改变行为。新增股票等工具时按实际 API 另建 adapter，不能复用天气字段冒充通用事实。

只读 HTTP 按剩余预算做有限重试；当前对网络/5xx 最多一次，429/4xx/错误 JSON/超大正文不自动重试。缓存必须包含权限范围、查询条件、供应商和有效期，不能把历史数字作为新实时事实。

## 6. 客户身份接入（D02）

复用现有 OIDC/JWT 校验：issuer、JWKS、audience、签名、有效期、组织和主体；浏览器 Authorization Code + PKCE，票据和 Cookie 遵守同源/TLS 约束。具体配置见 `.env.example` 和当前 auth 实现。

已实现 `customer`、`operator`、`admin` 的保守映射：customer 只有 `knowledge:read`；operator 另有现有天气读取；只有 admin 拥有 `tools:admin`。用户、tenant、角色和 KB 范围只来自已验证 JWT，KB 取签名 claim 与部署允许列表的交集，严格请求模型拒绝正文覆盖。

每条新知识引用记录本轮服务端授权 KB 范围。历史消息和 SSE 重放在读取时复核当前范围；范围被缩小后，涉及已撤销范围的整个旧答案会替换为 `KB_ACCESS_REVOKED`，不只隐藏链接。送入后续 Agent 的历史也执行相同裁剪，避免旧证据通过上下文再次泄露。升级前没有范围标签的旧引用不向 customer 展示；operator/admin 仅在仍拥有部署完整 KB 范围时兼容读取。

默认不启用访客模式；若客服系统要求免登录，另定义可撤销、有资源限额的受限访客身份，不开放匿名管理能力。本地测试已覆盖客户不能访问管理 API、不同用户会话隔离、KB 交集和撤权后历史/SSE 脱敏；真实 IdP 即时撤权传播、CueKB Key/ACL 和并发会话仍需验收。
