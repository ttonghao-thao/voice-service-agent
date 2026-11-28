# 当前系统架构

本手册描述当前源码中的组件、调用顺序和数据边界。按任务从[文档索引](README.md)进入对应小节。部署镜像、权重和挂载的实测身份记录在[验证手册](validation.md)。

## 1. 产品与部署边界

产品支持天气、通用知识和专业知识库三类查询。Sophon Live + LiveKit 默认启用三类，按 `ENABLED_TOOLS` 可省略天气和专业知识库，保留通用知识检索及受控兜底。NVIDIA 和旧 WebSocket 路径保留各自的兼容能力。

### 运行拓扑与数据

生产 Compose 运行 Web（Nginx/React）、API（Python 3.12/FastAPI）、LiveKit、PostgreSQL 和 Redis。API/Web 使用独立镜像，Alembic 迁移复用 API 镜像。Sophon、NVIDIA VoiceChat 和 CueKB 独立部署，每个 Room 的 AgentSession 在 API 内运行。[生产 Compose](../deploy/compose.production.yaml)

Web 在 `8087/TCP` 终止门户 HTTPS，在 `7880/TCP` 终止 LiveKit 信令 TLS。两个端口共用覆盖实际域名或公网 IPv4 的证书。公网部署使用系统信任链。LiveKit 直接发布 `7881/TCP` 和 `7882/UDP`，`LIVEKIT_RTC_INTERFACE` 可限定 ICE 候选的容器网卡。[公网证书与续期](deployment.md#公网证书与续期) · [媒体网卡](deployment.md#选择-livekit-媒体网卡)

PostgreSQL 持久化角色、会话、轮次和工具执行记录。Redis 的 DB0 供应用使用，DB1 供 LiveKit 使用，两者共用 Redis 实例。PostgreSQL 与 Redis 都有各自的数据卷。[存储模型](../apps/api/app/storage/models.py) · [数据库备份](deployment.md#升级前备份数据库)

### Provider 选择

VOICE_PROVIDER 是唯一后端选择，生产只接受 sophon_live 或 nvidia，模板默认只配置 Sophon。工厂每次创建一个 Provider；livekit_enabled 同时检查选择为 Sophon 和 URL，避免残留字段让 NVIDIA 会话误走 Room。启动脚本据选择派生 Compose profile：Sophon 启动同 Compose 的 LiveKit，NVIDIA 停止该实例且不要求 Sophon/LiveKit/Qwen 凭据；Web 对未选择的 LiveKit 入口返回 404，并延迟 DNS 解析以允许 SFU 缺席。保留的未选后端字段不会激活服务。本期仅验证 Sophon；双后端配置契约不认证 NVIDIA 的业务能力。

主路径使用 `dual_tools/general_qa` 和 `AGENT_PROVIDER=disabled`。工具数量由后台目录和 Provider 契约确定。Sophon 原生决定直接回答或委派，收到 task 后才调用已批准的 Qwen 解析自然语言。[原生 task 解析](#原生-task-解析)

## 2. 数据流与代码入口

### Sophon 通话链路

API 签发限定 Room、身份与时效的 Token。浏览器通过 `LIVEKIT_URL` 入房并发布音轨后打开门户控制 WS。WebRTC 传音频，门户 WS 传状态与字幕。控制 WS 建立后，API 使用同一 `LIVEKIT_URL` 入房并运行 AgentSession，通过 GPTLiveModel 连接 Sophon `/v1/live/sessions`。收到原生 `session.delegation.created` 后才运行 Router，业务调用沿 Gateway → Coordinator → Runtime → Registry → Adapter 执行。供应商字段在 voice/adapter 边界解释。[门户协议](portal-protocol.md) · [Sophon 原生委派接入](integration.md#2-sophon-与原生委派)

### 源码入口

| 环节 | 职责 | 源码（相对仓库根） |
| --- | --- | --- |
| 门户音频/状态 | WebRTC 音轨或 PCM WS，字幕、来源、文件重跑与取消 | [VoiceClient](../apps/web/src/audio/VoiceClient.ts)、[LiveKitMedia](../apps/web/src/audio/LiveKitMedia.ts)、[App](../apps/web/src/App.tsx) |
| 角色与指令 | 用户角色 CRUD、默认闹闹、固定规则组合、会话快照 | [VoiceProfiles](../apps/web/src/VoiceProfiles.tsx)、[profiles](../apps/api/app/voice/profiles.py)、[routes](../apps/api/app/api/routes.py)、[models](../apps/api/app/storage/models.py) |
| Gateway | 身份、Origin、票据、epoch、目录快照、Room Token | [gateway](../apps/api/app/voice/gateway.py) |
| Sophon LiveKit | AgentSession、音频窗口、原始 task、回执、候选字幕 | [livekit_room](../apps/api/app/voice/livekit_room.py)、[sophon_gpt_live](../apps/api/app/voice/sophon_gpt_live.py) |
| Sophon WS 兼容 | Live 协议、24 kHz PCM、输入/委派/候选片段 | [sophon_live](../apps/api/app/voice/sophon_live.py)、[sophon_relay](../apps/api/app/voice/sophon_relay.py) |
| Router | 结构化优先/异步 LLM 自然语言解析、参数校验、澄清/不支持 | [delegation_router](../apps/api/app/voice/delegation_router.py)、[固定解析指令](../config/delegation-router-prompt.txt) |
| Coordinator | 唯一业务提交、revision、租约、执行与交付复核 | [coordinator](../apps/api/app/sessions/coordinator.py) |
| Runtime / Registry | 可信分派、权限、版本、预算和执行审计 | [agent_runtime](../apps/api/app/agent_runtime)、[registry](../apps/api/app/tools/registry.py) |
| 三类 Adapter | CueKB evidence、QWeather、Tavily 的供应商边界 | [adapters](../apps/api/app/tools/adapters.py)、[qweather](../apps/api/app/tools/qweather.py)、[web_search](../apps/api/app/tools/web_search.py)、[tools.yaml](../config/tools.yaml) |
| NVIDIA 兼容 | 原生 call/batch/response、ASR 绑定、精确 NeMo 包预算 | [provider](../apps/api/app/voice/provider.py)、[relay](../apps/api/app/voice/relay.py)、[batches](../apps/api/app/voice/batches.py)、[native_cuekb](../apps/api/app/agent_runtime/native_cuekb.py)、[voice_budget](../apps/api/app/agent_runtime/voice_budget.py) |
| 外部 VoiceChat | 配套模型/模板/服务端完整文件 | [manifest](../docs/voicechat/snapshot-manifest.json) |

### 输入与委派绑定

LiveKit 根据 SDK 已发送的 PCM 观察输入，短停顿合并。只有唯一、仍有效的候选输入才执行每输入一次的查询；处理中、回执未知或重叠输入保守拒绝。已在新发声前结清的旧输入退出后续委派竞争，字幕归属不套用该排除规则。重复 ID 不重执行，不同输入允许主动重复同一问题；没有上游稳定 input ID 时，延迟旧任务的新 ID 仍不能可靠认证为新问题。

### 兼容链路

Sophon WS 兼容路径只执行可归属的 CueKB 请求。NVIDIA 使用自己的原生知识调用路径。三类查询发布使用 Sophon Live + LiveKit。[VoiceChat 能力门槛](integration.md#3-voicechat-接入与能力门槛)

## 3. 知识执行

### 查询类型

| task / 场景 | 当前处理 |
| --- | --- |
| WEATHER | QWeather GeoAPI 确认地点，实况或未来十日内单日预报；同名地点、缺参、过期/超范围分别处理 |
| CUEKB | 已配置时走原生知识工具、授权 evidence query/validate；保留问题、型号、版本、否定和条件 |
| WEB_SEARCH | Tavily，独立公开 query，可选 freshness；最多三条标题/URL/摘要，不冒充企业证据 |
| CLARIFY / UNSUPPORTED | 按原 delegation ID 明确结清，不查询猜测地点，不用公网搜索伪装执行动作 |

### 原生 task 解析

生产配置 `DELEGATION_ROUTER_PROVIDER=openai_compatible`。`Router.parse_native` 优先校验结构化前缀，否则经共享 HTTP client 调用 Chat Completions，使用 JSON Schema 限制类型与参数。Qwen 只解析已收到的原生 task，工具执行和答案仍走现有业务链路。`rule` 仅用于旧版本或受控兼容。请求关闭重试和重定向，默认总期限 4 秒、响应上限 16 KiB。错误、超时、截断、拒绝和越界输出按失败结清。

query 必须与完整原 task 一致；天气地点必须出现在原 task，日期/单位和参数 schema 另验。私有/敏感 task 或私有 Room 不发外部 LLM，结构化专业库任务仍按原授权路径执行。模型不接收 ASR、历史或证据，不能纠正 Sophon 已写错的型号。模型等待后的 epoch/输入窗口和提交前归属再次复核，防止补充发声或换题时执行旧意图。可见的 task、parse_method、配置 model 与结果写入按权限过滤的 sophon_route；原生触发、模型解析、真实工具和口述分别统计。

### 逐轮委派诊断

LiveKit 根据已送上游的音频建立输入记录，短停顿沿用同一 input_item_id。诊断在 SDK 的原始事件回调中保留委派结构，再按原有窗口绑定输入。字段缺失、类型不匹配和同 ID 冲突在业务校验之前有记录。已收件但无法归属的尝试留在通话的独立区域。

输入记录在现有八秒归属窗口结束后归纳结果。归属明确的输出活动以候选回复标识，交错发声、粘连欢迎语或其他未结清委派造成歧义时显示归属不明。诊断输出观察使用上游的有效输出字幕或有声 PCM，支持无 ASR。连接中断会保留已观察到的事件及未完成状态。观察窗口外的迟到事件仍可记录为未归属。

Router 保留初始意图、验证后的路由及公开兜底原因。Registry 在进入 Adapter 时记录实际运行，并保留技术状态与业务结果。CueKB Adapter 分别记录 evidence query/validate 的实际检查进度。Gateway 回执按原 commentary 事件 ID 匹配。逐阶段及终态日志通过 conversation_id、voice_session_id、epoch、input_item_id、delegation_id 和可选 turn_id 关联，内容使用字段结构与状态码。后台写入诊断快照及 SSE 事件，沿用既有历史、授权和游标。 [诊断采集](../apps/api/app/voice/sophon_diagnostics.py) · [门户协议](portal-protocol.md#逐轮委派诊断)

### 公开检索与兜底

获准公开检索的 `WEB_SEARCH` task 直接走 Tavily。对 `CUEKB` task，只有 `CUEKB_BASE_URL` 和 `CUEKB_API_KEY` 都为空，且该独立问题明确公开、未含敏感信息、当前 Room 及工具授权允许公开检索时，才可转 Tavily。只给出型号或资料属性不明时先澄清。任一 CueKB 连接字段已填写时，专业库停用、检索失败、无结果或权限拒绝均不触发此兜底。已回填企业资料的 Room 禁止后续公网搜索。停用 `weather` 时 Registry 拒绝该工具调用。

### 证据与结果交付

每个工具结果经 Coordinator/Registry 复核身份、epoch/revision/Turn、租约、权限、工具版本及输入归属，再按原 ID 回传。unknown 不自动重发，real 失败不回退 mock。QWeather/Tavily 无自动重试；天气 provider 的调用正常与可用天气答案分开。

专业知识使用 `cuekb.evidence/2.1`：每 input 最多一次 query，合法成功包再 validate；内部补召回由 CueKB 有界完成。Sophon 的业务问题来自已绑定原生 task；NVIDIA 的 question 来自最终稳定 ASR，model query 只供选择/审计。两者都不能让模型/浏览器设置 KB、凭据或扩大权限。

CueKB canonical model_view 默认 120 tokens；NVIDIA 最终完整 NeMo 包另由运行 VoiceChat tokenizer 精确计数，默认 256，仅整组省略并降级，不截断条件。Sophon 的 Commentary 封装与 NVIDIA 不同。字段与错误见 [CueKB 契约](interfaces/cuekb.md)，原生目录与 batch 见 [工具契约](interfaces/voice-tools.md)。

## 4. 全双工与生命周期

### 字幕与播放

收音、ASR、模型理解、播放和业务任务是不同状态。Sophon ASR 仅展示：无法唯一关联时发 association=unbound 独立字幕，不生成业务输入/授权。字幕缺失、空、错误或迟到不决定委派是否执行。NVIDIA 最终转写绑定是其自身的知识输入契约，不能套到 Sophon。

普通发声/Stop playback 清旧播放但不取消查询；明确取消、换题、新业务 revision、End call、失效 epoch/租约/权限按规则结清。新输入不因 related-input 或字幕相似就自动视为已消费/已回答。旧输出在生成时固定身份，不因发送变慢改绑新轮次。

### 回执与听音

NVIDIA 注入期间显示 RNNT 可领先 LLM 的 PCM 消费，工具提取仍缓冲、结果仍逐 token 注入；这不是同时完成理解。其普通回答 EOS 等待新输入，ACK EOS 可让待答结果续答；播后有限核验仍有语义/覆盖缺口。

Sophon Gateway 回执只证明接收；缺少稳定输入 ID、结果消费确认和可验证的结果→响应终态关系，候选字幕/音频保持 unverified。网络发送、audio.done、播放器排空、设备发声、人耳听到和事实正确均分开；重连仅恢复授权摘要，不恢复隐状态或重放 PCM/未知结果。

连接、工具和续答有界，不承诺无限通话。默认连接 105 秒、活动宽限 30 秒；NVIDIA 原生续答默认 60 秒，具体值以 config.py 为准。ProviderCapabilities 未经实际验证不得开启等待进度/修订扩展。

## 5. 多轮与权限

### 会话授权

当前 validation 身份是每通话独立 owner/capability，token 仅在标签页内存，结束撤销。用户范围、历史/SSE、来源和后续摘要在读取/执行/提交时复核；旧无权限标记的企业引用不能因兼容而泄漏给 customer。LiveKit 客户 scope 包括 knowledge/weather/web_search，仍受部署启用、后台状态、KB 范围和当前权限约束。

### 角色与多轮

角色按 owner_id 持久化；新语音 session 快照 Persona + 固定规则，后续编辑/删除不热改活动连接，异常回默认。麦克风重连保留选择；文件错误/轮换后的显式重跑使用新通话身份，不自动重放。validation 角色不跨新身份共享。

NVIDIA 产品指代通过授权实体目录和已审核别名建立 binding。Sophon 的型号与参数来自绑定的原生 task。当前验收缺口见[任务板](TASK_BOARD.md)。
