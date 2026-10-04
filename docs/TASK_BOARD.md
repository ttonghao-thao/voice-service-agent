# 当前任务板

更新：2026-10-04；分支 `codex/q1003`。本轮为附件确认的两个 P1 + 一个 P2；不是 Q03/Q04。本地工作区改动尚未推送，上一发布基线见历史记录。任务状态只在本板维护，测试数量/命令归 [验收 §0](acceptance-report.md#0-当前审计与证据索引)，不要默认读取历史。

## 1. 当前结论与审计基线

项目是英文实时语音问答系统，客服为首个场景。默认 legacy，可选 dual_tools + general_qa / knowledge_required；新模式本期只有 lookup_knowledge、reason_over_knowledge。VoiceChat 原生选工具；后台 search_knowledge 是独立层。直查交 Nano D2，复杂请求走外置 LLM，内部升级共享期限/预算。CueKB、VoiceChat 均独立部署。

| 当前事项 | 状态与边界 |
| --- | --- |
| CTX1 / EVAL1 | 来源条件/跨轮任务上下文、固定连续评测已本地验证；一般回复不作知识证据 |
| LVA1 / P1 | 已编码：答案呈现约束与实际口述关联；拟文本与实际转写分开校验，匹配不代表已听到 |
| LVA2 / P1 | 已编码：结束确认、播放排空估计、断开/恢复台账、授权重连摘要，不自动重播 |
| LVA3 / P2 | 已编码：门户进度查询；可信 Provider 下的等待问答/自然修订、旧调用结清与明确关联；真实 NVIDIA 能力仍关闭 |
| 数据库 | 当前迁移 0009，增加交付/口述检查/播放结束审计；部署须 upgrade head |
| 发布与验收 | 当前代码的本地结果见验收；真实 VoiceChat/CueKB/GPU/听音、生产 PG/Redis 恢复仍待 D07/Q07-E |

没有 tool call 时，工具网关不能捕捉所有企业问题漏调用；Prompt 不保证零漏调用。播放停止、业务失效和上游取消分别处理。NVIDIA 工具等待期能力不能由配置开关或模拟证明。

## 2. 已完成的编码里程碑

本轮实施与状态机详见 [Live 改进](live-agent-implementation.md)，不在本板复制设计。

| ID | 交付 |
| --- | --- |
| LVA1 / P1 | PresentationContract、批准文本比对/有限证据检查、answer/input/turn/call/response 关联、授权持久记录/SSE/WS 和门户警告 |
| LVA2 / P1 | voice_audio / tool_result / control / tool_settlement 台账；单调终态；正常上游 close 与异常 unknown；finished ACK、排空后轮换、断线与重启恢复 |
| LVA3 / P2 | 不新增工具名；受能力限制的 query/progress/revise schema，进度不检索，更正保留最终 ASR/条件来源并共享原期限和检索计数；独立网络正例及 NVIDIA 门槛负例 |
| DOC4 | 精简 AGENTS/README/本板；新增主题入口，同步当前契约和迁移，历史任务板转为按需档案 |

此前已完成范围：

| ID | 现行基础能力 / 历史入口 |
| --- | --- |
| Q07-A–D | 原生双工具/可扩展注册、一般问答、D2 续答、外置执行/单次升级、会话策略；[设计 §0](qa-routing-design.md#0-本轮编码范围与扩展契约) |
| CTX1 / EVAL1 | 两个 P0；[来源上下文与固定评测](task-context-evaluation.md) |
| Q06 / D16–D20 | 即时 ASR/实际口述聊天、最终输入绑定、单轮响应/ACK/播放控制、预算和 SSE 交付 |
| SIM1–SIM4 | 独立监听 CueKB/VoiceChat/文本模型 API、本项目网络闭环和浏览器；[模拟](simulated-full-flow.md) |
| D01–D06 / E01–E03 | 门户/权限、CueKB M3 上下文与条件、业务执行和英文适配 |
| D09–D15 | 单一验证配置、每通话 capability、HTTPS 门户、内网 URL/证书；不是正式客户认证 |
| REL1–REL2 / D08 | 上一版本 API/Web build、16 项隔离 Compose（PG 0008）及推送；不替代本轮迁移或真实端到端验收 |

完整旧任务板仅在追溯时查 [2026-10-04 档案](archive/2026-10-04/task-board-before-p1-p2.md)；已有验收日期记录不重写。

## 3. 待完成与建议顺序

| ID | 剩余范围与入口 |
| --- | --- |
| D07-A / B / C | 固定 API/Web/VoiceChat/模型/模板版本及迁移；真实 CueKB/ACL/文字模型、英文 ASR/工具/实际听音；[部署 D07](deployment.md#d07-分阶段执行设计) |
| Q07-E | 真实连续混合会话漏/误调用、参数/证据/口述与时延；[验收矩阵](qa-routing-design.md#14-验收矩阵) |
| D07-D / E / F | 真实断线/长会话轮换/旧输出、PG/Redis 租约/容量/drain/备份；按 [V01–V12](acceptance-report.md#4-最终方案验收清单) 汇总放行 |
| LVA3-R | 用指定真实 Provider 版本验证四项能力，才能启用等待期语音进度/修订；NVIDIA 默认关闭，门户进度可用 |
| Q01 剩余 | 聚合终态报表及真实恢复验收；本轮已补统一交付审计与摘要 |
| Q02 剩余 | 完整断言支持、单位换算、复杂否定/语义和实际音频准确性；本轮有限检查不替代这些目标 |
| Q03 后续 | 全阶段观测与语音样本分母/缺测/版本完整性；[架构 §10.3](architecture.md#103-q03可观测性与报告完整性)，不属本次三个任务 |
| Q04 后续 | 鉴权原件下载，需确认 CueKB 契约；[接入 §7](integration.md#7-q04原件查看的后续设计)，本轮未实施 |
| Q05 / Q07-F | 独立推理层优化、D1/D3、任意异步播报/自由交谈、其他 Provider；GPT Live 接入为可选实验，未实施 |

先完成真实版本/权限/旧输出隔离和听音验收，再放行。默认 legacy 保留；改变模式/策略仅作用新会话，活动会话先结束或 drain。

## 4. 文档整理里程碑

DOC4 将当前状态、设计、验证分开；默认入口只读 AGENTS + 一个主题段落。命令集中 README；状态集中本板；最新证据集中验收 §0。长篇旧状态进入 archive，不默认读取或递归追链接。历史 DOC1–DOC3 的验证数和版本仍在 [验收 §1](acceptance-report.md#1-按日期记录的编码与部署前验证)。

## 5. 实施约束与暂缓项

工程规则见 [AGENTS](../AGENTS.md)。不新增分类模型/Nano 服务/队列/正式客户认证；不以 ASR 关键词执行控制，不宣称 Prompt 或模拟解决真实供应商能力限制。D1/D3、中文/天气/股票/WebRTC/SIP/交易和 GPT Live Provider 不在本次实现范围。
