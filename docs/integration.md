# 后端服务与工具接入

更新：2026-10-04。按主题读取。架构决策见 [architecture.md](architecture.md)，当前差距见 [任务板](TASK_BOARD.md)。

## 1. 运行依赖与配置状态

| 依赖 | 最终职责 | 当前实现状态 |
| --- | --- | --- |
| VoiceChat | 独立实时语音服务，原生选择工具或直接回答 | legacy/dual_tools 与有序响应许可本地完成，真实选择/续答/音频待 D07/Q07-E |
| CueKB | 知识检索与版本来源 | 专用 adapter、契约和受控测试已实现；真实服务/ACL 待 D07 |
| 文本模型 | legacy、reasoned、直查升级和严格知识路径 | openai / compatible 已实现；合格 D2 直查零外置调用，部署仍要求真实文本模型 |
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

CueKB 上游 HTTP 响应上限为 256 KiB，内部工具输出上限为 32 KiB，外置模型证据正文加上下文预算为 6000 字符；Q07 D2 的 evidence-v1 包另限默认 6000 bytes / 3 项（包括问题、身份与条件字段）。这些是不同边界，超过内部预算时显式舍弃上下文或命中。知识工具预算仍为 5 秒，业务整轮预算默认 30 秒，均不能证明真实端到端时延已达标。

## 3. VoiceChat 接入与能力门槛

### 3.1 连接与线上音频

API 通过 `VOICECHAT_WS_URL` 连接独立服务的 `/v1/realtime`。受控同主机/隔离网络可用 WS，其它非受控链路使用 WSS；浏览器始终访问本项目同源 HTTPS/WSS。VoiceChat 端口与公网门户端口不能混为一谈。

握手固定为 `session.created → session.update → session.updated`。legacy 注册 `consult_service_agent(user_request)`；dual_tools 按当前授权注册快照，仅有 `lookup_knowledge` / `reason_over_knowledge` 两个默认工具。Adapter 用 flat name/description/parameters/ack_messages 定义及 instructions，验证 `audio/pcm`、24000 Hz；不发送 `tool_choice`、未证实的 response.cancel、动态 TTS 或后台推送字段。工具定义不是每个音频帧重复发送。

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

分析对象是 VoiceChat-11B 模型、nemotron-labs-voicechat 推理框架及实时 WebSocket 的完整栈。模型通过 AVAILABLE_TOOLS 与最终提示词选择工具/直接回答；运行层解析 TOOLCALL，并通过原生事件交给本项目执行，不需要 WS tool_choice 参数。

| 会话 | 提示词与参数 | 选择规则 |
| --- | --- | --- |
| legacy | config/voice-prompt.txt、BridgeArguments | 所有完整输入先调用 consult_service_agent |
| dual_tools / general_qa | config/voice-qa-prompt.txt、KnowledgeArguments | 一般问答可不调用；企业产品资料或明确查 KB 选 lookup/reason；无实时业务能力时如实说明 |
| dual_tools / knowledge_required | 同上，加严格知识指令 | 不从记忆给实质性答案；lookup 服务端改走外置执行 |

两个知识工具参数是 user_request、product_model、software_version，后两项为必填 nullable 字段；不知道用 null，不猜测。schema 见 [voice-knowledge-arguments](../contracts/voice-knowledge-arguments.schema.json)，KB/身份/endpoint/凭据/预算不进入模型可控参数。

Gateway 按 input item 绑定 native call，并最多等待 5 秒最终 ASR；最终 ASR 覆盖 user_request，是业务输入和用户气泡依据。型号/版本须在最终输入或本会话已确认条件中，否则澄清。无输入不建 Turn，ASR 完成不重复启动查询。ToolDispatcher 按会话快照里的名称和 schema 分派，不进行第二次语义分类。

同一推理批次需按 ASR → 工具 → ACK/回答输出。原生 response.function_call_arguments.done 的 call_id 用于 conversation.item.create / function_call_output 回填；解析 arguments 对象再编码一次，避免双重编码。客户端应用负责工具执行；VoiceChat runtime 负责原生脚本解析和结果上下文注入。

speech 保留现有 USE_JINJA_TEMPLATE_PROMPT=1 分支、AVAILABLE_TOOLS/TOOLCALL/TOOL_RESPONSE 及 ACK 元数据。这个开关控制模板构造，不是工具选择的 auto 开关。按所选模式核对最终渲染工具和指令：legacy 不能追加绕过 bridge 的规则，dual_tools 不能残留每句强制 consult_service_agent；不机械地关闭 Jinja 或改成通用 vLLM 配置。模型漏调用/错工具/错参数仍须真实验收。

### 3.3 Response、ACK 与输出授权

空闲 codec 数组不创建 response。BOS/有效口述或工具调用打开 response；工具和同批 ACK 使用相同 response_id。EOS 结束该轮：先刷新 soxr 与 PCM 尾帧，再排入 transcript.done/audio.done/response.done。静音等待结束后，后续实际回答创建新 ID。

speech 输出 FIFO 中每条事件在生成时固定 response/item，单发送器按序发送。旧音频即使遇到慢网络，也不能因全局 ID 更新而被标成下一轮。默认两步输出出现相邻 EOS/BOS 时按确切的两个 80 ms codec 帧拆分；无法确定帧边界的异常批次显式失败，不猜测切割位置。

网关授权规则：

1. 初始无输入输出被抑制；legacy/knowledge_required 中未经工具授权的实质性回答返回 VOICE_TOOL_REQUIRED。
2. general_qa 无工具回答须绑定一个明确的当前输入和有序 response 生命周期；保存 Utterance/字幕，不创建知识 Turn。歧义或 ID 复用失败关闭，不能永久授权整场输出。
3. 合法工具响应承载固定 ACK；写回结果前记录 DeliveryAttempt 并安装受限续答许可，避免供应商即时回应竞态。结果最多授权随后一个回答；同 response 回答结束也回收多余许可。
4. D2 回填 EvidenceReady 后保持知识任务待原生答案，聚合字幕、audio.done 后校验并唯一提交；不再写第二份工具结果或播报第二次答案。失败/超时清除该 response 剩余播放，其他合法 response 不受该 clear 影响。
5. 新 speech_started 不直接取消有效任务；revision/epoch/租约/权限与工具版本仍复核。无工具旧响应不能借新输入获得许可。

legacy ACK 为 Please wait while I check the knowledge base.；新模式为 Please wait while I check that.，按配置短语和阶段识别为状态提示，不当答案正文。prepared/write_started/sent 与播放 samples 独立记录；崩溃后未确认写回记 unknown、不重发，sent 或字幕到达都不证明已听到。

QA_PROVIDER_ANSWER_TIMEOUT_MS 默认 10000：D2 续答取它与业务剩余预算的较小值；一般无工具响应从已开始输出后计算期限，缺结束边界记失败。尚未开始的无工具回答不属于此独立超时，不自动补启动检索。

### 3.4 语言、版本和能力验证

本期为 en-US，提示词、工具描述、ACK 和工具结果必须 ASCII；非 ASCII 历史不注入供应商，非 ASCII 口述摘要降级为查看门户的英文提示，文字证据仍保留原文。该传输限制不代表已证明口述事实正确。

用户已确认现场云端与本机 `speech` 源码同源，且原生 HTML 已能对话、插话和逐句展示文字。自然让话不由本项目重新实现；普通发声不取消业务任务，显式停止清本地缓冲，明确取消/改问另由 Coordinator 处理。

原生 HTML 不检查 response ID，按 transcript.done 分气泡；这不能证明音频也具备相同结束边界。原 WebSocket 音频队列与直接发送的字幕可能错序、ID 跨轮复用，adapter 无法仅凭现有事件可靠重建逐轮采样归属。当前确认基线采用 WebSocket 层修复以保留严格业务授权，模型本身不改；其他已满足有序归属的供应商版本无需此补丁。

D19 上游补丁、适用源码 hash、CPU 测试与发布方式见 [VoiceChat 补丁交付](../deploy/voicechat/README.md)。本项目 API/Web 和独立 VoiceChat 必须分别重建发布，不引入第二套本项目 Compose 配置；云端实际运行 hash/digest 与听音仍须记录。

基础验收要求每轮 ID/结束、同 call 工具往返、最终 ASR 绑定、英文音频、停止后继续和旧连接隔离。增强能力另测延迟工具期间的新问题、改问、取消和旧结果抑制，固定 ACK 不算新问题回答。

`scripts/probe_voicechat.py` 使用授权 24 kHz WAV、固定 API revision/digest 和可控工具延迟，记录无正文事件时间线，可保存授权输出音频。它的工具结果是合成的，不能代替真实 CueKB 闭环。当前业务总预算默认 30 秒，独立模型源码的工具等待超时是另一预算；必须通过延迟探针核对，不能假定本项目 env 自动改变 GPU 服务。

公开参考：[API](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/api-reference.md)、[部署](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/deploy.md)。原 adapter 文档基线为 NVIDIA revision `097dfe9e2f55baf653b83035868bdc89849f1b47`；实际服务以用户 speech 源码与发布 hash 为准，不能仅由公开文档推断私有镜像行为。

2026-09-28 文献复核：[新论文 §7、附录 B](https://arxiv.org/html/2609.21967v1) 明确当前工具执行期间不支持 barge-in；持续收音、字幕或固定 ACK 不代表新音频参与响应生成。本机 backend 的工具期限还存在帧数/推理批次时间单位风险。详细证据见 [Q05 分析](voicechat-research-review.md)，不能据此宣称增强能力通过。

本轮 P2 增加应用端 ProviderCapabilities 门槛与进度/修订处理；**生产 NVIDIA 仍全部 false**，默认注册两个工具的旧 schema，不发送 operation/tool_choice/任意播报命令。只有明确验证等待进度、修订、call 关联与旧调用安全结清的可信 Adapter 才使用 operation 扩展；没有 env/browser 绕过。SimulatedWaitAdapter 的 parent_call_id 是独立测试协议扩展，不是 NVIDIA 已公开字段。详细调用状态及真实启用条件见 [Live §3](live-agent-implementation.md#3-等待进度与自然修订)。基础关闭只依据 output_audio.done 与 WS 正常 close handshake；异常关闭仍是 unknown，不等于已播放。

## 4. 文本模型与业务 Agent

当前 `AGENT_PROVIDER=openai` 使用 Responses API，明确配置模型/API key；`compatible` 配置独立 HTTP base URL 并使用 Chat Completions。这些是本仓库适配器行为，真实供应商必须另验工具调用、结构化输出、streaming 和错误语义。VoiceChat WS/WSS 地址不能代替文本模型 endpoint。

Runtime 复用授权工具和证据校验，输出 display_text、短 speech_text、引用和业务状态；SDK 的工具循环与耗时受整轮 deadline 约束。复杂知识子 agent 若启用，权限/预算继承并缩小，独立临时上下文，只返回候选结果。

后台默认只有 `search_knowledge`。Runtime 对 Responses API 和 compatible Chat Completions 在本轮没有真实授权检索时要求首次调用，并关闭并行工具调用；直查升级若已有本轮有效证据，则允许直接综合或在剩余预算内补一次检索。输出后仍校验实际授权检索后置条件。SDK 的 required/auto 是外置知识执行设置，与前台 VoiceChat WS 无关。模型未调用工具时返回 `AGENT_REQUIRED_TOOL_NOT_CALLED`；adapter 的授权、契约、限流、超时或上游错误优先于模型声称的 `insufficient_evidence`，只有真实检索结果才能形成未命中/证据不足状态。

Runtime、ToolRegistry、CueKB adapter 与 VoiceGateway 记录脱敏阶段日志，包含 conversation/turn/tool、调用状态、耗时、trace 及 endpoint 的 scheme/host/port/path；不记录问题正文、API key、URL userinfo、供应商原始错误或工具结果正文。管理员的 `search_knowledge` 连通性探测使用 CueKB `/v1/ready`，业务检索仍使用 `/v1/search`。

### 4.1 查询延迟与优化边界

现场反馈约 3 秒，尚无对应 turn 的完整分段日志，不能认定“CueKB 查询用了 3 秒”。默认按提交文字到完整答案分析：HTTP/鉴权/建 Turn → 模型生成检索参数 → CueKB 检索 → 模型生成结构化答案 → 证据/权限复核与提交 → SSE → 渲染。通常含检索规划和最终回答两次串行模型请求（与 [SDK agent loop](https://developers.openai.com/api/docs/guides/agents/running-agents) 一致）；额外工具轮次、上游排队/网络、有限重试会增加耗时。语音还包含说话结束判定、最终 ASR、原生工具提取、结果注入/TTS 和播放缓冲；ACK 不计作最终答案。

原页面固定 300 ms 数据库轮询，并在 final 事件后再 GET messages 才显示聊天答案。现在事务提交后唤醒同进程 SSE，通知只作为加速，持久化 Event/server_seq 仍是事实来源；回滚不通知，跨进程或漏通知保留 300 ms 补查，重连按 cursor 补读。页面对已加载 Turn 直接应用鉴权过滤后的 final，未知 Turn 才补取；晚到的 running 快照不能覆盖已收到的 final，新 epoch/revision 不能被旧请求覆盖。

这消除了同进程 0–300 ms 的轮询等待和已有气泡的一次 HTTP 往返，但不承诺总耗时从 3 秒降到某个数。legacy、文字和复杂路径保留模型规划；Q07 直查使用最终输入和已确认条件，经 EvidenceGate 决定 Nano 续答或升级，不先请求外置分类模型。直查的质量、升级率及实际时延仍需同样本实测，不能因少了调用就宣称整体效果已优化。

每个 `conversation_id/turn_id` 关联以下无正文日志：

| 阶段 | 日志与字段 | 解释 |
| --- | --- | --- |
| 每次模型调用 | `agent_model_call_finished`：call_index/status/duration_ms | 模型网络往返和完整输出；失败/取消也结束计时，不代表 TTFT |
| CueKB | `cuekb_response_validated`：trace_id/duration_ms/service_total_ms | 本项目往返含重试与解析；服务 total 若缺失为 null，不视为 0 |
| 工具 | `tool_run_finished`：status/duration_ms | 权限、当前任务检查及 adapter；其范围包含 CueKB，不能重复相加 |
| Agent | `agent_pipeline_finished`：model_calls/duration_ms | SDK 循环及结果校验，含模型与工具 |
| 提交与总计 | `answer_delivery_finished`：commit_ms/total_ms/committed | total 从 Coordinator execute 开始，含业务链和提交；不含浏览器网络/渲染 |

先收集同模型/KB/问题集的冷、热请求 p50/p95，按 turn 对齐。若模型阶段主导，再实测降低回答长度、模型服务排队与缓存；若 CueKB 主导，依据其 trace/timings 优化检索路径；若只在浏览器等待，检查 Nginx SSE 缓冲与额外代理。不得把 30 秒超时预算当实际等待，或把减少模型调用当不影响检索质量的已验证优化。

## 5. 第三方扩展（D06）

工具分为两个层次：

| 层次 | 注册与授权 | 当前名称 |
| --- | --- | --- |
| VoiceChat 可见业务工具 | agent_runtime/dispatch.py 的 NativeTool；自己的严格 schema、可信 executor、permission_scope、required_tools；会话定义快照 | dual_tools 默认仅 lookup_knowledge / reason_over_knowledge；legacy 单 bridge |
| 后台工具能力 | tools/registry.py 的 ToolSpec/adapter；部署白名单、管理员启停、身份范围和版本校验 | 默认 search_knowledge，通过 CueKB /v1/search 执行 |

新增原生工具在可信服务端初始化阶段调用 runtime.dispatcher.register(NativeTool(...))；executor 从 ctx.tool_arguments 获取经 schema 校验的参数，user_request 若存在仍以最终 ASR 覆盖。原生定义可以使用自己的参数模型，不必包含知识型号字段；新增业务需自行实现对象权限和结果校验，后端服务调用保持 Registry 边界。注册支持扩展，不意味着本期已启用额外工具或有动态插件加载。

可见工具必须同时满足身份 scope 与 required_tools 后端依赖。工具名称/定义在语音 session 固定，启停/变更后仍需版本与输出复核，新增定义在重建会话后生效。官方约 5 工具建议用于质量评估；超过仅告警，不能推断并行可靠，实际支持的调用组合另验。

沿用 ToolSpec + trusted adapter + ToolRegistry：受信任代码定义参数/返回类型、调用实现、固定 endpoint/凭据引用、权限、预算、版本及错误映射。工具启停/修订后旧 run 不得继续使用失效工具。

当前可用工具集合为部署启用 `ENABLED_TOOLS` ∩ 管理员当前启用 ∩ 用户授权；必需依赖只检查部署启用项。未配置天气/股票时不得暴露或调用，也不影响 CueKB-only 启动。部署白名单不能由管理员 API 重新开启；管理员只能在白名单内临时启停，运行中的旧任务会因版本或可用性变化被拒绝。`/capabilities` 返回非敏感的 enabled_tools、customer 范围的 available_tools 与 native_tools，以及部署默认模式/策略/工具版本；它不是某个已有会话快照，也不是行为实测。`/health/ready` 返回配置和容量状态。

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
