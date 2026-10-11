# 后端服务与工具接入

更新：2026-09-28。按主题读取。架构决策见 [architecture.md](architecture.md)，当前差距见 [任务板](TASK_BOARD.md)。

## 1. 运行依赖与配置状态

| 依赖 | 最终职责 | 当前实现状态 |
| --- | --- | --- |
| VoiceChat | 独立实时语音服务，原生工具调用 | NVIDIA adapter 与 speech 逐轮协议同步修复；真实复测待 D07 |
| CueKB | 知识检索与版本来源 | 专用 adapter、契约和受控测试已实现；真实服务/ACL 待 D07 |
| 文本模型 | BusinessRuntime 的推理与业务回答 | 已有 openai / compatible adapter；真实模型未验收 |
| 第三方工具 | 本期不启用 | 天气代理代码仍保留，生产默认无需天气配置 |
| 通话 capability | 每次测试通话的独立 owner/token 与服务端 KB 范围 | 标签页内存 token 已实现；正式客户身份服务暂缓 |

当前环境变量名和启动校验以 `apps/api/app/config.py`、`.env.example` 为准。本期 Compose 固定真实 CueKB 与 `search_knowledge`；运行配置只需 `CUEKB_BASE_URL` 和 `CUEKB_API_KEY`，检索模式与条数使用代码默认值。OpenAI-compatible 文本模型和 CueKB 基地址均接受 HTTP/HTTPS；VoiceChat endpoint 接受 WS/WSS。HTTP/WS 只用于同主机或已隔离、受控的内部网络，对外门户仍必须使用 HTTPS/WSS。旧 `RAG_*` 配置和 `/v1/retrieve` 契约已退出活动实现。

部署只使用一套功能验证配置；正常 API 启动拒绝 fixture 身份、mock、自动建表和未配置的 VoiceChat。自动化测试显式注入 fixture 设置，不代表另一个部署环境。VoiceChat API 版本、镜像 digest、事件和人工听音结论由协议探针参数及报告记录，不再复制为运行时开关。

## 2. CueKB 当前接入契约（D03、E01–E02）

核对本地 CueKB revision：`1b9379de53c55dd41193a1529e46d48c0f219c14`（M3）；来源为其 `src/cuekb/schemas.py`、API routes/dependencies 与检索实现。部署前核对目标服务 OpenAPI，不把该 revision 当作所有部署版本。

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

示例 UUID 仅说明类型。实际 KB UUID 来自服务端授权映射；旧 `kb_support` 不能直接传入。当前 CueKB query 上限 2000 字符，top_k 支持 1–20；默认选 5 是本应用建议。工具输入目前只开放 `product_model`、`software_version`，由 BusinessRuntime 从明确输入或已确认上下文传入；Adapter 不从自然语言猜测。CueKB 支持的 `document_ids` 暂不开放给模型。`relations` 是 CueKB M3 的可选请求字段；本项目可接收其返回的关系证据，但暂不让模型生成实体 UUID、关系类型或时间条件。

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
| context_parts、context_truncated | 保存逐块原文及锚点、CueKB 的上下文截断标记；门户可展开逐块来源 |
| relations | 保存关系类型、条件和 supports/refutes 立场；该立场不是事实真假结论 |
| title_path、anchor、metadata | 来源定位及适用条件；缺失标题用“来源片段”，不伪造 |
| rank、retrieval_sources | 检索排序/来源，不当作事实置信度 |
| timings_ms、retrieval_path、executed_stages、skipped_stages | 内部诊断，不要求普通客户理解 |

Citation 的 updated_at 已改为可选，未填当前时间冒充文档更新时间；不再生成 score。`version_id` 与 metadata 中受控的 `business_version` 分开，旧历史 JSON 在读取时仍按原数据兼容，新增任务字段由 Alembic 0004 迁移。

CueKB M3 已提供有界章节、相邻块及表头上下文，但预算耗尽时仍可能截断或返回空 context；本项目另以 6000 字符证据预算和小于 32 KiB 的工具结果预算裁剪，使用 `context_omitted` 和 `hits_omitted` 明示应用侧裁剪，不将其冒充 CueKB 状态。回答模块仍需检查证据充分性。原件查看需经本项目重新鉴权并固定检索版本；这是待实现入口，不向浏览器暴露服务 Key 或私有下载 URL。

### 2.2 身份和错误

有效范围取客户授权、部署允许、CueKB Key 可读范围的交集。CueKB 当前鉴权主体是 API Key；自定义 X-Tenant-ID/X-User-ID 不代表它已执行客户级 ACL。不同隔离范围由服务端映射受限 Key/KB，不由工具参数指定。

not_found 表示本次未命中，不能推导事实不存在；degraded 有 hits 时保留原因并判断可用性；401/403 为授权或配置问题，422 为契约问题，429 为负载限制，5xx/超时为服务故障。禁止统一降为“查无资料”。返回答案、读历史证据和原件时都需覆盖撤权策略。

CueKB 上游 HTTP 响应上限为 256 KiB，内部工具输出上限为 32 KiB，模型证据正文加上下文预算为 6000 字符；三者是不同边界，超过内部预算时显式舍弃上下文或命中。知识工具预算仍为 5 秒，业务整轮预算默认 30 秒，均不能证明真实端到端时延已达标。

## 3. VoiceChat 接入与能力门槛

### 3.1 连接与线上音频

API 通过 `VOICECHAT_WS_URL` 连接独立服务的 `/v1/realtime`。受控同主机/隔离网络可用 WS，其它非受控链路使用 WSS；浏览器始终访问本项目同源 HTTPS/WSS。VoiceChat 端口与公网门户端口不能混为一谈。

握手固定为 `session.created → session.update → session.updated`，首次配置注册 `consult_service_agent(user_request)`。Adapter 验证输入/输出为 `audio/pcm`、24000 Hz；不发送未证实的 `response.cancel`、`tool_choice`、动态 TTS 或后台推送字段。

| 阶段 | 采样和行为 |
| --- | --- |
| 设备 / AudioContext | 常见 48 kHz 或 44.1 kHz，以浏览器实际设置为准 |
| 门户 → API → VoiceChat | 单声道 PCM16 little-endian、24 kHz、80 ms、3840 bytes，包括静音 |
| VoiceChat 模型输入 | speech 服务内重采样到 16 kHz；两步默认 2560 samples/160 ms |
| 模型输出 → VoiceChat | 22.05 kHz codec 音频，按 response 重采样到 24 kHz |
| VoiceChat → 门户 | 24 kHz PCM16；正常帧 80 ms，结束时允许短残帧，audio.done 前排空 |
| 门户播放 | 重采样到 AudioContext，160 ms 起播缓冲、短回答完成时排空；ACK 仅估计进度 |

48 kHz capture 不表示发送了 48 kHz；内部 16 kHz 日志不表示接口应该改成 16 kHz。首包前几个零字节不能证明整段收音无声，供应商发送包数也不能证明音频含语音。

### 3.2 工具路由与最终用户输入

VoiceChat 对每个完整客户发言调用统一业务 bridge，包括问候、闲聊、听不清和知识问题。工具描述与 `config/voice-prompt.txt` 保持这个范围；只允许 `BridgeArguments`，KB/凭据/主机不能进入模型可控参数。

网关按 `speech_started` 的 item_id 收集输入，工具消费尚未绑定的 input item；工具参数做 schema 校验，最终 ASR 文本才作为业务请求和持久用户气泡。最终 ASR 最多等待 5 秒；无有效输入的原生调用只返回失败以结清，不创建业务 Turn。ASR 完成事件不能再触发重复查询。

独立 speech 在同一模型批次中先发 ASR，再发工具，再发 ACK/回答音频，以便网关先绑定输入并授权。原生 `response.function_call_arguments.done` 的 call_id 用于 `conversation.item.create/function_call_output` 回传；arguments 若由模型给出 JSON 字符串，服务端先解析对象再编码一次，不能双重 JSON 编码。

供应商最终渲染的工具模板不得追加“常识无需工具直接回答”等与应用规则冲突的路由。本项目工具描述覆盖每个完整输入；speech 使用现有 `USE_JINJA_TEMPLATE_PROMPT=1` 分支，保留 AVAILABLE_TOOLS、TOOLCALL、TOOL_RESPONSE 和独立 ACK 元数据。默认模板保持原样，不再为本项目改写。该环境变量在 Python 导入时读取，须随容器/进程启动生效。仅加强前置提示词不能抵消后置冲突模板，也不能替代网关后置条件。

### 3.3 Response、ACK 与输出授权

空闲 codec 数组不创建 response。BOS/有效口述或工具调用打开 response；工具和同批 ACK 使用相同 response_id。EOS 结束该轮：先刷新 soxr 与 PCM 尾帧，再排入 transcript.done/audio.done/response.done。静音等待结束后，后续实际回答创建新 ID。

speech 输出 FIFO 中每条事件在生成时固定 response/item，单发送器按序发送。旧音频即使遇到慢网络，也不能因全局 ID 更新而被标成下一轮。默认两步输出出现相邻 EOS/BOS 时按确切的两个 80 ms codec 帧拆分；无法确定帧边界的异常批次显式失败，不猜测切割位置。

网关授权规则：

1. 初始未桥接输出被抑制；用户输入后未桥接的直接答案以 `VOICE_TOOL_REQUIRED` 关闭。
2. 已绑定合法工具的 response 可承载固定 ACK；有效工具结果提交后最多再授权一个新 response。
3. 若工具结果在 ACK 未播完时已返回，固定 ACK 不消耗后续答案的许可。固定 ACK 文本由 adapter 统一提供并按规范化空白精确识别。
4. 若最终答案沿用工具 response，则该 response 完成时回收未使用的后续许可；若用新 response，则首次输出时消费许可。之后无关回答不能继承授权。
5. 新 speech_started 不撤销有效回答；取消/revision/epoch/租约检查独立执行。初始被抑制 ID 的文字已完成、audio.done 未到，用户输入后又携带非空口述时记录 `voice_response_lifecycle_mismatch` 并显式报错，不能把整场连接放行。

固定 ACK 不是证据答案，供应商写回成功不是客户已听到。业务答案、实际口述字幕和播放 ACK 分别记录。

### 3.4 语言、版本和能力验证

本期为 en-US，提示词、工具描述、ACK 和工具结果必须 ASCII；非 ASCII 历史不注入供应商，非 ASCII 口述摘要降级为查看门户的英文提示，文字证据仍保留原文。该传输限制不代表已证明口述事实正确。

用户已确认现场云端与本机 `speech` 源码同源，且原生 HTML 已能对话、插话和逐句展示文字。自然让话不由本项目重新实现；普通发声不取消业务任务，显式停止清本地缓冲，明确取消/改问另由 Coordinator 处理。

原生 HTML 不检查 response ID，按 transcript.done 分气泡；这不能证明音频也具备相同结束边界。原 WebSocket 音频队列与直接发送的字幕可能错序、ID 跨轮复用，adapter 无法仅凭现有事件可靠重建逐轮采样归属。当前确认基线采用 WebSocket 层修复以保留严格业务授权，模型本身不改；其他已满足有序归属的供应商版本无需此补丁。

D19 上游补丁、适用源码 hash、CPU 测试与发布方式见 [VoiceChat 补丁交付](../deploy/voicechat/README.md)。本项目 API/Web 和独立 VoiceChat 必须分别重建发布，不引入第二套本项目 Compose 配置；云端实际运行 hash/digest 与听音仍须记录。

基础验收要求每轮 ID/结束、同 call 工具往返、最终 ASR 绑定、英文音频、停止后继续和旧连接隔离。增强能力另测延迟工具期间的新问题、改问、取消和旧结果抑制，固定 ACK 不算新问题回答。

`scripts/probe_voicechat.py` 使用授权 24 kHz WAV、固定 API revision/digest 和可控工具延迟，记录无正文事件时间线，可保存授权输出音频。它的工具结果是合成的，不能代替真实 CueKB 闭环。当前业务总预算默认 30 秒，独立模型源码的工具等待超时是另一预算；必须通过延迟探针核对，不能假定本项目 env 自动改变 GPU 服务。

公开参考：[API](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/api-reference.md)、[部署](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/deploy.md)。原 adapter 文档基线为 NVIDIA revision `097dfe9e2f55baf653b83035868bdc89849f1b47`；实际服务以用户 speech 源码与发布 hash 为准，不能仅由公开文档推断私有镜像行为。

2026-09-28 文献复核：[新论文 §7、附录 B](https://arxiv.org/html/2609.21967v1) 明确当前工具执行期间不支持 barge-in；持续收音、字幕或固定 ACK 不代表新音频参与响应生成。本机 backend 的工具期限还存在帧数/推理批次时间单位风险。详细证据、现有实现与待确认优化见 [Q05 分析](voicechat-research-review.md)，不据此修改当前代码或宣称增强能力通过。

### 3.5 D21：语音桥接失败排查

`VOICE_TOOL_REQUIRED` 表示用户输入出现后，网关收到尚未授权的 `response_id` 的口述文字或音频。它与初始 response 跨轮复用触发的 `VOICE_PROTOCOL_ERROR` 是不同检查；新错误不能单独证明上一项生命周期问题已完成真实验收。

按同一语音 conversation 核对 `voice_tool_call_received`、`voice_unbound_tool_settled` 和 `voice_unbridged_response_rejected`。需要确认 `consult_service_agent` 原生调用已绑定客户 input，且工具/ACK/音频使用正确 response 并按顺序到达；不能仅凭提示词或绕过拒绝来认定已走业务检索。

对独立 VoiceChat 核对运行中的 `/s2s/audio_server.py` hash、配套模板和实际 Python 进程的 `USE_JINJA_TEMPLATE_PROMPT`。该开关在模块导入时读取；容器 shell 临时设置或已运行进程之后设置均不改变已导入的值。以 `USE_JINJA_TEMPLATE_PROMPT=1` 前缀启动 Python 时，另一次 `docker exec printenv` 只显示容器的基础环境，不能据此判定该 Python 进程是否继承了开关。应检查实际进程环境，或新 session 对应的 `Preparing prompt using jinja template` 日志。`./scripts/deploy-cloud.sh .env` 仅启动本项目，开关应在独立 VoiceChat 的进程/容器启动定义中生效。

## 4. 文本模型与业务 Agent

当前 `AGENT_PROVIDER=openai` 使用 Responses API，明确配置模型/API key；`compatible` 配置独立 HTTP base URL 并使用 Chat Completions。这些是本仓库适配器行为，真实供应商必须另验工具调用、结构化输出、streaming 和错误语义。VoiceChat WS/WSS 地址不能代替文本模型 endpoint。

Runtime 复用授权工具和证据校验，输出 display_text、短 speech_text、引用和业务状态；SDK 的工具循环与耗时受整轮 deadline 约束。复杂知识子 agent 若启用，权限/预算继承并缩小，独立临时上下文，只返回候选结果。

本期只有 `search_knowledge`，因此 Runtime 对 Responses API 与 compatible Chat Completions 都设置必需工具选择并关闭并行工具调用，同时在模型输出后再次校验已发生授权的 `search_knowledge` 调用。模型未调用工具时返回 `AGENT_REQUIRED_TOOL_NOT_CALLED`；adapter 的授权、契约、限流、超时或上游错误优先于模型声称的 `insufficient_evidence`，只有真实检索结果才能形成未命中/证据不足状态。

Runtime、ToolRegistry、CueKB adapter 与 VoiceGateway 记录脱敏阶段日志，包含 conversation/turn/tool、调用状态、耗时、trace 及 endpoint 的 scheme/host/port/path；不记录问题正文、API key、URL userinfo、供应商原始错误或工具结果正文。管理员的 `search_knowledge` 连通性探测使用 CueKB `/v1/ready`，业务检索仍使用 `/v1/search`。

### 4.1 查询延迟与优化边界

现场反馈约 3 秒，尚无对应 turn 的完整分段日志，不能认定“CueKB 查询用了 3 秒”。默认按提交文字到完整答案分析：HTTP/鉴权/建 Turn → 模型生成检索参数 → CueKB 检索 → 模型生成结构化答案 → 证据/权限复核与提交 → SSE → 渲染。至少两次串行模型请求（与 [SDK agent loop](https://developers.openai.com/api/docs/guides/agents/running-agents) 一致）；额外工具轮次、上游排队/网络、有限重试会增加耗时。语音还包含说话结束判定、最终 ASR、原生工具提取、结果注入/TTS 和播放缓冲；ACK 不计作最终答案。

原页面固定 300 ms 数据库轮询，并在 final 事件后再 GET messages 才显示聊天答案。现在事务提交后唤醒同进程 SSE，通知只作为加速，持久化 Event/server_seq 仍是事实来源；回滚不通知，跨进程或漏通知保留 300 ms 补查，重连按 cursor 补读。页面对已加载 Turn 直接应用鉴权过滤后的 final，未知 Turn 才补取；晚到的 running 快照不能覆盖已收到的 final，新 epoch/revision 不能被旧请求覆盖。

这消除了同进程 0–300 ms 的轮询等待和已有气泡的一次 HTTP 往返，但不承诺总耗时从 3 秒降到某个数。保持模型规划和工具链，避免直接检索原句导致上下文改写、型号/版本过滤退化；不以提前显示未验证模型流取代事实校验。

每个 `conversation_id/turn_id` 关联以下无正文日志：

| 阶段 | 日志与字段 | 解释 |
| --- | --- | --- |
| 每次模型调用 | `agent_model_call_finished`：call_index/status/duration_ms | 模型网络往返和完整输出；失败/取消也结束计时，不代表 TTFT |
| CueKB | `cuekb_response_validated`：trace_id/duration_ms/service_total_ms | 本项目往返含重试与解析；服务 total 若缺失为 null，不视为 0 |
| 工具 | `tool_run_finished`：status/duration_ms | 权限、当前任务检查及 adapter；其范围包含 CueKB，不能重复相加 |
| Agent | `agent_pipeline_finished`：model_calls/duration_ms | SDK 循环及结果校验，含模型与工具 |
| 提交与总计 | `answer_delivery_finished`：commit_ms/total_ms/committed | total 从 Coordinator execute 开始，含业务链和提交；不含浏览器网络/渲染 |

先收集同模型/KB/问题集的冷、热请求 p50/p95，按 turn 对齐。若模型阶段主导，再实测降低回答长度、模型服务排队与缓存；若 CueKB 主导，依据其 trace/timings 优化检索路径；若只在浏览器等待，检查 Nginx SSE 缓冲与额外代理。不得把 30 秒超时预算当实际等待，或把减少模型调用当不影响检索质量的已验证优化。

### 4.2 D21：文字请求失败排查

文字 POST → SessionCoordinator → BusinessRuntime → 配置的文本模型/ToolRegistry/CueKB，不经过 VoiceChat 音频服务。文字任务用其自身 `conversation_id/turn_id` 关联 `agent_run_started`、`agent_run_failed`、`agent_model_call_finished`、工具阶段与 `answer_delivery_finished`；同一 API 日志中较早的 voice 记录不属于该文字请求。

`agent_run_failed` 的完整 `exception_type` 是首要证据。用户已确认早期 `NotFoundError` 为文本 LLM 配置错误；它与后续 `RAG_INVALID_CITATION` 是不同故障。核对 API 容器实际的 `AGENT_PROVIDER`、`AGENT_BASE_URL` 和 `AGENT_MODEL`，不输出 API key。

### 4.3 D22：最终引用校验、一次修正与超时诊断

2026-10-11 现场 turn `e875b306-f662-4bad-93f2-a5259f84b377`：两次模型调用完成，CueKB `retrieval_status=ok/hit_count=5`，最终 `RAG_INVALID_CITATION`。这证明检索链路执行成功，不能证明模型引用正确，也不能把原始 hit_count 当作证据预算筛选后的可用引用数。

最终 `display_text` 的 `[Cn]` 必须列入 `citation_ids`，每个声明的 ID 必须属于本轮 `ctx.evidence`。提示词要求正文和列表一致，不能使用 document/chunk UUID、rank 或历史轮次 ID。后端继续验证真实工具调用、工具错误、启用状态/版本、证据状态及引用，Coordinator 保留提交时的 epoch/revision/租约检查。引用结构通过不代表逐条语义支持已获证明。

校验失败记录 WARNING `agent_citation_validation_failed`，包含 `undeclared_reference` / `unknown_citation`、可用/正文/声明三组 ID；每组最多 20 项，附总数及省略数。只有形如 `C` 加 1–8 位 ASCII 数字的值原样输出，其余值仅记录 SHA-256 摘要前 16 位；不记录问题、正文、工具参数、证据或供应商原始异常。

Runtime 对引用校验失败至多发起一次修正，复用同一次 SDK 执行的输入/工具结果和现有上下文。修正 Agent 无工具，`tool_choice=none/max_turns=1`，不再检索、不重置整轮 deadline、不发布原始增量；修正前检查当前任务及授权，修正后重新执行完整校验。已有证据不能支持回答时允许明确 `insufficient_evidence`；仍非法则失败，不按位置替换引用或强行指定 C1。取消正常传播，旧任务不会借修正恢复提交。

`agent_citation_repair_started/finished` 标识修正尝试和结果，第三次模型调用仍由 `agent_model_call_finished` 计时。Runtime/Coordinator 的整轮超时分别记录 WARNING `agent_run_timed_out` / `agent_turn_timed_out`，含 `AGENT_TIMEOUT`、关联 ID 和预算；外层 deadline 引发的内部 canceled 不等于用户主动取消，须结合外层日志判断。`answer_delivery_finished` 包含最终 `status/reason_code`，`committed=True` 只表示结果已保存。修正可能增加一次模型往返，真实模型兼容性、成功率和延迟须现场复测。

## 5. 第三方扩展（D06）

沿用 ToolSpec + trusted adapter + ToolRegistry：受信任代码定义参数/返回类型、调用实现、固定 endpoint/凭据引用、权限、预算、版本及错误映射。工具启停/修订后旧 run 不得继续使用失效工具。

当前可用工具集合为部署启用 `ENABLED_TOOLS` ∩ 管理员当前启用 ∩ 用户授权；必需依赖只检查部署启用项。未配置天气/股票时不得暴露或调用，也不影响 CueKB-only 启动。部署白名单不能由管理员 API 重新开启；管理员只能在白名单内临时启停，运行中的旧任务会因版本或可用性变化被拒绝。`/capabilities` 返回部署集合和当前身份的 `available_tools`，`/health/ready` 返回非敏感部署集合。

本期默认 `ENABLED_TOOLS=search_knowledge`。已有天气示例契约仍见 [weather-openapi.yaml](../contracts/weather-openapi.yaml)，但天气不属于本期产品范围，现存代码/schema 仅保留供后续需求评估，不对客户开放。

只读 HTTP 按剩余预算做有限重试；当前对网络/5xx 最多一次，429/4xx/错误 JSON/超大正文不自动重试。缓存必须包含权限范围、查询条件、供应商和有效期，不能把历史数字作为新实时事实。

## 6. 独立通话与知识范围（D02、D13）

本阶段没有登录、外部 IdP、tenant 或正式客户认证。每次在标签页点击开始通话，服务端创建独立 conversation/owner，并只返回一次高熵 `call_access_token`；后续 HTTPS/SSE 通过 Bearer token 解析 owner，WSS ticket 再绑定 owner、conversation、epoch 和 Origin。token 不写 cookie、localStorage 或 sessionStorage，刷新/关闭标签页不会恢复历史，结束通话会撤销 token。`KNOWLEDGE_BASE_IDS` 来自部署配置，call capability 固定为 customer 且只有 `knowledge:read`；请求正文不能覆盖 owner 或 KB 范围，管理 API 继续拒绝。门户只通过公网 HTTPS `8087` 的同源 `/api/` 进入 API 容器；API 不映射宿主端口。

这一 capability 只提供测试通话的对象隔离，不是登录态；它不改变 SessionCoordinator、业务 Agent、ToolRegistry、CueKB 授权过滤或 VoiceChat 全双工链路。正式多客户身份、账号恢复和撤权生命周期等验证通过后再设计。

每条新知识引用记录本轮服务端授权 KB 范围。历史消息和 SSE 重放在读取时复核当前范围；范围被缩小后，涉及已撤销范围的整个旧答案会替换为 `KB_ACCESS_REVOKED`，不只隐藏链接。送入后续 Agent 的历史也执行相同裁剪，避免旧证据通过上下文再次泄露。升级前没有范围标签的旧引用不向 customer 展示；operator/admin 仅在仍拥有部署完整 KB 范围时兼容读取。

本地测试覆盖两次通话 token 不同、交叉访问失败、结束后 token 失效、管理 API 拒绝、KB 范围由服务端确定，以及历史/SSE 的范围复核逻辑；真实 CueKB Key/ACL、多人并发语音和未来正式客户身份接入仍需另行验收。

## 7. Q04：原件查看的后续设计

状态：建议，未实现；不影响当前逐块证据展示。本仓库尚无原件下载代理，CueKB 原件读取的具体 API、版本固定与权限语义需先核对目标服务契约，不在此发明 URL。

若业务确需查看原件，由门户提交本项目 conversation 与 citation 标识，服务端检查会话归属、当前客户 KB 范围、部署范围及受限 Key；从已存引用取 document/version/anchor，不能接受浏览器提供任意上游 URL。通过受信任 Adapter 获取对应版本，旧版本不可用时明确说明，不能静默跳到最新文档。

下载响应限制大小、超时、内容类型和缓存权限；撤权后拒绝原件访问。优先受控附件下载，不把未知 HTML 内联为同源页面，不泄漏供应商 Key/私网 URL。若采用临时链接，须先确认其有效期和撤权语义；不能因已有链接而绕过当前授权。

实施顺序：确认业务需要与上游契约 → 设计本项目只读接口与错误映射 → Adapter/鉴权/门户入口 → 导出契约和受控测试 → 云端权限/版本验证。测试覆盖越权、撤权、旧版本缺失、上游失败及恶意 URL；未知契约前保持现有证据片段展示。
