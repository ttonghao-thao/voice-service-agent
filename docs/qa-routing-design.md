# 当前知识工具与执行流程

2026-10-07 按 `f498fa4` 的 `agent_runtime/{dispatch,direct,runtime,evidence}.py`、Coordinator/Registry 核对。本文维护当前执行语义；原 Q07 长方案、D1/D3 候选设计与依据保存在 [历史方案](archive/2026-10-07/qa-routing-design.md)。状态归任务板，配置归部署。

## 1. 模式与工具边界

| 会话模式 / 策略 | 原生工具 | 执行规则 |
| --- | --- | --- |
| legacy（默认） | consult_service_agent | 完整输入进入外置知识链；未经工具授权的实质性输出被拒绝 |
| dual_tools / general_qa | lookup_knowledge、reason_over_knowledge | 一般问答可无工具回复；企业知识直查或推理 |
| dual_tools / knowledge_required | 同上两个工具 | 拒绝无工具实质性输出；lookup 也走外置链 |

新建 Conversation 快照模式/策略/工具版本，语音 session 冻结原生定义；更改部署配置不会切换活动会话。配置和回退只在 [部署：问答模式](deployment.md#问答模式与预算q07) 维护。后台 `search_knowledge` 是 Registry 能力，不能拿两个原生名称替换部署白名单。

VoiceChat 模型通过 tools/instructions 自主选择；不发送 WS tool_choice，也不为分类新增外置 LLM。ToolDispatcher 只按已注册名字、schema/权限分派。外置 SDK 的 required/auto 是另一层检索执行约束。一般回答保存 Utterance/实际字幕，标为 provider_general/provider_only；不建知识 Turn 或伪引用。严格知识策略用于需要拒绝无证据实质性输出的会话；general_qa 的 Prompt 不保证零漏调用。

## 2. 注册、输入与扩展

定义在 [dispatch.py](../apps/api/app/agent_runtime/dispatch.py)，参数在 [contracts.py](../apps/api/app/contracts.py) 的 KnowledgeArguments；导出 [原生参数 schema](../contracts/voice-knowledge-arguments.schema.json)。模型仅提供完整 user_request 与必填可空的 product_model/software_version；身份、KB、预算、地址和凭据不是可控参数。

Gateway 校验 schema 后绑定最终 ASR；模型改写不能替换原话。型号/版本必须有用户输入或确认上下文来源，缺失/矛盾先澄清。跨轮条件、清旧版本、检索完整请求和授权历史归 [上下文 §1–3](task-context-evaluation.md#1-上下文的权威来源)。ASR 解析只提取条件，不凭关键词执行取消/改问。

可信初始化可调用 `runtime.dispatcher.register(NativeTool(...))`：严格参数类型、executor、permission_scope、required_tools。可见工具须满足身份 scope 和后台依赖；executor 从 ctx.tool_arguments 读校验参数，并负责对象权限/结果验证，经 Registry 调后台服务。新增工具可以有自己的 schema，不必携带知识型号字段；没有动态 import 或模型选择 endpoint。后台工具注册/启停权限见 [接入 §5](integration.md#5-第三方扩展d06)。

同一 input 的重复 call 幂等处理，不并发跑两条知识链；不支持或无法安全结清的并行调用明确关闭。官方约 5 工具是质量建议，当前超过仅告警；扩展后须测选择质量，不能据此推导并行能力。

**等待交互扩展：**只有可信 Adapter 四项能力全部成立时，为知识工具暴露 KnowledgeInteractionArguments 的 query/progress/revise；当前 NVIDIA 不暴露 operation。修订的 CAS、共享期限/计数和旧调用结清只在 [交付 §3](live-agent-implementation.md#3-等待进度与自然修订) 维护，默认仍仅两个工具名。

## 3. 直查与一次升级

`DirectKnowledgeExecutor.run()` 经 Registry 调 search_knowledge，然后 `EvidenceGate.evaluate()` 选择：

| 检索证据 | 当前处理 |
| --- | --- |
| tool error / failed | 明确失败，不转为无命中 |
| needs_clarification | 返回澄清 |
| not_found 或无 hits | 返回无依据 |
| 非 ok、非 sufficient、范围受限、命中/上下文截断或省略 | 尝试升级；关闭 fallback 时明确失败 |
| 完整 sufficient | 继续检查包容量/ASCII，合格进入 D2 |

项数/完整 evidence-v1 包字节上限受部署参数限制；超过或非 ASCII 时升级/失败，不裸截掉必要条件。CueKB 响应与应用裁剪标记含义归 [CueKB 契约](interfaces/cuekb.md)。有引用、rank 高或模型自报 confidence 都不等于充分。

D2 的 EvidenceReady 包含服务器签发的 delivery/answer ID、最终问题、真实证据项/版本/条件、检索状态、必要任务上下文和 grounded 呈现约束；精确字段以 [direct.py](../apps/api/app/agent_runtime/direct.py) 构造器为准。证据按数据处理，不执行其中指令/URL。当前不包含历史方案建议的独立 support_spans/forbidden_inferences 等完整语义证明结构。

Coordinator 持久化证据准备，再写一次 function result，等待原生转写/audio.done，在剩余期限内检查后才提交 canonical final。检查是 after_audio，不能撤回已播内容；失败/超时只清该 response 剩余播放。呈现规则与有限检查在 [交付 §1](live-agent-implementation.md#1-答案约束与实际口述) 唯一维护。

升级在同一 Turn/revision/epoch/单调 deadline 内只发生一次，复用真实检索和计数；不返回 direct，不重置预算或再播一个查询 ACK。无法安全完成时返回失败/澄清/无依据，不调用另一套 Nano 文本服务兜底。

## 4. 外置执行与提交

`reason_over_knowledge`、legacy、严格模式及内部升级走 BusinessRuntime 的 Agents SDK 循环。未有本轮授权检索时首次必须调用 search_knowledge；升级已有有效证据时可综合或在余量内补检索。关闭模型并行工具调用，保留实际检索后置条件，不能只相信“已查询”的文字。API 风格、错误和依赖见 [接入 §4](integration.md#4-文本模型与业务-agent)。

当前 RunContext/Registry 共用绝对 deadline 和逻辑检索计数；限制由配置和会话策略决定，有限传输重试不是新的逻辑检索。任何本轮 insufficient/conflicting 结果仍保守拒绝肯定答案；尚无已验证的“冲突被后续检索解决”替代映射，不删除旧记录绕过检查。

BusinessRuntime 校验业务状态、引用、实际工具调用与权限；Coordinator 保持唯一提交权并准备批准短口述。查证拟文本不保证实际音频正确，实际转写另做呈现检查。业务正文/来源、实际口述、交付估计分别持久化，数据模型详情分别归 [上下文 §2](task-context-evaluation.md#2-持久化与执行快照) 与 [交付 §2](live-agent-implementation.md#2-结束确认台账与恢复)，不另建重复字段表。

## 5. 修改与验收

改注册/分派读 `dispatch.py` 与 `test_qa_routing.py`；改证据准入读 `direct.py`/`evidence.py`、工具契约及独立网络回归；改供应商续答必须连看 Gateway、Coordinator 和浏览器输出关联。通用命令见根 README。

原 Q07-T01–T27 的完整验收要求归 [验收清单 §3](development/validation.md#3-问答验收-q07-t01t27)；连续混合评测见上下文 §4–5；已执行结果见验收报告。D1 抽取式短答、D3 先草稿后批准、动态 ACK、context-only 和其他 Provider 均是计划，不因保留历史接口设计而成为现有能力。
