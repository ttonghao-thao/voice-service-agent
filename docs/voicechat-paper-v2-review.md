# VoiceChat 论文 v2：原理、项目差距与改进设计

研究日期 2026-10-13。本文核读 [arXiv:2609.21967v2](https://arxiv.org/html/2609.21967v2) 正文、图 1/2、表 1–7 和附录 A–D，并逐节比较 v1；当时应用基线为 `codex/q1003@d0dea88`。本文保留模型原理和研究证据，2026-10-15 更新方案引用；当前实施顺序以 [目标方案](q1003-target-architecture.md) 为准，现场结论以 [日期证据](voicechat-validation-20261014.md) 为准，不从论文推定部署行为。

## 1. 版本与来源核验

| 来源 | 本次核对 | 能证明什么 |
| --- | --- | --- |
| [论文版本历史](https://arxiv.org/abs/2609.21967v2) | v1：2026-09-18；v2：2026-10-01 | 论文修订日期，不是模型/镜像发布日期 |
| 论文正文逐节文本对比 | §1–4、§6–7、附录 A–D 相同；§5 主要将 V-Model 改为 VC-Model/NemotronLabs VoiceChat 并补注释，贡献者列表有增加 | 未发现 v2 改变架构、训练、关键数值或工具等待限制；不依据 v2 标号宣称能力升级 |
| [HF VoiceChat 11B](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-VoiceChat-11B/tree/443794ea956ef0065f001967ffd00e77f519cb39) | 本次 API 返回 commit `443794ea956ef0065f001967ffd00e77f519cb39`，模型卡版本 v1.0 | 官方发布产物身份；没有下载约 44 GB 权重或验证现场加载 |
| [固定 HF config.json](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-VoiceChat-11B/blob/443794ea956ef0065f001967ffd00e77f519cb39/config.json) | 音频/回答文本/function 权重 1/1/2；`use_function_head=true` | 发布配置与论文 §2.2 一致；配置 SHA-256 为 `2e0b67b56fefeb4815e436d66ad84f2f94be192b1d543054c810ed239a28cff5` |
| NVIDIA Speech | 固定 `097dfe9e2f55baf653b83035868bdc89849f1b47` | 公开推理实现，不等价于现场 NIM backend |
| VoiceChat-TTS | 核读论文 §2.3、附录 C.2；v2 引用 [20] 仍列匿名在审 V-Model-TTS 稿件 | 本次未定位到可核实的独立官方 VoiceChat-TTS HF 仓库/完整技术报告，不能把主论文对 TTS 的描述冒充已读独立报告 |

下载的 HTML、图、模型卡、配置、章节差异和 hash 位于忽略目录 `artifacts/voicechat-paper/`。这些是公开资料与受控验证产物，不含现场录音。论文陈述、公开源码行为、发布配置、现场实际行为四层分别记录。

## 2. 核心原理：共享声学输入，三个不同输出职责

### 2.1 音频理解与字幕不是同一条解码路径

论文 §2.1 的内部输入为 16 kHz 波形，经 25 ms Hann 窗、10 ms 步长形成 128 维 log-Mel；约 600M 的 24 层 cache-aware FastConformer 将时间降采样 8 倍，每 80 ms 产生一个 1024 维 encoder state。论文配置使用 70 帧左上下文、无右上下文，卷积也为因果结构。该声学左上下文约 5.6 秒，不是整个会话的记忆时长。

同一个 state 分成两路：原始特征送 RNN-T；投影后的特征送 Nano-9B-v2 主干。RNN-T 的两层 640 维 predictor、640 维 joint 和 1024 单元 BPE 加 blank 负责字幕。主干根据声学表示与历史 token 生成回答和工具。论文明确 **RNN-T 的用户转写不作为 LLM 输入**。[§2.1](https://arxiv.org/html/2609.21967v2#S2.SS1)

```text
用户音频 → 共享流式 encoder ┬→ RNN-T → 用户字幕
                          └→ 投影/通道融合 → Nano 主干 ┬→ 回答文本 → 持续式 TTS/codec
                                                     └→ function head → 工具调用
                                                                            ↓
                                                           同 call 结果 → function 上下文
```

因此，字幕缺少 `Hell` 不能单独证明 function head 没听到完整语义；工具参数完整也不能证明字幕正确。共享 encoder 的上游错误可能同时影响两路，RNNT 的活动/静音状态还会影响运行时轮次控制，所以两路也不是完全独立。

本项目在收到原生 tool event 后等待绑定的 final ASR，再以它构成业务请求，是应用额外建立的“一次输入、一次业务请求”边界。它保证业务使用的文本来源可追溯，不能证明转写忠于录音。保持原生调用作为业务入口，不能将 final ASR 自动补发成一个不存在的调用。

### 2.2 function channel 是带训练约束的自回归通道

忽略 batch/投影细节，论文 §2.2 的融合输入可写成：

`x[t] = 1 × audio[t] + 1 × Embed(answer[t−1]) + 2 × Embed(function[t−1])`

同一主干的回答 head 和 function head 分别预测 token。function 的 PAD 是合法输出，代表此时不调用工具；并非每一个输入帧都应该出现 SOTC。

工具状态依次为：PAD → SOTC → 工具 JSON payload → EOTC → 注入外部结果 → EOTR → 继续回答或下一次调用。SOTC/EOTC/EOTR 对应 SPECIAL_20/21/22；可读的 TOOLCALL/TOOL_RESPONSE 标签仍属于 payload。列表格式可表达多个调用，但论文明确同时多工具执行尚不可靠，不能从 JSON 列表推导生产并行能力。[§2.2](https://arxiv.org/html/2609.21967v2#S2.SS2)

训练时工具结果是外部上下文，不计入 function loss；调用及注入区间的回答文本监督为 PAD，用户音频以静音替代。推理通过 fast-decode 快速完成调用，再通过 fast-inject 写回结果；这不是额外部署一个普通 Chat Completions 文本模型。

由此必须分开检查：

- 接口 tools/instructions 是否注册、最终 Jinja 是否渲染及 prefill；
- function head、真实权重、特殊 token 与融合配置是否匹配；
- runtime 是否返回 function key，返回后是 PAD 还是调用边界；
- 完整调用是否被 parser/WS 发布，业务是否收到并绑定；
- 结果是否沿原 call 注入，续答是否通过业务授权。

仅增加 prompt 的 “MUST” 或 SOTC boost 无法替代上述契约核验。公开源码在 key 缺失时保留预填 PAD 的行为，见 [源码分析 §1](voicechat-upstream-analysis.md#1-已确认结论与不能越过的边界)。

### 2.3 全双工靠时间轴与状态，不是仅靠 WebSocket 同时读写

回答文本也在 80 ms 时间轴上训练：BOS 学习开始说话，EOS 学习结束/让话，PAD 学习沉默。SFT 中回答文本目标后移两帧，即 160 ms，function 通道不做该偏移。另有提前打断、附和与噪声增强。故“在一次回调内没收到 function”与“永远不调用”不能混为一谈，也不能要求字幕、工具、文本、音频在同一时刻完成。

推理另用 RNNT blank/non-blank 活动启发式注入 BOS/EOS，弥补学习式轮次控制失败。这里有模型行为和 runtime 规则两层。应用应保留输入、回答、任务、播放各自的状态：用户发声可能是附和；停止播放不自动取消检索；明确改问/取消才改变业务 revision。[§2.1、§4、附录 A.3/B](https://arxiv.org/html/2609.21967v2#A1.SS3)

### 2.4 VoiceChat-TTS 持续运行，但不负责理解用户意图

TTS 为约 778M Gemma 3 decoder 加约 199M 因果 codec，总约 977M。每 80 ms 预测一帧 31 个 RVQ code，利用 MoG head 的约 4–8 次 refinement 减少逐 codebook 的串行预测。codec 内部输出为 22.05 kHz；外部 WS 仍按已确认 API 使用 24 kHz PCM16，不能按论文模型采样率直接改浏览器协议。

TTS 持续消费回答文本、BOS/PAD/中断控制和历史声码；它不自行从用户音频决定何时让话。文本到声码还有一帧对齐偏移，并用字符感知子词表示改善罕见子词发音。3 秒参考音频用于 speaker conditioning，不能当作前置用户静音。[§2.3](https://arxiv.org/html/2609.21967v2#S2.SS3)

对项目有三个结果：token 结束、合成结束、浏览器播放结束要分别核验；模型说过的内容无法靠事后字幕检查撤回；只比较 `speech_text` 不能证明 PCM 与文本相同，更不能证明用户听到了。零样本说话人第四轮的 SECS 从 0.757 降到 0.685，而 WER/MOS 较稳定，表明长会话应分别测身份漂移和可懂度，不能只测首轮声音。

## 3. 训练和实验解释了哪些能力边界

CPT 用 TTS 渲染的伪对话建立语音—语言能力，SFT 再混合保留知识、自然对话、工具和安全数据。SFT 原始采样权重之和为 1.175，归一化后四组约为 46.8%、23.8%、26.0%、3.4%；工具数据也包含闲聊和不调用工具。不能把原始 0.305 直接读成 30.5% 的样本占比。

主干与 TTS 独立训练；主干训练不回传 TTS 梯度。之后冻结 encoder/LLM/各 head/TTS，只训练 RNNT predictor/joint。ASR、工具调用和发声的误差因而需要分开评估。[§3、附录 A](https://arxiv.org/html/2609.21967v2#S3)

function SFT 对 call 内容的 loss 权重为 64，SOTC/EOTC 各 6，EOTR 为 3，PAD 为 0.3；CPT 没有工具正例时仍监督 function 输出 PAD。这些是**训练 loss 系数**，不是推理 sampling/boost 参数。不能将 64 或 6 填入 runtime boost 来“恢复”工具能力。

| v2 报告结果 | 正确解读 | 本项目应验什么 |
| --- | --- | --- |
| FDB3 工具选择 F1 82.5%、参数准确 42.2%、Pass@1 33.0% | 路由能力明显强于参数与完整调用序列；F1 不等于逐例选对率，三项不能相乘推导成功率 | 分别测缺调用/选错、完整请求与关键参数、所有调用序列、证据正确性和口述结果 |
| IFEval 19.3，VoiceBench 平均 55.1 | 有工具能力不意味着严格遵守所有提示词；不是企业事实可靠性证明 | 服务端身份、schema、参数来源、证据和提交/交付校验继续独立存在 |
| 自然接话 TOR 81.5%，延迟 448 ms；打断接管 TOR 100%，延迟 480 ms | 不等于每次都会接话；该打断结果不涵盖工具执行阶段 | 分状态测普通说话、工具等待、注入、续答和播放；ACK 不算业务答案 |
| 附和后 Resume 93% | 简短“uh-huh”通常应继续原回答 | 不以任何 speech_started 取消查询，验证明确改问与附和的区分 |
| ASR 平均 WER：80 ms 9.02%、160 ms 8.28% | chunk 改变有精度/延迟权衡，平均值不定位首词缺失 | 同音频、同模型/初态、同版本做 80/160 ms 单变量对照；单独统计首词/型号/数字/否定词 |
| H100 PCIe 80 GB，4 流，每流每 160 ms chunk p95 118 ms | 单 chunk 推理测量；不是完整请求延迟或并发容量上限；不能每个组件重复相加 | 实测编码、调用抽取、业务执行、注入、TTS 与首个播放样本的各自时间点 |

论文未评估量化；其 BF16 perception/LLM、FP32 TTS 与 Mamba recurrent state 条件不能忽略。80 ms 模型步、160 ms 批次、160 ms 训练文本偏移、TTS 一步偏移和 448 ms 实验延迟属于不同定义，不能直接相加当作系统 SLA。[§5、附录 C](https://arxiv.org/html/2609.21967v2#S5)

## 4. 对当前故障链的修正

早期反馈包含句首缺失、无工具回答、拒绝和无恢复提示。最新现场已确认注册恢复和可见超时；ASR/function/结束边界分别仍待定位。以下机制解释不把旧现象当作当前状态，也不把事件先后当作已证实因果。

| 现象 | 论文/源码支持的判断 | 仍需什么证据 |
| --- | --- | --- |
| final ASR 为 `o, please…` | 可能在采样、encoder、RNNT、词表或发布中产生；不是 LLM 文本输入被裁剪的证明 | 同 sequence 的各层 PCM/帧范围、raw RNNT token 与最终显示 |
| function 只有 PAD | 可能是合法不调用、输入/权重/模板差异，也可能是 runtime key 缺失后的 PAD 占位 | key presence、真实 token、special ID、fusion 数值、加载关键 key、调用观察窗口 |
| 没工具却普通回答 | §7 明确承认会漏调用、编参数或从内部知识回答 | 继续执行授权门槛；不能把一次 prompt 修改视为确定解决 |
| Gateway 拒绝回答 | 在知识强制策略下是预期保护；空边界事件触发拒绝才是本项目已修缺陷 | 验证拒绝的是非空回答，且 final 转写、错误和人工重试能正常交付 |
| 工具等待时能收音/出字幕 | 只证明传输/RNNT 仍活动，不能证明改变了回答条件 | 相同工具结果下用互斥的新语音对照其回答，并核对调用关联与旧结果隔离 |

**v2 §7 和附录 B 仍明确：工具执行期间的输入不用于条件化回答，barge-in 不可用。**“全双工 + 原生工具”不是“所有工具状态都能自由插话”。应用不能通过开放 capability flag 或重新发送 final ASR 绕过模型机制。普通对话是否自然、工具期间能否更正、取消能否安全结清是三个不同验收项。

## 5. 项目改进优先级与退出证据

### P0：确定 function/ASR 首差异，并守住业务输出

1. **发布工件一致性**：固定原始 checkpoint、转换产物、tokenizer、模板、代码和镜像；除 head/output 检查外增加融合配置检查。本轮已编码，见 §6。若发布配置正确、runtime 错，再定位转换或 override；不直接把 function 权重改为 2 试运气。
2. **两条链分别捕获**：ASR 对齐输入样本→encoder→RNNT→display；function 对齐 prefill→融合输入→head/runtime key/token→parser→WS。同 sequence、同初态、单变量；原录音/前置静音只用于边界实验，不做文本补词。
3. **分离模型与应用策略**：保留 Gateway → Coordinator → Runtime → Registry。Prompt 表达意图，服务端判断是否可以交付。当前严格知识验证采用已有 knowledge_required；social_only 推迟评审，输入白名单不能授权任意模型回答，见 [当前选择](q1003-target-architecture.md#2-本阶段选择及推迟的方案)。
4. **隔离晚到工具的竞态**：无授权的 PCM 不能先播再撤回。仅在证实关联/验证等待需要时设计按 response/input/epoch 有界隔离，不以缓冲替代工具可靠性修复。只有合法原生 tool event 才发起业务，不由分类或 final ASR 伪造调用。

退出证据：能指出同一失败样本的首差异层；未授权非空输出零字节交付；错误保留输入且可显式重试。当前本地恢复修复已通过测试，不等于模型首差异已找到。

### P1：优先改善参数来源和工具后续回答

- 论文参数指标明显弱于工具选择。现有 `user_request` 完整请求、型号/版本必填可空和源绑定 TaskContext 应保留；增加口音、品牌首词、数字、否定、跨轮更正的真实用例。schema 合法但无用户来源的值不能当事实；未知/冲突进入澄清。
- 全局工具目录保持所有客户一致，执行权限另验。论文建议会话不超过约五工具，作为质量评测阈值/告警，不擅自硬截断 64 项配置上限，也不按客户隐藏目录。工具名称和 description 必须可区分；不要给大量相近别名然后期望 prompt 保证选择。
- 回填只传完成回答所需的简短 ASCII 事实、条件、版本和限制；已有直查最多三条证据与字节预算不等于 tokenizer 的实际 token 预算。先测结果 token 数对 fast-inject/续答延迟的影响，再确定 token 上限；避免把完整文档或原始大 JSON 回填语音模型。
- ACK 是等待话术，不能当检索成功或答案。现有固定 ACK 校验继续保留。ACK 播放耗时、工具耗时和结果注入可能重叠，别用更长 ACK 掩盖超时。正确的工具回填仍不能保证模型口述准确；保留 presentation 检查及真实听音。

退出证据：分别报告选择、参数、证据、口述的分母与失败原因；与当前版本用相同测试集比较，不能仅报告“接口调用成功”。

### P1：如实呈现等待能力，保护跨会话状态

现有 NVIDIA Adapter 四项等待能力均为 false，继续保持。查询期间提供门户进度、明确停止/取消和再次提问路径；不把字幕继续变化显示成“语音更正已处理”。用 0/5/15 秒工具延迟及短/长返回值覆盖边界，再判断 runtime 是否真的有新能力。

论文约两分钟训练窗口不是硬性两分钟断开协议。当前 105 秒轮换加最多 30 秒宽限可能超过该训练长度，需测试长回答、工具等待与播放尾部；未来优化轮换点时显式考虑 pending call、响应结束、播放 ACK 与用户安静。授权摘要和来源绑定 TaskContext 可跨连接恢复，模型隐藏状态与未确认业务不能自动重放。

### P2：以端到端证据做性能优化

用 `用户真实语音结束 → 原生 call → 业务结果 → 注入结束 → 答案首 PCM → 实际首播放样本` 建立时间点。ASR final 和 ACK 单独计时，不把任何 output_audio.delta 当成最终业务答案开始。跨进程用关联 ID 和明确时钟对齐，不能直接相减不同进程的 monotonic。

先固定 24 kHz/80 ms 外部协议、保留持续静音，再对内部 80/160 ms、并发、结果长度、播放起缓冲做单变量实验。论文中的 118 ms 测量不支持直接提高 `MAX_VOICE_SESSIONS` 或部署量化模型。真实可懂度、首词/型号准确性、p95 与旧结果误播必须一起看。

## 6. 本轮落地、验证与未完成项

扩展 `scripts/inspect_voicechat_artifacts.py`，新增可选 `--voicechat-config`，读取原始/合并前 VoiceChat `model.stt.model` 的融合字段：

- 显式列出配置字段及缺省字段，按固定公开 wrapper 的回退规则推导三通道权重；
- 比较论文的 1/1/2，报告 `matches` / `differs` / `unverified`；没有给文件或把转换后 LLM config 错当原始 config 时不通过；
- function head 关闭按 0 处理；gated/concat 或非法数值不冒充加权求和；
- 只读，不修改权重，不代表 live override、实际加载 head 或 embedding 已验证。

**官方固定 HF 配置实际检查为 `matches`**。所以不能指控官方默认发布遗漏 function 权重；新增检查用于发现现场转换、挂载或配置传递的差异。使用方法见 [部署](deployment.md#voicechat-诊断探针与部署产物核验)。

验证：先以 7 个新增用例复现审计能力缺失，编码后诊断相关 **30 项通过**（14 项产物检查、16 项协议探针），Ruff 和 diff 检查通过。本轮只改独立诊断工具/测试与文档，没有修改应用运行时、契约、默认策略、模型或依赖锁；此前 318 项完整 Python、26 Node 和 18 Chromium 是上一轮证据，不与本轮定向数累加。

论文对比和官方配置检查在 CPU 完成，未下载完整权重、部署镜像或调用真实 GPU/CueKB；原始故障样本、现场运行 hash 和同 sequence 音频推理捕获仍缺。2026-10-14 新增的 [源码快照](voicechat/README.md)已用于握手契约核验，不能替代实际部署 hash 或模型推理验证。本文 P0–P2 是有依据的实施顺序，不是现场故障已修复或生产已放行的声明。
