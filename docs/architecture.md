# 当前系统架构

类型：当前代码事实。2026-11-23 对照 b60028d 调用链整理；部署实例的镜像/权重/挂载身份必须现场核对。需求见 [delivery.md](delivery.md)，未实施设计见 [implementation-plan.md](implementation-plan.md)，证据见 [validation.md](validation.md)。

## 1. 产品与部署边界

产品固定提供天气、通用知识和专业知识库三类查询，通用知识检索受控兜底。当前完整接入路径是 Sophon Live + LiveKit；NVIDIA/旧 WebSocket 的局部能力限制是兼容实现事实，不是仅知识库的产品模式。

Python 3.12/FastAPI、React/TypeScript/AudioWorklet、PostgreSQL/Redis。API/Web 独立镜像，迁移复用 API 镜像。Sophon、NVIDIA VoiceChat、CueKB 独立部署，LiveKit 单独运行；每 Room 的官方 AgentSession 已在 API 内，不另设业务 Agent 服务。

主路径使用 `dual_tools/general_qa` 和 `AGENT_PROVIDER=disabled`。dual_tools 是兼容配置名，不表示只有两个工具；三类业务能力也不等于三个原生函数。模型原生决定直接回答或委派，不启用独立分类 LLM、ASR 关键词路由、强制 tool_choice 或子 Agent 调度。

## 2. 数据流与代码入口

| 环节 | 职责 | 源码（相对仓库根） |
| --- | --- | --- |
| 门户音频/状态 | WebRTC 音轨或 PCM WS，字幕、来源、文件重跑与取消 | [VoiceClient](../apps/web/src/audio/VoiceClient.ts)、[LiveKitMedia](../apps/web/src/audio/LiveKitMedia.ts)、[App](../apps/web/src/App.tsx) |
| 角色与指令 | 用户角色 CRUD、默认闹闹、固定规则组合、会话快照 | [VoiceProfiles](../apps/web/src/VoiceProfiles.tsx)、[profiles](../apps/api/app/voice/profiles.py)、[routes](../apps/api/app/api/routes.py)、[models](../apps/api/app/storage/models.py) |
| Gateway | 身份、Origin、票据、epoch、目录快照、Room Token | [gateway](../apps/api/app/voice/gateway.py) |
| Sophon LiveKit | AgentSession、音频窗口、原始 task、回执、候选字幕 | [livekit_room](../apps/api/app/voice/livekit_room.py)、[sophon_gpt_live](../apps/api/app/voice/sophon_gpt_live.py) |
| Sophon WS 兼容 | Live 协议、24 kHz PCM、输入/委派/候选片段 | [sophon_live](../apps/api/app/voice/sophon_live.py)、[sophon_relay](../apps/api/app/voice/sophon_relay.py) |
| Router | 结构化优先/自然语言规则、参数校验、澄清/不支持 | [delegation_router](../apps/api/app/voice/delegation_router.py) |
| Coordinator | 唯一业务提交、revision、租约、执行与交付复核 | [coordinator](../apps/api/app/sessions/coordinator.py) |
| Runtime / Registry | 可信分派、权限、版本、预算和执行审计 | [agent_runtime](../apps/api/app/agent_runtime)、[registry](../apps/api/app/tools/registry.py) |
| 三类 Adapter | CueKB evidence、QWeather、Tavily 的供应商边界 | [adapters](../apps/api/app/tools/adapters.py)、[qweather](../apps/api/app/tools/qweather.py)、[web_search](../apps/api/app/tools/web_search.py)、[tools.yaml](../config/tools.yaml) |
| NVIDIA 兼容 | 原生 call/batch/response、ASR 绑定、精确 NeMo 包预算 | [provider](../apps/api/app/voice/provider.py)、[relay](../apps/api/app/voice/relay.py)、[batches](../apps/api/app/voice/batches.py)、[native_cuekb](../apps/api/app/agent_runtime/native_cuekb.py)、[voice_budget](../apps/api/app/agent_runtime/voice_budget.py) |
| 外部 VoiceChat | 配套模型/模板/服务端完整文件 | [manifest](../docs/voicechat/snapshot-manifest.json) |

Sophon 主链：浏览器与 LiveKit 直接收发 WebRTC；门户 WS 传状态和字幕。API 签发限定 Room/身份/时效的 Token，并在 Room 中运行 AgentSession，通过 GPTLiveModel 连接 Sophon `/v1/live/sessions`。收到原生 `session.delegation.created` 后才运行 Router；业务方向保持 Gateway → Coordinator → Runtime → Registry → Adapter。供应商字段只在 voice/adapter 边界解释。

LiveKit 根据 SDK 已发送的 PCM 观察输入，短停顿合并。只有唯一、仍有效的候选输入才执行每输入一次的查询；处理中、回执未知或重叠输入保守拒绝。已在新发声前结清的旧输入退出后续委派竞争，字幕归属不套用该排除规则。重复 ID 不重执行，不同输入允许主动重复同一问题；没有上游稳定 input ID 时，延迟旧任务的新 ID 仍不能可靠认证为新问题。

未配置 LiveKit 的 Sophon WS 代码仍存在，当前只执行可归属的 CueKB 请求；NVIDIA 保留自己的原生知识调用路径。这些路径没有因此获得天气/Tavily 能力，也不满足三类查询的完整发布要求。

## 3. 知识执行

| task / 场景 | 当前处理 |
| --- | --- |
| WEATHER | QWeather GeoAPI 确认地点，实况或未来十日内单日预报；同名地点、缺参、过期/超范围分别处理 |
| CUEKB | 已配置时走原生知识工具、授权 evidence query/validate；保留问题、型号、版本、否定和条件 |
| WEB_SEARCH | Tavily，独立公开 query，可选 freshness；最多三条标题/URL/摘要，不冒充企业证据 |
| CLARIFY / UNSUPPORTED | 按原 delegation ID 明确结清，不查询猜测地点，不用公网搜索伪装执行动作 |

Router 优先解析 WEATHER/CUEKB/WEB_SEARCH 前缀，否则对原生 task 做确定性规则解析，返回内部 route、arguments/decision、parse_method、source、reason。它不读取 ASR、输出字幕或猜测的用户位置。原 task 和结果写入按权限过滤的 sophon_route 记录，原生触发和 Router 成功分开统计。

当前受控兜底分支：仅在 CueKB 地址和凭据均为空、CUEKB task 明确公开且无敏感内容时转 Tavily；裸型号、资料属性不明先澄清。CueKB 已配置/部分配置、服务失败或权限拒绝不自动外发。已回填企业资料的 Room 不再向公网发送后续搜索。此分支描述未完整接入环境的代码行为，不是关闭专业知识库的部署方案；完整交付仍要求三类服务。

每个工具结果经 Coordinator/Registry 复核身份、epoch/revision/Turn、租约、权限、工具版本及输入归属，再按原 ID 回传。unknown 不自动重发，real 失败不回退 mock。QWeather/Tavily 无自动重试；天气 provider 的调用正常与可用天气答案分开。

专业知识使用 `cuekb.evidence/2.1`：每 input 最多一次 query，合法成功包再 validate；内部补召回由 CueKB 有界完成。Sophon 的业务问题来自已绑定原生 task；NVIDIA 的 question 来自最终稳定 ASR，model query 只供选择/审计。两者都不能让模型/浏览器设置 KB、凭据或扩大权限。

CueKB canonical model_view 默认 120 tokens；NVIDIA 最终完整 NeMo 包另由运行 VoiceChat tokenizer 精确计数，默认 256，仅整组省略并降级，不截断条件。Sophon 的 Commentary 封装与 NVIDIA 不同。字段与错误见 [CueKB 契约](interfaces/cuekb.md)，原生目录与 batch 见 [工具契约](interfaces/voice-tools.md)。

## 4. 全双工与生命周期

收音、ASR、模型理解、播放和业务任务是不同状态。Sophon ASR 仅展示：无法唯一关联时发 association=unbound 独立字幕，不生成业务输入/授权。字幕缺失、空、错误或迟到不决定委派是否执行。NVIDIA 最终转写绑定是其自身的知识输入契约，不能套到 Sophon。

普通发声/Stop playback 清旧播放但不取消查询；明确取消、换题、新业务 revision、End call、失效 epoch/租约/权限按规则结清。新输入不因 related-input 或字幕相似就自动视为已消费/已回答。旧输出在生成时固定身份，不因发送变慢改绑新轮次。

NVIDIA 注入期间显示 RNNT 可领先 LLM 的 PCM 消费，工具提取仍缓冲、结果仍逐 token 注入；这不是同时完成理解。其普通回答 EOS 等待新输入，ACK EOS 可让待答结果续答；播后有限核验仍有语义/覆盖缺口。

Sophon Gateway 回执只证明接收；缺少稳定输入 ID、结果消费确认和可验证的结果→响应终态关系，候选字幕/音频保持 unverified。网络发送、audio.done、播放器排空、设备发声、人耳听到和事实正确均分开；重连仅恢复授权摘要，不恢复隐状态或重放 PCM/未知结果。

连接、工具和续答有界，不承诺无限通话。默认连接 105 秒、活动宽限 30 秒；NVIDIA 原生续答默认 60 秒，具体值以 config.py 为准。ProviderCapabilities 未经实际验证不得开启等待进度/修订扩展。

## 5. 多轮与权限

当前 validation 身份是每通话独立 owner/capability，token 仅在标签页内存，结束撤销。用户范围、历史/SSE、来源和后续摘要在读取/执行/提交时复核；旧无权限标记的企业引用不能因兼容而泄漏给 customer。LiveKit 客户 scope 包括 knowledge/weather/web_search，仍受部署启用、后台状态、KB 范围和当前权限约束。

角色按 owner_id 持久化；新语音 session 快照 Persona + 固定规则，后续编辑/删除不热改活动连接，异常回默认。麦克风重连保留选择；文件错误/轮换后的显式重跑使用新通话身份，不自动重放。validation 角色不跨新身份共享。

NVIDIA 产品指代通过授权实体目录和已审核别名建立 binding；Sophon 保留原生 task，不用 ASR 修正型号。固定音频中的 F7005 → F705、Persona 额外归属、证据复验到写回的窗口和语义覆盖缺陷仍需对应验收，状态只维护在 [任务板](TASK_BOARD.md)。
