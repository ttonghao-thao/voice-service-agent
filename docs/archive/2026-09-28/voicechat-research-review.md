> 历史快照：源自应用文档基线 `f498fa4`，2026-10-07 归档；保留当时方案/证据，不能用于推断当前实现。仅相对链接重定位；当前入口见 [文档索引](../../README.md)。

# NemotronLabs VoiceChat 论文、容器与项目优化设计

日期：2026-09-28。状态：**设计待确认，未实施**。本轮只做文献、协议、源码分析及文档整理；不运行 GPU、Docker、真实 VoiceChat/CueKB，不重新转换模型，不修改应用或独立 speech 代码。

2026-10-04 维护说明：本文保留上述日期的研究/源码基线和独立优化建议。当前应用已增加 Q07 原生双工具、D2 证据续答及交付记录；本文当时的单 bridge、口述和交付现状不能替代最新实施状态。当前实现见 [架构](../2026-10-07/architecture.md)、[Q07 §0](../2026-10-07/qa-routing-design.md#0-本轮编码范围与扩展契约)，剩余任务见 [任务板](../../TASK_BOARD.md)。本文未重新核验模型权重、镜像或现场服务。

## 1. 结论与证据基线

继续采用用户已经准备好的 NVIDIA VoiceChat 容器及 Triton Model Repository。保留 `VoiceGateway → SessionCoordinator → BusinessRuntime → ToolRegistry → CueKB` 的业务边界。优化重点是工具等待状态、结果口述可靠性和可测量的实时性能，不是更换语音模型或重建推理栈。

当前最重要的能力差距是：**普通对话全双工成立，工具等待期间自然插话尚不成立**。论文明确披露此限制；固定 ACK、ASR 继续输出或者 WebSocket 持续收音都不等于模型会用新语音改变正在执行的请求。知识库客服频繁调用工具，所以这个限制比通用语音基准排名更影响产品体验。

| 对象 | 本轮证据 | 能够得出的结论 |
| --- | --- | --- |
| 最新论文 | [arXiv:2609.21967v1](https://arxiv.org/abs/2609.21967)，2026-09-18 提交；本轮页面仅列 v1 | 新增了架构、训练、推理与限制的正式说明；不等于权重或现场镜像已经更新 |
| HF 发布 | [模型卡](https://huggingface.co/nvidia/NVIDIA-NemotronLabs-VoiceChat-11B) 标记 v1.0；本轮 API revision 为 `443794ea956ef0065f001967ffd00e77f519cb39`，lastModified 为 2026-09-22 | 这是 HF 仓库快照；仓库更新时间不证明权重文件发生变化，需另比对文件 hash |
| 官方 Speech 分支 | `git ls-remote` 核实 `nemotron-labs-voicechat` 指向 `097dfe9e2f55baf653b83035868bdc89849f1b47` | 与本项目原公开文档基线相同，不应凭“新论文”认定必须升级代码 |
| 本项目 | HEAD `a0bc258`；开始分析时工作区干净 | D19/D20 已存在，不能重复列为待开发 |
| 本机 speech | `/Users/snowking/Documents/speech`；`audio_server.py` hash 为 `0047916176aa09a1b40b32d97f99e339edcce70e1cea80416a732f7bd51e4125` | 与本项目 D20 补丁 manifest 的 after hash 一致；不能代替现场运行文件检查 |
| 本机 GPU backend | `model.py` hash `f48d5b5a59795617552292574a36fb9ff28ff00fe2c6bd54aebb3f37ab450d43`；`data_types.py` hash `cfa0e9a4c8367d306a1167efc0ee13b57b75029092df634eaa2c35ab987cb798` | 本轮推理行为分析的源码基线，不宣称等同任意 NGC latest 镜像 |
| 用户已完成的转换 | 用户确认 HF checkpoint 已生成可加载的 Triton Model Repository | 按已完成前置条件设计；本轮未读取现场模型仓库或执行加载验收 |

“离线部署”在本方案中指本地/私有环境运行。它与官方文档的 offline inference（非交互批量脚本）不同：当前容器是实时流式服务，文件测试也需要按时间推送音频和尾部静音。若要求整个客服系统断开公网运行，还需分别核实文本模型、CueKB 的 embedding/rerank 等依赖，单独部署 VoiceChat 不自动使整条业务链离线。[官方部署说明](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/deploy.md)

## 2. 论文对架构理解的修正

### 2.1 主路径由音频驱动，ASR 是旁路

论文 §2、附录 B 与本机 backend 的 `generate_tokens`、`_llm_step`、RNN-T 路径共同说明：音频经共享 perception encoder 后，一路投影到 Nemotron 主干，另一路进入 RNN-T 转写。主干结合语音表示、前一步回答 token 和 function token，分别生成回答文本与工具调用；回答文本再进入持续运行的 TTS/codec。

因此，不能把系统理解成“先完成 ASR，再把 ASR 文本交给 VoiceChat LLM”。论文中的 STT 命名包含 speech-conditioned LLM，不只是独立转写器。转写与语音理解可以不完全一致：语音模型生成的 `user_request` 和 RNN-T 最终字幕有差异，并不一定是本项目传输丢字。

对本项目的意义：D17 以最终 ASR 作为业务请求与用户气泡的统一文本，是应用主动建立的一致性边界，应保留。但它只能保证“查的内容与展示的文本一致”，不能保证文本等于用户实际说的话。型号、版本、数字、单位、否定词必须独立测量，不能用模型调用成功率代替识别正确率。

### 2.2 学习式轮次控制与运行时兜底共同工作

论文说明 BOS/EOS/PAD 承担开始回答、结束回答和沉默的训练目标。独立训练的持续式 TTS 跟随上游文本和控制信号，自己不负责从用户语音判断轮次。推理时还有 RNN-T 活动/静音启发式兜底。本机 `runtime_setup.py` 与 `_apply_turn_taking_from_rnnt_states` 可见这些阈值。

这支持现行分工：VoiceChat 管自然停顿、让话、附和；应用管理明确停止、取消、改问、权限与旧结果。不能把每个 `speech_started` 变成业务取消，也不能从“模型支持自然插话”推导“应用无需 output epoch 和 pending-call 管理”。论文训练配方是理解能力边界的证据，本期不新增训练或微调任务。

训练上，CPT 建立语音与语言对齐，SFT 混合知识保留、自然对话、工具调用与安全数据；主干与 TTS 分别训练，主干阶段不反向传播到 TTS。稀疏轮次边界和工具 token 被提高损失权重，PAD 则约束何时不输出。其工程含义是：调用时机、沉默与插话不是普通文本 prompt 能完整重造的行为，不能为了“更积极调用工具”同时放宽所有输出授权。训练数据还包含不调用工具的闲聊，本项目“所有完整输入都走 bridge”是更严格的应用策略，需要持续做路由回归。[论文 §3、附录 A](https://arxiv.org/html/2609.21967v1)

### 2.3 工具并行通道与实际执行限制

function channel 与回答文本 channel 分开，有独立的调用/结果边界。它支持工具表示，不意味着并发工具和参数抽取已经生产可靠。推理使用 fast-extract/fast-inject 加速工具 token 生成与结果注入；等待期间运行时插入 ACK。

本机 `model.py` 的快速抽取/注入阶段会跳过相应 sequence 的普通音频处理并返回静音；收到结果时若仍处于 `speaking_ack`，先缓存结果，等 ACK token 注入结束后进入 `process_response`。这里的 ACK token 结束、TTS 合成结束、浏览器播放结束是三个不同时间。

论文 §7 与附录 B 明确指出：工具执行期间新音频可能仍经过 perception/RNN-T，但不用于条件化响应生成，因而不支持该阶段的 barge-in。已完成的 WebSocket 生命周期补丁解决有序归属，不改变这一模型运行时限制。[论文全文](https://arxiv.org/html/2609.21967v1)

### 2.4 基准应如何解读

| 论文结果 | 数值 | 项目含义 |
| --- | --- | --- |
| 自然轮次接管及延迟 | 81.5%，448 ms | 不是每次都正常接话；448 ms 不是知识检索答案的端到端 SLA |
| 用户打断后的接管、延迟、回答质量 | 100%，480 ms，4.33/5 | 该基准不证明工具等待可打断；质量分是指定评测器结果 |
| 简短附和后继续原回答 | 93% | 保留自然附和能力，不因“uh-huh”立即取消查询 |
| FDB 3.0 工具选择 F1 / 参数准确 / Pass@1 | 82.5% / 42.2% / 33.0% | 选对工具远不等于完整任务成功；支持本项目一个 bridge + 服务端参数/权限边界 |
| VoiceBench 平均 / IFEval | 55.1 / 19.3 | 不应撤销独立 BusinessRuntime，提示词也不能替代强制后置条件 |
| ASR 80 / 160 ms chunk 平均 WER | 9.02% / 8.28% | 缩短批次可能牺牲识别；先保留默认 160 ms，再做对照 |
| 单 H100 PCIe 80 GB、4 路流 | 每流每 160 ms chunk 的 p95 推理耗时 118 ms | 仅为指定精度/硬件下的 chunk 性能，不包括本项目检索与播放链路 |

论文将系统简称为 V-Model；模型卡的 smooth-turn TOR 0.82 是论文 81.5% 的近似表达，不构成另一个模型版本。论文也指出约两分钟训练音频上下文、复杂背景语音敏感、长工具结果增加延迟等限制。跨模型比较包含作者报告和不同评测设置，不能用一个总排名代替现场客服数据验收。[论文 §5、§7、附录 C](https://arxiv.org/html/2609.21967v1)

## 3. 容器与 Model Repository 的实际职责

### 3.1 保持现有部署分层

实际路径为：门户音频 → 本项目 VoiceGateway/Adapter → 独立 VoiceChat WebSocket 服务 → Triton Python backend → perception、定制 vLLM 主干、定制 vLLM TTS、codec。工具请求回到本项目，再经 Coordinator/Runtime/Registry 调用 CueKB；简短结果沿相同 call 返回 VoiceChat。

Triton 是有状态 sequence 的调度和组件协作边界，Model Repository 是其加载工件。官方使用定制 vLLM 来支持递增音频表示和多 codebook TTS；这里的 vLLM 不是本项目 `AGENT_BASE_URL` 对接的普通文本 Chat Completions 服务。不能直接升级 pip 的 vLLM、将仓库交给普通 `vllm serve`，或把 VoiceChat 地址填入文本模型配置。

本机转换器 `checkpoint_utils/import_utils.py` 生成的工件包括 `nano-v2-vllm`、`eartts_vllm`、perception/codec/embeddings、RNN-T 与 tokenizer、模型配置及 `config.pbtxt`。这是部署格式转换，不是重新训练，也不代表工具等待、业务授权和口述事实问题已解决。用户已完成转换，本方案不重复执行。[官方转换说明](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/generate-model-repo.md)

### 3.2 音频协议维持现状

外部传输保持 24 kHz、单声道 PCM16 little-endian、80 ms/3840 bytes。服务内部输入 16 kHz，codec 输出 22.05 kHz，再转回 24 kHz。浏览器设备常用 48 kHz，不代表 WS 应发送 48 kHz。静音帧是流式时间轴的一部分，不能为了省流量在用户不讲话时停止推流。[官方 API](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/api-reference.md)

当前 speech 的 `MODEL_STEPS_PER_CALL=2` 同时用于 WebSocket 聚合与 backend：默认两步对应 160 ms。改为 1 必须同步两端且重新验证相邻 EOS/BOS 的音频切分、吞吐和 ASR，不应只改浏览器包长或只改一个 Python 进程。现行 160 ms 播放起缓冲同样应先测 underrun，再考虑缩短。

API 仅实现部分 Realtime 风格事件。未知 session 字段可能被静默忽略；不能凭 API 外形宣称 `tool_choice`、`response.cancel`、动态 `voice` 或任意后台结果注入已支持。论文附录举例的 `ack_message` 单数也不是本项目应直接采用的 wire 字段；现行容器 API/本地实现使用 `ack_messages` 数组，契约以实际版本为准。

### 3.3 工件和运行环境作为一个发布单元

建议记录 HF revision/权重 hash、转换脚本 hash、转换时镜像 digest、Model Repository manifest、运行镜像 digest、backend/WebSocket/template hash 及有效参数。`latest` 只适合查找镜像，不作为验收版本号。修改 WebSocket/Jinja/应用无需重新转换权重；只有工件格式、tokenizer、模型组件等变化才重新评估转换兼容性。

本机离线脚本已将 tokenizer 等依赖打包并设置离线环境变量。现场应验证预热、重启和冷启动均不访问 HF，而不是凭转换成功推定运行时完全离线。保留现场已验证的 GPU、端口、共享内存与挂载；检查 bind mount 是否覆盖镜像文件。只变更镜像/环境后必须重建容器，单纯 restart 不会更新配置。

官方当前要求至少 80 GB 显存、x86_64 Linux，文档给出模型约占 66 GB。剩余显存不能直接换算并发人数，精度、cache、长会话、工具注入和 CUDA Graph 都会影响峰值。保留已验证的混合精度；论文未评估量化版本，不提出直接量化或更换 GPU 的本期任务。[硬件前提](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/prerequisites.md)、[容器部署](https://github.com/NVIDIA-NeMo/Speech/blob/nemotron-labs-voicechat/voicechat_realtime_instructions/deploy.md)

## 4. 当前项目已经实现什么

| 已实现边界 | 当前证据 | 本方案处理 |
| --- | --- | --- |
| 单 bridge，不向语音模型暴露 KB/凭据 | `voice/provider.py`、`BridgeArguments` | 保留，不改成 VoiceChat 直接访问 CueKB |
| 业务知识请求强制检索和后置校验 | `agent_runtime/runtime.py` | 保留，不能因工具基准较好而删除 |
| 工具参数绑定最终 ASR | `voice/gateway.py:bridge` | 保留；增加识别质量与等待时间分析 |
| revision/epoch/租约、取消和旧结果防护 | Coordinator、Gateway、Store | 保留，不以普通发声触发取消 |
| speech 有序事件、逐轮结束和 ACK 授权 | D19/D20、`deploy/voicechat` | 已本地完成，现场发布/听音仍待 D07 |
| Jinja 消除冲突默认路由 | `USE_JINJA_TEMPLATE_PROMPT=1` 分支 | 现有方案已采用；检查最终渲染结果，不再提出重写默认模板 |
| SSE 通知与 final 直接渲染、分段日志 | D20 | 已实现，下一步用现场数据，不重复开发 |
| 105 秒轮换和历史恢复 | config、Gateway watchdog、授权历史 | 已实现；检查恢复内容及边界，不再提出“新增轮换机制” |

## 5. 建议优化及实施顺序

### P0-A：先冻结基础能力，明确工具等待体验

基础交付保留自然对话/普通口述打断；工具等待由可打断的本地播放、明确取消按钮和文字改问提供确定的控制。取消后业务 revision 立即失效，旧 call 必须失败结清或关闭旧连接；不能把旧 call_id 注入新连接。界面根据业务实际状态显示查询中/已取消/完成，不把 ACK 当成检索结果。

如果产品要求“工具正在执行时仍能随口改问并立即生效”，应列为独立的增强能力，而不是调整超时或增加一个 prompt 即可实现。必须先证明等待期间 ASR 完整、取消语义可靠、旧 GPU 状态可结束、新请求可建立。现有快速抽取/注入阶段可能跳过音频，单靠应用读 ASR 还不能保证捕获全部改问。

后续候选路线是增强独立 speech 的工具状态机，保留输入处理并支持明确的取消/新轮次切换，再由现有 Coordinator 管业务失效。它涉及 GPU/runtime 能力，不在本轮默认实施。不要先返回假的 accepted 来解除 pending call：当前 API 没有已验证的任意晚到结果注入通道。

验收：工具延迟 0.2/2/5/10/20/35 秒，分别覆盖无插话、附和、停止播放、按钮取消、文字替换、语音改问。基础与增强结论分别记录；禁止把固定 ACK 或字幕更新计为增强通过。

### P0-B：统一截止时间与 pending call 终态

本项目 `agent_deadline_ms=30000`；最终 ASR 另有最多 5 秒等待。本机 speech `data_types.py` 的请求/响应阈值写成 `10/0.08=125`，注释称 10 秒。但 `parse_function_call_tokens` 每次批量解码只增加一次计数，默认每次 160 ms；按正常节奏估算可能接近 20 秒，而非严格 10 秒。ACK、fast-path 跳步和调度又会影响实际值，所以这里只确认**预算与时间单位不一致风险**，不宣称已测得某个准确超时。

设计使用单调时钟的明确期限：分别记录调用抽取、最终 ASR 等待、业务执行、结果注入/合成。GPU 的有效等待窗口应覆盖从原生调用发出到结果到达的总预算和余量；到期必须产生可识别终态，应用不能继续向已经 reset 的工具状态写成功结果。期限不是简单把某个 env 从 10 改成 30；当前两个服务没有这种自动联动。

实施范围：speech `data_types.py/model.py` 的等待语义、WebSocket 失败映射，本项目 Gateway/Coordinator 的期限传播和 pending 结清。新增配置时同步 `.env.example`、部署契约和文档；若修改协议才导出 schema。优先修复一致性，之后再根据实测决定是否缩短业务总预算。

### P0-C：缩短 ACK 与最终答案之间的额外等待

先记录 call 发出、ACK token 完成、工具结果到达、注入结束、最终首个非静音音频、实际播放开始。ACK 首声不计为有效答案首声。当前“一律查知识库”的 ACK 对问候/澄清也不自然，提示词应与业务实际动作一致。

第一阶段建议只比较更短的固定 ACK，不改变状态机；修改 ACK 必须同步 `BRIDGE_ACK` 和精确识别/授权测试。更进一步的自适应 ACK（快速结果跳过、结果提前到达时安全收尾）属于 speech 状态机调整，需证明不会损坏 function channel、吞掉答案许可或留下 TTS 尾帧。不能仅在浏览器静音 ACK 来宣称注入更快。

工具结果继续只回传简短 `speech_text`、状态和必要语言字段；完整证据/引用留门户。这一点当前已做，不应把长篇 CueKB 命中或完整 display_text 改塞回 VoiceChat。短句长度、数字展开和术语发音以客服样本校准。

### P0-D：补齐“有依据文字答案”到“实际口述正确”的验收

当前后置条件证明有授权检索、有匹配输出许可，并不证明 VoiceChat 对 `speech_text` 的转述没有改数字、丢否定或追加知识。应对每例保存或在授权范围内关联：用户音频、最终 ASR、业务 speech_text、VoiceChat 输出字幕、实际音频以及播放估计；敏感正文/音频不进入普通应用日志。

核心回归是型号、版本、数值/单位、否定、适用条件、无证据、冲突证据、ASCII 降级和工具失败。分别统计输入准确、证据充分、文字正确、口述忠实及播放交付。输出字幕仍不是声学事实，需授权录音与人工听音/独立转写交叉核实。

在线流式播报无法等最终字幕审完再撤回已经播放的错误音频。若业务要求逐字确定性口述，应另评估受控 speech-text 注入/TTS 接口，并验证当前容器是否实际提供；不能假定 prompt 就能实现逐字朗读。本期按英文知识客服保留 VoiceChat，优先完善 Q02 与 D07-C 的口述质量门槛。

### P1-A：在保留模型检索规划前提下优化业务往返

D20 已去除可确认的 SSE/渲染固定等待，但真实 3 秒分解仍未知。先用已有日志测文本模型第一次调用、CueKB、第二次调用、提交和呈现。语音另测最终 ASR 等待与工具注入。各阶段可能并行或包含其他阶段，不能把所有 duration 机械相加。

本机 `runtime_setup.py` 的 RNN-T 静音结束兜底默认 40 个 80 ms 帧，名义为 3.2 秒；普通 barge-in 也有独立兜底阈值。它们不表示每轮固定等 3.2 秒，更不能解释文字入口的 3 秒。应关联最终 ASR 完成、模型原生 BOS/EOS 与兜底触发日志，确认是否真的卡在这一段后再调；缩短阈值必须同时复测句中停顿与附和误打断。

当前 bridge 对问候、听不清和闲聊也调用 Runtime，而 Runtime 会强制 `search_knowledge`。可选优化是在 Runtime 增加极窄、受控的“纯问候/请重复/明确不支持请求”处理；仍走 Coordinator 和统一答案契约，无业务事实时才免检索，不让 VoiceChat 自由回答。此项改变现有“全部请求必检索”的策略，**需作为明确设计变更确认后实施**；混合请求或不确定时仍走原知识路径，不用宽泛关键词猜测绕过。

不默认把两次文本模型调用改为一次，也不取消检索规划。只有现场证实第一次规划是主要瓶颈，且基准证明原问直查不损害追问消解、型号/版本条件和召回时，才单独比较直查候选。禁止按 partial ASR 提前执行并把猜测结果当最终事实。

### P1-B：用正确单位控制音频队列和容量

本机 `audio_server.py` 的 `MAX_QUEUE_SIZE=100` 注释为 1.6 秒，但入站队列每项默认 160 ms，仅按输入音频量计算上限约 16 秒；输出队列混合事件，不能同样换算。入站满时丢最旧 chunk，这可能损害型号/否定词，也可能让延迟数据看似下降。

建议测量队列的音频时长、最旧项年龄、丢帧数和实时系数，按可接受的音频迟滞设定准入/失败策略，不盲目增大队列。持续过载应可见地拒绝新会话或结束异常会话并提示重试，不能悄悄损坏现有请求。保持单会话有序输出和有界缓存，不新增队列服务。

应用默认最多 8 场语音不代表 GPU 已通过 8 路验收。先跑 1/2/4，再测 6/8；包含模型在说话、工具等待、工具注入、长会话与断线恢复。记录每 chunk p95/p99、队列年龄、GPU 峰值、丢帧和有效答案延迟。`config.pbtxt` 的 batching 与 session 数也是不同层次。API 多进程的进程内上限若被扩容放大，应复用现有 Redis/协调边界设计共享准入，而非视每进程 8 路为全局 8 路。

### P1-C：在既有轮换机制内改善长期上下文

105 秒轮换与论文约两分钟训练上下文方向一致，但不是硬保证。当前 watchdog 等待用户安静且没有 running/ready pending，最多另宽限 30 秒；它没有显式以浏览器播放完成作为轮换条件，因此还需验证长回答在临界点是否被截断。

当前恢复取最近 6 条授权历史的尾部 1500 字符，并做 ASCII 过滤；它不是结构化事实摘要。建议恢复内容保留带来源的型号、版本、已确认约束、未决问题，保留角色与明确的数据边界，禁止将历史文本当 system 指令。只恢复当前授权内容，不恢复旧 call_id、旧音频或自动重播旧答案。

实施前检查会话空闲、无业务 pending、输出结束/缓冲清空三项；播放 ACK 只估计客户端进度，不宣称用户已听到。不要简单扩大到 300/600 秒来消除重连，因为容器最大连接时间不是有效语音记忆长度。轮换前后用同一事实追问测试，多次轮换与撤权另测，和 Q01 交付记录设计保持一致。

### P2：需要独立评审的能力升级

真正工具等待全双工、可确定朗读、无缝长期会话、低精度/量化及模型训练均为独立方案。只有 P0/P1 的证据表明现有模型无法达到已确认产品要求，才考虑扩大推理层改动。现阶段不重构为新 ASR→LLM→TTS 服务，不让 VoiceChat 取代 BusinessRuntime，不增加 GPU 或训练任务。

## 6. 阶段验收与交付顺序

以下为建议计划，不表示已执行或已批准编码。路线图状态以 [任务板 Q05](../../TASK_BOARD.md) 为准。

| 阶段 | 工作 | 通过条件 | 主要影响 |
| --- | --- | --- | --- |
| M0 基线 | 固定模型/容器/源码/有效参数，使用现有 Model Repository；原生客户端与门户同音频对照 | 版本可复现；问题可归属输入、推理、业务或播放；冷启动/预热无意外下载 | 部署 manifest、现有探针、D07-A/C |
| M1 可靠性 | P0-A/B/D；明确基础/增强、deadline 和口述证据 | 过期/取消/跨会话结果零误提交零误播；工具失败不能伪装成功；旧 call 全部结清或连接关闭 | speech backend/WebSocket、Gateway/Coordinator、Q02 |
| M2 延迟 | P0-C、P1-A；测量后缩短 ACK 与无效等待 | 同样本正确性不回退；最终答案延迟改善，ACK 不冒充答案；模型规划保留 | provider、Runtime、speech、Q03 |
| M3 稳定性 | P1-B/C；容量、背压、轮换 | 目标并发长跑无持续积压/静默丢帧；轮换不丢关键事实和当前回答 | 音频队列、准入、历史恢复、D07-D/E |
| M4 增强评审 | 仅在确需工具中语音改问时启动 runtime 设计 | 真正新语音被处理，任务失效、响应控制、旧工具结清均可证明 | 独立 speech 与现有协调器 |

功能不变量先冻结：跨权限泄漏为零、旧结果误播为零、错误不伪装成功、只有授权检索才能给知识结论。性能目标应在 M0 同硬件/同样本/同并发测量后冻结 P50/P95/P99，再使用独立样本复测；论文的 448/480/118 ms 均不直接成为本项目 SLA。

最小样本覆盖：英文清晰/口音/远场/回声/背景说话；型号、数字、否定和修正；停顿、附和、普通打断；快慢工具和超时；停止后连续两轮；取消/改问竞态；错误/空命中/证据冲突；105 秒附近及多次轮换；1/2/4/6/8 路与真实 PG/Redis。自然暂停测试与完整语音请求识别测试分开，避免为低延迟牺牲用户没说完时的等待。

## 7. 本轮验证与仍未知事项

本轮完成公开资料查阅、官方分支 hash 与 HF 元数据核实、本项目及本机 speech 相关源码/调用链审阅。最终文档链接、diff 与仅文档变更检查记入验收记录；没有重跑应用测试，也没有模型推理、容器运行、现场网络、声学或真实知识检索证据。

现场仍需记录：GPU 型号与驱动、运行镜像 digest、Model Repository manifest、实际 env、backend/template hash、有效并发及各阶段实测。无需为完成当前设计重复询问这些信息；它们是 M0 部署验收输入。D07 不因本轮分析或用户已完成转换而自动通过。

本方案引用的论文作者为 NVIDIA 团队，arXiv 页面标注 CC BY 4.0；本文为针对本项目的分析与设计，不是论文原文或 NVIDIA 对本项目的性能承诺。官方文档、公开 Speech 代码、本机容器导出代码和现场运行镜像分层记录，不能互相替代。
