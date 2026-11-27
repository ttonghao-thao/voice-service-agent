# 后端服务与工具接入

类型：Provider/Adapter/身份边界。需求以 [delivery.md](delivery.md) 为准，配置操作只在 [deployment.md](deployment.md)，协议字段查 interfaces，实际结果与复跑只在 [validation.md](validation.md)。

## 1. 三类服务与接入边界

| 系统 | 入口与职责 | 服务器控制项 |
| --- | --- | --- |
| Sophon Live | `/v1/live/sessions`，语音理解、直接回复、原生 delegate(task) | URL、凭据、Locale、固定委派规则 |
| LiveKit | 同一生产 Compose 内的 WebRTC Room 服务，复用 Redis DB1 | Web 7880 TLS、媒体 7881/TCP 与 7882/UDP、API Key/Secret、短时 Room Token |
| QWeather | GeoAPI 地点解析 + Weather v1 实况/预报 | 专属 HTTPS API Host、API Key、超时与来源 |
| Tavily | `https://api.tavily.com/search` 的公开知识检索/受控兜底 | API Key、独立 query、可公开范围、来源 URL |
| CueKB | `/v2/evidence/query` 后 `/v2/evidence/validate` | KB UUID、scoped Key、证据与权限修订、tokenizer/预算 |
| PostgreSQL / Redis | 角色/业务/审计、协调租约 | Compose 内网、真实凭据、持久卷 |

三类查询同时属于产品范围。`config/tools.yaml` 的后台名是 `weather`、`web_search`、`search_knowledge`，原生函数的名称和数量按 Provider 契约生成。可执行工具取部署启用、管理员当前启用和用户授权的交集，执行前后复核版本与范围。Sophon 部署按 `ENABLED_TOOLS` 选接服务并保留 `web_search`。[查询配置](deployment.md#查询服务的选择)

CueKB 的专业资料不能因查询失败转成公网问题。通用知识兜底仅对合法公开的独立 query；敏感信息、已回填企业上下文、资料属性不明必须按确定性边界拒绝/澄清。真实服务错误、未命中、冲突和权限拒绝分别保留。QWeather/Tavily 的供应商 JSON 只在 adapter 内映射，不能泄漏为核心业务协议。

## 2. Sophon 与原生委派

session.start 包含已快照的 instructions、24 kHz PCM、delegation.type=client、store=false。LiveKit 通过官方 GPTLiveModel；直接 WS 通过 SophonLiveAdapter。未配置上游 Key 时省略占位 Authorization，有 Key 时保持 TLS 校验并由服务端发送。

LiveKit 使用公开的原始 server event 按 delegation ID 读取 task；SDK 的 pending_transcript 不是查询参数来源。音频窗口确认唯一输入后，DelegationRouter 直接校验结构化 task，或使用用户批准的 Qwen 解析自然语言，再经 Coordinator → Runtime → Registry。LLM 只输出受限结构，不调用工具，不接收 ASR/历史/证据；私有任务不自动外发，失败不回退规则。等待模型后复核输入归属。原始 task、解析方式/model、执行、发送、回执和候选语音分别记录。

Sophon ASR 仅展示。字幕正常、迟到、空、错或关闭不得改变合法 task 的业务选择；unbound 字幕不创造输入授权。收到 session.commentary.appended 只匹配本次 client_event_id 的接收状态，不证明引擎消费或完整回答；候选语音和最终答案关联仍受上游协议限制。

CueKB 请求文本取已绑定 task 的完整问题，不把 ASR 或普通输出改造成工具调用。专业证据、签名、query/validate 及失败语义见 [CueKB 契约](interfaces/cuekb.md)。未配置 LiveKit 的旧 Sophon WS 当前只执行 CueKB，是三类能力的实现限制，不是产品部署选项。

角色统一由 voice/profiles.py 组装 Persona 和固定业务规则；用户配置不能改变工具许可、凭据、真实目录或活动会话快照。原生 delegate 注册类型不因角色变化改变。

## 3. VoiceChat 接入与能力门槛

本节仅描述仍在运行的 NVIDIA 兼容 Provider，不是单知识库产品方案。实际镜像与源码以 VoiceChat manifest/部署身份为准，不能从公开 HEAD 或会话回显推断。

握手为 session.created → session.update → session.updated；目录字段、精确名称、schema/ACK/指令回显和 native batch 规则只在 [原生工具契约](interfaces/voice-tools.md)。传输为 24 kHz mono PCM16，门户上行固定 80 ms（3840 bytes）；不发送未经证实的 tool_choice、response.cancel、任意 TTS 或后台推送字段。

模型先原生选工具，API 再将已绑定输入的最终稳定 ASR 作为 question；确认上下文只入 search_query/filters/context_bindings，model query 保留审计，不能替代用户原问题。单 input 问题级 query/validate、完整组准入和实际 NeMo 包计数见 [CueKB 契约](interfaces/cuekb.md)。这一 ASR 约束仅适用于 NVIDIA，不约束 Sophon。

空闲 codec 数组不创建 response；BOS/有效口述/工具调用打开 response，同批 ACK 与工具共享 response_id。EOS 先刷新 soxr/PCM 尾帧再排队文本/音频结束；后续回答使用新 ID。FIFO 在生成时固定身份，两个相邻 80 ms codec 帧可按明确 EOS/BOS 边界拆开，不猜异常帧。

持续 relay 转发普通回答、ACK 和知识回答；ACK 不完成知识答案，普通发声不取消查询。原生续答默认 60 秒，与检索 deadline/旧 QA_PROVIDER_ANSWER_TIMEOUT_MS 分开。英文链路的 instructions、ACK、工具正文按该 Provider ASCII 约束处理，门户证据仍保留原文，不将该限制推广到 Sophon 中文。

ProviderCapabilities 的等待进度/修订能力未获生产验证；SimulatedWaitAdapter 的 operation/parent_call_id 是测试扩展。最终工具数由全局目录决定，初始 search_knowledge_base 名称不是数量上限。外部完整文件与启动见 [源码交付](../deploy/voicechat/README.md)、[运行手册](voicechat/docker-run.md)。

## 4. SDK 与第三方扩展

legacy、reasoned 和外置文本 Agent 兼容代码仍存在；三类查询主交付使用 AGENT_PROVIDER=disabled。新增 DELEGATION_ROUTER_* 只用于已委派 task 的解析，不能填入 AGENT_* 来启动额外答案模型。兼容 Responses/Chat Completions 的 required/auto 是后台 SDK 行为，不是前台语音原生工具已调用的证据，不能用于补救 Sophon 漏委派。

扩展现有 ToolSpec + trusted adapter + Registry：参数/返回类型、实现、endpoint/凭据引用、权限、超时、版本、错误和审计由可信代码定义。新增 YAML 名称不会自动生成服务；后台只能在部署白名单内启停，工具失效时旧 run 不得继续提交。/capabilities 是配置/可用性快照，/health/ready 是启动门禁，二者均非实际查询或听音验收。

阶段日志使用 conversation/turn/tool、状态、耗时、trace 和脱敏 endpoint；不输出密钥、票据、私有结果正文或供应商原始错误。CueKB 原生 evidence HTTP 不自动重试，其他旧路径的有限重试不改变本期约束。

## 5. 独立通话与知识范围

当前 AUTH_MODE=validation。Start call（或先打开 Sophon 角色设置）创建独立 conversation/owner，返回一次性高熵 access token；HTTPS/SSE 使用 Bearer，WS 票据绑定 owner/conversation/epoch/Origin。token 不写 cookie/localStorage/sessionStorage，结束撤销，刷新不恢复历史。正式账号与跨身份角色共享未实现。

scope 和 KB 范围由服务端生成：LiveKit 客户可具有 knowledge:read、weather:read、web_search:read，仍受 Registry 和真实 KB grant 限制。请求正文/角色/模型不能扩大权限。管理凭据不授权读客户通话，客户 token 不获 tools:admin。

历史、SSE、引用和后续模型摘要在读取时复核当前授权；收缩后的企业答案替换为 KB_ACCESS_REVOKED，不能只隐藏链接。旧无授权标签的引用不向 customer 展示，operator/admin 兼容读取仍要求完整范围。真实 CueKB Key/ACL、多人并发和账号生命周期的验收状态只在 validation.md / TASK_BOARD.md。
