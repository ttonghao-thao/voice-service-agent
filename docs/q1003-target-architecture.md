# q1003 目标架构与故障恢复设计

- 状态：目标设计；Gateway/门户故障恢复已本地实现，完整目标架构尚未实施
- 目标方案原基线：`origin/codex/q1003@56d7774`
- 当前实现核对基线：`origin/codex/q1003@1158760`（2026-10-13）
- VoiceChat 参考：`NVIDIA-NeMo/Speech@097dfe9` 的 `nemotron-labs-voicechat` 分支（2026-10-13 核对）
- 适用范围：英文知识库语音客服、VoiceChat 原生交互式函数调用、CueKB 检索与口述交付

本文是后续编码的单一设计入口，说明业务目标、当前故障、目标架构、实施顺序和离线验证。当前已实现能力仍以代码、[知识执行](qa-routing-design.md) 和 [任务板](TASK_BOARD.md) 为准；Gateway/门户恢复的已实现范围见 [D21 §7](voicechat-fault-recovery-design.md#7-门户失败恢复)。本文中的“目标”“新增”“应”均不表示已经交付或通过真实环境验收。

论文机制与局限已经按 2026-10-01 发布的 v2 重新核对，详见 [论文 v2 原理及 P0–P2 改进](voicechat-paper-v2-review.md)。v2 未取消工具等待不可插话的限制，本文目标能力仍需逐项实现与实测。

## 1. 结论与演进原则

`codex/q1003` 适合作为演进基线，但原因不是“允许模型不调用工具就能解决当前故障”，而是它已经建立了可继续加固的边界：会话级模式/策略快照、全局版本化原生工具目录（初始含两个知识工具）、服务端 ToolDispatcher、DirectKnowledgeExecutor、EvidenceGate，以及一般回答的独立生命周期。

后续演进保留这些边界，并增加两类能力：

1. 对同一段音频做逐阶段、同序列诊断，分别定位首词丢失和函数通道不产出的问题；只修复首个出现差异的处理阶段。
2. 在浏览器收到任何无工具回答前增加确定性的服务端授权。生产默认采用新的 `social_only` 策略：只有狭窄的社交话语可直接回答，企业知识、混合意图和不确定输入必须进入原生知识工具路径；未授权的 VoiceChat 音频先隔离，不能边播放边事后撤回。

该方案可以阻止“漏调用工具后直接口述无依据企业答案”，但不能把模型的概率性工具选择变成协议级强制调用。若修复运行时、函数头、输入和 prompt 后，真实企业语料仍出现漏调用，则原生知识模式不满足放行条件，应另行设计启动时固定的确定性 `ASR → BusinessRuntime → controlled TTS` 路径；不得在运行中自动切换或降级。

## 2. 业务目标

| ID | 目标 | 可验收结果 |
| --- | --- | --- |
| BG-01 | 准确接收英文语音 | 最终 ASR 保留句首和产品词；目标样例必须得到完整 `Hello, please introduce MONKEY MOONCAKE products.` |
| BG-02 | 保持自然全双工交互 | 用户可在正常对话阶段连续说话、停顿和插话；录音与播放可并行，普通 `speech_started` 不自动取消业务任务 |
| BG-03 | 社交话语低延迟响应 | 问候、致谢、告别、确认、请求重复等不需要事实的完整话语，可在 `social_only` 下不访问 CueKB |
| BG-04 | 企业知识有依据 | 产品、公司、政策、规格、售后等企业事实必须通过 q1003 原生知识工具进入服务端 CueKB，并经过证据和引用校验后才能口述 |
| BG-05 | 一次输入只执行一次业务任务 | 最终 ASR 是业务查询权威来源；一个 `input_item_id` 只创建一个有效业务执行，旧 revision/epoch/lease 的结果不能提交 |
| BG-06 | 工具结果和口述严格关联 | 仅相同 `call_id` 的 `function_call_output` 可授权后续响应；实际播放状态与业务提交、生成文本分开记录 |
| BG-07 | 故障显式且可恢复 | 转写保留在门户；无工具、超时、证据不足、协议错误和播放失败显示不同状态，不再静默结束会话 |
| BG-08 | 权限和部署边界不回退 | 模式/策略在会话创建时冻结；KB 范围由服务端固定；浏览器和模型不能覆盖 scope、伪造工具结果或获得管理权限；real 失败不回退 mock |

“一般常识由 VoiceChat 直接回答”不是首阶段生产目标。q1003 的 `general_qa` 能力保留，但必须完成独立的企业知识泄漏评测和风险批准后才可放行。

## 3. 当前基线

### 3.1 已有能力

- `dual_tools` 暴露 `lookup_knowledge` 和 `reason_over_knowledge`，工具名称表达检索与综合的语义差异。
- 会话创建时快照运行模式和 `answer_policy`，不会按单次请求热切换。
- ToolDispatcher 校验原生工具、参数和调用状态；DirectKnowledgeExecutor 将最终 ASR、会话上下文与服务端授权范围合并后执行。
- EvidenceGate、BusinessRuntime 和存储层维护当前轮证据、引用、revision、epoch、lease 与终态。
- provider-general 回答有独立状态，不伪造 CueKB Turn、引用或原生函数调用。

### 3.2 仍存在的结构性缺口

- `general_qa` 可以在“当前输入唯一绑定且无 pending tool”时接受无工具响应，但这只能证明响应归属，不能证明用户输入属于一般常识而非企业知识。
- prompt 和模型原生工具选择只能影响调用概率；当前 WebSocket 协议没有经验证的 `tool_choice=required` 控制，也不能由 API 伪造原生函数调用。
- 当前分支还没有主线后续形成的完整 VoiceChat 输入/函数通道诊断能力，不能仅凭浏览器显示或最终错误判断故障发生层级。
- 工具等待阶段的自然插话仍是未验证能力；目标全双工不能把它误报为已支持。

### 3.3 官方交互式函数调用约束

固定到上述 NVIDIA commit 的 [部署说明](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/voicechat_realtime_instructions/deploy.md#function-calling) 和 [Realtime API](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/voicechat_realtime_instructions/api-reference.md) 明确了以下协议事实：

1. 客户端必须在音频前通过 `session.update.tools` 注册工具；`name` 必填，`description` 虽可选但官方建议提供以提高模型准确性。
2. 只有当**模型判断需要工具**时，服务端才发送 `response.function_call_arguments.done`。官方流程没有声明随机抽签，也没有给出“注册后每轮必调用”的保证。
3. 客户端只能在收到该事件后执行函数，并用原 `call_id` 发送 `conversation.item.create(function_call_output)`；未注册工具时回传结果会得到 `tools_not_set`。
4. 该版本 `session.update` 公开字段只有 audio、instructions 和 tools，没有 `tool_choice=required`。因此工具定义、description、instructions 和业务语义是模型产生调用的必要输入条件，但不是协议级充分条件。

由此，q1003 的非强制工具模式可以满足社交话语不查库、企业知识自主选 `lookup`/`reason` 的交互目标，但只有在服务端播前授权与真实漏调用门槛同时成立时才满足生产安全要求。任何未收到原生函数事件的普通响应都不能被当作“工具其实已经调用”。

## 4. 当前故障与证据边界

本节只保留最新测试语音；详细证据、复现和退出条件统一见 [D21 当前语音故障](voicechat-fault-recovery-design.md)。

### 4.1 用户可见现象

目标输入：

```text
Hello, please introduce MONKEY MOONCAKE products.
```

当前现象：

1. 门户只显示 `o, please introduce MONKEY MOONCAKE products.`，句首 `Hell` 丢失。
2. 用户结束说话后，系统直接结束当前对话，没有业务回答或可恢复提示。

### 4.2 当前最可能的故障链

现有现象与主线后续诊断经验支持以下工作假设，但必须在 q1003 的同一音频序列上复现后才能定案：

1. 句首丢失发生在门户显示之前。若多个不同长度的前置静音版本仍从 `o` 开始，问题更可能位于 VoiceChat 输入重采样、encoder 窗口、RNN-T 解码或 tokenizer/display 映射，而不是前端少拼了四个字符。
2. 用户结束输入后，函数通道可能只产生 PAD，没有合法 SOTC/EOTC，因此 Gateway 没有登记 pending native tool。
3. VoiceChat 随后产生普通文本/音频；严格知识策略把它判定为未桥接响应并关闭连接，于是门户表现为“对话直接结束且无回答”。

第二步和第三步是不同边界：无 SOTC/工具调用是上游函数路径问题；`VOICE_TOOL_REQUIRED` 是服务端对未授权非空输出的安全拒绝。不能把拒绝本身当作工具不产出的根因，也不能为恢复语音而直接放行该输出。

### 4.3 禁止的症状补丁

- 不在前端补写 `Hello`，不按产品名纠正文本，不依赖固定静音长度掩盖窗口问题。
- 不把缺少企业关键词视为“一般问题”，不从 assistant 输出倒推路由。
- 不在没有原生事件时伪造 `function_call`，不把最终 ASR 自动提交成工具调用。
- 不先播放后审查；已被用户听到的无依据内容无法可靠撤回。
- 不把 `accepted`、生成文本或 `audio.done` 当作用户实际听到声音的证据。

## 5. 目标架构

### 5.1 组件和责任

```text
Browser / AudioWorklet
  │ 24 kHz mono PCM16 + playback ACK/control
  ▼
VoiceGateway
  ├─ Input/response identity ledger
  ├─ Response quarantine (bounded)
  ├─ ResponseAuthorizationPolicy
  └─ Native tool lifecycle bridge
         │
         ▼
NvidiaVoiceChatAdapter ── WebSocket ── VoiceChat runtime
                                      ├─ ASR/audio encoder
                                      ├─ language/function heads
                                      └─ SOTC/EOTC parser
         │ response.function_call_arguments.done
         ▼
SessionCoordinator → DirectKnowledgeExecutor → ToolDispatcher
                                              ├─ lookup_knowledge
                                              └─ reason_over_knowledge
                                                       │
                                                       ▼
                                                 ToolRegistry → CueKB
                                                       │
                                                       ▼
                                      BusinessRuntime / EvidenceGate
         │ matching call_id + authorized result
         └──────────────────────────────────────────────► VoiceChat speech
```

边界保持不变：供应商事件只存在于 `app/voice`，Agents SDK 只存在于 `app/agent_runtime`，业务执行仍经过 `SessionCoordinator → BusinessRuntime/DirectKnowledgeExecutor → ToolRegistry → CueKB`。新增授权策略只决定某个 VoiceChat 无工具响应能否交付，不执行检索、不改变 KB scope，也不替代 EvidenceGate。

### 5.2 启动级回答策略

| 策略 | 无工具响应 | 企业知识 | 用途 |
| --- | --- | --- | --- |
| `knowledge_required` | 全部拒绝 | 必须使用原生知识工具 | 最严格回退与对照组 |
| `social_only` | 仅 `SOCIAL_SAFE` 可放行 | 必须使用原生知识工具 | 目标生产默认 |
| `general_qa` | `SOCIAL_SAFE` 及经批准的 `GENERAL_SAFE` 可放行 | 仍必须使用原生知识工具 | 保留能力，完成泄漏评测后才可放行 |

策略在应用启动配置和会话创建时确定，并写入 Conversation 快照。会话中不因延迟、CueKB 健康或模型表现热切换，也不从 real 自动回退 fixture/mock。

### 5.3 确定性 ResponseAuthorizationPolicy

新增纯规则、可审计的 `ResponseAuthorizationPolicy`；它不是额外的 ML/LLM 分类模型。输入仅包括：

- 与响应唯一关联的最终 ASR；
- 有界、已确认的当前会话上下文；
- `conversation_id`、`epoch`、`input_item_id`、`response_id` 和 pending-tool 状态；
- 会话快照中的 `answer_policy`。

输出为：

| 分类 | 定义 | `social_only` 动作 |
| --- | --- | --- |
| `SOCIAL_SAFE` | 不需要外部事实的完整社交行为：问候、致谢、告别、简单确认、请求重复 | 可放行唯一绑定的无工具响应 |
| `GENERAL_SAFE` | 一般常识且不涉及企业/产品/账号/交易/政策；首阶段只记录评测，不放行 | 拒绝或明确提示当前仅支持社交与知识客服 |
| `BUSINESS_REQUIRED` | 企业、产品、规格、政策、故障、售后、价格、库存等需要授权知识的问题 | 必须等待合法知识工具；无工具响应丢弃 |
| `UNSUPPORTED_ACTION` | 本期不支持的执行类请求或范围外业务 | 丢弃无工具响应并返回明确能力边界 |
| `AMBIGUOUS` | 无法安全确定，或社交与业务混合、依赖不充分上下文 | 失败关闭；请求澄清或引导重试 |

决策优先级固定为：

```text
BUSINESS_REQUIRED > AMBIGUOUS > UNSUPPORTED_ACTION > GENERAL_SAFE > SOCIAL_SAFE
```

规则必须匹配完整规范化话语和受控上下文，而不是关键词包含。例如：

- `Hello.` → `SOCIAL_SAFE`
- `Thank you, goodbye.` → `SOCIAL_SAFE`
- `Could you repeat that?` → `SOCIAL_SAFE`
- `Hello, please introduce MONKEY MOONCAKE products.` → `BUSINESS_REQUIRED`
- `Thanks, and what is its shelf life?` → `BUSINESS_REQUIRED`
- `What is the capital of France?` → `GENERAL_SAFE`，但在 `social_only` 中仍不直接放行
- `Order ten boxes for me.` → `UNSUPPORTED_ACTION`

任何规则无法完整覆盖的表达都归为 `AMBIGUOUS`；“没有命中企业词”不能作为一般常识的证据。

### 5.4 无工具响应隔离与授权

VoiceGateway 在收到无 pending tool 的 VoiceChat 文本/音频时，不立即发给浏览器，而是写入有界 quarantine：

1. 按 `epoch + input_item_id + response_id + segment_index` 关联响应，并等待同一输入的最终 ASR。
2. 若随后出现合法 native tool，以工具路径为准；隔离中的前导普通响应不得抢先播放。
3. `social_only + SOCIAL_SAFE` 时放行隔离内容，持久化 provider-only utterance；不得创建伪知识 Turn 或引用。
4. `general_qa + GENERAL_SAFE` 只有在后续风险门槛通过后才可放行，且同样不得携带企业引用。
5. `BUSINESS_REQUIRED`、`AMBIGUOUS`、`UNSUPPORTED_ACTION` 或身份不唯一时，丢弃隔离内容，保留客户转写，并发出明确的恢复事件。
6. quarantine 按每响应字节数、音频时长、段数和等待时间设硬上限；超限失败关闭。连接关闭、epoch 变化和明确取消必须清理。

该机制解决的是“未授权回答不能泄漏到用户”，不是“强迫 VoiceChat 调工具”。若业务输入没有产生合法工具事件，应显示可重试的 `VOICE_TOOL_REQUIRED`/能力提示，而不是静默结束。

### 5.5 原生知识工具路径

1. VoiceChat 识别用户输入，并产生 `lookup_knowledge` 或 `reason_over_knowledge` 的原生函数事件。
2. Gateway 校验工具名、参数边界、`call_id`、当前 epoch/input/response 归属，并登记 pending tool。
3. 服务端最终 ASR 覆盖模型可控的自由查询文本；工具参数只能补充 schema 允许且经校验的信息，不能覆盖 tenant/KB scope。
4. SessionCoordinator 只启动一次有效执行，DirectKnowledgeExecutor/BusinessRuntime 通过 ToolRegistry 访问 CueKB。
5. EvidenceGate 验证当前轮证据、引用和答案；旧 revision/lease、失败或证据不足不得伪装为成功。
6. 只用相同 `call_id` 回传 `function_call_output`。VoiceChat 后续生成的文本/音频继承该授权，Gateway 仍检查 response 生命周期。
7. 门户分别记录业务已提交、语音已生成、音频已发送和浏览器已播放；任一前置状态都不等于“用户已听到”。

### 5.6 全双工、取消和竞态

- 正常对话阶段保持录音和播放并行；VoiceChat 负责自然停顿、让话和普通插话。
- 普通 `speech_started` 只更新输入状态，不取消 BusinessRuntime。
- Stop playback 只清本地/服务端播放队列，不修改业务 revision，不撤销已经提交的答案。
- 明确取消、替换问题、连接代际变化才使旧任务失效；提交前必须复核 epoch、revision、lease 和工具授权。
- 一个响应可包含多个 spoken segment；固定 ACK 只属于 status，不能被持久化为最终 assistant utterance。
- 旧 pending call 必须以结果、取消或连接关闭结清，禁止跨新输入复用 `response_id` 或 `call_id`。
- `native_tool_phase_barge_in` 在真实验证前保持关闭/未承诺。工具等待阶段的插话若不能安全结清旧调用，必须明确限制，而不是宣称全阶段全双工。

工具注册的当前实现已于 2026-10-13 扩展为后台全局多工具目录、版本化刷新、字段说明与回显确认；两个知识工具是初始配置，数量不固定。其统一契约见 [工具目录](interfaces/voice-tools.md)。本次注册加固不表示本页计划的 `social_only`/quarantine 或现场 function channel 故障已经修复。

## 6. 两个当前故障的定位与修复路径

### 6.1 句首 `Hell` 丢失

对同一份 WAV 和同一轮 sequence ID 逐层保存有界诊断：

```text
浏览器原始 PCM
→ Gateway 收到的 24 kHz PCM
→ VoiceChat 接收/soxr 后 PCM
→ encoder window/frame
→ RNN-T raw token（含 reference greedy 对照）
→ tokenizer 文本
→ Gateway transcript event
→ 门户累计文本
```

比较原始样本、前置 500 ms 静音和前置 1 s 静音，每种至少重复三次。修复首个发生差异的层级：

- 若浏览器与 Gateway PCM 不同，修复 AudioWorklet/分片/首包边界。
- 若 VoiceChat 接收前一致、重采样后不同，修复 soxr 状态和 flush。
- 若 PCM 一致但 encoder/RNN-T 丢失，修复窗口/缓存/首帧提交；用 reference greedy 判断是服务调度还是模型解码差异。
- 若 raw token 完整但文本缺失，修复 tokenizer/display 映射。

没有该证据前，不修改门户文本、不加入固定静音、不调 prompt 猜测 ASR。

### 6.2 输入结束后无回答

沿同一 `conversation_id/epoch/input_item_id/response_id/call_id` 检查：

```text
runtime/loader
→ function_head 与权重/token ID
→ combined_embeds
→ function_tokens（PAD/SOTC/EOTC）
→ parser/WS function event
→ Gateway pending tool
→ function_call_output
→ authorized response/audio
```

定位顺序：

1. 确认实际运行镜像、启动参数、prompt 模板和函数 schema 与本次记录一致。
2. 检查函数头是否加载正确，SOTC/EOTC token ID 与 tokenizer/runtime 一致。
3. 记录每帧函数 token 统计；若全为 PAD，问题位于 parser 之前，不能通过 Gateway 放宽授权解决。
4. 若有 SOTC/EOTC 但没有 WebSocket 事件，修复 parser/event 边界。
5. 若 Gateway 收到事件但没有 pending tool，修复工具 schema、参数或生命周期校验，并返回具体 reason code。
6. 若工具执行成功但无回答，检查相同 `call_id` 的结果提交、response 授权和播放 ACK。

q1003 的两个专用工具可以改善模型对工具语义的理解，但它们不构成调用可靠性证明。只有目标语料的真实统计和完整事件链才能决定是否放行。

## 7. 代码影响范围

| 模块 | 目标改动 |
| --- | --- |
| `apps/api/app/voice/response_policy.py` | 新增纯规则分类、优先级、策略矩阵和可审计 reason code |
| `apps/api/app/config.py` | 增加 `social_only` 合法值与生产默认/校验；保持启动级固定 |
| `apps/api/app/voice/gateway.py` | quarantine、身份关联、工具优先、上限、清理和显式恢复事件 |
| `apps/api/app/voice/provider.py` | 有界事件元数据和同序列诊断；不得记录转写、音频内容、工具参数或凭据 |
| `apps/api/app/contracts.py`、`apps/api/app/api/routes.py` | 若新增门户状态/reason code，更新契约与路由；实际变更后重新导出 schema |
| `apps/api/app/storage/models.py`、`store.py` | 复核策略快照、provider-only utterance 和终态持久化；现有 `answer_policy String(32)` 若足够则不做迁移 |
| `apps/api/app/agent_runtime/*` | 保留 q1003 dispatcher/direct/evidence 边界；只按失败测试补最小行为 |
| `config/voice-qa-prompt.txt` | 明确社交/知识工具语义；prompt 不是安全授权边界 |
| `apps/web/src/App.tsx`、`apps/web/src/audio/VoiceClient.ts` | 显示最终 ASR、可恢复错误、播放状态；不得在前端判断业务类型 |
| `scripts/probe_voicechat.py`、`deploy/voicechat/*` | 同序列 WAV/函数 token/manifest 采集和受控分析 |
| `tests/integration/*`、`apps/web/e2e/*` | 策略矩阵、竞态、隔离上限、工具一次性和门户恢复回归 |

主线后续已有的空授权边界、完整 WAV/持续静音探针、捕获分析器及引用修复只能按语义移植：先在 q1003 增加失败测试，再审计 Gateway、Provider、Runtime、schema 和 Alembic 分歧，最后做最小实现。禁止整段 cherry-pick 覆盖 q1003 的双工具与生命周期设计。

## 8. 实施里程碑

| 里程碑 | 交付 | 完成门槛 |
| --- | --- | --- |
| M0 基线冻结 | 从远端 q1003 创建演进分支，记录 commit、契约、迁移头和现有测试结果 | 基线可复现，无混入 main 工作区修改 |
| M1 诊断移植 | 语义移植空事件边界、PCM/WAV 探针、函数 token 与事件关联诊断 | 日志有界且不泄露内容；受控夹具覆盖 |
| M2 同序列复现 | 原始/500 ms/1 s 样本逐层采集；建立首个差异点 | 每种至少三次，ID 和 artifact manifest 完整 |
| M3 ASR 修复 | 只修首个差异层级 | 目标句完整且无前端/产品特例 |
| M4 函数路径修复 | 修复 loader/head/token/parser/prompt 中被证明的故障点 | 企业语料稳定产生合法工具事件；无伪造调用 |
| M5 回答授权 | 实现 `social_only`、ResponseAuthorizationPolicy 和 quarantine | 社交可直答；企业/混合/不确定响应零字节泄漏 |
| M6 门户恢复 | 保留转写，显示分类明确的重试/能力提示和播放状态 | 无静默结束；Stop 与 cancel 语义不混淆 |
| M7 本地验证 | Python/TypeScript/契约/迁移/浏览器夹具与 diff 审计 | 所有相关检查通过，失败如实记录 |
| M8 真实验收 | GPU VoiceChat、真实 CueKB/ACL、浏览器麦克风和人工听音 | D07 对应项目全部有证据 |
| M9 放行决策 | 根据漏调用率、ASR、证据、播放和竞态结果 go/no-go | 任一硬门槛失败即不放行原生知识模式 |

## 9. 离线验证方法

离线验证必须单独报告；它能验证协议、状态机、DSP 和受控推理结果，但不能替代真实 VoiceChat/CueKB/GPU/浏览器听音。

### 9.1 静态、契约与状态机

1. 为策略矩阵逐项测试：`social_only` 只允许完整 `SOCIAL_SAFE`，混合输入优先落到业务/不确定。
2. 构造无工具文本、分段音频、工具迟到、最终 ASR 迟到、超限、取消、断线和 epoch 变化；验证 quarantine 不提前交付且总能清理。
3. 验证一个 `input_item_id` 最多一个有效业务执行，相同 `call_id` 才能授权工具结果；旧 revision/lease 无法提交。
4. 验证 provider-only utterance 不产生知识引用，知识回答必须含本轮有效 EvidenceGate 结果。
5. 若协议/schema 有变化，执行 `PYTHONPATH=apps/api python scripts/export_contracts.py`，确认生成物无漂移。

### 9.2 精确音频复现

准备同一目标录音的三个 24 kHz、mono、PCM16 WAV：原始、前置 500 ms 静音、前置 1 s 静音。每个样本至少运行三次，保存：

- 输入文件 hash、时长、采样率、声道和运行版本；
- 浏览器/Gateway/VoiceChat 接收与重采样后的 PCM hash/长度；
- encoder 窗口、RNN-T raw token、reference greedy token、tokenizer 文本；
- `conversation_id/epoch/input_item_id/response_id/call_id` 时间线；
- function token 中 PAD/SOTC/EOTC 计数和 parser 事件。

分析时只比较同一个 sequence 的相邻阶段，报告首个差异点。若环境没有对应 GPU/runtime，可完成文件与夹具检查，但结果必须标为“未运行真实推理”。

### 9.3 路由与工具矩阵

固定语料至少包括：

| 输入 | `social_only` 期望 |
| --- | --- |
| `Hello.` | 无工具响应可放行 |
| `Thank you, goodbye.` | 无工具响应可放行 |
| `Hello, please introduce MONKEY MOONCAKE products.` | 恰好一次合法知识工具；普通音频零交付 |
| `Thanks, and what is its shelf life?` | 使用已确认上下文后仍须知识工具 |
| `What is the capital of France?` | 不直接放行；返回当前能力提示 |
| `Order ten boxes for me.` | 明确不支持，不执行知识或交易伪操作 |

对 `lookup_knowledge` 和 `reason_over_knowledge` 分别验证：最终 ASR 覆盖自由查询、服务端 scope 不可覆盖、一个 Turn、相同 `call_id` 回传、证据/引用合法、超时/不足/失败状态可区分。

### 9.4 本地回归命令类别

按实际改动执行并记录精确命令和计数：

- API 目标测试与完整 pytest；
- Ruff/类型或静态检查；
- Web 单测、typecheck、build 和相关 Chromium E2E；
- Alembic 空库升级（仅实际迁移变化时）；
- 契约导出一致性（仅协议/schema 变化时）；
- `git diff --check`、敏感内容和非预期文件审计。

不得把 fixture、静态检查或本地浏览器结果写成真实 CueKB、VoiceChat、GPU、云端或人工听音通过。

## 10. 真实验收与放行门槛

真实环境必须使用目标镜像 digest、启动参数、prompt、模型/函数权重和部署 manifest，并完成：

1. 目标产品句最终 ASR 完整以 `Hello` 开头，且整句与实际语音一致。
2. 企业/混合语料在合法工具前向浏览器交付的普通文本/PCM 字节数为 0。
3. 每个业务输入恰好一次合法原生工具事件，CueKB 返回当前授权范围内的真实证据。
4. 只有相同 `call_id` 的结果授权后续语音；引用可追溯到本轮证据。
5. 浏览器实际播放并由人工听到完整回答；生成文本或 `audio.done` 不能代替该证据。
6. 社交语料可在 `social_only` 下不调用 CueKB，并且不会创建伪知识 Turn。
7. 无工具、超时、证据不足、协议错误、断线和播放失败均保留用户转写并提供明确恢复，不静默结束。
8. 普通插话、Stop playback、明确取消、替换问题和旧调用结清符合各自语义；工具等待插话单独报告是否支持。
9. 长会话、并发、PG/Redis、drain、备份/恢复及权限撤销按 D07 完成。

任何企业测试出现漏调用、未授权音频泄漏、错误 scope、旧结果提交、虚假引用或无法确认实际播放，都应判定 no-go，而不是切换到 `general_qa` 掩盖问题。

## 11. 非目标

- 不引入额外分类模型、独立 Nano 服务、子 Agent、新队列或浏览器业务路由。
- 不支持中文、天气、股票、交易下单或正式客户认证。
- 不承诺工具等待阶段的自然插话，除非真实能力验证全部通过。
- 不把 q1003 的现有本地测试、本文档或离线复现当作生产故障已经修复。
- 不改变 VoiceChat、CueKB 独立部署以及服务器固定 KB 范围的边界。
