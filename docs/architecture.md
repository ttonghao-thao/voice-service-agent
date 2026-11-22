# 当前系统架构

基线：本项目 `2a8ba8b` / CueKB `30970e9`。本文描述代码行为；故障及未验证边界见[任务板](TASK_BOARD.md)，不将目标当作已实现保证。

## 1. 产品与部署边界

Python 3.12/FastAPI、React/TypeScript/AudioWorklet、PostgreSQL/Redis。NVIDIA VoiceChat（英文）、Sophon Live（中文或英文验证）和 CueKB 均独立部署。门户界面使用英文，模型语音语言按 Provider 配置。

当前知识语音使用 `dual_tools/general_qa`、`AGENT_PROVIDER=disabled`。原生工具目录可配置，执行由静态可信实现和权限复核决定；不是固定只能注册两个工具。不启用分类模型、独立 ASR/TTS 服务或子 Agent 调度。模型可直接回答一般问题；企业问题漏调用仍是待优化项，不能冒充已查询。

## 2. 数据流与代码入口

| 环节 | 当前职责 | 代码入口（相对仓库根） |
| --- | --- | --- |
| 浏览器 | LiveKit WebRTC 或 24 kHz PCM16 WebSocket，显示字幕和来源 | apps/web/src/audio/VoiceClient.ts、LiveKitMedia.ts、apps/web/src/App.tsx |
| Gateway / Relay | 票据与 epoch、持续收发、input/call/batch/response 关联、单输入预算 | apps/api/app/voice/gateway.py、relay.py、batches.py |
| Coordinator | 唯一业务提交；revision、租约、等待口述和取消 | apps/api/app/sessions/coordinator.py |
| Runtime / Registry | 可信工具分派、权限、执行审计、问题/证据投影 | apps/api/app/agent_runtime/native_cuekb.py、native_input.py；app/tools/registry.py |
| CueKB Adapter | 问题文本、facets、授权实体目录；query 后 validate | apps/api/app/tools/adapters.py、entity_context.py |
| 最终语音预算 | 实际 tokenizer 对整个 NeMo 包计数；整组准入、记录省略 | apps/api/app/agent_runtime/voice_budget.py |
| VoiceChat 快照 | 原生工具提取/注入，显示 ASR、音频位置、响应结束 | docs/voicechat/s2s/audio_server.py；快照 backend/model.py、data_types.py |

调用方向保持 Gateway → Coordinator → Runtime → Registry → CueKB。音频保持单条 VoiceChat 连接；供应商结构仅在 voice 边界解释，业务不能凭模型参数扩大 KB/权限。

未配置 LiveKit 时，Sophon Live 使用原门户 WebSocket 音频协议和业务链。Adapter 通过 `session.start` 建连，relay 在 24 kHz PCM 上处理连续音频、字幕和打断。客户端委派的 `task` 须明确请求企业资料，并能归属到唯一稳定的语音输入，才能执行已注册并授权的 `search_knowledge_base`。CueKB 2.1 完成 query 和 validate 后发一次 Commentary。Gateway 回执只结清消息接收状态。候选语音以 `phase=candidate` 播放和显示字幕，业务记录保留授权来源和语音未核实状态。只有具备输入、委派和回答的可验证关联，才能进一步认证完整口述。

配置 LIVEKIT_URL 后，Gateway 继续从现有会话入口鉴权，并签发仅限当前 Room 的短时 Token。浏览器以 WebRTC 收发音频，门户 WebSocket 只传状态与字幕。API 每 Room 启动一个官方 AgentSession，由 GPTLiveModel 连接独立 Sophon Live。Sophon 负责语音打断，门户的 Stop playback 即时静音并在新输入的字幕到达后恢复播放。

LiveKit 路径根据官方 SDK 已发送的 PCM 观察输入窗口，短停顿归入同一个输入。Router 校验委派的路由和参数，现有 Coordinator、Registry 和 CueKB 负责授权、证据校验及业务提交。只有唯一候选输入时才执行每轮一次的检索。已收到工具结果或澄清回执、且在新发声前结清的输入退出后续委派竞争；处理中、回执未知和重叠输入仍按歧义拒绝，字幕归属不使用这个排除规则。同一请求的重复 ID 不执行，新输入允许主动重复同一查询。此本地生命周期修复不补足上游缺少输入 ID 的协议，延迟旧任务使用新 ID 时仍不能作可靠归属认证。结果回传前复核输入及权限。两条 Sophon 路径均使用模型生成的 `task` 提供查询参数，业务记录标为委派来源，ASR 仅作为门户字幕。已回传企业资料的 Room 拒绝向公网发送后续搜索，用户可在新通话请求公开搜索。

CueKB 未配置地址和凭据时，只有明确公开、独立且不含内部或个人信息的 CUEKB 任务才能转 Tavily；裸产品型号、资料范围不明和敏感内容均澄清。后台没有启用 web_search 时执行被 Registry 拒绝；CueKB 已配置或仅部分配置时不自动外发，私有服务失败也不切换。公开结果保持 general/public_web 来源，不冒充私有证据。

DelegationRouter 优先解析 WEATHER/CUEKB/WEB_SEARCH 前缀，否则只对原生 task 做确定性自然语言规则解析，输出 route、parse_method、source 和原因；没有原生事件不运行 Router。天气缺地点或日期不明、多个工具同时匹配时澄清；不支持的动作明确结清，不搜索伪装执行。原 task 和解析结果在按权限过滤的 sophon_route 记录中关联原 delegation ID。触发、路由、执行、回执和候选语音独立统计。

两种 Sophon 传输统一通过 voice/profiles.py 组合 Persona 与服务端固定业务指令。voice_profiles 按 owner_id 持久化，默认闹闹保持精确 Persona；创建语音会话时读取并快照，后续编辑/删除不修改活动会话。验证身份仍是每通话独立身份，角色 API 必须携带该 conversation_id 和原 call token，不引入跨通话或跨用户共享访问。麦克风重连保留选择；角色不修改 delegation.type=client、工具目录或 Registry 权限。原 WebSocket 仍只执行 CueKB。

ASR 无法唯一关联音频输入时，字幕使用独立展示 ID 和 association=unbound，不生成业务输入、权限或工具调用。字幕缺失、延迟、空值或错误不参与 task 解析与音频归属；门户明确标识未关联字幕。

QWeather Adapter 使用 GeoAPI 和 Weather v1，将供应商响应映射到既有天气契约。唯一精确名称/Location ID 可从行政区候选中确定地点，多个同名地点仍澄清。实况返回 `temperature`，预报返回 `temperature_min/temperature_max`，不将最高温误作实况。GeoAPI 未提供 ISO 国家代码时留空，完整国家名称保留在地点显示中；来源保留 attribution。Tavily Adapter 使用固定 HTTPS endpoint、Bearer 请求头、最多三条摘要和链接。两者无自动重试，继续由 Registry 控制超时、权限及审计。

口述字幕标为候选。Gateway 回执用于确认消息收到，真实口述归属和听音另验。

## 3. 知识执行

1. 模型原生 call 触发；相同 call_id 幂等，冲突身份严格拒绝；不同 input 不凭文字相似合并。
2. NVIDIA VoiceChat 使用最终 ASR 作为 `question` 创建业务 Turn；受控上下文进入 `search_query/filters/context_bindings`，模型 query 保留审计。Sophon 在核对音频归属后使用 `task` 中的完整问题创建业务 Turn。
3. 每 input 最多一次 `/v2/evidence/query`，合法成功包再一次 `/v2/evidence/validate`；服务内部补召回归 CueKB。目录 GET 和 tokenizer 预检不属于第二次证据查询。失败不自动重试或回退旧 search。
4. 严格消费 facets/decision/完整来源。CueKB canonical model_view 默认 120 tokens；最终 NeMo 包另由运行 VoiceChat 精确计数，默认上限 256。超过预算仅整组省略并降级决策，不能截断条件。
5. 每原生批次一份回填，未执行成员显式结清。结果终态后新 call 不查库、不新建 Turn，返回小型 settlement 并抑制冗余 ACK；仍不能保证模型一定遵从停止指令。
6. 正式文本和 audio.done 触发播后检查及 Coordinator 提交。ACK 不结清知识答案。实际已准入证据用于核验，原始包另存审计。当前检查不证明逐 facet 完整性/语义真值，见待优化 F1。

字段及错误只维护在[CueKB 契约](interfaces/cuekb.md)；注册与批次在[工具契约](interfaces/voice-tools.md)。

## 4. 全双工与生命周期

收音、转写、模型理解、播放与业务任务是不同状态。欢迎语允许无用户 input；后续无归属输出抑制但不断连。用户开口清理并抑制已有 response 的迟到播放；Stop playback 不取消检索。明确业务取消/End call/失效 epoch 或权限按规则结清。

注入期间 display RNNT 以独立音频游标运行，原始 PCM 留给 LLM 后续一次消费；不把显示 ASR 的推进说成 LLM 同时理解。工具提取期仍缓冲，结果仍逐 token append/anext。新显示输入与旧工具所属输入按音频位置区分；VoiceChat 的 related-input 判定仅覆盖到起点；API 因此不再将 related 当作完成或向门户宣称包含回答，补充问题保留独立处理资格（F3）。

VoiceChat 普通回答 EOS 等待新输入；ACK EOS 允许待答结果继续。API 按 response 抑制旧音频/重复输出并处理 drained。工具正文提交后等待答案默认 60 秒；连接默认 105 秒、活动宽限 30 秒。到期/背压/协议错误仍可能结束连接，不是无限会话。

网络写入与数据库无共同事务；DeliveryAttempt 的 unknown 不重发。音频 done 是发送边界，Worklet finished 是排空估计，均不证明设备发声或用户听到。重连只恢复经授权业务摘要，不恢复隐状态/PCM/旧播放。

## 5. 多轮与权限

最终 ASR/Utterance 是可信用户输入来源；助手文字和模型 query 不授权产品实体。代词追问读取授权实体目录，以唯一名字/已审核别名建立 binding；英文缩写撤回已统一规范化（F2），复杂指代仍需语料验证。自然停顿可生成多个 input，不把 WAV 文件边界强制当一轮。

一个 conversation 的新业务 revision 会替代旧任务；普通发声不等同新业务提交。当前只将响应主 input 标完成，关联输入不再自动结清。历史、来源、后续摘要、执行和提交均需检查授权；CueKB validate 与实际写入间仍有窗口（F7）。

未知 input/tool、归属中途变化、权限撤销不能以“不断连”为由忽略。增强等待 operation 的 ProviderCapabilities 仍未获生产验证；实时显示 ASR 不自动使这些能力变 true。
