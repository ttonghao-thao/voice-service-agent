# 当前任务板

更新：2026-10-13。目标设计见 [architecture.md](architecture.md)，状态证据见 [acceptance-report.md](acceptance-report.md)。本期范围已确定为英文知识库客服，默认只启用 `search_knowledge`。

## 1. 当前结论与审计基线

当前主链保持：门户 → SessionCoordinator → BusinessRuntime → ToolRegistry → CueKB；语音经 VoiceGateway/Adapter 桥接独立 VoiceChat。D01–D06、D08–D10、D13–D20、E01–E03 的编码与本地验证构成当前交付；D07 真实语音、知识、数据库和容器验收仍未完成。现行身份为每通话独立 capability，KB 范围由服务端确定，Web 标准 HTTPS 8087，API 不映射宿主端口。

D19/D20 对照同次录像/日志、speech 源码与原生 HTML 确认：原生模型有自然插话和停顿能力，HTML 不依赖 response ID 播放。当前门户缺陷包含自身 Stop playback 状态及 ACK 授权竞态；严格旧音频拒绝还需要 WebSocket 有序归属，不能把缺失音频边界在 adapter 中凭字幕猜出。现有 speech 基线更新 WebSocket 层并使用已有 Jinja 开关，不改模型/离线转换，默认工具模板恢复原样。说明见 [speech 更新清单](../deploy/voicechat/README.md)。实际 ASR 不准仍须录音/GPU 对照。

查询约 3 秒的分段占比尚无现场证据。本轮去除同进程 SSE 300 ms 轮询等待与 final 后已有气泡的额外 GET，补齐逐次模型、CueKB、Agent、提交耗时；保留模型检索规划和证据校验。详见 [查询延迟](integration.md#41-查询延迟与优化边界)。

D16–D18 的知识工具后置条件、最终 ASR 输入绑定、文字总预算和答案终态修复继续保留；设计已整体更新为当前流程，历史逐轮问题与验证只保存在 [验收记录](acceptance-report.md)。2026-09-22 的 `9b44859` 仅为历史文档审计基线。

尚有独立边界：`parent_task_id` 不是子 Agent 调度，`accepted` 不代表已听到，引用存在不证明语义充分，原件查看尚未实现；这些属于 Q 系列建议，不把它们自动扩张为本次交付范围。

## 本轮 D21 现场语音与文字失败排查

2026-10-10 用户明确两次独立请求：语音门户报 `VOICE_TOOL_REQUIRED`，文字请求后台报 `agent_run_failed`。分别按各自 conversation/turn 关联，不将两者认定为同一异常。调用链与排查入口见 [接入 §3.5](integration.md#35-d21语音桥接失败排查) 和 [接入 §4.2](integration.md#42-d21文字请求失败排查)。

1. M1（完成）：核对 Gateway、provider、Runtime、Coordinator 与部署边界；确认语音报错为未授权 response 拒绝，文字走独立文本模型与 CueKB 链路。
2. M2（本地完成）：未桥接口述回归用例允许 `portal.input.state` 先于 `portal.error`；已纳入本轮语音交付并通过全量受控回归，保持生产授权逻辑。
3. M3（文字根因已确认；语音待现场证据）：用户确认文字 `NotFoundError` 来自 LLM 配置；后续文字引用校验失败见 D22。语音仍待核对运行文件/模板及 native tool/response 顺序。
4. M4（待现场复测）：文字引用校验按 D22 实施；语音不绕过桥接授权。现场 GPU、模型/CueKB、容器及听音归 D07，不由本地测试替代。

2026-10-11 语音编码续项（已交付 eca87d6）：

1. M5（分析完成）：现场日志确认 Jinja 与工具配置已生效；新 response 未获授权导致关闭，尚缺具体事件类型和工具到达顺序。源码确认空字幕/孤立 audio.done 也参与授权，可误关闭或消耗后续许可；这是独立可复现缺陷，尚未确认为本次现场根因。
2. M6（本地完成）：忽略未授权且未抑制 response 的无内容边界事件，保留非空文字/PCM 的严格授权；增加有界事件元数据诊断和探针关联字段，不记录正文、参数或音频。
3. M7（本地验证完成）：先复现孤立 audio.done 导致旧代码误关闭，再验证修复；全量 pytest **144 passed**，Ruff、diff 检查通过。接入/验收文档已同步，交付分支 main；真实 VoiceChat 事件顺序与听音仍待现场复测。

2026-10-11 现场复测后的定位与修复设计（先完成文档，随后获编码授权）：

1. M8（设计完成）：对齐七字符转写、agent BOS、工具状态与首帧未授权音频；核对本地 RNNT/工具/传输/门户完整调用链，确认原句为 `HELLO THAO MOONCAKE`，现场根因仍需完整音频和边界证据。
2. M9（设计完成）：[D21 定位与修复设计](voicechat-fault-recovery-design.md) 给出三组同样本对照、最小有界诊断、R1–R5 判定与条件修复、状态/取消约束、文件/发布清单和验收矩阵。
3. M10（文档完成）：设计阶段只改 Markdown 并同步接入索引与验收记录；随后用户确认实施必要诊断。根因未闭合，不能将 M8–M10 完成写成故障已解决。

2026-10-11 用户确认设计后的实施：

1. M11（本地完成）：探针完整输入后持续静音，握手/观察有界，合成结果与音频单写入，退出记录未发送样本并清理任务；API/provider 和浏览器增加采集/转发/事件元数据，保留生产授权。
2. M12（本地完成）：speech WS、RNNT/BOS、raw function 与 decode 边界诊断；增量补丁/6 文件 before-after hash、原基线应用和应用后 CPU 回归通过；Dockerfile 已交付三个 backend 文件及启动级开关，未执行 Docker/GPU。
3. M13（本地验证完成）：pytest **157 passed**、Ruff、Web **14 passed**、构建、Chromium 受控链 **9 passed**；独立 speech 及应用后补丁 **22 CPU passed**。文档同步，main 交付；现场固定音频/GPU/CueKB/听音仍归 D07，R1–R5 行为修复仅在证据命中后实施。当前故障仍未通过现场验收。

2026-10-11 补齐 WS/backend 日志后的分析顺序（仅分析与文档）：

1. M14（源码审计完成）：先查工具能力的真实执行路径。NeMo `use_function_head` 缺省为 true，但当前 Triton 通过自定义 vLLM 获取 `function_tokens`；本机未包含实际 vLLM head 实现。现场必须核对实际模块、模型配置、function head 权重和 tokenizer，不能以 `tools=1` 或 Jinja 开关代替能力证明。
2. M15（分析计划完成）：核对 Browser → API → VoiceChat → Triton 的格式、内容、顺序和完整处理。已成功转发的帧不等于整句话已采集/完成推理；补齐日志仍有约 140 ms 的 16 kHz 输入缓冲。先用完整固定 WAV 和持续静音探针，再与生产门户及同配置原生客户端对照。
3. M16（待现场执行）：在配置与完整输入均通过后，检查 perception/LLM 输入、ASR display、native BOS 和 function 通道；随后才做单变量模板对照。新 sequence 记录 `PAD=64/SOTC=0/EOTC=0`，只证明断连前 backend 采用的 function 输出未触发调用。具体判定与退出条件见 [D21 设计 §4.0](voicechat-fault-recovery-design.md#40-先核对工具能力与完整输入再判断模型行为)。不修改运行代码、配置、权重或生产授权。
4. M17（2026-10-12 操作说明完成）：[三项只读核对命令](../deploy/voicechat/README.md#d21原生工具通道的只读核对) 给出同环境 vLLM/registry/head 源码定位、index/shard 与独立 embeddings 的 header 读取、实际本地 tokenizer ID 核对。三段 Python 语法检查和 metadata-only header 夹具读取通过；未在现场运行 vLLM/tokenizer 或读取真实权重。用户提供的 config 已声明 function_tokens，实际 head 加载及 ID 匹配仍待现场输出。
5. M18（2026-10-12 可行性方案完成，随后 M21 编码）：门户增加预录 WAV 实时输入可以复用现有 VoiceClient/AudioWorklet/WS 业务链，无需 API 文件上传或服务端存储。方案见 [门户 §8](portal-protocol.md#8-d21预录语音实时输入)。每次测试独立 call、ready 后开始、EOF 后持续静音、保留生产拒绝和不自动重播；文件模式只覆盖固定音源的 B 组真实链，不能替代麦克风/声学验收或独立 A 组完整观察。可行性阶段只改 Markdown；实施结果见 M21。

2026-10-12 用户授权整理、编码并发布 main：

1. M19（完成）：归并当前定位/优化的实施清单，保持既有 D20/D21/D22 修复与诊断，区分待现场证明的 R1–R5、模型/模板条件调整及 D23 文字意图方案；不得把未知假设作为生产修复。
2. M20（本地完成）：实现可复用的只读模型产物核对 CLI，覆盖 config、vLLM 磁盘实现、safetensors header/index/输入 embeddings 和实际本地 tokenizer；输出证据分项与未知运行状态，不加载模型/权重 payload。
3. M21（本地完成）：实现门户 WAV 文件音源，复用现有 WS/Worklet 链；独立 call、ready 后输入、尾部完整发送与静音等待、错误/轮换不重播，保留麦克风与生产授权。
4. M22（本地验证完成）：pytest 194 passed、Web 38 passed、Chromium 14 passed、Ruff、类型/构建和 diff 检查；其中 CLI 31 项 metadata-only 回归。主题方案、CLI/门户运行说明与验收记录已同步，公开协议/schema 和依赖未改。
5. M23（随本轮交付）：只提交预期文件，推送 main 并核对 HEAD、origin/main 与远端 main hash；发布结果以 Git refs 与完成报告为准。现场模型/VoiceChat/CueKB/GPU、真实麦克风与听音仍待 D07，不以本地回归代替。

D23 一并完成有界内部诊断：query 长度/是否等于当前输入、filter 是否存在、history 数量、检索证据投影计数和最终校验分支；不记录正文、不调整 query/prompt/filter 或公开答案语义。

2026-10-12 WAV 现场复测后，两个问题的优化方案（本轮设计，不改运行行为）：

1. M24（证据与设计完成）：核对三份附件、浏览器实际 PCM 对照、文件完成 summary、RNNT 1→45 累计/发布及 raw function 全 PAD；首词转写与工具触发分开定位，同次关联及实际模型输入仍有证据边界。方案见 [D21 §5.6](voicechat-fault-recovery-design.md#56-两个用户问题的优化方案与实施门槛)。
2. M25（本地完成，待离线 GPU 执行）：交付单 sequence、默认关闭的最小隔离诊断包；固定 WS/Triton sequence，保存并精确核对 WS/soxr/submitted/received PCM，记录 encoder window/选帧、`asr_emb`/`encoded_audio`、`combined_embeds` 与 custom output；同帧使用支持 `partial_hypotheses` 的 NeMo `GreedyRNNTInfer` 对照手写 greedy。原始内容只写有界 `0700/0600` 授权 artifact，不进普通日志；离线分析器按 R1→frame→RNNT→combined→function 输出首个分支。增量补丁/manifest/具体执行见 [VoiceChat M25 离线验证](../deploy/voicechat/README.md#m25-隔离离线验证方法)。
3. M26（按证据实施）：修复 PCM、选帧、RNNT decoder/state 或词表映射的首个差异；同帧参考路径也失败时记录声学/产物能力问题，不直接补词/减 gate/加静音。
4. M27（按证据实施）：完整独立观察后核对工具触发；输入/head 确认后才做模板单变量对照，命中解析/WS 差异才改 R3b/R4。生产桥接授权保留，不静默改为 ASR 自动受理。
5. M28（待真实验收）：原版/500 ms/1 s 各至少三次及语速/停顿/插话回归；原生工具→真实知识→有效答案→授权口述→实际播放闭环。更新涉及的独立 VoiceChat 文件/补丁/manifest/部署与回退；本地验证不替代 D07。
6. M29（本地验证完成）：新增分析器/manifest 回归纳入全量 pytest **200 passed**；Ruff、部署脚本语法、diff 检查通过。M25 补丁在独立 D21-after 基线 `git apply --check`、实际应用、4 文件 after hash 与 `py_compile` 通过；本机无 speech CPU 依赖、Docker/GPU/真实模型环境，未临时安装，独立 speech pytest 和 M26–M28 均保持待执行。

## 本轮 D22 引用校验与失败诊断（2026-10-11）

用户已授权编码并发布 main。现场请求检索成功后以 `RAG_INVALID_CITATION` 失败。

1. M1（完成）：核对模型输出、引用校验、证据投影、整轮 deadline 和提交链路。
2. M2（完成）：有界脱敏引用诊断、输出约束及同一预算内至多一次无工具修正；保留全部证据/权限校验。
3. M3（完成）：两种 SDK API、流式/非流式、错误引用、失败修正、超时及取消回归；132 项 pytest、Ruff 和差异检查通过，详细边界见验收记录。
4. M4（完成）：实现与验收文档以 `06015d6` 提交并推送 main，已核对 HEAD、origin/main 与远端 main 一致；现场模型复测仍待 D07。

## 本轮 D23 多轮文字检索意图定位（诊断已编码，意图行为待证据）

用户现场对照：同会话先问 `Find the product troubleshooting procedure`，助手要求产品型号；随后输入 `THAO MOONCAKE` 返回 `insufficient_evidence`，而新会话直接输入该名称可以回答。

1. M1（源码审计完成）：沿门户输入、Turn、会话历史、模型规划、CueKB 请求和答案显示核对；未发现输入串接或上一轮证据容器直接共享。第二轮保留上一轮的澄清正文，存在“补充型号/独立品牌问题”的意图歧义，尚未取得真实 query/filter。
2. M2（本地验证完成）：隔离 HTTP/Coordinator 回放确认第二轮原文不变、历史包含前轮两条正文、新会话无历史、每轮上下文/证据容器独立；相关既有回归 **11 passed**。夹具替换 Runtime，未复现真实模型的查询语义，不调用真实模型/CueKB/VoiceChat，不执行容器操作。
3. M3（现场日志部分确认）：图 1 同一 conversation 的首轮 `54031f6e` 检索 `ok/5 hits`、最终 `insufficient_evidence/CUEKB_NO_EVIDENCE`；次轮 `76697f25` 检索 `not_found/0 hits`、最终 `insufficient_evidence/reason_code=none`；两次工具均 `ok`、交付均已提交。当前 reason_code 不能统一表达检索原因。仍需实际 query/filter、图 2 基线及必要的部署版本/原始模型状态，辅以明确换题/继续旧问对照，闭合意图与过滤参数原因。分析及诊断设计见 [接入 §4.4](integration.md#44-d23多轮文字检索意图定位)。分析阶段不修改运行代码。
4. M4（2026-10-12 本地完成）：补 query/filter/history 形状、原始/保留证据计数、最终验证分支日志，并验证正文/历史/参数不进入普通日志；原始 query/prompt/filter/公开答案语义未改。真实意图根因与行为修正仍待受控现场对照。

## 本轮 D20 实施计划

1. M1（完成）：复核原生 HTML/VoiceChat 音频与工具时序、文字请求耗时链；区分可确认成本与真实环境缺测。
2. M2（完成）：保留自然打断，收敛 D19 中与模型无关的播放/授权修复；核实安全关联所需的最小 WebSocket 变更及启动配置。
3. M3（完成）：移除 SSE 固定轮询等待和答案展示的额外请求，补充阶段耗时；保留检索规划、证据/权限校验和外部取消边界。
4. M4（完成）：回归测试、构建、补丁基线与文档检查；重写当前设计和 speech 更新步骤，保留真实云端验收边界。
5. M5（发布）：本轮相关文件发布到 GitHub main，按 Git 本地/跟踪分支/远端 hash 核对；发布结果以仓库记录为准。

## 本轮 Q05 论文与容器优化设计（不编码）

1. M1（完成）：复核最新论文、官方容器/API 文档、本项目 D19/D20 和本机 speech 调用链；区分公开事实、本地源码与现场未验证项。
2. M2（完成）：形成 [VoiceChat 论文与容器优化设计](voicechat-research-review.md)，说明能力限制、优化优先级、影响模块和验收条件。
3. M3（完成）：检查设计与现行边界一致，核对引用和仅文档 diff；交付评审。本轮不改变代码、运行配置、模型仓库或依赖。
4. 后续实施：方案经用户确认后另行拆分编码任务；D07 真实验收状态不变。

## 本轮 Q06 语音文字展示实施计划（本地完成）

用户已明确截图中的展示目标，详见[门户方案 §7](portal-protocol.md#7-q06语音文字统一聊天展示)。本轮 M1–M4 已编码并完成本地验证；D07 真实验收另行执行。Q05 研究结论继续作为独立未实施方案保留。

1. M1（完成）：Turn 新增 `input_item_id`，口述事件关联 `turn_id` 与 `segment_index`；Alembic 0006 保留旧行 nullable，消息以稳定身份接管。
2. M2（完成）：Gateway 区分固定 ACK 状态与答案口述，持久 Record 保存 Turn 归属并按现有 owner/KB 范围过滤；ASR 不等待业务执行。
3. M3（完成）：统一聊天时间线，用户 ASR 原位升级、口述正文保留、完整答案/引用展开；停止播放不删除文字，向上阅读时不自动滚动。
4. M4（完成）：Python/Web/Chromium 回归、类型与构建、迁移和导出契约验证；受控用例覆盖相同问题、工具/ASR 顺序、ACK、重复片段与旧事件。现场撤权/声学/设备体验仍归 D07。
5. M5（待真实验收）：纳入 D07-C/D，用真实英文语音及实际浏览器验证即时展示、口述保留、连续对话和播放中断；本地测试不替代现场结论。

## 2. 已完成的编码里程碑

D01–D06、D08–D10、D13–D20、E01–E03 已完成代码和本地自动化验证。编码阶段没有 CueKB/VoiceChat 真实接口，也没有 Docker 环境；按接口规范、处理逻辑、契约及本地自动化测试验收，真实联调和容器运行不作为编码交付前提。D07 的部署前代码与静态验证已具备，真实部署环境生产验收仍待执行。

| ID | 任务与影响模块 | 完成条件 | 状态 |
| --- | --- | --- | --- |
| D01 | 冻结门户契约；记录 API/源码规范基线，提供部署版本固定和能力探测机制。`voice/`、`contracts/`、探测脚本 | 原生 call/result、音频格式及异常处理符合规范；契约测试通过；基础/增强模式声明受控。真实部署 digest、事件/音频证据归 D07 | 编码阶段完成；真实探测在 D07 执行 |
| D02 | 身份与知识权限。`api/auth.py`、API、历史/引用读取 | call capability 不具备管理权限；KB 由服务端确定，正文不能覆盖 | D13 改为每次通话独立 token 与 owner；真实 CueKB ACL 待验收 |
| D03 | CueKBAdapter、证据契约与引用。`tools/`、`contracts/`、Runtime/存储 | `/v1/search`、UUID KB、trace/status/version/anchor 按规范正确映射；空命中/降级/错误和权限的受控测试通过；真实 CueKB 闭环归 D07 | 编码阶段完成；真实 CueKB/ACL 在 D07 验收 |
| D04 | 简单 HTML 客户门户。`apps/web/`、门户消息接口 | 开始/结束、字幕、语音播放、状态、答案/引用、窄屏及可选文字；不含管理工作台；复用现有音频模块 | 编码阶段完成；真实设备体验在 D07 验收 |
| D05 | 任务 revision、停止播报/取消/改问分离、pending call 恢复、长会话。`sessions/`、`voice/`、`storage/` | 晚到旧结果不提交、不播报；旧 call 结清或关闭；新会话无旧音频；附和不误取消；迁移与恢复测试通过 | 编码阶段完成；连续交谈仍受 D01 真实能力门槛限制 |
| D06 | 按部署及用户选择启用工具。`config.py`、registry、capabilities/health、部署模板 | CueKB-only 模式不要求天气；仅暴露部署启用、管理员启用且授权工具；新增第三方不改语音主流程 | 编码及本地测试完成 |
| D07 | 云端 Docker 真实端到端、故障、性能与上线。部署及测试模块 | 多个独立 call capability 下英文语音→CueKB→实际口述正确且互不串话；PostgreSQL/Redis/容器与长会话通过；记录版本、样本和阈值 | 部署前检查、Compose 与英文探针代码完成；云端真实验证待执行 |
| D08 | 将生产镜像构建与 Compose 部署分离。`deploy/`、部署脚本、模板及文档 | 独立 `docker build`；部署仅消费预置镜像；静态契约通过 | 编码与本地静态/自动化验证完成；Python 依赖统一为 pip + 固定版本 requirements，已删除 uv 及其锁文件；Docker 运行待 D07 |
| E01 | 接入 CueKB M3 命中契约与预算。`tools/`、`contracts/`、证据存储 | `context_parts`、`context_truncated`、`relations` 严格校验并保存；超预算明示本地裁剪；新版受控响应通过 | 编码及本地契约测试完成；真实 CueKB 待 D07 |
| E02 | 检索条件、逐块引用与文档。Runtime 工具输入、门户、集成文档 | 明确型号/版本经工具输入传递；引用显示来源块、截断及关系证据；历史读取兼容 | 编码、本地测试及前端构建完成；真实资料验收待 D07 |
| E03 | 英文语音与门户适配。`voice/`、Runtime、门户、消息契约 | 英文提示词/文案/默认语言、VoiceChat ASCII 工具结果及英文样本契约验证 | 编码和本地契约验证完成；实际英文语音与口述正确性待 D07 云端 Docker 验收 |
| D13 | 独立测试通话、实时字幕与 HTTPS 门户。`api/auth.py`、存储/迁移、Web、部署 | 点击开始即创建独立 conversation/token；标签页不共享历史；实时打印输入转写和实际口述字幕；移除 `TENANT_ID`，保留 Web TLS | 编码与本地自动化验证完成；公网证书/浏览器及真实多人语音仍待 D07 |
| D14 | 隔离内网文本模型与 CueKB 传输协议。`config.py`、部署模板/文档 | `AGENT_PROVIDER=compatible` 的 base URL 和启用中的 CueKB 接受 HTTP/HTTPS；拒绝非 HTTP 协议或缺少 host；公网门户仍为 HTTPS | 编码与本地契约验证完成；VoiceChat 当时保持 WSS 的边界已由 D15 替换；真实内网服务调用待 D07 |
| D15 | 同主机/隔离内网 VoiceChat WS/WSS。`config.py`、部署模板/文档 | `VOICECHAT_WS_URL` 接受含 host 的 WS/WSS endpoint；WS 仅用于同主机或受控隔离内网；浏览器 HTTPS/WSS 不变 | 编码、模板/文档和本地契约验证完成；真实 WS 路由、防火墙和 VoiceChat 握手待 D07 |
| D16 | 现场知识调用、语音 Turn 与收音诊断修复。Runtime、registry、VoiceGateway、门户音频/UI | 知识请求未调用工具或工具失败不能伪装为无资料；语音必须桥接业务 Runtime；语音问答按 Turn 展示，实时字幕有界；门户显示实际麦克风处理能力；阶段日志不泄密 | 编码和本地自动化验证完成；真实模型/CueKB/VoiceChat/浏览器听音归 D07 |
| D17 | 语音输入绑定、字幕交接与合法口述放行。VoiceChat adapter、VoiceGateway、门户 Live captions | 无有效用户输入的工具调用不创建 Turn；最终 ASR 是展示和业务执行的权威文本；完成字幕保留至持久 Turn 接管；合法 bridge 后的音频不因新输入状态被误拒 | 编码和本地自动化验证完成；真实声学验收归 D07 |
| D18 | 文字问答总预算与答案终态一致性。Runtime、配置/部署模板、门户状态 | 两次模型调用和 CueKB 检索使用明确的总预算；模型不能把成功检索且已有依据的答案标成系统失败；失败答案不携带成功来源 | 编码和本地自动化验证完成；真实文本模型/CueKB 延迟与终态复测归 D07 |
| D19 | 现场无声的跨服务修复与整体设计同步。VoiceGateway/Adapter、VoiceClient/Worklet、独立 speech、设计/部署文档 | 静音不占用整场 response；工具/ACK/回答身份和完成顺序正确；快速结果不耗尽答案许可；停止后新回答恢复且旧帧不播；交付匹配补丁和回归 | 本地实现与验证完成；三镜像发布、真实 ASR/听音仍归 D07 |
| D20 | 原生能力边界、查询交付延迟与阶段计时。Store/SSE、Runtime/Coordinator、门户、speech 发布说明 | 事务后通知/回滚与重连正确；final 直接渲染且晚到历史不回退；自然插话不自动取消；逐模型耗时和当前部署文件明确 | 本地验证完成；main 发布；真实 3 秒分解与云端验收待执行 |

## 3. 待完成与建议顺序

D19 里程碑：M1 附件/源码与调用链审计完成；M2 门户及网关修复完成；M3 独立 speech response 和队列修复完成（D20 将模板改为已有 Jinja 配置）；M4 受控协议、浏览器与构建回归完成；M5 架构/接入/门户/部署/验收整体同步与补丁交付完成。真实联调和人工听音不计入这些本地里程碑。

下一步优先按匹配版本发布 API/Web/VoiceChat，再完成 D07-C/D 的真实英文语音、工具和停止后连续两轮验证。Q 系列仍是建议待排期，正式客户认证留待验证后设计。

| 顺序 / ID | 工作与状态 | 依赖 / 完成条件 | 方案入口 |
| --- | --- | --- | --- |
| P0 / D21 | 必要诊断/探针本地完成，现场故障仍未解决 | 发布匹配 API/Web/speech WS/backend 后，以同样本 A/B/C 定位 `HELLO THAO MOONCAKE` 缺字与工具缺失的首个阶段，再针对性修复；文字项转 D22 | [定位与修复设计](voicechat-fault-recovery-design.md)、[诊断执行](integration.md#36-d21完整输入与模型边界诊断) |
| P0 / D09 | 单一配置与本地测试账号，历史完成、部署入口由 D12 替换 | 原账号 JSON、密码、Cookie/JWT 流程不再用于本期验证；底层范围过滤保留 | [验收历史](acceptance-report.md#2026-09-23-d09-单一配置与受限测试认证) |
| P0 / D10 | Web HTTPS 与私网 API，历史完成、私网 API 由 D12 移除 | PostgreSQL/Redis digest 和 Web `8087` 保留；API 不再映射宿主 `8088` | [验收历史](acceptance-report.md#2026-09-23-d10-镜像与公网-https--私网-api-端口) |
| P0 / D11 | 入口文档、任务路线图与设计/证据同步，历史完成 | 当时记录的账号与私网 API 边界已由 D12 更新 | [验收 §1](acceptance-report.md#1-按日期记录的编码与部署前验证) |
| P0 / D07-A | 发布基线与云端环境，待执行 | 固定版本/配置/样本，按公网 HTTPS Web 的单一流程验收；迁移、readiness、TLS/浏览器麦克风与恢复证据齐全 | [部署 D07](deployment.md#d07-分阶段执行设计) |
| P0 / D07-B | 独立 call capability、真实文本模型与 CueKB，待执行 | 多用户并发隔离、服务端 KB 范围/管理拒绝、M3 命中/故障/限额和有依据回答通过 | 同上；V02–V04、V10 |
| P0 / D07-C | 英文基础语音闭环，待执行 | 实际录音→真实检索→实际口述，型号/版本/否定条件准确；基础模式报告 | 同上；V01、V03、V07、V09 |
| P0 / D07-D | 竞态、恢复与能力分级，待执行 | 旧结果零泄漏、pending call 关闭/结清、轮换/断网；增强能力单独裁定 | 同上；V05、V06、V08 |
| P0 / D07-E | 容量与故障恢复，待执行 | 真实 PG/Redis、drain、备份恢复、预算；冻结测量阈值并复测 | 同上；V11、V12 |
| P0 / D07-F | 放行与发布证据，待执行 | 核对各 V 项的证据、失败和不适用原因；配置声明与实际能力一致 | [验收 §5](acceptance-report.md#5-测量与放行) |
| P0 / D12 | 功能验证部署配置收敛，历史完成并由 D13 替换身份 | 当时固定 customer/tenant/KB、移除登录/JWT和宿主 API 端口；共享身份由 D13 替换，HTTPS 入口保留 | [部署](deployment.md#配置与独立测试通话)、[验收 D12](acceptance-report.md#2026-09-24-d12-功能验证配置收敛) |
| P0 / D13 | 每通话独立 capability、实时字幕与 HTTPS 门户，本地完成 | 每标签页只持有本次 call token，开始通话新建 conversation，结束撤销；输入/输出字幕实时显示；公网入口由 Web Nginx 终止 TLS | [门户](portal-protocol.md#1-简单门户)、[验收 D13](acceptance-report.md#2026-09-25-d13-独立测试通话实时字幕与-https-门户) |
| P0 / D16 | 现场知识调用、语音 Turn 与收音诊断修复，本地完成 | 强制 `search_knowledge`、错误状态不冒充未命中；VoiceChat 完整请求必须桥接；门户按业务 Turn 显示并收敛 Live 字幕；记录实际麦克风处理设置 | [门户](portal-protocol.md)、[接入](integration.md)、[验收 D16](acceptance-report.md#2026-09-27-d16-知识调用语音-turn-与收音诊断) |
| P0 / D17 | 语音输入绑定、字幕交接与合法口述放行，本地完成 | 工具调用绑定真实 input item；最终 ASR 驱动 Turn；无输入不自动查询；已桥接 response 的字幕/音频按响应授权 | [门户](portal-protocol.md)、[接入](integration.md)、[验收 D17](acceptance-report.md#2026-09-27-d17-语音输入绑定字幕交接与口述放行) |
| P0 / D18 | 文字问答总预算与答案终态一致性，本地完成 | 总预算覆盖模型→检索→模型完整链路并在部署模板可见；运行失败由服务端判定；有依据答案的 Answer/Turn/来源状态一致 | [门户](portal-protocol.md)、[验收 D18](acceptance-report.md#2026-09-27-d18-文字问答总预算与答案终态一致性) |
| P0 / D19 发布复测 | 本地修复完成，云端待执行 | API/Web 与 speech 补丁同时发布；固定最终提示词、每轮 ID、实际录音/口述、停止后继续与预算证据 | [部署](deployment.md)、[验收 D19](acceptance-report.md#2026-09-28-d19-逐轮-voicechat桥接授权与播放恢复) |
| P1 / Q01 | 交付状态可追溯，建议待排期 | 业务提交/供应商写回/播放估计分离；断线后可查 unknown，不自动重播 | [架构 §10.1](architecture.md#101-q01交付记录与恢复语义) |
| P1 / Q02 | 证据与口述质量回归，建议待排期 | 型号/版本/冲突/裁剪/数字/ASCII 降级样本；独立统计文字与口述正确性 | [架构 §10.2](architecture.md#102-q02证据充分性与口述质量) |
| P1 / Q03 | 运维指标与验收报告完整性，建议待排期 | 可分解延迟/故障；报告显示缺测与预期分母，避免部分样本冒充整体通过 | [架构 §10.3](architecture.md#103-q03可观测性与报告完整性) |
| P2 / Q04 | 鉴权原件查看，待业务与上游契约确认 | 版本固定、撤权复查、受控下载；未确认接口前不创建假入口 | [接入 §7](integration.md#7-q04原件查看的后续设计) |
| 设计 / Q05 | 最新 VoiceChat 论文与离线容器优化方案，设计完成待确认、未实施 | 以已转换的 Triton Model Repository 为基线；复核工具等待、ASR、口述、容量与版本边界；确认后实施 | [研究与设计](voicechat-research-review.md) |
| 本地完成 / Q06 | 语音文字统一聊天展示，M1–M4 已完成，D07 真实验收待执行 | ASR 右侧气泡，查询并行；实际口述左侧正文并保留；稳定消息关联、完整答案/来源展开及受控竞态回归 | [门户方案 §7](portal-protocol.md#7-q06语音文字统一聊天展示) |

本次保留 Coordinator、revision/epoch、单写入器和 pending call 结清语义；不放宽知识授权、不以真实失败回退 mock、不新增 GPU 或队列服务。D07 的现场缺陷继续回到对应模块修复并用原样本复测。

## 4. 文档整理里程碑

2026-09-22 的 M1–M4 为历史审计记录；D11 按当前代码和部署边界再次核对入口、主题设计和验收证据。

| 里程碑 | 状态 | 交付 |
| --- | --- | --- |
| M1 代码、提交、任务与证据核对 | 完成 | 当前基线、已完成清单与真实缺口 |
| M2 增量方案与验收分解 | 完成 | D07-A–F、Q01–Q04，归入现有主题文档 |
| M3 入口精简与文档去重 | 完成 | 精简 AGENTS，索引按问题路由，README 不重复状态明细 |
| M4 文档验证 | 完成 | 63 处本地链接及锚点、代码块闭合、状态/实现核对、差异检查通过；仅修改 9 份 Markdown，详见验收 §0 |
| M5 D11 当前文档与发布准备 | 完成 | 按需入口、Web/API 网络与认证边界、历史/当前状态区分；本地验证见验收 §1，GitHub `main` 推送状态以仓库记录为准 |

## 5. 实施约束与暂缓项

- 编码和文档阶段边界以 [AGENTS](../AGENTS.md) 为准；真实环境缺失不阻塞按契约编码，不能据此声称生产验收完成。
- KnowledgeAgent 仅在复杂知识样本证明收益后评估；当前不新增子任务调度、递归多 Agent 或队列服务。
- 本期仅英文知识服务；中文、天气/股票、WebRTC/SIP、交易和多组织完整多租户均未纳入。匿名 call capability 只用于隔离测试通话，不等同正式客户认证。本项目不更换语音模型、不建设 CueKB 索引。
- 新需求归主题文档与本表；不新增并行总设计或长期聊天纪要。
