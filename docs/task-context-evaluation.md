# 统一任务上下文与连续对话评测（CTX1 / EVAL1）

本页维护 CTX1 来源上下文与 EVAL1 固定连续评测，2026-10-07 按 f498fa4 核对。实现仍由 VoiceChat 原生选择两个知识工具；不增加分类模型、Supervisor、语义改写模型或新的部署服务。任务状态归 [任务板](TASK_BOARD.md)，实际验证归 [验收](acceptance-report.md)。

## 1. 上下文的权威来源

一般回答与知识执行共用服务端记录的用户上下文。最终 ASR 或提交的文字是原始请求；Nano 的 `user_request` 不覆盖它。原始输入保存在 Utterance/Turn，检索用的 `resolved_request` 是另一个字段。

`app/task_context.py` 使用有界、确定性的规则接收明确条件，例如 `My model is AX100, software version 2.1.`、`Actually use AX200 instead.`。这只是条件绑定，不选择工具、不识别或执行取消命令。规则不覆盖全部自然语言表达；无法安全确定的条件需要澄清。假设、引用和比较不被当成新的已确认配置；多个候选、`Wrong model`、`My model is not AX100` 等撤回表达会清除旧条件并标记待澄清。

模型提出的型号/版本必须与已有来源条件一致，或能绑定到当前最终输入中的肯定陈述。用户本轮明确条件优先；冲突、猜测和已撤回条件不能直接检索。型号变更清除旧版本，除非当前输入又明确给出版本。确认条件是用户指定的查询范围，并不证明产品事实。

## 2. 持久化与执行快照

Alembic **0008** 增加 `Conversation.context_state` 和可空 `Turn.task_context`。迁移保留原 history/slots/summary，但旧 slots 没有来源，不能自动升格为已确认条件；后续请求重新确认或绑定用户输入。降级删除新增上下文字段，不能视为无损回滚。

| 字段 | 内容与用途 |
| --- | --- |
| context_state.inputs | 最近 12 个最终用户输入，含 channel、input_item_id、epoch、接收时 request_revision 和递增 sequence；一般回复显式标注为未验证背景 |
| context_state.conditions | product_model / software_version 的值和原始输入来源；与授权范围无关 |
| context_state.changes / unresolved | 最近 16 次条件变更及前值、来源、原因；撤回或多个候选需要澄清 |
| task_context.original_request / input_source | 完整原始输入及接收来源，不用模型改写覆盖 |
| task_context.resolved_request | 原始输入加确认条件；有明确回指时可附上之前的知识请求，已替换/撤回的条件在该背景中替换或标记，不再带回旧检索条件 |
| task_context.history | 按输入顺序合并一般背景和当前授权知识历史，最多 24 条；一般回复带 `Unverified general reply; not knowledge evidence` 标记 |
| task_context.conditions / background_conditions / changes | 本轮有效条件、用户背景条件及纠正来源的独立快照；比较查询不把背景设备/版本强制用于每次检索，不与会话状态共享可变对象 |
| task_context.turn_id / parent_task_id / request_revision / epoch / deadline_at | 任务关联、修订、连接及绝对期限；原 ASR 接收 revision 可以早于任务 revision |
| task_context.authorized_kb_ids | 当前身份的服务端范围快照；实际检索仍重新检查权限、工具版本、有效期和任务栅栏 |

快照是内部数据，不接受浏览器或模型设置，不增加门户 API 配置字段。历史引用不会填充新任务的 evidence；只使用本轮经 Registry 取得的证据。已取消/过期任务的请求不自动用作回指目标，已确认用户条件可以在重连后继续使用。

## 3. 两条执行路径与重连

Coordinator 在创建任务时构建快照，Runtime 接收授权历史。直查将 `resolved_request` 与确认过滤条件交给 CueKB；充分证据仍由 Nano 续答，**零外置模型调用**。复杂执行的 SDK 同样接收完整请求及有来源的历史，Registry 防止模型工具参数丢失或覆盖确认条件。所有步骤共享同一个 monotonic deadline；持久化的 deadline_at 用于审计，不重新开预算。

CueKB 的 query 保留原 2000 字符上限。原始输入不为容纳背景而截断；没有余量时省略附加背景，确认条件仍通过结构化 filters 传递。直查证据包携带本轮输入、条件来源和任务版本，继续受原有字节预算约束，超限按原规则升级或失败。

重连 instructions 优先包含条件和来源、待澄清项及上轮任务状态，再补充有界授权历史。明确说明过去回复可能没有完全被听到；不会把发送/播放 samples 当成听完证明。

自然 speech_started、ACK 与停止播放不使业务任务失效。停止播放保留当前任务内部口述校验，正文/音频仍抑制；任务可正常完成。新任务不会消耗旧续答许可，已有旧 response_id 的迟到帧在重新关联前丢弃。默认 NVIDIA 尚未结清的并行调用仍拒绝并关闭。当前 P2 已增加受可信 Provider 门槛限制的 progress/revise：保留最终 ASR/来源，修订共享原期限/检索计数并安全结清旧调用，见 [Live §3](live-agent-implementation.md#3-等待进度与自然修订)；不表示当前 NVIDIA 支持等待自由交谈或任意播报。

## 4. 固定连续评测清单

清单为 [continuous-dialogue-cases.json](../tests/fixtures/continuous-dialogue-cases.json)，不改动历史单轮语音清单。十个序列使用独立监听的 CueKB HTTP、VoiceChat WS 和兼容模型 HTTP，经本项目生产适配器及实际 SQLite/Alembic 0008 执行。

| ID | 连续序列 | 核心断言 |
| --- | --- | --- |
| C01 | 一般对话提供型号/版本 → 代词直查 | 原始输入保留，来源继承，1 次 CueKB / 0 次外置模型 |
| C02 | 一般背景 → 复杂知识查询 | SDK 看见背景，确认 filters 不能丢失 |
| C03 | 旧工具结果已回填 → 型号纠正 → 已绑定旧 ID 的迟到口述帧 | 旧版本清除、旧任务 superseded、不覆盖新答案，不占新许可 |
| C04 | 用户纠正型号，模型仍传旧型号 | 澄清，零检索，不能恢复旧条件 |
| C05 | 检索等待期间 ACK / 普通 speech_started | revision 不变，原任务完成 |
| C06 | 检索等待期间显式取消 | pending call 对应连接关闭，旧结果不提交/播出 |
| C07 | 检索等待期间停止播放 | 原 revision/任务和条件保留，内部证据校验完成 |
| C08 | 一般背景 → 断线重连 → 知识查询 | 条件和来源恢复，提示不能假定已经听完 |
| C09 | 一般回复含错误产品数字 → 知识查询 | 一般背景标记未验证，新答案依据本轮检索 |
| C10 | 提供型号/版本 → 撤回型号 → 工具调用 | 型号与旧版本清除、先澄清、零检索 |

ASR、工具选择、模型回复由脚本产生，音频是合成音。上述成功证明应用上下文/协议/竞态处理，不能称为 Nano 选路准确率、真实听音、跨主机或真实服务通过。未绑定到旧任务的供应商响应关联及真实混合会话能力仍须 D07/Q07-E 验收。

## 5. 运行与报告

仓库根目录、已安装开发依赖的环境中运行：

```sh
python -m pytest tests/contract/test_task_context.py \
  tests/contract/test_voice_evaluation.py tests/integration/test_task_context_storage.py \
  tests/integration/test_continuous_dialogue.py -q
PYTHONPATH=apps/api:. python -m scripts.evaluate_continuous_dialogue \
  --output artifacts/continuous-dialogue
python -m scripts.score_voice_evaluation artifacts/continuous-dialogue/results.jsonl \
  --manifest tests/fixtures/continuous-dialogue-cases.json --mode simulation --require-complete
```

Runner 写 `results.jsonl` / `report.json`，每项保存真实观测的工具步骤、布尔断言及调用次数，错误保留 failed/缺测。报告固定清单分母，分别统计漏调用、误调用、错工具、上下文、纠正、控制与旧结果泄漏。缺步骤和明确观测到不调用工具不同；缺结果不按成功计。应用/配置/迁移与夹具的内容摘要用于版本分组，多版本混合不能称为可比较的完整结果。报告的 complete 表示覆盖完整，仍须检查 passed 和泄漏数。

评分器默认 `--mode real`：模拟项不会计入真实指标。真实人工审查项须 `real_service=true`，evidence_mode 为 real（兼容历史结果未提供该字段）、同一版本组、固定 case_id/step_id 和完整适用指标；不确定观察保留 null/缺测。无真实观察的语音事实或权限指标仍为 rate=null，不能用本次模拟通过填充。
