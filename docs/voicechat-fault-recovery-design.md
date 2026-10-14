# D21：当前测试语音的转写与无回答故障设计

- 状态：Gateway/门户恢复及工具回显兼容处理已实现并纳入本次发布；ASR 句首丢失与 function channel 根因尚未闭合，待真实服务复测
- q1003 修复基线：`d0dea88` 开始实施，2026-10-14 同步 `cce8e75` 的用户源码快照；本次提交包含恢复、诊断和握手修复，未执行现场部署
- 唯一测试语音：`Hello, please introduce MONKEY MOONCAKE products.`
- 上层目标：[q1003 目标架构](q1003-target-architecture.md)
- 固定 NVIDIA 源码阅读与实现依据：[上游核对](voicechat-upstream-analysis.md)；已修正探针输入停顿/错误目录并新增 CPU 产物检查，不替代现场根因证据

本文只记录当前测试语音及其同一录音的受控对照，不保留其他语音、旧截图或旧会话的故障描述。一般回归语料和非故障能力验收归 [验收清单](development/validation.md)，不得混入本次根因证据。

## 1. 当前现象与影响

### 1.1 用户可见现象

实际输入：

```text
Hello, please introduce MONKEY MOONCAKE products.
```

门户转写：

```text
o, please introduce MONKEY MOONCAKE products.
```

随后用户结束输入，当前语音会话直接结束，没有企业知识回答，也没有持续可见的恢复提示。

### 1.2 两条独立故障链

| 故障链 | 当前表现 | 直接影响 |
| --- | --- | --- |
| ASR/展示链 | 句首 `Hell` 在门户最终转写中缺失 | 用户原始意图被改变，且错误 final ASR 可能影响工具参数和业务查询 |
| 原生工具/回答链 | 输入结束后没有合法知识工具闭环，随后没有回答 | CueKB 未形成可证明的业务结果；用户看到通话突然结束 |

两条链共享音频/perception 输入，但不能预设同一根因。ASR 丢字不证明工具必然失败，工具未调用也不证明 CueKB 故障。

### 1.3 业务与安全影响

- 当前样例无法完成“语音提问 → 企业知识工具 → CueKB 证据 → 授权口述”的最小业务闭环。
- 放宽 Gateway 让普通 VoiceChat 回答通过，会把漏调用转化为无 CueKB 依据的企业回答风险。
- 继续严格拒绝但不改善门户恢复，会让安全失败表现为无提示断线，用户无法判断是否应该重试。
- 只修转写不修工具链，仍然没有回答；只放行回答不修转写，会把错误输入或无依据内容交付给用户。

## 2. 当前证据边界

### 2.1 已确认事实

- 最新测试语音内容和门户缺失结果如 §1.1 所示。
- 当前录音为 24 kHz、mono、PCM16 WAV，语音内容约 4 秒；同一录音可通过 q1003 已实现的预录 WAV 入口受控重放。
- 同一录音增加前置 500 ms、1 s 零样本后，用户观察到转写仍从 `o` 开始。静音变体只用于判断首包/窗口边界，不是新的测试语音。
- q1003 预录 WAV 入口已经本地验证格式、ready 后实时发送、EOF 和错误不重播；这些结果只证明浏览器发送控制，不证明 VoiceChat ASR、原生工具或实际听音正确。
- Gateway 的安全边界仍应拒绝没有合法原生工具授权的非空企业回答；不得为恢复声音而直接播放未桥接输出。

### 2.2 强证据但尚未闭环

相关受控捕获曾显示：浏览器文件样本已全部进入发送帧，VoiceChat 也接收到对应 PCM；RNNT display 的已累计内容没有在最终 reset 时被再次删掉。这把重点缩小到“可显示累计文本形成之前”，但尚未用 q1003 当前版本的同一组关联 ID 证明首个差异究竟在 soxr、encoder、RNNT raw token 还是 tokenizer/display mapping。

相关 function 捕获曾显示 raw function output 仅有 PAD，没有 SOTC/EOTC，Gateway 因而没有 pending native tool。该证据支持“没有观察到原生工具起始”，但仍需在 q1003 当前 VoiceChat 镜像上确认 function head、权重、special token ID、`combined_embeds` 和 runtime output 契约。

### 2.3 尚未证实

- 当前精确测试是否已经用同一组 `conversation_id`、epoch、provider session、speech client、Triton sequence、`input_item_id`、`response_id` 和 `call_id` 串成单一证据链。
- `Hell` 首次丢失的具体处理阶段。
- function channel 持续 PAD 的根因是 loader/head/权重/tokenizer/input 接线、生成窗口还是其他 runtime 行为。
- 当前通话结束是否由同一 sequence 的 `VOICE_TOOL_REQUIRED` 触发；必须以 Gateway reason code 和 WebSocket close 事件确认。
- 修复后用户是否实际听到完整答案；`accepted`、生成字幕和 `audio.done` 都不能单独证明播放成功。

### 2.4 2026-10-14 新部署的握手阶段断开

用户提供的文件通话日志对应 client `05f9a925`、sequence `3246057663`：14:46:20.094 收到 session.update（instructions 2019 字符、2 工具），20.095 启动 prompt prefill 后即断开；收到音频块、解码输入样本、音频推理次数均为 0。后台 prefill 在约 902 ms 后完成。此序列尚未进入音频推理，不能用于验证 `Hell` 缺失、function PAD 或 CueKB；7 ms 内关闭也不符合 API 8 秒握手超时。

核对固定官方 API 文档发现 session.updated.tools 示例为 JSON 字符串（如 `"[]"`），此前 Adapter 仅接受 list。独立 WS 夹具在 legacy/自定义原生目录均复现送音前 VOICE_PROTOCOL_ERROR；2026-10-14 已本地修复字符串/数组同等严格校验，并增加无正文格式/拒绝字段日志。用户随后补充同一通话的 API WS session 为 `ba50a8c0-a870-4106-8d9a-7546ecbfde30`，门户消息为 `VoiceChat did not confirm the registered tools`。该消息准确命中 Adapter 的 tools 类型/数量检查，说明音频格式校验已通过、instructions/工具内容比较尚未执行。官方字符串格式是已复现的兼容缺陷，但现场究竟是字符串、缺失 tools 或数量不符仍需实际类型/数量确认。API 仅提供 accepted/open，未提供失败阶段日志；不能以此认定 API 修复已经部署。

2026-10-14 发布前同步用户上传的源码快照 `cce8e75`，确认 `docs/voicechat/s2s/audio_server.py:1729/1757` 实际序列化并回显字符串 tools；新增测试从该快照提取真实会话更新方法，在 CPU/本地 WS 上验证兼容握手、后台 prefill 与首帧 PCM 发送。模板渲染和 Triton 为受控夹具，仍不证明 GPU 已通过。

重测入口：发布修复后的 API，创建新通话，确认 `voice_registration_confirmed` → `portal.session.ready` → VoiceChat received_chunks > 0，再继续原样本 ASR/function 定位。此次修复不需要改变模型参数或延长握手期限。

## 3. 目标故障链与判定

若同一 sequence 重现下列顺序，则本次故障链闭合：

```text
当前 WAV 完整发送
→ VoiceChat 完整接收/提交模型
→ RNNT 首个可显示 token 已缺少 Hell
→ function output 持续 PAD，无 SOTC/EOTC
→ Gateway 无 pending native tool
→ VoiceChat 产生普通回答文本或音频
→ Gateway 以 VOICE_TOOL_REQUIRED 拒绝并关闭连接
→ 门户未保留明确恢复提示
```

每一步必须来自相同关联 ID。若实际顺序不同，按首次差异重新分类，不套用该链。

## 4. 复现与采集要求

### 4.1 固定输入

只使用当前录音，并生成三个输入文件：

1. 原始 WAV；
2. 同一 WAV 前置 500 ms 零样本；
3. 同一 WAV 前置 1 s 零样本。

去掉前置静音后，三者的语音 PCM 必须逐字节一致。每个文件至少运行三次，每次创建独立 call，不复用连接状态、`response_id` 或 `call_id`。

### 4.2 统一关联

一次运行必须同时记录：

- Browser call ID、epoch 和文件 hash；
- conversation ID、provider session ID、speech client ID、Triton sequence ID；
- input item ID、response ID、native call ID；
- WebSocket close code、Gateway reason code 和门户最终状态。

跨进程不能用各自 monotonic 时间直接排序；以 ID、发送序号、sample/frame 区间和明确事件先后为准。

### 4.3 有界诊断与隐私

- 普通日志只记录 ID、枚举、长度、计数、hash 和截断状态，不记录录音、完整转写、prompt、工具参数、答案或凭据。
- 当前授权测试 WAV 和原始 artifact 只能写入忽略目录，并设置清理期限；不得提交 Git。
- 高频帧按连续区间汇总，遇到 ASR final/reset、SOTC/EOTC、工具入队、结果写回、拒绝或关闭时刷新。
- 诊断缓冲必须有事件数和字节上限；超限显式标记，不能静默丢失后仍宣称链路完整。

## 5. ASR 句首丢失定位

对同一 sequence 逐层比较：

```text
Browser WAV PCM
→ Gateway 收到的 24 kHz PCM
→ VoiceChat WS 解码 PCM
→ soxr 16 kHz PCM
→ Triton submitted/received PCM
→ encoder window/frame
→ 同帧 RNNT raw token / reference GreedyRNNTInfer
→ tokenizer/display mapping
→ Gateway transcript event
→ Portal final text
```

| 首个差异点 | 允许的修复方向 | 退出证据 |
| --- | --- | --- |
| Browser/Gateway/WS PCM 不一致 | 修复实际丢帧的 Worklet、WebSocket、解析或首包边界 | 样本范围和内容按允许的重采样误差对齐 |
| soxr/submitted/received PCM 不一致 | 修复 resampler 状态、flush、尾部或长度处理 | 相邻阶段样本映射可解释且没有首部缺失 |
| encoder window/frame 不覆盖有效首部 | 修复窗口起点、有效长度、frame index 或重复/遗漏处理 | 每个有效 frame 恰好一次并可映射到输入样本 |
| reference greedy 有 `Hello`、当前路径无 | 修复 RNNT predictor/state、blank/symbol loop 或复用参考实现 | 同帧、同权重、同初态 token 序列一致 |
| raw token 有完整句首、display 无 | 修复 tokenizer、词表或发布游标 | raw token 到 final 文本无首缀损失 |
| 两条等价解码均无 `Hello` | 判定当前模型/encoder 对该样本能力不满足 | 记录失败并进入模型能力 no-go，不做产品词补写 |

禁止通过继续增加固定静音、把 `o` 替换为 `Hello`、按产品名模糊补词、降低无关门槛或调整门户气泡来通过。

## 6. 无回答与 function channel 定位

沿同一 sequence 检查：

```text
实际运行镜像与挂载文件
→ loader/function_head/权重/tokenizer special IDs
→ 有声区间 combined_embeds
→ runtime function_tokens
→ SOTC/EOTC 状态
→ parser/JSON/WS tool event
→ Gateway pending tool
→ ToolDispatcher/BusinessRuntime/CueKB
→ matching call_id function_call_output
→ authorized response/text/audio/playback
```

| 证据 | 故障层 | 下一步 |
| --- | --- | --- |
| runtime 没有 `function_tokens` 或 shape/step 无效 | loader/vLLM/custom output 契约 | 修注册、加载或输出契约，并增加失败测试 |
| runtime 缺 function key，上层保留 PAD | runtime output 契约/转换产物 | 先确认 key 存在；不能归因为模型选择 PAD |
| runtime 有 function key 且有效输入期间持续 PAD | head/权重/tokenizer/input 接线或模型选择 | 核对加载产物及 PAD/SOTC score/rank，再决定是否修代码 |
| 有 SOTC、没有 EOTC | function generation 状态或观察窗口 | 修结束条件、状态维护或完整输入观察期限 |
| 有完整 SOTC…EOTC、没有 tool event | parser、JSON 提取或 WS 队列 | 修一次性交付与有界错误回显 |
| tool event 到达、Gateway 没有 pending tool | 工具目录/schema/调用归属 | 核对当前全局目录版本、注册回显、名字、参数和 response/input 绑定 |
| 工具与同 `call_id` 结果成功、仍无回答 | 结果注入或 response/lease/revision 授权 | 修绑定和后续响应许可 |
| 工具前先产生普通企业回答 | 原生模式未满足强制知识门槛 | 继续安全拒绝；不得把普通回答追认为成功 |

只有 runtime/head/input 契约确认正确后，才允许进行单变量 prompt 对照。工具目录、模型、音频、采样参数和 special ID 必须保持一致；prompt 成功加载不等于模型实际调用工具。

## 7. 门户失败恢复

2026-10-13 已在 `d0dea88` 基线上实现本节的恢复处理。Gateway 在通知错误前尝试交付尚未发送的客户 final ASR，仅保留发送队列及在途事件对应的有界集合；交付前复核 epoch/租约/有效身份，不排空 assistant 文本或音频。终端错误携带递增 `server_seq`，在 Provider 关闭与存储清理前发送；日志记录相同 `trace_id`、reason code、epoch 和响应/输入关联，不记录语音或文本内容。关闭阶段不会被门户收到错误后发起的 DELETE 再次取消，上游关闭与历史审计继续完成。

空音频 delta、没有非空输出的文本/音频结束边界不触发 `VOICE_TOOL_REQUIRED`，也不消耗工具结果后的续答许可；已开始输出中的空白文本分片仍保留，结束 response ID 仍禁止复用。非空未授权回答继续拒绝，策略和原生工具执行路径保持会话快照。Provider 显式结束或流结束产生可恢复 `VOICE_UNAVAILABLE`。

门户收到错误后保留已接收转写、显示稳定 code/message、清除等待/说话状态并释放当前录音和播放。即使只有 WebSocket close 而没有 `portal.error`，也显示持久断线及重试提示；用户主动 End call 不产生该提示。录音不自动重播，final ASR 不自动提交为文字或工具请求。相关本地验证见 [当前验收证据](acceptance-report.md#0-当前审计与证据索引)。这些修复不证明本次真实 ASR/function 故障已解决。

无合法工具时仍保持 fail-closed，但必须改善用户可见结果：

1. 保留当前客户 final ASR，不因连接清理删除。
2. 显示持久错误，明确区分“语音未到达知识工具”“知识检索/回答失败”“播放失败”。
3. 停止当前录音和未授权播放，清理旧 epoch、response 和 pending call。
4. 可以提供由用户确认的重试或文本提交操作；不得自动补词、自动提交 final ASR、自动重试业务任务或播放未桥接音频。
5. 门户错误必须与 Gateway reason code 对应，并允许 `portal.input.state` 等已排队状态先到，不能因事件顺序不同丢失最终错误。

## 8. q1003 实施范围

| 阶段 | 内容 | 当前状态 |
| --- | --- | --- |
| D21-1 | 固定当前 WAV、版本、ID 关联和 artifact manifest | 待执行 |
| D21-2 | 语义移植 main 的有界 PCM/RNNT/function capture 与分析器 | 协议探针与静态产物检查已实现；用户快照已有 WS/backend capture，现场启用、hash 与同 sequence 采集仍待复测，不能整体 cherry-pick |
| D21-3 | 运行原始/500 ms/1 s 同录音对照，确定 ASR 首个差异 | 待真实 VoiceChat/GPU 验证 |
| D21-4 | 只修复证据命中的 ASR 层 | 待 D21-3 |
| D21-5 | 定位并修复 loader/head/token/parser/result 中首个 function 差异 | 待真实 VoiceChat/GPU 验证 |
| D21-6 | 实现门户显式失败恢复 | 已本地实现并通过受控回归；待真实部署 |
| D21-7 | 与 `social_only`/quarantine 集成 | 待目标架构对应实现 |
| D21-8 | 真实 CueKB、工具结果、授权口述和人工听音验收 | 待 D07 |

q1003 已实现预录 WAV 输入和全局版本化工具目录。前者用于稳定复现当前录音；后者用于核对工具定义、启停、刷新和注册回显。两者都不证明 VoiceChat 已正确转写或调用工具。

主线已有的空边界处理、诊断采集和分析能力只能按行为移植。q1003 的 Gateway、Provider、全局工具目录、Runtime、schema 和迁移已经分叉，每项必须先增加 q1003 失败测试，再做最小实现。

## 9. 离线验证方法

离线验证单独报告，不与真实验收混写。

### 9.1 文档与静态验证

- 检查当前测试句是本文唯一的故障语音内容；旧测试句、旧截图文本和旧会话长度不得残留。
- 检查本地链接、锚点、代码路径和 Markdown 格式。
- 审查 diff，确认没有提交 WAV、artifact、日志、密钥或其他业务内容。

### 9.2 受控代码验证

编码阶段按实际影响执行：

- PCM/soxr/frame/RNNT/reference greedy 的确定性单元测试；
- function token PAD/SOTC/EOTC、parser、JSON、队列和 call binding 测试；
- Gateway 无工具、非法工具、结果迟到、断线和 epoch/revision 竞态测试；
- Web 预录文件、final ASR 保留、错误持久显示和不播放未授权 PCM 的测试；
- 相关 Python/Node/Chromium、Ruff、build 和 `git diff --check`。

受控夹具通过不表示真实模型已经识别完整句子、调用工具或播放声音。

## 10. 真实放行门槛

当前录音及其两个静音变体每个至少执行三次，并同时满足：

1. final ASR 完整为 `Hello, please introduce MONKEY MOONCAKE products.`，允许标点/大小写规范化，不允许补词。
2. 任何企业回答前恰好一次合法原生工具事件，工具来自当前注册目录，参数使用完整 final ASR 和服务端 scope。
3. CueKB 返回当前授权范围的真实证据，BusinessRuntime/EvidenceGate 成功，结果以相同 `call_id` 写回。
4. 工具前向浏览器交付的普通回答字幕和 PCM 字节数为 0。
5. 后续回答与相同 input/response/call 关联，浏览器实际播放，人工听到的内容与显示和证据一致。
6. 失败时保留客户转写并显示明确恢复，不静默结束，也不自动改成文本任务。
7. Stop playback、明确取消、连接关闭和旧结果晚到不会污染下一轮。

任一次出现句首缺失、企业输入漏调用、未授权音频泄漏、错误 scope、旧结果写入、虚假引用或无法确认实际播放，都判定 no-go。

若 runtime/head/input 均正确且原生工具仍不能稳定调用，应停止继续用 prompt 掩盖，单独评审启动时固定的 `ASR → BusinessRuntime → controlled TTS` 路径；不得在当前会话内自动 fallback。
