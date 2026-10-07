# 门户与 HTTP/SSE/WS 契约

2026-10-07 按 `f498fa4` 核对。定义源为 [contracts.py](../apps/api/app/contracts.py)、[routes.py](../apps/api/app/api/routes.py)；完整 [OpenAPI](../contracts/openapi.json)、[上行](../contracts/portal-client-events.schema.json)/[下行](../contracts/portal-events.schema.json) 由脚本生成，按对象查阅，不整读 JSON。本文维护消息含义和 UI 关联，配置归部署，Provider 原始事件归接入。

## 1. 通话与界面

点击 Start call 才创建新的 conversation/owner/token，再申请语音 session；token 只在当前标签页内存中，刷新不能恢复历史。Voice ready 表示握手/格式就绪，不承诺自动问候。门户申请麦克风，ready 后持续发送含静音的音频；设备 capture Hz 与 sent Hz 分开显示。

用户转写以 `(epoch,item_id)` 更新右侧气泡，持久 Turn 用 input_item_id 原位接管；不凭文本相同合并。答案阶段实际口述按 response/segment 进入左侧正文，结束后保留；固定 ACK 只显示状态。完整业务答案和引用展开显示，文字输入直接显示业务答案。一般无工具回答按 input_item_id 关联，标明 Model general answer / No company sources were checked，不创建假知识 Turn。

`portal.presentation.updated` 的失败结果显示独立警告，不用正确文字掩盖错误口述；历史 speech_validation 按授权过滤。已知 Turn 的 SSE final 原位更新，未知 Turn 才补 GET messages；迟到 running 快照不覆盖 final。向上阅读不强制滚到底部，引用按实际 citation 展示片段/上下文/版本，不能凭无 URL 的证据生成原件下载地址。

Stop playback 保留查询和正文；Cancel search 使当前任务失效；End call 释放设备并撤销 capability。Check progress 读取真实状态，不检索、不调用模型、不报虚构百分比。文字提交会结束当前语音，UI 明示这一行为。异常恢复重新开始通话，不恢复录音或旧播放队列。

## 2. HTTP 与 SSE

业务路径前缀 `/api/v1`。创建会话不需要登录，返回仅展示一次的 access_token；后续会话请求使用 Bearer token。客户 capability 不能调用管理 API；身份/KB/历史撤权的完整规则归 [接入 §6](integration.md#6-独立通话与知识范围d02d13)。

| 方法和路径 | 用途/关键返回 |
| --- | --- |
| GET `/capabilities` | 部署模式/策略/工具版本、后台 available_tools 与原生 native_tools；不是已有会话快照或实测结论 |
| POST `/conversations` | 点击开始通话时请求 title、locale；返回 id、epoch、request_revision、locale、access_token；新会话仅接受 `en-US` |
| GET `/conversations/{cid}/messages` | 当前 capability 的 Turn（含 input_item_id、selected_tool、effective_executor、escalation_reason、execution_phase）及权限过滤后的 Record（含 created_at、口述 turn_id/input_item_id/answer_kind）；仅本次 capability 可读取 |
| POST `/conversations/{cid}/voice-sessions` | 返回 voice_session_id、epoch、request_revision、ws_url（含一次性 ticket） |
| DELETE `/conversations/{cid}/voice-sessions/{sid}` | 关闭语音、使旧 epoch/未完成任务失效，保留历史；关闭原因与用户硬中断分开 |
| DELETE `/conversations/{cid}` | 结束本标签页通话、关闭语音并撤销 call token |
| POST `/conversations/{cid}/messages` | `{text}`，要求 Idempotency-Key；202 返回 task_id/turn_id、epoch、request_revision、status；新问题 supersede 旧运行任务 |
| GET `/conversations/{cid}/events` | SSE 业务流；`Last-Event-ID` 或 `after` 恢复游标 |
| POST `/conversations/{cid}/playback/stop` | expected_epoch、expected_revision、可选 response_id；停止当前播报，不取消查询 |
| POST `/conversations/{cid}/tasks/current/cancel` | expected_epoch、expected_revision；取消当前查询并拒绝晚到结果 |
| POST `/conversations/{cid}/tasks/current/progress` | expected_epoch、expected_revision；返回当前 status/phase/message，版本过期为 409，不新增任务/检索 |
| POST `/conversations/{cid}/interrupt` | `{expected_epoch}`；当前是取消业务、失效 epoch、关闭语音的硬中断 |

同一文字幂等键相同正文不重复执行，不同正文返回 409。管理 API 不属于客户协议权限集合。验证身份和知识范围见 [接入文档](integration.md)。

SSE 的 `id` 是持久化 `server_seq`，不是 JSON `event_id`。仅重放业务事件，不重放实时音频。前端按 `event_id` 去重；不得把 SSE 和 WebSocket 的序号混为一个全局连续序列。

SSE 由持久 Event/server_seq/cursor 支持重放；同进程事务提交后唤醒，回滚不通知，跨进程/漏通知保留 SQL 补查。事件和历史均复核当前权限；内部授权标签不属于公开 schema。

## 3. WebSocket 与音频

使用签发的 ws_url：`/api/v1/voice-sessions/{sid}/stream?ticket=...`；公网 HTTPS 对应 WSS。票据短期一次性、绑定 owner/conversation/epoch/Origin，query/token 不写访问日志。握手失败不能换身份继续。

### 3.1 上行

| 客户端事件 | payload / 语义 |
| --- | --- |
| portal.audio.append | format=pcm16、sample_rate=24000、base64 audio；外壳含 epoch、seq 和可选 event_id |
| portal.playback.ack | response_id、played_samples、可选 finished=false；附 epoch，仅是播放估计 |
| portal.playback.stop | 可选 response_id，附 epoch；清/抑制播放，不取消业务 |
| portal.interrupt | 附 epoch；硬中断语义 |
| portal.session.close | 附 epoch；关闭本次语音资源 |

音频为 little-endian PCM16、单声道、24 kHz；每帧 80 ms = 1920 samples = 3840 bytes，无 WAV header。浏览器先从设备采样率重采样；seq 连接内递增，重复丢弃，缺帧/过快/持续停顿明确报错。未知控制事件拒绝，不从文本猜语义；取消查询使用带 revision 的 HTTP 接口。

finished=true 只在该 response 收到 audio.done 且 Worklet 对应队列排空后发送；服务端要求未抑制、发送结束且样本数等于 sent_samples。排空不是实际听到，部分样本不能确认完成。

### 3.2 下行与关联

事件外壳包含 type/event_id/conversation_id/epoch/request_revision/turn_id/server_seq/timestamp/payload；turn_id 可空，不能伪造一般回答与知识任务的关系。WS 序号在连接内递增，旧连接/epoch 的数据丢弃。

| 事件 | 通道 | payload / 客户端行为 |
| --- | --- | --- |
| `portal.session.ready` | WS | 确认音频格式后开始采集发送 |
| `portal.transcript.delta` / `done` | WS | item_id、text；首个非空文本更新右侧气泡，done 定稿，Turn 以 `input_item_id` 接管 |
| `portal.speech_text.delta` / `done` | WS | response_id、segment_index、phase、text，Q07 可选 input_item_id/answer_kind；知识回答关联 turn_id，一般回答 turn_id 可空；状态阶段不进正文 |
| `portal.audio.delta` / `done` | WS | response_id、delta 的 audio；done 的 phase 区分状态提示和答案，表示服务端发完，客户端仍须排空缓冲；过期 epoch 一律丢弃 |
| `portal.input.state` | WS | state=speaking/quiet；仅输入状态，不自动等于取消业务 |
| `portal.tool.started` | SSE | 客户可理解的查询状态；不暴露私有工具参数 |
| `portal.answer.final` | SSE | canonical AnswerBundle 及终态；D2 在续答结束/事后检查后才提交，一般无工具回答不伪造该知识事件 |
| `portal.presentation.updated` | SSE / WS | response_id、answer_id、input_item_id、mode、status、reason_code；实际 Provider 转写的呈现检查结果，after_audio；失败警告与完整文字答案分开 |
| `portal.playback.clear` | SSE / WS | epoch 失效或同 epoch 清播放；有 response_id 时只抑制该响应，不能停止其他当前合法回答；旧 epoch clear 不影响新连接 |
| `portal.session.ended` | SSE/连接生命周期 | 显示结束并释放设备；也须处理 WS close，不能只依赖单个消息 |
| `portal.error` | WS；HTTP 使用错误响应 | code、message、retryable、trace_id；按错误恢复，不无限重试 |

下行 delta 长度可不同于上行固定帧长，按真实字节计算 samples。并非所有事件都在 SSE 与 WS 重复发送。实际呈现检查与 answer/input/Turn/call/response 关联规则归 [口述与交付](live-agent-implementation.md)。

### 3.3 播放、时序与背压

| 边界 | 浏览器行为 |
| --- | --- |
| 新连接/epoch | 清队列，等待 ready 才允许播放 |
| 合法 audio.delta | 按 response 独立重采样并入队 |
| audio.done | 刷新短尾帧；继续排空缓冲，保留身份至对应 Worklet 完成 |
| 同 epoch clear / Stop playback | 清指定响应/当前缓冲，保留会话许可，丢弃该响应迟到 PCM；下一合法响应可播放 |
| 失效/结束/网络错误 | 禁止旧输出、清队列、停止设备；旧 epoch 不得恢复 |

页面静音只影响上行麦克风，音量只影响本地播放。播放环有界、短尾帧须可排空；积压显式报错，不以无限缓冲掩盖延迟。Worklet 的内部 done 转为公开 finished ACK，按响应独立处理；不能因 ACK 提示播完而让业务答案提前完成。

普通 speech_started/quiet 不取消查询。工具只有绑定最终 ASR 后才执行；输出须持有当前许可，ACK 不消耗最终答案许可。精确上游时序归 [接入 §3](integration.md#3-voicechat-接入与能力门槛)。轮换/重连和无确认状态归 [交付 §2](live-agent-implementation.md#2-结束确认台账与恢复)，不能把 audio.done 当作设备已经播放完。

## 4. 兼容与错误

- v1 的已定义字段和音频格式不能暗改；新格式/二进制协议须协商或新版本。可选字段兼容旧数据，未知非关键展示事件可忽略，未知客户端控制必须拒绝。
- 401/403、429、契约错误、超时和未命中分开处理；不把所有错误显示为无知识。VoiceChat 失败可以显式使用文字入口，不能回退 mock。
- VOICE_TOOL_REQUIRED 是严格模式未满足工具许可；一般模式合法无工具回答不触发它。未知工具、参数错误、原生回答超时和证据检查失败按各自路径结束，不自动补查或伪造成功。
- Answer/Turn/来源面板使用一致业务终态；错误响应只公开稳定 code/message/retryable/trace_id，不转发上游原文、密钥或堆栈。完整枚举和验证约束以代码/生成 schema 为准。

修改后按场景查 `tests/contract/test_protocol.py`、`tests/integration/test_voice.py`/`test_live_agent_interactions.py`、`tests/e2e/portal.spec.ts`/`voice-audio.spec.ts` 与 `apps/web/tests/worklet.test.mjs`。真实音频标准归 [验收清单](development/validation.md)，不在接口页复制测试数量。
