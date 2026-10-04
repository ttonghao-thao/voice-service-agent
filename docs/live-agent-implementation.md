# Live Agent 借鉴：两个 P1 与一个 P2

本轮范围按用户附件：LVA1「答案呈现约束与实际口述关联」、LVA2「结束确认和恢复状态」、LVA3「等待期间进度问答和自然更正」。没有可确认的 gpt-live-1 专属实现资料，本项目实现基于已确认设计和现有接口，不声称复制该模型的内部 Agent。状态只在 [任务板](TASK_BOARD.md) 维护，结果见 [验收 §0](acceptance-report.md#0-当前审计与证据索引)。

## 1. 答案约束与实际口述

实现入口：`contracts.py`、`agent_runtime/evidence.py`、`direct.py`、`sessions/coordinator.py`、`voice/gateway.py`。

| 路径 | 呈现约束 | 实际输出检查 |
| --- | --- | --- |
| 外置/legacy 短答 | `PresentationContract(mode=verbatim)`；服务端批准 `speech_text` | 与 Provider 最终转写比对；仅容许大小写、空白和句末标点变化，数字不能自由改写 |
| Nano D2 证据续答 | `mode=grounded`；证据及确认的型号/版本随 envelope 发送 | 有限引用、数字、常用单位、型号/版本边界及否定/only/unless 检查 |
| 无工具一般回复 | 无知识约束 | `mode=unverified`，不建知识 Turn、不伪造引用 |
| 等待进度回复 | 服务端真实任务状态生成的短句，按批准文本呈现 | 按 verbatim 比对；这是状态快照，不是持续更新或虚构百分比 |

Coordinator 提交外置答案前准备约束。短句漏关键条件、非 ASCII 或不能安全呈现时，用英文提示查看完整文字答案，保留文字与引用，不裸截型号/数值。D2 envelope 带约束，并在当前任务绝对期限内等待实际转写结束。

Gateway 在 `audio.done` 后生成 `PresentationAssessment`，通过准确的 input item、Turn/revision、native call、response 和 answer ID 关联，不以文本相似度推断。`speech_validation` Record 保存检查依据、结果和授权 KB 范围；`DeliveryAttempt` 保存 answer_id、validation_status/reason。SSE/WS 发 `portal.presentation.updated`；历史及 SSE 撤权过滤，门户展示口述检查失败警告。

当前 NVIDIA 续答没有显式 parent_call_id，仍按单个有效续答许可绑定；这不是供应商跨轮关联的独立证明，未知迟到 ID 的归属仍待真实版本验收。增强等待 Adapter 必须提供明确 call 关联，多许可不允许猜测。相关边界也见 [上下文与评测](task-context-evaluation.md)。

外置实际口述偏离批准文本时，完整文字答案仍保留；只清尚未播放的缓冲。D2 有限检查失败时，知识任务按既有机制失败并清播放。停止播放不等于任务取消，当前 D2 内部转写仍用于检查。旧 epoch/revision/Turn 输出不提交、不消耗新续答许可。

`matched` 表示 Provider 转写通过该模式的文本检查。它不证明转写与音频逐字相同、每条断言由证据支持、单位换算正确或人已听到。`verification_timing=before_audio` 仍只描述外置拟文本的校验；实际呈现的 check_timing 为 after_audio。一般回复为 unverified。

## 2. 结束确认、台账与恢复

实现入口：`storage/models.py` / `store.py`、Alembic `0009_delivery_audit.py`、Coordinator、Gateway、`VoiceClient.ts` / `audio-worklet.js`。

| 状态/观察 | 准确含义 |
| --- | --- |
| 业务答案 accepted | 服务端接受结果，未证明网络发送或播放 |
| prepared / write_started | 准备交付 / 已开始可能产生外部效果的写入 |
| tool_result sent | 原生 function result 写入完成；不代表随后语音已生成 |
| voice_audio sent / completed | 音频块写入浏览器完成 / audio.done 边界写入完成 |
| played_samples / playback_finished_at | 浏览器报告的播放/排空估计，不是听到的证明 |
| suppressed / discarded / unknown | 播放被抑制 / 准备交付未执行 / 写入或响应结束无法确认 |
| control applied | 应用已执行 stop/cancel/interrupt 的控制效果，不是网络确认 |
| tool_settlement sent | 可信增强 Provider 上旧 pending call 的作废结果写入；不允许业务提交或语音输出 |

每个 voice_audio 尝试以 response ID 标识；同一已结束 ID 不能重新开始输出。唯一键仍为 conversation/epoch/native_call_id/kind，终态不能被迟到写入重新打开。多个字幕 segment 可属于一个尚未结束的响应；已结束 response 被复用时报明确协议错误。

单写入器先记录 write_started，再写网络；发送样本只在写入完成后增加。网络与数据库没有共同事务：写入中断标 unknown，prepared 标 discarded；连接中断/重启/租约替换也将未结束的音频 sent 标 unknown。unknown 不重发。DB 事务结束在取消时完成清理后才释放写锁，避免新写入与未提交事务互锁。

客户端 `portal.playback.ack` 新增可选 `finished=false`，兼容旧客户端。只有该 response 收到 audio.done 且本地对应队列排空才发 finished=true；按响应独立追踪，ACK 结束不会让另一个答案被误标完成。服务端要求 audio.done 已写入、未抑制且样本数恰好等于 sent_samples，不能靠伪造或部分 samples 提前完成。静音/零音量等情况仍可能“排空但没听到”。

自动轮换等待安静、任务工具写回结束及已发送音频排空/抑制；最长仍有 30 秒 grace，到期关闭而不是无限等待。当前 NVIDIA 只使用既有 output_audio.done 与 WS close handshake，不发明 response.done、response.cancel 或任意 TTS 命令。正常上游 close code 1000/1001 才记录 `voice_session_end.confirmed`；异常/缺确认为 unknown，业务取消原因单独保留。

重连仅注入当前授权范围内的来源条件、任务状态和少量准确交付观察，注明“不证明听到、不要重播”。不恢复录音、旧播放队列或旧 native call。旧 epoch 的结束记录允许历史审计，不能发布旧业务结果。

0009 增加 sent_samples、input_item_id、phase、reason_code、output_suppressed、finished_at、playback_finished_at、answer_id、validation_status、validation_reason；不把旧记录升格为已验证关联。部署前 upgrade head；降级删除审计字段，需要备份，见 [部署](deployment.md#升级drain回滚)。

## 3. 等待进度与自然修订

实现入口：`voice/provider.py` 的 ProviderCapabilities、`agent_runtime/dispatch.py`、Gateway、Coordinator、Store；前端 Check progress。本期仍只有两个原生工具，无额外分类模型。

### 3.1 能力门槛

`ProviderCapabilities` 是可信 Adapter 的不可变声明，创建会话时快照，连接时核对一致。以下四项全部通过才能暴露等待交互 schema：

| 能力 | 必须验证的行为 |
| --- | --- |
| wait_progress | 原查询未返回时，新语音可进入模型并生成独立工具调用/回答 |
| wait_revision | 等待时可识别用户更正，按最终 ASR 创建修订 |
| correlated_tool_output | 每条工具续答可准确归属 native call，多个许可不靠任意 pop |
| settles_superseded_calls | 旧 pending call 可收到作废结果、无旧输出，或连接明确关闭 |

当前 NVIDIA Adapter 全部为 false；不提供 env/browser 开关绕过验证。普通全双工、持续收音、ASR 字幕或 ACK 不证明工具等待时的新问题参与推理。`/capabilities` 继续声明 native_tool_phase_barge_in=false。

**当前可用降级：**门户 `POST /conversations/{cid}/tasks/current/progress` 及 Check progress 不依赖上述语音能力。body 为 expected_epoch/expected_revision；验证归属、租约与版本，返回 TaskProgress 的真实 status/phase 和有界短句，不新增 Turn、检索或外置 LLM。不虚构百分比/剩余时间。自然语音更正未获能力许可时不能承诺生效，可显式取消后重新提问。

### 3.2 两个工具的可选 operation

默认 NVIDIA 仍使用严格 KnowledgeArguments，不接收 operation。可信等待 Adapter 才为这两个工具使用 KnowledgeInteractionArguments：

| operation | 服务端行为 |
| --- | --- |
| query（默认） | 原有直查/推理路径 |
| progress | 读取当前任务状态，将短句交回本次 call；一般状态回复不作知识证据，revision/原任务不变 |
| revise | 原任务须仍 running；epoch/当前 Turn/revision 比较并交换，创建子 Turn，supersede 原任务 |

工具参数仍包含 user_request、必填可空 product_model/software_version；完整用户输入来自最终 ASR，模型改写不能替换它。operation 由模型 tool calling 选择；ASR 字段解析只记录确认条件，不按“actually/correction”等关键词执行控制。工具 schema 扩展仅用于原生知识工具，未来可信工具保留自己的参数类型。

修订保留原始更正、上一问题的有界完整意图、确认条件及来源。型号更正清旧版本，新的显式版本再写入；`use model AX200` 不吞掉型号。原任务必须仍在运行，不恢复 completed/canceled/expired 任务；新任务继承原单调绝对期限与已用检索计数，不能靠反复改问刷新预算。

新 revision 使旧业务提交和音频许可失效。旧 call 未写回时，在单上游写入器发送无事实/无输出许可的 canceled 结果，并独立记录 tool_settlement；已经写回的旧 call 不重复返回结果。旧响应/旧 call 的迟到帧丢弃，不能借走新任务的续答许可。无法确认关联或安全结清时关闭连接，不能宣称只改 Prompt 即可安全继续。

### 3.3 模拟与真实的边界

`SimulationHarness(wait_interaction=True)` 显式注入 SimulatedWaitAdapter；只有该夹具 Adapter 将独立模拟 WS 的 parent_call_id 扩展映射为内部关联。生产 NVIDIA normalizer/公开 WS 字段不增加这一扩展。默认模拟仍用 NVIDIA 现行适配器和 schema；两种结果分开记录。

通过增强模拟只证明本项目的 progress/revise、版本 fence、预算、旧 call 结清和关联处理。真实启用必须指定 Provider/模型/Prompt/推理版本并用授权音频验证四项能力、等待时更正/取消、交错输出、慢工具、失败和重连；再由可信 Adapter 映射真实关联。GPT Live Provider 接入是可选实验，本轮未增加。

## 4. 验证与维护入口

命令集中 [README](../README.md#本机验证)。重点测试：

- [test_live_agent_interactions.py](../tests/integration/test_live_agent_interactions.py)：实际口述偏差、撤权、条件、完成 ACK、正常/异常关闭、进度/自然修订、交错续答许可与 NVIDIA 负例。
- [test_delivery_lifecycle.py](../tests/integration/test_delivery_lifecycle.py)：三条输出路径、断线 unknown/不重播、幂等控制、重启及取消中 DB 清理。
- [test_task_context.py](../tests/contract/test_task_context.py)：来源条件提取/冲突与 use model 更正；[迁移](../tests/contract/test_qa_migration.py) 验证真实 upgrade/downgrade/check。
- [worklet.test.mjs](../apps/web/tests/worklet.test.mjs)：排空须结合响应结束、多个响应独立完成；Chromium 网络用例验证实际门户。

网络夹具使用真实 HTTP/WS 和隔离 SQLite/Alembic，不用 MockTransport。真实 Nano 选路、ASR/TTS、实际听音、生产 PG/Redis/容量及跨机网络不在模拟证据内。Q03 全阶段观测、Q04 原件下载、Q02 完整语义验收另有后续范围，不能混为本次三项。
