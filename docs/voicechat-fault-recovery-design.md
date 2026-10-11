# D21：部分转写与未桥接音频的定位、修复设计

日期：2026-10-11。状态：**必要诊断与探针修复已本地实现、验证；现场根因尚未闭合**。R1–R5 条件修复仍以现场证据为前提，不代表声学或真实业务验收已通过。状态统一维护在 [任务板](TASK_BOARD.md#本轮-d21-现场语音与文字失败排查)，当前接口见 [接入 §3](integration.md#3-voicechat-接入与能力门槛)。

## 1. 目标与范围

用户确认现场原句为 **`HELLO THAO MOONCAKE`**，门户只显示 `T H A O`，随后报 `Voice did not reach the knowledge assistant. Restart voice or use text.`。错误出现时用户是否仍在说话、实际发音方式及原始录音尚未提供，不猜测这些事实。

目标分为三个可独立验证的结果：

1. 定位 `HELLO` 与 `MOONCAKE` 首次缺失的阶段，证明整个输入音频是否进入 VoiceChat、是否被完整处理和发布。
2. 定位首个非空回答音频之前未形成有效 `consult_service_agent` 的原因，区分模型生成、token 识别、工具解析、发送及 Adapter 适配。
3. 在已确认故障层修复，完整用户输入只触发一次业务请求，保留自然让话和现有授权/取消约束，再进行真实闭环验收。

沿用 Browser → VoiceGateway → 独立 VoiceChat 与 SessionCoordinator → BusinessRuntime → ToolRegistry → CueKB 的架构。本轮设计不引入新的 ASR 服务、队列、训练、GPU 服务或正式客户认证，不把最终 ASR 自动改成业务触发器，不伪造工具调用。保持默认英文支持，不根据产品名猜测缺失问题。

## 2. 证据与结论强度

### 2.1 已确认的现场事实

关联：conversation `cc6ebc9f-5b2e-4bd8-bab9-413b783fefa2`，VoiceChat client `567a5110`，Triton sequence `1682276254`。两份日志由用户确认对应；时间均按原日志保留，未把未标时区的时间擅自换算。诊断 `at_ms` 是网关连接内相对时间，不是模型音频时间。

| 网关事件 | 相对时间 | 事实及边界 |
| --- | --- | --- |
| 初始两个 response 结束 | 2708–4311 ms | 两段 final 字幕各为 35 字符，均有 audio.done；只知道长度，不能认定重复内容或固定 ACK |
| input.state speaking，seq 46 | 5273 ms | 用户 input item 为 `3a41b991-adcd-4864-8675-60ca02534269`；不是物理开始说话的可靠时间 |
| transcript.delta，seq 47 | 5276 ms | 非空文本长度 7 |
| transcript.done，seq 48 | 5573 ms | final 仍为 7；截图 `T H A O` 与长度吻合，不是整句完成识别的证明 |
| input.state quiet，seq 49 | 5583 ms | 上游发布的输入状态，不是独立声学端点证明 |
| audio.delta，seq 50 | 5585 ms | 新 response `0e4a1369-6abc-4b80-906a-1f9795962794`；Base64 长度 5120，非空，内容是否有声未知 |

拒绝时 pending 的 running/ready/sent 都为零。当前 Gateway 保留已处理调用的 sent 状态，所以不只是在最后 32 条时间线中未见工具：截至拒绝，网关未登记有效工具调用。尚不能推导供应商绝未生成任何工具 token。

`08:04:30.187` 网关拒绝，`.188` VoiceChat 断连，随后取消推理。没有日志中的推理 timeout/工具 JSON 解析错误。Jinja、工具定义、ACK 元数据和系统提示词加载均有日志；提示词加载不证明模型遵守。此次触发的是非空 audio.delta，排除已发布 `eca87d6` 所修复的空事件误拒绝分支。

### 2.2 本地源码事实，不冒充现场版本证明

本项目审查基线：`eca87d60f200a8e67bf031340ab103945e817406`。独立 speech 源码位于 `/Users/snowking/Documents/speech`，不是本项目 Git checkout。阅读时核对到的 SHA-256：

| speech 相对路径 | 本地 SHA-256 |
| --- | --- |
| `s2s/audio_server.py` | `0047916176aa09a1b40b32d97f99e339edcce70e1cea80416a732f7bd51e4125` |
| `opt/tritonserver/backends/nemotron-voicechat/model.py` | `f48d5b5a59795617552292574a36fb9ff28ff00fe2c6bd54aebb3f37ab450d43` |
| 同目录 `runtime_setup.py` | `0b605e1984e4c244863aaf46ddb6382bffa4f32635dffdeb873555e9570864c9` |
| 同目录 `data_types.py` | `cfa0e9a4c8367d306a1167efc0ee13b57b75029092df634eaa2c35ab987cb798` |

`audio_server.py` 与当前交付清单的 after hash 一致；模型 backend 并不在当前 D19 WebSocket 补丁的更新范围内。日志中的行号不能替代现场文件 hash。

| 层 | 现有实现与影响 |
| --- | --- |
| Browser | `VoiceClient.ts` 在 ready 前丢弃 capture 消息；错误后 stop 停止麦克风。Worklet 采集 24 kHz、1920 samples/80 ms 的帧，不按词截断 |
| 门户 | `App.tsx` 对同 epoch/item 拼接 delta、用 done 原位替换；气泡 CSS 支持换行，没有按字数截断。当前证据不支持优先修改显示逻辑 |
| Gateway | 转写展示独立于业务；合法原生工具到达才启动 bridge，并等待最终 ASR；最终文本才是业务请求；未授权非空音频触发关闭 |
| speech WS | `_dispatch_asr_text` 把模型 `<s>`/`</s>` 映射成 input 状态及转写；`_dispatch_model_output` 通常先处理 ASR/工具，再处理同批 ACK 音频；此前一轮尾帧有独立分支，不能全局交换顺序 |
| RNNT 展示 | `_append_rnnt_display_output` 在 agent BOS 时发布剩余文本、追加 `</s>` 并清空展示缓冲；因此 transcript.done 与助手开口耦合 |
| RNNT 让话 | `generate_tokens` 顺序为 `_llm_step` → RNNT EOU 更新 → `_apply_turn_taking_from_rnnt_states` → RNNT display → TTS。BOS 可能是模型原生输出，也可能是 EOU 逻辑强制写入，两者必须分开记录 |
| 工具 | `_llm_step` 接收 custom `function_tokens`；`parse_function_call_tokens` 识别 `sotc_id/eotc_id`，并解析 `<TOOLCALL>`；有效调用再通过 output_function_text 交给 WS |

本地工具状态开始/完成及 fast extraction 有 INFO 日志，此次现场未出现，支持“工具未进入或未完成”的怀疑。仍需现场源码一致性、完整日志和 token 计数才能排除识别/截取问题。

### 2.3 尚未证实、不得直接作为修复依据的判断

- 用户何时相对 ready 开口；完整语音是否进入浏览器实际发送流。
- RNNT 没识别出两个单词，还是发布门槛、frame 索引、提前清空或断连导致遗漏。
- agent BOS 是否过早，来自原生模型还是强制 EOU；不能直接以 quiet 或 transcript.done 推断用户已结束。
- 未调用工具是否由模板可选措辞造成；也不能仅凭 INFO 缺失断言模型永不支持该调用。
- 本次不同 response ID 排除了“这次命中同 ID 复用检查”，不代表所有生命周期场景已验收。

## 3. 一次复现需要采集的最小证据

下列必要诊断已按本设计实现，字段留在内部日志/探针报告，不属于 OpenAI/VoiceChat/门户协议。在现有模块扩展，没有新建诊断服务；现场使用方法见 [接入 §3.6](integration.md#36-d21完整输入与模型边界诊断)。

### 3.1 关联与隐私

Adapter 读取已有 `session.created.session.id`，记录 conversation/epoch ↔ provider session ID；speech 已以 client ID 记录 sequence ID，形成完整关联。若供应商缺该可选诊断标识，标记 unavailable，不新增握手要求，不按时间猜测并发会话。

记录单调递增事件序号、进程单调相对时间和 UTC 时间；跨进程顺序以关联 ID、发送序号、sample/frame 位置为主，不直接相减各进程 monotonic。任何 sample/frame 字段都必须附采样率与含义，24 kHz 输入样本、16 kHz 模型输入和 decoder step 不可混算。

默认只记录标识、计数、长度、有限枚举和状态。不得直接开启会输出完整 prompt/转写/工具参数的全局 verbose。语音或原文对照只用专门授权测试样本，单独保存在既有忽略的 artifacts 下，设明确清理时间；不得提交录音、用户日志、票据或业务内容。短 ASR 不做公开哈希替代脱敏。

### 3.2 采集点与判别用途

| 位置 | 诊断元数据 | 用于判别 |
| --- | --- | --- |
| VoiceClient/Worklet | ready 时刻、capture/sent/pre-ready-discarded 帧数、mute/track 状态、首末 seq、24 kHz 样本累计 | 开头是否在 ready 前丢弃；断连是否发生在整句发送前 |
| Gateway/Adapter | 浏览器接收帧数、上游发送帧数、队列残留、正常化前事件 type 计数、provider session ID | 音频有没有卡在本项目；是否有未知工具事件类型 |
| speech 输入 | 接收 append 数、解码后样本数、提交 Triton 样本数及剩余队列 | 网络接收不等于已推理，检查尾部是否尚在队列 |
| RNNT | 每阶段 frame/step、累计非 blank 数、display 已发布/累计字符数、gate 状态、reset 原因、reset 前长度 | 识别缺失与发布/清空缺失分离 |
| BOS/EOU | 覆写前后 text token 类别、BOS 来源、覆写前用户/助手状态、silent/nonblank 计数、当次有效门槛 | 原生 BOS 与强制 EOU 区分，避免先置 user_speaking=false 再记录而误判 |
| function channel | raw function token 类别 pad/sotc/eotc/other、计数、step 切片范围、fn_call 前后状态、有效 JSON 数、提取取消原因 | 模型未触发、特殊 token 不匹配、窗口漏读、解析失败、提取被取消分别定位 |
| speech 输出 | 每个 response 首次输出类型、工具解析/入队/实际发送序号、ASR 结束原因、input/call/response 本地关联 | 解析过但未发送、同批次乱序、错误归属 |
| 门户 | 接收 item 的 delta/final 长度、最终 DOM 文本（仅测试断言）、错误/stop 顺序 | 输入完整但显示少字时才进入 UI 修复分支 |

边界日志在 gate、BOU/EOU、BOS、SOTC/EOTC、ASR final/reset、工具入队/写回及拒绝时输出。高频音频只按连续区间汇总，遇到控制事件先刷新汇总，不跨控制边界合并。结构事件环形缓冲上限 256 条、序列化数组最多 64 KiB；超限标记截断和丢弃计数，不静默丢失证据。现有 Gateway 32 条窗口保留，但不再靠它独自证明长句前半段没有事件。speech 运行开关已定义为 `VOICECHAT_DIAGNOSTICS`，按原有环境变量方式在启动时读取。

## 4. 可复现的定位流程与分支判定

### 4.1 固定样本与三组对照

建立人工核对的英文样本 `HELLO THAO MOONCAKE`，记录实际发音、每个词在音频中的位置、开头静音和句尾。不能用自动识别结果反向当参考答案，也不能把输入为文字或 TTS 合成样本的结果当真人发音验收。原现场录音不可得时，明确标记为重新录制复现。

1. **A：独立 VoiceChat 探针。** 以 24 kHz mono PCM16、80 ms 节奏播放同一完整 WAV。未经授权输出只计数/隔离，不播放、不进入业务；探针不因为首帧未桥接音频提前退出，继续观察整段输入和受控尾部静音，区分“永未出工具”与“工具晚到”。这是离线诊断策略，不修改生产 Gateway 的关闭策略。
2. **B：完整门户链。** 在明确 ready 后使用相同样本送入测试浏览器音频源，保留正常 Gateway 授权与断连。对比在同一输入样本位置开始分叉的事件。必须是真实 Browser → API → VoiceChat 链；受控 WS fixture 只能验证 UI，不替代 B。
3. **C：原生客户端对照。** 同版本 VoiceChat、同采样条件和工具配置，观察是否同样只显示中间词。原生客户端直接播放未授权回答不算业务成功。

每组最少重复 3 次，保存逐次结果而非只给平均值。另测试慢速、正常语速、词间短停顿及拼读 THAO；记录间隔，不能预先宣称任意长停顿都是同一输入。B 的 ready 前开口只作为单独负例，不与 ready 后正例混算。

旧 `probe_voicechat.py` 在 WAV 发完后等待工具时没有持续送静音，污染需要持续音频驱动解码的诊断。已修复为单一输入发送循环：完整 WAV 后继续发送 80 ms 静音直到观察期限，工具结果经同一写入路径提交；监听、发送、结果任务共享取消/关闭流程。保留合成工具结果标记，不使用真实业务数据。

已实现 `--diagnostic`：WAV 完成后默认继续观察 10 秒，连接握手与观察共享 `--total-timeout`，最大 60 秒，另有最多 2 秒关闭宽限；输入和尾部观察超过预算在启动前拒绝。期限为可调整的探针参数，不是生产让话规则。退出记录 input_complete/tool_seen/exit_reason（observation_complete/tool_timeout/total_timeout/peer_closed/error）及所有未发送样本；握手慢或对端断开也可能使输入未发完，不能用于判断整句 ASR 准确性。样本计数标明 24 kHz 与末帧补零，synthetic tool result 永远不记为 CueKB 闭环通过。

### 4.2 判定表

| 证据 | 定位结论 | 修复分支 |
| --- | --- | --- |
| HELLO 所在样本未过 ready 或 capture/sent 数不匹配 | 收音/发送阶段缺失，先于 ASR | R1 |
| 完整 WAV 已送入 Triton，RNNT 累计文本始终缺 HELLO/MOONCAKE | 识别、frame 选择或展示 decoder 的问题 | R2a |
| RNNT 累计包含完整片段，但 published 长度变短、门槛阻塞或 reset 丢失 | 展示发布/缓冲缺陷 | R2b |
| 尚有明确后续用户语音时，agent BOS 导致 final/reset | 结束时序提前；区分原生 BOS/强制 EOU | R2c |
| A 最终完整，B 在未桥接首音频关闭后缺尾部 | 断连放大了上游未桥接问题；不将其单独归咎于 UI | R3/R4，同时验证 R2 |
| raw function 无 SOTC，模型先生成回答 | 工具触发行为不符合当前产品要求 | R3a |
| raw 已有 SOTC，但 parser 没进入状态或未完成 | token 映射、step 范围、解析/提取问题 | R3b |
| output_function_text 有效，工具事件却晚于同一工具 ACK 首帧或 ID 错误 | WS 事件归属/发送缺陷 | R4 |
| Gateway 收到完整 final，DOM 仍只有中间词 | 前端归并/渲染问题 | R5 |

允许多个分支同时成立；修复最早丢失点后重新运行整条链。每项根因结论必须附同样本的修复前证据、具体函数/条件、修复后差异，不能以“新增日志已完成”代替根因闭合。

## 5. 修复设计与必须保持的约束

### 5.1 R1：采集或发送缺失

若 ready 前开口，继续维持不发送未就绪音频的边界；检查 UI ready 提示是否与 `portal.session.ready` 一致，并明确连接中不能开始提问。不得引入无界预录音、自动重放旧问题或将旧 epoch 帧投递给新会话。若 ready 后仍丢帧，按 capture → client seq → API queue → speech sample 计数找到首个断点，修复该位置并保留背压和 seq 校验。

### 5.2 R2：完整转写与用户输入结束

**优先复用现有 VoiceChat/RNNT 让话能力，不在 API 或前端新增定时 VAD。** 仅凭本地 BOS/reset 代码不能直接删除 reset 或增加固定等待时间。

- R2a：先核对每个有效 encoder frame 是否恰好处理一次、滚动窗口索引是否漏帧、RNNT 词表与已部署模型一致、独立 display predictor 的推进是否与既有实现一致。找到具体差异才改对应索引/解码逻辑；不得通过硬编码 THAO/MOONCAKE 替换或补词通过用例。
- R2b：以同一 input 的累计解码快照为 final 权威；发布游标、标点状态与缓冲生命周期一致。清空前必须确认完整快照已移交输出队列；gate 尚未打开不等于没有识别文本。不得将历史输入和下一次输入拼成一条，也不得按文本相等去重不同输入。
- R2c：在已有 turn-taking 决策点保留覆写前状态与明确边界原因。若证据确认原生/强制 BOS 在用户仍发声时提前结束，修复该决策或展示边界耦合；**BOS 本身不能作为“整句完整识别”的充分条件**。在有效输入结束决策与对应帧处理完成前，禁止发送该 input 的最终结束事件或清空其展示缓冲。保留已经确认的自然插话规则，不能把每次 speech_started 当作业务取消。

R2c 的内部状态要求（拟定，非新线上字段）：

| 输入状态 | 允许动作 | 不允许动作 |
| --- | --- | --- |
| 未打开 | 有效转写开始时分配 input item | 仅因助手输出创建客户业务请求 |
| 收集中 | 对同 item 发布增量，保存完整累计文本与已处理帧位置 | 仅因 agent BOS 清空；把尚未确认的片段标 final |
| 边界待提交 | 已有明确输入结束证据；先处理该边界内已接收帧，再形成 final 快照 | 在尾帧仍未解码时先发布 final；把边界后的新输入并入旧 item |
| 已结束 | final 至多一次、speech_stopped 至多一次；下一次输入使用新 item | 修改已被业务消费的 final；静默补写旧 Turn |
| 已中断/关闭 | 标记不完整，释放缓存；遵守旧连接关闭路径 | 将关闭时的部分文本伪装成成功完成并启动业务 |

边界证据应来自经验证的现有模型/RNNT 输入结束决策，具体到函数、帧和状态；不能把 quiet、固定字符数或“过了几秒”作为替代。若本版本无法提供可靠证据，R2c 停在能力缺口，不选择任意等待阈值发布。若需要调整已有门槛，需同样本语速/停顿对照及多批次时基验证，单独记录参数变更与回退值。

改动必须同时核对合法工具先到/final 后到：当前 Gateway 等 final 最多 5 秒。不为了隐藏错误直接延长；若模型在输入尚未结束时提前提出工具请求，应定位其触发阶段。若真实正确句子仍需改变等待契约，另列明确预算变更及长句验收后再实施。

### 5.3 R3：原生工具必须真正生成并解析

- R3a：只在证明 raw function channel 从未触发时，做最小模板对照。保留现有工具标记/参数形状，收敛“if required/if you decide”等可选措辞与应用的每次桥接要求，分别测试问候、产品名、知识问题、听不清与闲聊。不得发送未证实的 `tool_choice=required` 协议字段。模板对照有效才更新运行模板和交付清单，不仅看提示词日志。
- R3b：核对实际 tokenizer 对工具起止 special token 的解析、custom function_tokens 的来源、generated_function_tokens 的写入与读取 step 范围，以及 fast extraction 的取消/一次性交付。修复后合法 JSON 只发一次，非法 JSON 明确失败，不吞异常或从语音正文猜工具调用。
- 工具参数继续通过 BridgeArguments 校验，最终用户请求仍取完整 final ASR。拼写或产品含义不清交业务澄清，不能拿工具参数臆测补齐 ASR。
- 若完整输入与 token 通道均正常，而模型仍不稳定调用工具，则当前模型/模板组合未满足本期原生 bridge 能力门槛。记录失败并暂停放行；不得在本轮静默切换成“ASR 到达即调用业务”或伪造 call_id。此类确定性编排属于另一个架构变更，需要单独设计批准。

### 5.4 R4：工具、ACK 与音频顺序

保持现有有界 FIFO 与单发送器。工具/ACK 的 response ID 在事件生成时固定；同一工具的 ASR 绑定信息与原生工具事件必须先于 ACK 的首帧音频发送。旧 response 尾帧/结束必须仍归旧 ID，不能为了“工具优先”把旧音频移到新工具名下。

同批次修复只依据明确模型边界；跨批次工具晚到时先确认音频究竟属于此前回答、工具 ACK 还是未经桥接的新回答。未获确认的归属不可通过在 Gateway 暂存几秒然后追认来处理。无合法工具的非空输出仍拒绝；不以音量小/全零或字幕像 ACK 自动授权。

工具成功与业务授权仍是两个阶段：工具绑定只允许既有固定 ACK 语义；有效业务结果提交最多授予一次后续回答许可。取消/revision/epoch/租约检查、旧 pending call 结清/关连接、Stop playback 与任务失效分离全部保留。

### 5.5 R5：门户与失败恢复

只有 Gateway 已交付完整 final 而 DOM 缺字才修改文字归并；沿用 `(epoch,input_item_id)`，不得依据最新输入或内容相等合并。done 后收到非法同 item 增量应记录协议异常，不靠 UI 自动拼接掩盖上游生命周期问题。

错误后的已识别文字继续保留；不将 `Request not accepted` 理解为 ASR 已完整。可在确认编码范围时改善恢复提示，说明本次语音未完成业务受理、允许用户改用现有文字输入重新提交；不自动补全原句、不自动重试查询。UI 文案优化不是本故障成功条件。

## 6. 文件与发布影响

| 文件/模块 | 必要或条件性改动 | 验证要求 |
| --- | --- | --- |
| `scripts/probe_voicechat.py` | 必要：完整输入/持续静音/观察期限/结果任务生命周期；记录新元数据 | 受控假时钟/Adapter 验证，无真实调用 |
| `apps/api/app/voice/provider.py`、`gateway.py` | 必要：关联 provider session、发送样本与事件类型证据；授权保持 | contract + integration，异常/取消/资源上限 |
| speech `s2s/audio_server.py` | 必要诊断；R2 发布或 R4 顺序修复按证据实施 | 扩展现有 `s2s/tests/test_response_lifecycle.py` |
| speech backend `model.py`、`data_types.py` | 必要的边界/token 诊断；R2/R3b 才改运行行为与状态 | CPU 受控状态测试 + 现场相同 WAV/GPU 对照，缺一不宣称声学通过 |
| speech backend `runtime_setup.py` | 条件性诊断开关/原有参数；不默认改让话门槛 | 配置语义、单位、启动生效与回退记录 |
| speech `s2s/prompt_template.jinja`、本项目 `config/voice-prompt.txt` | 仅 R3a 对照有效时调整 | prompt 渲染契约 + 真实原生调用覆盖 |
| `apps/web/src/audio/VoiceClient.ts`、`public/audio-worklet.js` | 必要的受控采集证据，R1 成立才改行为 | `apps/web/tests/worklet.test.mjs`、浏览器音频测试 |
| `apps/web/src/App.tsx`、`tests/e2e/voice-audio.spec.ts` | 必要：console capture summary 与 DOM/断连受控回归；R5 才改渲染行为 | 完整句/断连/相同内容不同 item 的 DOM 断言 |
| `deploy/voicechat/` 补丁、manifest、README、Dockerfile | 编码结束后按实际涉及文件更新交付，不手填未验证 hash | 原基线 apply-check、应用后 hash；保留旧发布可回退 |
| 主题文档、任务板、验收记录 | 分别记录设计、状态与实际证据 | 相对链接、diff、未验证项 |

本轮设计不改变外部 schema，不新增数据库字段/迁移；诊断字段留在日志/探针报告，不泄漏到业务 domain。若后续需要线上事件新字段或修改 schema，必须先修订设计及 contracts、运行 export_contracts，再更新前后端回归，不能借诊断直接加入协议。

发布范围由根因分支决定：API 诊断需更新 API 镜像；浏览器采集改动需 Web；WS/模板改动更新独立 speech 文件；**若改 model.py/runtime_setup.py/data_types.py，现有仅 COPY WebSocket 文件的 Dockerfile 不够，须增加 backend 文件交付及重新加载服务的步骤**。默认不重新训练/转换权重；需要改变 tokenizer/模型产物时另列发布门槛。核对挂载覆盖、源码 hash 和已加载进程，沿用已有端口/GPU/模型卷配置，不在编码机执行 Docker。

## 7. 实施顺序与退出条件

1. **设计确认后先补最小诊断和探针**：不改变生产授权/让话语义；本地受控验证字段、上限、隐私、发送完整性和关闭。完成不等于故障修复。
2. **现场固定样本对照**：确认实际版本并执行 A/B/C，保存完整元数据、人工标注及首次差异；形成 R1–R5 根因条目。没有数据的分支保持未验证，不靠推测同时改所有层。
3. **只实现证据命中的修复**：先写能暴露原问题的用例，再改对应模块；涉及模型边界先证明自然插话、短暂停顿和工具等待无回归。增补详细的内部字段/边界决定规则后再改该分支，不能让实现者自行猜测声学阈值。
4. **本地验证与补丁交付**：按 §6 运行受影响测试、全量 Python、静态检查；涉及 Web 才增加 npm/build/浏览器；审查补丁、清单和部署步骤。
5. **现场复测与放行**：同音频、同版本对照证明先前差异消失，再跑真实 CueKB 与听音。失败保留证据、回退对应发布，不扩大模型权限或在运行时自动切模式。

2026-10-11 用户确认编码后，已完成第 1 项以及第 4 项的本地受控验证与补丁交付：探针、浏览器/API 采集元数据、独立 speech WS/backend 边界诊断和发布路径。`VOICECHAT_DIAGNOSTICS` 在 WS/backend 模块导入时读取，默认关闭，派生诊断镜像默认开启；关闭不改变模型行为。第 2、3、5 项仍需现场同样本证据，R1–R5 运行行为修复没有凭推测实施。详细验证归 [验收](acceptance-report.md)，本地交付不等于现场故障解决。

## 8. 验收矩阵

| 用例 | 必须满足的结果 | 证据层 |
| --- | --- | --- |
| `HELLO THAO MOONCAKE` ready 后正常说完，至少 3 次 | HELLO、THAO、MOONCAKE 三部分均保留；大小写/标点可归一，`T H A O` 与 THAO 的比较规则预先注明，不能靠补词通过；同输入一条完整气泡 | 真实音频 + ASR + DOM |
| 同句正常/慢速/短停顿/拼读 | 无已确认继续发声时的提前 final/reset；记录自然句边界和延迟，不能为完整性无限等待 | 真实模型/RNNT |
| ready 前开口负例 | 明确记录未发送区间，不把它归类为 ASR 漏字；ready 后重说正常 | Browser + API |
| 模型原生 BOS、强制 EOU BOS、ACK BOS、重复 BOS | 区分来源；用户 final/reset 与实际输入边界一致；旧响应尾帧不混入新轮 | CPU 状态回归 + GPU |
| 完整问题/问候/闲聊/不清楚输入 | 每个有效完整输入先有合法原生工具，后有 ACK/答案；不清楚交业务澄清；没有直接常识回答 | 真实 native tool |
| 工具先到/final 后到、final 先到、重复 call | 最终 ASR 权威、一个业务 Turn、一次查询；现有 5 秒等待路径及错误语义可解释 | integration |
| 无工具、非法 JSON、unknown tool、跨 response ACK | 明确失败且零未授权音频交付，日志指出所处阶段 | contract + WS |
| 空事件、重复 audio.done、快工具结果/慢 ACK | 继续保留 eca87d6 的空事件行为与一次后续许可，避免回归 | integration |
| 相同文字两次说、真正两轮输入 | 不因文本相同去重，不合并不同 item，不关联到最近一条猜测输入 | WS + DOM |
| Stop playback、取消/替换、断线、旧 epoch 晚到 | 播放和业务失效分离，旧结果不写回，不把残留录音自动重放 | integration + 现场 |
| 工具等待期间继续说话 | 单独记录增强能力，不假称当前模型支持；不能影响基础路径放行标准或伪称新问题已处理 | D07 增强项 |
| 诊断上限/恶意大标识/无关字段 | 日志有界、截断可见、无正文/PCM/密钥；退出后任务和队列清理 | CPU |

通过定位不等于解决：单独“看到了工具”不证明完整输入；完整字幕不证明 CueKB 已执行；工具写回/audio.done 不证明用户听到正确内容。最终放行须同时具备完整输入、正确业务关联、受控语音授权、真实检索结果与听音证据。若任一项失败，任务板保持现场未解决。
