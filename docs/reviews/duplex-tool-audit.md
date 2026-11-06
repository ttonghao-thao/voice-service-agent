# 全双工与工具流程审查

日期：2026-11-06。本项目 `2a8ba8b9c96d61bb43caba248e56104fbcb2ee04`；CueKB `30970e9f6b0d36e012180022c2421c4d48a9de22`。两者均已核对 GitHub main 与本地源码。本次未修改业务代码、未调用生产 CueKB、未做 GPU/听音验证。函数探针使用现有代码或从源码抽取的函数体，不创建测试文件。

## 现有能力与结论

已具备持续收音/播放、原生工具注册与调用、单输入一次问题级查询、证据包 validate、终态后冗余调用结清、实际 tokenizer 完整包准入、注入期间显示 ASR、逐响应播放抑制及 Coordinator 唯一提交。CueKB 2.1 的 query/validate、facet/decision、授权实体过滤、源节点/必要依赖检查、签名短期包和 tokenizer 预算确有实现。

当前最大问题已从“有没有检索结果”转向“能否正确理解补充输入、确认回答覆盖、拒绝错误产品和事实”。代码未再使用旧版“两次检索后第三次立即断连”路径处理合法终态冗余调用；欢迎语只一次/ACK EOS/已抑制 done 等已编码修补仍需配套现场验收，不根据旧错误日志断言新版本仍复现。

## F1：答案核验可放过矛盾与全部漏项（P0，已复现）

位置：[EvidenceGate.check_spoken/check_presentation](../../apps/api/app/agent_runtime/evidence.py)、[finish_provider/evidence_outcome](../../apps/api/app/sessions/coordinator.py)。

现有检查是数字集合、数值单位对和若干全局条件词。它不检查每个已支持 facet 是否在口述中作答，也不把否定词绑定到具体命题。只读调用真实函数得到：

- 来源 `It cannot be shipped. Do not freeze it.`，回答 `It can be shipped. Do not freeze it.` → 返回 `None`（未检出错误）。回答中的另一个 `not` 满足全局否定检查。
- 来源包含 `RMB 42` 和 `65 grams`，回答 `Please check with the manufacturer.` → 同样返回 `None`；没有新数字并不等于回答完整。

当 provider decision=answer 时，Coordinator 按 decision 映射 answered；所以这个漏检可进一步形成 source_checked/answered，并非只影响诊断文案。播后检查也无法撤回已播错误。

待优化：把“证据支持哪些问题项”和“实际口述回答哪些问题项”分开维护；逐主张绑定主体、属性、值、单位和条件。可确定的矛盾/遗漏必须失败或明确未验证，无法确定的语义不得认证成功。不建议堆更多全局关键词或引入未经授权的额外模型。

## F2：缩写否定被误认为确认实体（P1，已复现）

位置：[entity_context.normalized/context_binding](../../apps/api/app/tools/entity_context.py)、[CueKBAdapter._context_bindings](../../apps/api/app/tools/adapters.py)。

`normalized()` 会把 `don't` 变成 `don t`；后续否定规则却匹配 `don't`。输入 `I don't mean Golden Yolk. Can it be shipped?`，目录只含 Golden Yolk 时，真实函数返回该实体的 `confirmed_conversation_context` binding。此 ID 会进入 CueKB 过滤，导致对被否定产品查询/回答。

待优化：否定/更正作用域使用同一规范化规则，明确撤回旧主体；没有新的肯定主体时澄清。覆盖 don't/isn't/wasn't、明确更正、跨句指代，同时保持 `Can it not be shipped?` 这类属性否定不撤销主体。

## F3：读到输入起点即被视为相关且可结清（P0，判定缺口已复现，现场漏问待验）

位置：快照 [audio_server._native_consumed_input/_emit_response_created](../voicechat/s2s/audio_server.py)、[relay.receiver](../../apps/api/app/voice/relay.py)。

`_native_consumed_input` 只检查 `start_frame < audio_frame_end`，不保存/要求输入结束位置。例如新输入从 frame 10 开始、LLM 仅消费到 11，函数即返回 True。`_emit_response_created` 可据此放入 related_input_item_ids；Relay 收到旧回答 audio.done 时，将主输入和所有 related 输入一起标 completed。

这条控制路径没有证明后续问题完整消费，更没有检查答案覆盖它。被标 completed 的后续 call 会走 settlement，不再查询，可能造成漏问。函数边界已确认；真实 GPU 是否在指定窗口产生响应、是否实际漏问，需定向交错验收，不能从静态结果直接宣称线上复现。

待优化：记录每输入的 start/end/final-ASR，区分“已开始/已完整消费/被回答覆盖”；related 仅作关联，不自动授权业务完成。未完整消费或未覆盖的输入保持 pending，后续不得被误当重复工具调用。

## F4：复合 facet 与 CueKB 单候选支持规则不匹配（P1，联合函数逻辑已复现）

位置：[CueKBAdapter._question_facets](../../apps/api/app/tools/adapters.py)；[CueKB _assemble](https://github.com/ttonghao-thao/CueKB/blob/30970e9f6b0d36e012180022c2421c4d48a9de22/src/cuekb/services/evidence_query.py#L421)。

`What is its price and weight?` 被本项目作为单个 facet。CueKB 的 direct 要求该 facet 的剩余词项全部包含于某一个完整 candidate。两条分别载有价格、重量的完整同产品证据，虽合起来覆盖问题，逐候选都不能满足。抽取实际 `_assemble` 支持判定循环、注入这两条受控 candidate，返回 `f1=unassessed`。这不是生产检索试验，但足以确认两端规则的表达缺口。

待优化：与 CueKB 协调子问题/联合证据覆盖契约；消费者确定拆分时保留精确跨度，拆不动时由 provider 明确支持组合覆盖或返回澄清。不能放宽成“有关键词重叠即 direct”，也不能将 2.1 契约解析成功当作自然语言覆盖成功。CueKB 当前无校准 entailment，词项集合判定本身不是完整语义证明。

## F5：企业首问仍依赖原生模型选工具（P1，已知能力限制，当前版本现场待验）

位置：[relay.receiver](../../apps/api/app/voice/relay.py)、[工具提示](../../config/voice-qa-prompt.txt)。一般回答路径可直接落库，不执行 CueKB；只有原生 call 进入 Registry。历史已有独立 Black/Golden 首问漏调用，但不能用旧提示/版本样本证明当前失败率。

待优化：冻结 prompt/目录/模型身份，用独立首问和追问金标衡量；明确企业问题应查而未查的失败，不能以一般回答结束算通过。先验证原生能力上限，继续保留不加分类器、不偷偷 ASR 补查、不强制 tool_choice 的边界。

## F6：显示 ASR 已解耦，LLM 仍逐 token 串行消费（P1，源码确认，当前速度待验）

位置：[model._inject_response_tokens/infer](../voicechat/opt/tritonserver/backends/nemotron-voicechat/model.py)、[voice_budget.fit_voice_result](../../apps/api/app/agent_runtime/voice_budget.py)。

每个工具 token 仍一次 append/anext；注入期间 display ASR 可推进，LLM 音频理解仍等待重放，提取期仍缓冲。最终 256-token 上限限制工作量，不代表延迟已达标。历史 331-token/10.756 秒数据说明旧串行成本，但不能外推为本次 GPU 实测。

待优化：分别预算预检往返、ACK、注入、PCM 追赶、正式首音频；只在同一引擎/序列语义成立时考虑受支持的批量消费，禁止盲目并发 append。预检最多七次顺序请求，每次等待上限八秒，当前无覆盖预检全过程的独立总期限；应纳入整体等待预算。真实显存/竞争/音频追赶仍需 GPU 验收。

## F7：包复验与实际交付之间有时序窗口（P1，源码确认，现场变更影响待验）

位置：[CueKBAdapter._invoke_evidence](../../apps/api/app/tools/adapters.py)、[relay.tool_worker](../../apps/api/app/voice/relay.py)。query 后立即 validate，之后才进行本地构包、数次 tokenizer 预检、锁和写入。原始 package.expires_at 没有成为实际发送前的门禁；发送时复查本项目权限/工具版本，不再次检查 CueKB content/ACL revision。

若预检等待时 CueKB 侧撤权或内容更新，第一次 valid 不能证明发送时仍 valid。本次没有实测越权或过期交付，不把 TTL 到期本身等同正文一定失效。

待优化：先定义证据复验保证的时间边界；尽量将唯一 validate 移至最终发送前，保留包标识/到期信息，变化则拒绝旧包、不自动再检索。跨服务无法消除最后一次检查到写入的全部竞态，应明确保证范围。

## F8：冗余调用结清不等于模型已收敛（P1，源码确认）

位置：[relay.tool_worker/watchdog](../../apps/api/app/voice/relay.py)。当前每次合法终态后调用都会返回小 settlement，避免第三次立即 VOICE_TOOL_LOOP；但模型仍可继续 call。连接有 128-call 上限，答案默认 60 秒 deadline，最终仍可关闭整条通话。不存在确定性的“改为正式回答”控制能力保证。

待优化：区分检索终态、原生 call 结清和业务回答终态；增加输入级结清预算/明确失败结果，并验证 VoiceChat 是否支持安全结束本轮后继续听下一问。不能单纯吞掉 call（会留下原生等待），不能增加无限结果注入或宣称提示词强制了模型。

## F9：固定连接寿命可截断长轮次（P2，保留的设计限制）

位置：[relay.watchdog](../../apps/api/app/voice/relay.py)、[Settings](../../apps/api/app/config.py)。默认 105 秒后空闲轮换，活动最多宽限 30 秒；长输入+多次注入+播放可能跨界，文件模式不自动续播。用户明确暂缓后台持续播报，继续非阻塞。

待优化：在语义安全边界轮换、提前提示、保留业务上下文；明确文件与麦克风恢复差异。不得以无限连接或自动重放 unknown 结果规避边界。

## 修复顺序与边界

先 F3/F1 防止漏问和误认证，再 F2/F4 修复产品绑定与组合事实覆盖；同步收紧 F8/F7 的终态和交付窗口；最后用同一配套版本验证 F5/F6，再评估 F9。本轮只记录，不实施以上建议。执行方法及本次只读证据仅在[验证手册](../validation.md)维护，状态只在[任务板](../TASK_BOARD.md)更新。
