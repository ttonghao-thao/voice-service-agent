# 后端服务与工具接入

2026-10-21 按一期原生路径整理。本页维护 VoiceChat/CueKB 接入与授权边界；CueKB 请求映射单列，配置只在部署维护。原因见 [关键决策](decisions/architecture-decisions.md)，状态归 [任务板](TASK_BOARD.md)。

## 1. 运行依赖与配置状态

| 依赖 | 最终职责 | 当前实现状态 |
| --- | --- | --- |
| VoiceChat | 独立实时语音服务，原生选择工具或直接回答 | 连续 relay 和显式批次处理；真实验收状态见验收记录 |
| CueKB | 知识检索与版本来源 | 专用 adapter 与真实检索已有证据；完整权限/恢复验收仍待完成 |
| 文本模型 | legacy、reasoned、直查升级和严格知识路径 | 兼容实现保留；本期 AGENT_PROVIDER=disabled，不配置外置模型 |
| 第三方工具 | 本期不启用 | 天气代理代码仍保留，生产默认无需天气配置 |
| 通话 capability | 每次测试通话的独立 owner/token 与服务端 KB 范围 | 标签页内存 token 已实现；正式客户身份服务暂缓 |

当前环境变量名和启动校验以 `apps/api/app/config.py`、`.env.example` 为准。本期 Compose 固定真实 CueKB 与 `search_knowledge`；运行配置只需 `CUEKB_BASE_URL` 和 `CUEKB_API_KEY`，检索模式与条数使用代码默认值。OpenAI-compatible 文本模型和 CueKB 基地址均接受 HTTP/HTTPS；VoiceChat endpoint 接受 WS/WSS。HTTP/WS 只用于同主机或已隔离、受控的内部网络，对外门户仍必须使用 HTTPS/WSS。旧 `RAG_*` 配置和 `/v1/retrieve` 契约已退出活动实现。

部署只使用一套功能验证配置；正常 API 启动拒绝 fixture 身份、mock、自动建表和未配置的 VoiceChat。自动化测试显式注入 fixture 设置，不代表另一个部署环境。VoiceChat API 版本、镜像 digest、事件和人工听音结论由协议探针参数及报告记录，不再复制为运行时开关。

## 2. CueKB 当前接入契约（D03、E01–E02）

请求、证据映射、错误与预算只在 [CueKB 契约](interfaces/cuekb.md) 维护；改 CueKBAdapter/工具 schema 时读取该页。路由判定见 [知识执行](qa-routing-design.md)。

## 3. VoiceChat 接入与能力门槛

### 3.1 连接与线上音频

API 通过 `VOICECHAT_WS_URL` 连接独立服务的 `/v1/realtime`。受控同主机/隔离网络可用 WS，其它非受控链路使用 WSS；浏览器始终访问本项目同源 HTTPS/WSS。VoiceChat 端口与公网门户端口不能混为一谈。

握手固定为 `session.created → session.update → session.updated`。legacy 注册 `consult_service_agent(user_request)`；dual_tools 注册后台配置的全局目录，本期只启用 search_knowledge_base(query)。Adapter 用 flat name/description/parameters/ack_messages 定义及 instructions，验证 `audio/pcm`、24000 Hz 与工具/指令回显一致性；不发送 `tool_choice`、未证实的 response.cancel、动态 TTS 或后台推送字段。目录与刷新规则见 [原生工具契约](interfaces/voice-tools.md)，不是每个音频帧重复发送。

握手与流事件必须是带有效 `type` 的 JSON 对象；非法结构、音频格式、标识或 PCM16/base64 返回 `VOICE_PROTOCOL_ERROR` 并关闭连接。事件 `response_id` / `item_id` 若出现，须为 1–128 字符的字符串，输出 PCM16 chunk 限 48000 bytes；合法的未知事件类型仍可忽略。这些校验在供应商 Adapter 内完成，不将错误包交给浏览器处理，也不切换到 mock。

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

Provider 根据会话快照选择 `config/voice-prompt.txt`（legacy）或 `config/voice-qa-prompt.txt`（双工具），严格策略追加知识要求。模型可见参数及三种执行规则只在 [知识执行 §1–2](qa-routing-design.md#1-模式与工具边界) 维护。

Gateway 按 input item 绑定 native call，并最多等待 5 秒最终 ASR；最终 ASR 是业务输入归属和用户气泡依据，原生模型 query 不被覆盖。无输入不建 Turn，ASR 完成不重复启动查询。ToolDispatcher 按会话快照里的名称和 schema 分派，不进行第二次语义分类。

同一推理批次需按 ASR → 工具 → ACK/回答输出。原生 response.function_call_arguments.done 的 call_id 用于 conversation.item.create / function_call_output 回填；解析 arguments 对象再编码一次，避免双重编码。客户端应用负责工具执行；VoiceChat runtime 负责原生脚本解析和结果上下文注入。

speech 保留现有 USE_JINJA_TEMPLATE_PROMPT=1 分支、AVAILABLE_TOOLS/TOOLCALL/TOOL_RESPONSE 及 ACK 元数据。这个开关控制模板构造，不是工具选择的 auto 开关。按所选模式核对最终渲染工具和指令：legacy 不能追加绕过 bridge 的规则，dual_tools 不能残留每句强制 consult_service_agent；不机械地关闭 Jinja 或改成通用 vLLM 配置。模型漏调用/错工具/错参数仍须真实验收。

### 3.3 Response、ACK 与输出授权

空闲 codec 数组不创建 response。BOS/有效口述或工具调用打开 response；工具和同批 ACK 使用相同 response_id。EOS 结束该轮：先刷新 soxr 与 PCM 尾帧，再排入 transcript.done/audio.done/response.done。静音等待结束后，后续实际回答创建新 ID。

speech 输出 FIFO 中每条事件在生成时固定 response/item，单发送器按序发送。旧音频即使遇到慢网络，也不能因全局 ID 更新而被标成下一轮。默认两步输出出现相邻 EOS/BOS 时按确切的两个 80 ms codec 帧拆分；无法确定帧边界的异常批次显式失败，不猜测切割位置。

一期 relay 持续转发当前 input/response 的普通回答、ACK 与知识回答，不采用一次性发声许可。工具批次结清后，等待对应非 ACK 回答文本/音频结束，由 Coordinator 按实际交付证据检查并提交。ACK 不能当作答案；一般回答不能伪造企业来源。旧 legacy/knowledge_required 规则只保留兼容，不进入一期路径。

音频和工具结果写入串行化，台账区分发送与未知状态，不因断线自动重放。查询任务有执行期限，模型续答观察与连接同寿命；QA_PROVIDER_ANSWER_TIMEOUT_MS 属于旧工作流，不控制本期原生续答。连接轮换、取消及权限失效仍是明确边界。

### 3.4 语言、版本和能力验证

本期为 en-US，提示词、工具描述、ACK 和工具结果必须 ASCII；非 ASCII 历史不注入供应商，非 ASCII 口述摘要降级为查看门户的英文提示，文字证据仍保留原文。该传输限制不代表已证明口述事实正确。

用户已确认现场云端与本机 `speech` 源码同源，且原生 HTML 已能对话、插话和逐句展示文字。自然让话不由本项目重新实现；普通发声不取消业务任务，显式停止清本地缓冲，明确取消/改问另由 Coordinator 处理。

原生 HTML 不检查 response ID，按 transcript.done 分气泡；这不能证明音频也具备相同结束边界。原 WebSocket 音频队列与直接发送的字幕可能错序、ID 跨轮复用，adapter 无法仅凭现有事件可靠重建逐轮采样归属。当前维护版本在 WebSocket 层保留逐轮身份和顺序，模型本身不改；其他已满足有序归属的供应商版本无需此补丁。

维护的完整文件、适用源码 hash、CPU 测试与发布方式见 [VoiceChat 补丁交付](../deploy/voicechat/README.md)。本项目 API/Web 和独立 VoiceChat 必须分别重建发布，不引入第二套本项目 Compose 配置；云端实际运行 hash/digest 与听音仍须记录。

基础验收要求每轮 ID/结束、同 call 工具往返、最终 ASR 绑定、英文音频、停止后继续和旧连接隔离。增强能力另测延迟工具期间的新问题、改问、取消和旧结果抑制，固定 ACK 不算新问题回答。

`scripts/probe_voicechat.py` 使用授权 24 kHz WAV、实际 session.update 快照、固定 API revision/digest 和可控工具延迟，持续送静音推进等待/续答，记录有界无正文事件时间线，可保存授权输出音频；多工具模式不再默认测试 legacy bridge。当前验证统一见 [验证操作手册](validation.md)，固定上游实现核对见 [VoiceChat 源码交付](voicechat/README.md)。它的工具结果是合成的，不能代替真实 CueKB 闭环。检索任务和模型续答的期限分开，应用 env 不自动改变 GPU 服务的超时。

公开参考：[API](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/api-reference.md)、[部署](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/deploy.md)。原 adapter 文档基线为 NVIDIA revision `097dfe9e2f55baf653b83035868bdc89849f1b47`；实际服务以用户 speech 源码与发布 hash 为准，不能仅由公开文档推断私有镜像行为。

当前应用已有 ProviderCapabilities 门槛与进度/修订处理；**生产 NVIDIA 仍全部 false**，知识实现不暴露 operation，不发送 tool_choice/任意播报命令；工具数量独立由后台目录决定。只有明确验证等待进度、修订、call 关联与旧调用安全结清的可信 Adapter 才使用 operation 扩展；没有 env/browser 绕过。SimulatedWaitAdapter 的 parent_call_id 是独立测试协议扩展，不是 NVIDIA 已公开字段。详细调用状态及真实启用条件见 [Live §3](live-agent-implementation.md#3-等待进度与自然修订)。基础关闭只依据 output_audio.done 与 WS 正常 close handshake；异常关闭仍是 unknown，不等于已播放。

## 4. 文本模型与业务 Agent

以下仅说明尚存的兼容实现，不是本期部署依赖。`AGENT_PROVIDER=openai` 使用 Responses API，明确配置模型/API key；`compatible` 配置独立 HTTP base URL 并使用 Chat Completions。这些是本仓库适配器行为，真实供应商必须另验工具调用、结构化输出、streaming 和错误语义。VoiceChat WS/WSS 地址不能代替文本模型 endpoint。

Runtime 复用授权工具和证据校验，输出 display_text、短 speech_text、引用和业务状态；SDK 的工具循环与耗时受整轮 deadline 约束。当前没有知识子 Agent；后续引入须重新评审权限、预算及唯一提交边界。

后台默认只有 `search_knowledge`。Runtime 对 Responses API 和 compatible Chat Completions 在本轮没有真实授权检索时要求首次调用，并关闭并行工具调用；直查升级若已有本轮有效证据，则允许直接综合或在剩余预算内补一次检索。输出后仍校验实际授权检索后置条件。SDK 的 required/auto 是外置知识执行设置，与前台 VoiceChat WS 无关。模型未调用工具时返回 `AGENT_REQUIRED_TOOL_NOT_CALLED`；adapter 的授权、契约、限流、超时或上游错误优先于模型声称的 `insufficient_evidence`，只有真实检索结果才能形成未命中/证据不足状态。

Runtime、ToolRegistry、CueKB adapter 与 VoiceGateway 记录脱敏阶段日志，包含 conversation/turn/tool、调用状态、耗时、trace 及 endpoint 的 scheme/host/port/path；不记录问题正文、API key、URL userinfo、供应商原始错误或工具结果正文。管理员的 `search_knowledge` 连通性探测使用 CueKB `/v1/ready`，业务检索仍使用 `/v2/search`。

### 4.1 查询延迟与优化边界

按 Turn 关联模型、工具、CueKB、提交与 SSE；日志字段、包含关系和复测步骤只在 [统一验证手册](validation.md) 维护。

## 5. 第三方扩展（D06）

工具分为两个层次：

| 层次 | 注册与授权 | 当前名称 |
| --- | --- | --- |
| VoiceChat 可见业务工具 | NativeTool 可信 schema/执行器 + tool_catalog.py 全局后台元数据；版本化会话快照 | dual_tools 数量由后台配置，对所有客户可见；执行单独授权；legacy 单 bridge |
| 后台工具能力 | tools/registry.py 的 ToolSpec/adapter；部署白名单、管理员启停、身份范围和版本校验 | 默认 search_knowledge，通过 CueKB /v2/search 执行 |

新增原生工具的注册/schema/executor/scope/依赖规则只在 [知识执行 §2](qa-routing-design.md#2-注册输入与扩展) 维护；本节维护后台 ToolSpec/Registry 的服务接入与启停授权。

沿用 ToolSpec + trusted adapter + ToolRegistry：受信任代码定义参数/返回类型、调用实现、固定 endpoint/凭据引用、权限、预算、版本及错误映射。工具启停/修订后旧 run 不得继续使用失效工具。

当前可用工具集合为部署启用 `ENABLED_TOOLS` ∩ 管理员当前启用 ∩ 用户授权；必需依赖只检查部署启用项。未配置天气/股票时不得暴露或调用，也不影响 CueKB-only 启动。部署白名单不能由管理员 API 重新开启；管理员只能在白名单内临时启停，运行中的旧任务会因版本或可用性变化被拒绝。`/capabilities` 返回非敏感的 enabled_tools、customer 范围的 available_tools 与 native_tools，以及部署默认模式/策略/工具版本；它不是某个已有会话快照，也不是行为实测。`/health/ready` 返回配置和容量状态。

本期默认 `ENABLED_TOOLS=search_knowledge`。已有天气示例契约仍见 [weather-openapi.yaml](../contracts/weather-openapi.yaml)，但天气不属于本期产品范围，现存代码/schema 仅保留供后续需求评估，不对客户开放。

一期原生 CueKB HTTP 不自动重试。其他兼容路径的只读 HTTP 按剩余预算有限重试；429/4xx/错误 JSON/超大正文不自动重试。缓存必须包含权限范围、查询条件、供应商和有效期，不能把历史数字作为新实时事实。

## 6. 独立通话与知识范围（D02、D13）

本阶段没有登录、外部 IdP、tenant 或正式客户认证。每次在标签页点击开始通话，服务端创建独立 conversation/owner，并只返回一次高熵 `call_access_token`；后续 HTTPS/SSE 通过 Bearer token 解析 owner，WSS ticket 再绑定 owner、conversation、epoch 和 Origin。token 不写 cookie、localStorage 或 sessionStorage，刷新/关闭标签页不会恢复历史，结束通话会撤销 token。`KNOWLEDGE_BASE_IDS` 来自部署配置，call capability 固定为 customer 且只有 `knowledge:read`；请求正文不能覆盖 owner 或 KB 范围，管理 API 继续拒绝。门户只通过公网 HTTPS `8087` 的同源 `/api/` 进入 API 容器；API 不映射宿主端口。

这一 capability 只提供测试通话的对象隔离，不是登录态；它不改变 SessionCoordinator、业务 Agent、ToolRegistry、CueKB 授权过滤或 VoiceChat 全双工链路。正式多客户身份、账号恢复和撤权生命周期等验证通过后再设计。

每条新知识引用记录本轮服务端授权 KB 范围。历史消息和 SSE 重放在读取时复核当前范围；范围被缩小后，涉及已撤销范围的整个旧答案会替换为 `KB_ACCESS_REVOKED`，不只隐藏链接。送入后续 Agent 的历史也执行相同裁剪，避免旧证据通过上下文再次泄露。升级前没有范围标签的旧引用不向 customer 展示；operator/admin 仅在仍拥有部署完整 KB 范围时兼容读取。

本地测试覆盖两次通话 token 不同、交叉访问失败、结束后 token 失效、管理 API 拒绝、KB 范围由服务端确定，以及历史/SSE 的范围复核逻辑；真实 CueKB Key/ACL、多人并发语音和未来正式客户身份接入仍需另行验收。
