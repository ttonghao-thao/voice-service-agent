# 当前待实施方案

类型：方案，非实现或通过记录。需求以 [delivery.md](delivery.md) 为准，实际行为看 [architecture.md](architecture.md)，状态只在 [TASK_BOARD.md](TASK_BOARD.md)，操作和证据只在 [validation.md](validation.md)。

本轮目标（2026-11-23 用户确认）：启用 qwen3.7-flash 解析 Sophon 已产生的原生 task，替代自然语言规则解析；结构化任务仍快速解析，ASR 仅展示，原生触发与服务端授权保持不变。用户明确允许本机验证，本次不接入真实 CueKB；这只是本轮验收范围，不改变产品三类交付或正式容器发布要求。真实凭据只留服务端私密配置，不入 Git/报告。

本轮上述接入与验证已完成，当前实现看 architecture，实际结果与未验边界只看 validation §8；不再重复列作待编码任务。接口冒烟不代替原生语音测试，固定集通过不代替完整生产放行。剩余正式交付顺序如下。

续办补充（2026-11-23）：Sophon/NVIDIA 是互斥语音后端，VOICE_PROVIDER 必须单选，默认 Sophon；本期只验证 Sophon。已完成配置/工厂/Compose 选择边界、按所选后端检查和启动依赖（Sophon 的 LiveKit 仍在同一 Compose）、模板/手册同步；必要的双后端配置契约、Sophon 定向回归和最新源码真实天气语音复测见 validation §8。此前误纳入的跨后端全量结果只作为历史旁证，不列为本期验收阻塞；续办未运行 NVIDIA 服务/GPU 验证。

## 1. 容器发布剩余实施

部署配置和启动操作见 [deployment.md](deployment.md)，验证记录见 [validation §7](validation.md#7-开发检查与文档执行记录)。生产验收剩余顺序如下。

| 顺序 | 实施内容 | 涉及现有模块 | 完成条件 |
| --- | --- | --- | --- |
| 1 | 补齐 Sophon 容器验收运行时和宿主单入口 | 复用 scripts/validate_sophon_web.mjs；新增必要的验收 Dockerfile/Compose profile/宿主编排 | 浏览器、ffmpeg、固定官方材料在验收容器内；日志按窗口采集，输出有界摘要；不挂 Docker socket，不在宿主安装验收依赖 |
| 2 | 执行真实容器构建、迁移与发布验证 | 现有 API/Web Dockerfile、Compose、LiveKit、Alembic 0011 | 镜像/source hash 可核对，WSS/媒体/NAT 实际可达，PostgreSQL 升级保留旧数据和角色；失败可恢复匹配的旧镜像/备份 |
| 3 | 完成三类能力及可选配置的语音验收 | 现有 Provider → Coordinator → Runtime → Registry | 已启用的真实服务、可选服务停用、受控兜底、ASR 开关、多轮打断、参数保真和实际听音各有证据 |

API/Web 发布独立镜像，migrate 复用 API 镜像；LiveKit 与 PostgreSQL/Redis 同属一份 Compose，Sophon/CueKB 保持独立部署。AgentSession 已在 API 内，不另建第二套 Agent Runtime。验收容器是一次性任务，不是新增业务服务。

仅引入本轮批准的原生 task 意图解析 LLM，不增加 ASR 分类模型、第二套 Agent Runtime、强制 tool_choice、通用失败后无条件外发或猜测协议字段。原有公开兜底约束保持；LLM 不读取 ASR、历史、证据或权限，私有/敏感 task 不自动发给外部模型。

## 2. 模型与验收缺口

原生委派漏触发与已委派任务解析已在 b60028d 的固定官方录音集分别修复并复测，详见 validation 的已解决章节；不再作为当前待修复任务。更大样本稳定性、多轮、参数保真与完整口述仍单独验收，不能用该组通过替代。

型号保真使用固定音频、完整原生 task 和目标型号金标验证；F7005 → F705 不在 Router 中用 ASR 猜改。角色名字生效不等于完整 Persona 遵循；Gateway 回执、候选字幕、首段语音均不等于已核实完整回答。

ASR 启停由独立 Sophon Gateway 管理。本项目只验证字幕展示及业务解耦，不把开启/关闭切成新的业务模式。

## 3. 交付与验证边界

每阶段同步部署操作、实际架构、任务板和唯一验证记录；已经修复并通过对应验证的问题移入已解决章节，不让历史失败继续驱动当前工作。外部 VoiceChat 配套源码/镜像的变更须另按根 AGENTS.md 交付，不能随应用发布自动升级。

仓库检查包括部署契约、Compose 展开、Shell/文档语法、链接和 diff。真实部署在容器执行；缺少 Docker Engine 时不得声称镜像/真实网络通话验收通过，不刷新历史语音结果。完成后按根 AGENTS.md 交付 origin/main。

## 4. Sophon 逐轮委派诊断设计

2026-11-28 待评审。范围为 Sophon Live + LiveKit 的日志和英文门户。以服务端观察到的一轮语音输入为起点，使操作者可以依次核对 Sophon 发来的内容、系统理解、实际执行和回传。整个诊断过程只观察现有调用，不产生业务提交。

### 当前缺口

- [RoomCall.receive / run_tools](../apps/api/app/voice/livekit_room.py) 分别接收原生事件和处理 SDK 委派。入口日志的 route 来自 task 前缀，自然语言会显示 unclassified。协议校验可以直接结束连接，非字符串或过长 task 会被归一为空串，输入归属检查也可能先于 Router 返回。这些路径都可能没有 sophon_route。
- [DelegationRouter](../apps/api/app/voice/delegation_router.py) 会将允许的 CUEKB 公开兜底转成 WEB_SEARCH，也会将模型不可用或参数不符收敛为 CLARIFY。只看最终 route 无法还原初始判断。[Registry.invoke](../apps/api/app/tools/registry.py) 的 started 位于授权检查之前，finished 的技术状态与工具返回的业务状态也可能不同。
- [门户](../apps/web/src/App.tsx) 已展示字幕、候选口述和业务结果，但没有逐轮呈现 sophon_route。当前输出字幕按 latest 暂挂，首个音频帧是会话级信号。二者都不足以单独认定某轮获得了自主回答。

### 一轮如何识别和结束

沿用 conversation_id、epoch 和 SpeechInput.id（门户 input_item_id）标识输入。首次发声即创建卡片，短停顿合并沿用原 ID，关闭 ASR 也能显示。每个原生委派独立保留 delegation_id，Coordinator 真正提交后才补 turn_id 和 request_revision。一轮允许看到多次委派尝试。无法唯一归属的事件置于本通话的 Unassigned events；无有效 ID 时用本地观察编号供查日志，不作上游 ID 使用。

| 服务端观察 | Web 显示（英文文案建议） | 判定时机 |
| --- | --- | --- |
| 已识别输入，委派仍可能到达 | Awaiting Sophon | 输入开始即显示。quiet 或首段输出均不结案 |
| 收到可归属的原生委派 | Delegation received | 按原生事件记收件。状态可以继续变为格式无效、澄清、拒绝或执行完成 |
| 观察期完整，仅一个可归属输入，有对应的候选回复，未见委派 | Sophon replied · no delegation observed | 使用既有输入归属窗口及会话生命周期。只表示观察结果，回复内容仍标为候选 |
| 观察期完整，既无可归属回复也无委派 | No reply or delegation observed | 保留本轮，即使没有字幕或业务 Turn |
| 重叠输入、疑似迟到、仅有欢迎语/会话级音频，或断线致观察不完整 | Attribution uncertain 或 Observation interrupted | 无法绑定的收件、输出仍在会话内可查；欢迎语不创建用户轮次 |

复用 RoomInputs 的唯一候选判定和现有 INPUT_LOOKBACK。输入结束后仍可接收该窗口内的委派，已接收的调用继续跟踪至结清。窗口到期或正常结束通话时再归纳观察结果。若候选字幕仅有 latest 关联，须核对整段输出期间的输入窗口、交错发声及未结清委派；证据不足即展示归属不明。若后续证据可可靠绑定则更新原卡，否则保留独立事件。不得据此声称 Sophon 使用了哪份内部知识，或已经完整口述工具结果。

### 日志与 Web 使用同一组事实

| 事实 | 写入时机与内容 |
| --- | --- |
| 原生收件与结构校验 | 在已识别的 session.delegation.created 进入现有校验、strip 或截断之前，提取有界结构快照。保留实际存在的 type、delegation.id/type/target/task，区分缺失、null、类型错误、超长、非法目标和同 ID 内容冲突。记录失败字段路径、期望与实收类型。无法辨识事件种类的协议错误留在会话级。原始回调与 SDK 回调按 ID 合并，同 ID 重复不再执行 |
| 输入归属 | 记录已绑定、归属不明、重复或失效，以及是否进入 Router。即使因归属拒绝，仍保留“已收到委派”及可见结构 |
| 意图与策略 | 分别保存结构化前缀或 Qwen **实际给出的**初始意图、解析方式、模型（仅实际调用时）、校验后的 route 和参数、选定工具及改道/拒绝原因。模型超时或输出无法识别时初始意图留空。区分主动要求 CLARIFY、参数不合约、模型不可用和 UNSUPPORTED |
| 执行与结果 | 由 Coordinator 关联 Turn，以 Registry 校验、实际进入 Adapter 和工具返回值记录工具名、服务/运行模式、执行状态、业务结果或错误码、耗时。可区分未调用、调用失败、完成但无可用结果、取得结果。CueKB 同时标明原生工具 search_knowledge_base 与底层 search_knowledge，query/validate 依据各自实际证据记录 |
| 交付 | 分列已排队回填、Gateway 已回执、回执未知、失效/被取代；标明回填的是工具结果还是澄清/拒绝。候选口述另列。回执超时保留已知执行结果，沿用现有禁止自动重发的行为 |

后台用可检索的结构化日志输出各阶段结果，并为每个结束的输入和每个已收委派输出汇总。字段包括上述关联 ID、阶段、状态、原因、耗时；没有 Turn 或工具时明确标记未执行。格式错误、队列已满、权限变化、取消、提前返回、关闭连接均须有处置记录。进程异常中断只有起始记录时，恢复视图显示观察中断。避免在音频帧回调执行数据库 I/O，状态变化才写，短停顿和重复事件不反复生成总结。

在原有输入气泡下显示状态，展开可看 **Sophon payload → Interpreted intent → Tool execution → Delivery**。无字幕时使用 Voice input 占位。示意：Sophon 发来 `查一下杭州明天的天气` → Qwen 得到 WEATHER、地点杭州、日期 tomorrow → 实际调用 weather / QWeather → 业务结果及回执分别显示。如果原文是天气问题而模型判成 WEB_SEARCH，同一处会显示原文、错误选择及实际是否调用 web_search。线上不设第二个模型自动判对错，验收用事先标注的意图和参数判错。对 CUEKB 的获准公开兜底则显示 CUEKB → WEB_SEARCH 及具体策略原因。

展开的 JSON 是**收到的数据经权限过滤后的结构快照**。例如以下为合成示意，仅展示所关注的字段；task 在这个协议中是字符串，Router 的解析结果另行展示。

```json
{"type":"session.delegation.created","delegation":{"id":"example-1","type":"delegation","target":"client","task":"查一下杭州明天的天气"}}
```

有效、可见的 task 保留原文，不用规范化文本回填原始字段。异常值保留类型、长度和有界字段结构，未知字段值省略，省略/遮盖/截断显式标识。生产通用日志只写结构、类型、长度、安全字段及错误码；授权门户才显示经过敏感值遮盖的 task 和参数。敏感值包括凭据、票据和个人信息，未知嵌套对象也不整包透传。撤权时重新过滤历史、SSE 重放和打开的详情，凭据、音频、内部证据正文及模型隐式推理均不进入诊断快照。

### 实施顺序与验收

1. **服务端采集**。在 RoomCall、DelegationRouter 和现有执行链补齐上述事实。复用 Record 与持久 SSE Event，拟新增一个门户事件 `portal.sophon_interaction.updated`。诊断快照按输入或未归属观察编号维护，幂等合并，同一事务更新快照并追加事件；现有 Store.record 对重复键直接返回，须定向支持该类更新。沿用 server_seq 做版本比较。旧数据只展示能恢复的字段，缺失部分显示未记录。
2. **门户与日志对照**。从同一份后端事实投影生成日志和客户可见记录，复用 messages 历史和 SSE 游标。实现前同时更新历史与实时授权过滤、contracts.py、导出 schema 和 Web 类型。Web 收到同轮更新原位刷新，重复投递或迟到的历史快照不能覆盖新状态；已结束通话仍受原有 capability 生命周期约束。
3. **验证**。受控事件覆盖坏结构（含缺 ID、task 为对象）、结构化与自然语言任务、Qwen 超时和错误路由、获准兜底与拒绝、工具被禁用/超时/无结果、ACK 缺失、安静无回复、无 ASR、重叠输入、欢迎语、重复/冲突 ID、断线重放及撤权。断言每轮可查、原文与意图区别可见、所有实际执行可关联、拒绝/重复无额外调用、日志与界面一致。诊断写入失败须显式报告诊断不完整，禁止为补记录再次执行工具。
4. **真实语音验收**。沿用完整官方录音集、原分母和生产容器入口要求，加入直接回答与三类委派对照。逐条人工预标是否应委派、目标意图/参数和允许的工具，分别核对原生收件、结构可识别、意图匹配、真实工具执行、回执和听音。保留未委派、误路由、未绑定、未执行、未听证以及环境缺失的样本和原因。设计评审与受控事件均不能替代这一步。
