> 历史快照：源自应用文档基线 `f498fa4`，2026-10-07 归档；保留当时方案/证据，不能用于推断当前实现。仅相对链接重定位；当前入口见 [文档索引](../../README.md)。

# HTML 门户与消息/语音接口

更新：2026-10-04。本文包含 Q06 统一聊天与 Q07 可配置问答的当前本地契约；真实语音、选路和设备验收仍归 D07/Q07-E。Q07 新增可选输入/答案来源元数据，音频格式与 v1 外壳不变；Worklet 排空标记不暴露为新服务端事件。总架构见 [architecture.md](architecture.md)。

## 1. 简单门户

一个客户页面即可：开始语音、结束语音、连接/聆听/查询/播放状态、用户转写、实际口述字幕、最终答案与可展开引用。麦克风被拒绝、断网或语音不可用时给出明确恢复提示；可选显示文字输入。

- 页面加载不创建 conversation 或读取历史；每个标签页点击 “Start call” 后创建全新的 conversation/call token，再申请语音 session。token 只在该标签页内存中保存，刷新即丢失。
- 开始通话成功以 `Voice ready` 为准；不承诺自动语音欢迎，连接初始未桥接输出被抑制。用户主动点击后申请麦克风，ready 后连续发送音频，包括静音。门户显示浏览器实际选择的设备、采样率、声道及 echo cancellation/noise suppression/auto gain 设置；这些诊断值不等同于收音准确率。门户使用公网 HTTPS，使页面满足浏览器 secure context 前提；证书信任、实际麦克风授权、设备选择和英文识别质量仍须在 D07 实机确认。
- `portal.transcript.delta/done` 以 `(epoch,item_id)` 更新右侧用户气泡；持久 Turn 的 `input_item_id` 使同一气泡原位接管，不按文本相等归并。`portal.speech_text.delta/done` 的答案阶段用 `turn_id`、`response_id`、`segment_index` 更新左侧口述正文，done 后保留。固定工具 ACK 只显示状态；知识完整答案和来源在语音 Turn 中展开，文字 Turn 直接显示业务答案。dual_tools/general_qa 无工具回答以 input_item_id 关联普通助手正文，标明 Model general answer / No company sources were checked，不创建假知识 Turn；开始一般回答时清理上一轮来源面板。
- 客户页面不展示工具配置、模型参数、内部运行日志或后台管理菜单。
- 当前交互区分“停止播报”和“取消查询”：前者清除客户端缓冲并由服务端抑制当前 response，保留仍有效业务任务；后者使当前 revision 失效，存在无法安全结清的原生 call 时关闭旧语音连接。
- 显示业务答案与实际语音字幕的区别；来源与版本可查看，工具密钥和内部地址不可出现在页面。
- 客户入口复用现有 TypeScript/AudioWorklet 采集、重采样和播放模块，并由现有构建链输出静态 HTML；工具管理不进入客户页面。

当前门户为英文，引用可展开 `context_parts`、截断提示和关系证据；原件下载不在已实现入口中。字段映射见 [接入 §2](../../integration.md#2-cuekb-当前接入契约d03e01e02)，本页不重复供应商结构。

## 2. “标准消息接口”的含义

使用通用 HTTP JSON、SSE、WebSocket 传输；`/api/v1` 和 `portal.*` 是本项目公开且版本化的应用契约，不是 NVIDIA 原生 API，也不声称兼容 OpenAI Realtime。

门户只处理会话、音频、字幕、状态、答案与错误。CueKB schema、VoiceChat 原生 function call 和供应商凭据均留在服务端。新增第三方工具不要求门户理解供应商消息。

当前代码依据：`apps/api/app/contracts.py`、`api/routes.py`、`voice/gateway.py`，以及 `apps/web/src/audio/VoiceClient.ts`。生成契约为 [HTTP OpenAPI](../../../contracts/openapi.json)、[服务端事件联合类型](../../../contracts/portal-events.schema.json)、[客户端事件联合类型](../../../contracts/portal-client-events.schema.json)、[答案](../../../contracts/answer-bundle.schema.json)。网关在收发边界执行同一套严格校验；未知客户端事件、额外身份字段和错误音频格式会被拒绝。

## 3. HTTP 与 SSE（当前路径）

所有业务接口都在 `/api/v1` 下。创建 conversation 不需要登录；响应返回一次性展示的高熵 `access_token`。此后该 conversation 的 HTTPS/SSE 请求必须使用 `Authorization: Bearer <call_access_token>`，结束 conversation 后 token 失效。

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

同一文字幂等键相同正文不重复执行，不同正文返回 409。管理 API 不属于客户协议权限集合。验证身份和知识范围见 [接入文档](../../integration.md)。

SSE 的 `id` 是持久化 `server_seq`，不是 JSON `event_id`。仅重放业务事件，不重放实时音频。前端按 `event_id` 去重；不得把 SSE 和 WebSocket 的序号混为一个全局连续序列。

SSE 的持久 Event 表与 server_seq/cursor 保持不变。同进程提交后通知订阅者立即读取已提交事件；不推送未提交对象，不在回滚时通知。跨进程继续 300 ms SQL 补查，监听器随连接释放。门户收到鉴权过滤后的 final 后直接更新已加载的 Turn，未知 Turn 才补取消息；延迟到达的历史快照不得覆盖更高 epoch/revision 或当前已验证的 final。

## 4. WebSocket 和音频（当前 v1）

连接签发的 `ws_url`，路径 `/api/v1/voice-sessions/{sid}/stream?ticket=...`；公网 HTTPS 部署使用 WSS。票据有效 60 秒、一次性，绑定 call owner、conversation、epoch、Origin；握手失败不能静默切换其它身份。URL query 与凭据不得写访问日志。

本阶段门户可直接访问 Web 容器的公网 HTTPS `8087`；Nginx 在 Web 镜像内终止 TLS 并提供静态门户，无需另行部署。无需登录或 tenant；每次 call 的 owner/token 由服务端生成，KB 范围只来自部署配置。浏览器只请求 Web 同源 `/api/`，镜像内的 Nginx 将其转到容器网络中的 `api:8000`；API 不映射宿主端口。

### 4.1 上行消息

```json
{
  "type": "portal.audio.append",
  "event_id": "<optional-client-event-id>",
  "epoch": 1,
  "seq": 0,
  "payload": {
    "format": "pcm16",
    "sample_rate": 24000,
    "audio": "<base64-encoded-PCM-bytes>"
  }
}
```

`audio` 为示意占位值。实际每帧 PCM16 little-endian、单声道、24 kHz、80 ms，即 1920 samples / 3840 bytes；不是 WAV 文件，不带 WAV header。浏览器设备采样率先经重采样转换。门户分别显示设备 `Hz capture` 和 `24000 Hz sent`，设备 48 kHz 不代表按 48 kHz 发送。上行 seq 为连接内递增非负整数；重复丢弃，缺帧/过快/持续停顿触发显式错误。连接绑定身份与 conversation，正文不能覆盖。

| 客户端事件 | payload / 行为 |
| --- | --- |
| `portal.audio.append` | 上述音频字段；附 epoch、seq |
| `portal.playback.ack` | response_id、played_samples、可选 finished=false，附 epoch；finished=true 须已收到 audio.done 且该响应排空，服务端核对全部发送样本；只是播放估计 |
| `portal.playback.stop` | 可选 response_id，附 epoch；立即清播放器并抑制当前或下一段 response，不取消业务任务 |
| `portal.interrupt` | 附 epoch；同 HTTP 硬中断语义 |
| `portal.session.close` | 附 epoch；释放本次语音资源 |

取消查询使用上述 HTTP revision 条件接口；WS 仅新增停止播报事件。未知控制事件仍按协议错误关闭，不能猜测语义。

### 4.2 服务端事件外壳

```json
{
  "type": "portal.session.ready",
  "event_id": "<event-id>",
  "conversation_id": "<conversation-id>",
  "epoch": 1,
  "request_revision": 0,
  "turn_id": null,
  "server_seq": 1,
  "timestamp": "2026-09-16T00:00:00Z",
  "payload": {"sample_rate": 24000, "format": "pcm16", "chunk_ms": 80, "is_mock": false}
}
```

示例仅展示格式，不代表真实服务已验证。字段定义复用 PortalEvent；实时 WS `server_seq` 在连接内递增，SSE 序号属于持久业务流。客户端用绑定的 voice_session 与 epoch 隔离重连前数据，用 response_id 关联某次语音输出。

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

下行音频按握手确认的 24 kHz PCM16 播放，实际 delta 长度不要求与上行 80 ms 相同；按字节长度计算 samples。当前 WS 事件 turn_id 可空，不伪造用户转写与工具轮次的关联。持久业务事件主要走 SSE，不能假定所有事件在两个通道重复发布。

本轮口述/结束/等待交互的精确语义见 [Live §1–3](../../live-agent-implementation.md)。messages 的 speech_validation 历史与 presentation SSE 按原授权范围过滤；matched 只代表转写通过文本检查，completed 只代表响应边界已写入，finished ACK 仍不证明听到。浏览器按响应独立排空，轮换等待播放结束估计或抑制，不恢复旧播放队列。

### 4.3 时序和背压

普通插话和停顿由 VoiceChat 推理处理，不触发应用自动取消。Stop playback 是显式本地控制，用于立即清空已到达的音频，并不取消查询；Cancel search/新请求替换控制任务有效性，结果提交和写回仍复核。原生 HTML 的连续播放方式不需要逐轮 ID，当前门户的严格抑制才依赖有序输出归属。

建立 conversation/capability → 签发票据 → 连接 WS → VoiceChat 握手 → portal ready 后，采集与播放并行运行。用户 input item 的开始、临时转写和最终转写是同一输入；原生工具只能消费尚未绑定的 input item，网关等待完成态 ASR 后进入 Runtime。工具参数不能替代最终转写，无输入的工具不能创建业务 Turn。

外置业务答案提交后走 SSE；D2 先回填 EvidenceReady，待原生字幕/audio.done 和检查完成后提交 canonical final。一般无工具回答以输入/有序 response 许可走 WS 并持久 Record。合法工具 response 可播放固定 ACK，工具结果写回后最多放行一个后续回答。ACK 正在播报时结果提前到达，不能消耗最终答案许可；供应商沿用原 response 返回最终答案时须回收多余许可。新的输入活动不取消仍有效的已授权回答，legacy/knowledge_required 未经工具授权的实质性输出失败关闭；general_qa 仅允许已绑定当前输入的一般回答。供应商完整时序见 [接入 §3](../../integration.md#3-voicechat-接入与能力门槛)。

浏览器按下面的状态转换控制播放：

| 事件/操作 | 队列和许可 | 当前 response 身份 |
| --- | --- | --- |
| 初始连接 / 新 epoch | 清队列，等待 ready 才允许播放 | 清空 |
| 合法 audio.delta | 入队并按 24 kHz 解码/重采样 | 记录该 response |
| audio.done | 刷新重采样尾部，允许短回答排空；继续播放现有队列 | 保留到 Worklet 报实际排空 |
| Stop playback / 同 epoch clear | 清队列，保留会话播放许可；丢弃已停止 response 的晚到 PCM | 停止请求携带仍在缓冲的 ID；没有当前 ID 时服务端按契约抑制下一段 |
| 下一段合法 response | 正常入队播放 | 切到新 response，不等待被抑制旧段的 done |
| 失效 / 结束 / 网络或音频错误 | 持续禁止播放并清队列、停止设备 | 清空；旧 epoch/旧连接不能恢复 |

Worklet 的内部 `done` ACK 表示播放队列排空，向服务端发送的仍只有公开 `response_id/played_samples`。页面静音只影响上行麦克风，不关闭连接；音量控制只影响本地播放。播放估计不能证明扬声器有效或客户听见。

所有方向保持有界队列和背压。门户上行固定序号及帧长，捕获停顿/网络积压报错；播放环最多缓冲约一秒，初始目标 160 ms，短尾帧不应被起播阈值吞掉。服务端生成事件有独立的序号，不能把 WS 和持久 SSE 的计数合并。

取消查询推进 revision；无法安全结清 pending call 时关闭旧连接。恢复重新取票，不重放旧录音。默认 105 秒在输入 quiet 且无运行/待写回 call 时轮换，宽限内无法安全轮换则结束；历史恢复不等于恢复模型隐藏状态。

## 5. 版本和错误处理

- v1 固定当前音频格式与已定义必需字段；未来二进制音频/不同格式需要明确协商或新版本，不能暗改现有字段含义。
- 兼容性新增先由 capability 声明并提供降级；未知服务端非关键展示事件可忽略，未知客户端控制事件拒绝；格式/鉴权错误不得继续播放。
- 当前已有典型错误：AUTH_REQUIRED、FORBIDDEN、VOICE_UNAVAILABLE、VOICE_CAPACITY_EXCEEDED、VOICE_PROTOCOL_ERROR、VOICE_TOOL_REQUIRED、VOICE_SESSION_ROTATION_REQUIRED、VOICE_SESSION_EXPIRED、AUDIO_BACKPRESSURE。`VOICE_TOOL_REQUIRED` 表示 legacy/knowledge_required 下未满足工具输出许可；general_qa 的合法无工具回应不触发该错误。Q07 的 VOICE_TOOL_UNKNOWN/VOICE_TOOL_ARGUMENTS/VOICE_ANSWER_TIMEOUT 及证据检查失败由相应路径处理，不自动补查或假造成功。
- 工具错误在业务层映射为稳定的失败/无依据/澄清状态；不能把 401/403/429/5xx 都显示为“没有知识”。
- `failed` 是服务端运行/工具异常状态，不能由模型覆盖成功检索结果；已有正文与已校验 citations 的答案必须在 Answer、Turn 和来源面板显示同一终态。读取历史矛盾记录时以规范化后的 Answer 终态为准。
- `AGENT_DEADLINE_MS` 是知识 Turn 的直查、升级、外置模型、检索及答案回收总预算；D2 回收另取 QA_PROVIDER_ANSWER_TIMEOUT_MS 与剩余预算较小值，不是单个请求超时；验证部署默认并在 `.env.example` 显式填写 `30000` 毫秒，真实延迟分布仍在 D07 测量后冻结。
- 不把服务商 error 原文、密钥或内部堆栈直接转发客户。完整错误集合随实现和契约同步维护。

## 6. 接口验收

必须验证：独立简单客户端可接入、门户不直连供应商、身份/票据/Origin、正确音频格式、序号和去重、硬打断竞态、SSE 恢复、旧连接隔离、字幕与答案分离、窄屏和设备释放。D19 另覆盖固定 ACK 与快速结果竞态、audio.done 后仍在缓冲时停止、停止后下一轮恢复、旧 PCM 拒绝及 44.1/48 kHz 重采样。真实语音结论记录于 [验收报告](acceptance-report.md)，不以合成音频替代。

## 7. Q06：语音文字统一聊天展示

本节保留 2026-09-29 的设计基线与实施验收要求。M1–M4 已本地实现，D07 真实验收待执行；进度见 [任务板](../../TASK_BOARD.md#3-待完成与建议顺序)。论文与离线容器优化仍由 [Q05](../2026-09-28/voicechat-research-review.md) 单独维护。

### 7.1 已明确的展示目标与实施前差距

以用户提供的 GPT 语音交互截图为布局参考：用户文字在右侧浅色圆角气泡，助手文字在左侧以无背景正文展示，连续问答按顺序保留。用户的短追问和助手的短回复也各自保留；不把所有转写累积到一个字幕框。截图用于消息布局；当前交互仍为英文，Q07 已增加一般问答分支，不要求复制 GPT 的全部按钮和功能。

| 内容 | 实施前 | Q06 目标与本地实现 |
| --- | --- | --- |
| 用户 ASR | delta/done 在独立 Live captions 区；持久 voice Turn 到达后显示右侧问题气泡 | 首个非空 delta 即创建右侧用户气泡；后续 delta 更新，done 用最终 ASR 定稿同一条消息 |
| ASR 与后台查询 | Gateway 转发转写；原生工具与最终 ASR 就绪后桥接业务执行；不等待答案再转发 ASR | 明确输入展示和业务查询并行推进，用户气泡不依赖 Turn 创建、检索或答案完成 |
| 临时消息接管 | 完成的用户字幕按相同文本匹配 voice Turn 后移除 | 以稳定身份关联同一输入与 Turn，原位接管，不重复创建、闪烁或丢失消息 |
| 助手正文 | Turn 展示业务 `display_text`；实际口述在 Live captions，音频完成后清理 | 语音模式主正文流式展示 `portal.speech_text.*`，口述结束后保留，左侧无背景正文 |
| 完整答案和来源 | 主答案及 Sources 展示业务结果 | 语音模式将业务完整答案及引用放入可展开区域；文字模式继续直接展示业务 `display_text` |
| 后续发言、停止播放 | 新输入或播放清理会清理临时字幕 | 创建后续消息并保留已有正文；清理音频不删除已显示的文字 |

当前右侧用户气泡和左侧无背景答案布局可复用。主要缺口是文字来源、出现时机、消息身份与保留规则，并非只改颜色或圆角。

### 7.2 正确时序与消息更新

1. 浏览器发送音频，经 VoiceGateway → NvidiaVoiceChatAdapter → 独立 VoiceChat；现有音频格式、采集、播放与授权边界不变。
2. Adapter 返回用户 ASR delta 时，Gateway 即转发给浏览器，浏览器创建或更新当前用户气泡。若上游只提供 final，也须在收到 final 后立即展示，不能假造增量能力。
3. 最终 ASR 到达时，绑定对应 input item，并将最终文本发给浏览器，原位替换临时识别内容。与此同时，当该输入的合法原生工具调用已到达时，桥接 SessionCoordinator → BusinessRuntime → ToolRegistry → CueKB。业务执行仍必须等待最终 ASR，工具参数不能替代用户实际问题。
4. 此处“同步”指两条路径在同一阶段推进、互不等待业务答案，不指阻塞式调用，也不要求浏览器渲染 ACK 成为业务执行前提。ASR 发送应优先安排，但网络和浏览器调度不保证屏幕绘制一定早于 Coordinator 开始执行。若工具调用先到，等待对应 final ASR；若 final 先到，立即展示并等待合法工具调用。
5. 服务端创建持久 Turn 后，以明确的输入关联将临时用户消息升级为该 Turn 的用户消息。持久结果较晚、SSE 重放或历史补取均不得再创建同一问题的第二个气泡。
6. 业务答案提交后按现有 SSE 契约交付完整答案和来源，并经 Adapter 写回 VoiceChat。合法的实际口述 delta 更新关联的左侧助手正文，done 定稿；`audio.done` 和播放队列排空仅推进播放状态，不删除文字。
7. 下一次输入创建新的用户消息；后续回复按明确关联追加或更新助手段落。ASR 完成但业务未成功受理时仍保留用户文字，展示可理解的失败/恢复状态，不能凭气泡存在声称业务已受理。

这一时序修正“两个闭环串行完成后才返回用户文字”的误解：输入转写直接驱动展示，后台业务闭环独立推进，随后交付答案和口述。

### 7.3 消息关联、持久化与恢复设计

服务端负责建立输入、工具调用、业务 Turn 与口述 response 的关联；浏览器只消费应用契约，不自行解释 NVIDIA 工具协议。关联使用现有 `conversation_id`、epoch、input item、`call_id`、`turn_id`、`response_id` 以及应用新增的 `input_item_id`/`segment_index`；不要求供应商新增字段。

- 用户临时消息使用当前通话、epoch 和 input item 的身份定位；Turn 到达后保持同一前端消息身份。禁止只用文本相等、数组位置或“最近一次输入”进行接管，避免同一句问题连续出现时误合并。
- 工具等待 ACK 与最终回答可能分属不同 response，也可能沿用同一个 response。一个 Turn 允许多个口述片段；不能假设一个 response 就是一轮完整问答。等待 ACK 收敛为简短状态提示，最终口述作为助手正文；固定 ACK 只按本项目配置的精确短语和 response 阶段识别，不用任意口述文本推断阶段。
- 复用现有 Turn、Record、持久事件和 `/messages` 读取链，通过 Alembic 0006 补齐输入关联字段；口述 Record 的应用关联写入 payload，保留原有授权过滤。
- 实时 WS、持久 SSE 和历史快照通过各自序号及稳定消息身份去重；不能把两条传输的计数当作同一序列。历史快照不得覆盖更新的正文或终态，重复 done/final 不得重复追加。
- 同一次仍有效通话的断线恢复可补取已持久消息，不自动重放音频。未定稿文字如因断线没有持久版本，应标明中断，不声称恢复完整。刷新、新标签页或新通话继续使用新的 capability，不引入跨标签页或账号历史共享。
- 所有读取继续执行 owner、KB 权限和撤权过滤；口述文字也不得绕过来源权限。只接纳当前有效 epoch/连接和已授权输出；失效任务的晚到文字不能写入新消息或重新出现。

### 7.4 正文、完整答案与中断边界

语音模式的主正文来自已授权的 VoiceChat 口述转写，业务 `display_text` 保存在“View full answer / Sources”区域，避免两套答案在主时间线重复出现。文字模式仍直接展示业务答案。业务答案先完成而口述尚未到达时，可显示答案已就绪及展开入口，不提前把完整业务答案标成已口述。

引用归属于经过业务校验的答案，不能仅因两段文字关联同一 Turn，就声称 VoiceChat 的改写已逐句通过证据校验。Q07 D2 有口述结束后的有限数字/单位/条件检查；完整语义和实际音频质量仍属后续 Q02/Q05 验证。文本生成与实际播放也分开：到达的口述转写不证明客户已经听到；精确逐字播放高亮、已听边界不在本方案承诺内。

Stop playback 保留已收到正文并更新播放状态；普通 `speech_started` 保持模型自然插话语义，不自动取消查询。明确取消/替换继续使业务 revision 失效，保留可见的既有文字并注明中断，拒绝后续旧输出。若已有通过校验的外置文字答案，口述失败时仍可查看并明确语音失败；D2 未通过时不能把准备好的证据冒充成功答案，不得伪造口述正文。

新消息出现时，仅在用户位于列表底部时自动跟随；用户向上阅读时保留位置并提供返回最新消息入口。窄屏、长文本换行、滚动容器与底部通话控制区需一起验收，不新增客户可见的协议 ID、模型参数或日志。

### 7.5 影响模块与验收条件

已修改 `apps/web/src/App.tsx`、`style.css` 的统一消息渲染及状态归并，API 的 `voice/gateway.py` 输入/口述关联、`storage/` 的持久读取、API 事件与消息契约，以及对应后端和 `tests/e2e/portal.spec.ts` 回归。Alembic 0006 仅增 nullable 输入关联字段；保留 Coordinator → BusinessRuntime → ToolRegistry 及独立 VoiceChat/CueKB 部署边界。

实施验收必须覆盖：

1. 在业务受理/查询人为延迟时，ASR delta/final 直接出现在右侧气泡；done 修正同一消息，持久 Turn 到达后没有重复或消失。
2. 连续两次相同问题、多个不同问题、工具先到/ASR 先到、WS/SSE 交错、重复事件和晚到历史均保持正确身份、顺序及终态。
3. 实际口述逐步出现在左侧正文；口述完成、音频完成、停止播放和下一次输入后仍保留；完整业务答案与来源可展开，主正文不重复。
4. 工具 ACK 与最终回答共用/分用 response、口述失败、断线、取消/替换和 epoch 轮换均不串轮，不接纳已失效输出，不把生成文字标为已听。
5. 新通话/跨标签页隔离、KB 撤权、同通话恢复、文字模式、窄屏和向上阅读时的滚动行为不回退。
6. 本地受控测试、类型检查和构建通过后，另在 D07 用真实英文语音、VoiceChat/CueKB、浏览器设备验证时序、连续问答及可读性。本地测试通过不能作为 Q06/Q07 真实服务验收的证明。

### 7.6 实施结果与边界

Turn 通过 Alembic 0006 新增 nullable `input_item_id`，旧行不伪造关联。Gateway 在合法工具调用与最终 ASR 绑定后提交 Turn，把对应 response 的口述事件标记为 `phase=answer` 与 `turn_id`；同一 response 的多段口述用 `segment_index` 分开。固定 ACK 用已配置短语和响应阶段识别为 `phase=status`，不存成答案正文。`speech_text.done` 的受控 Record 保存 `turn_id`，按既有 owner、KB 范围与 revision/epoch 过滤；历史快照和 WS 各自按稳定身份归并。

现有部署升级前的旧 Turn 没有可靠的输入/口述关联，页面保留完整业务答案的展开入口，不将旧 `display_text` 冒充实际口述。正在使用的标签页可通过同一 capability 补取已定稿文字；刷新后的新 capability 不访问旧通话。生成口述文字不表示已听到。受控回归与构建见 [验收记录](acceptance-report.md)，真实 VoiceChat/CueKB、浏览器设备和撤权现场复测仍归 D07。

## 8. Q07：一般问答、证据续答与来源标识

Conversation 的模式/策略由服务端设置并快照，创建请求不接受模型、工具或策略覆盖字段。消息 UI 只呈现回答来源与状态，不暴露工具选路实现或要求用户选择检索执行器。

| 类型 | 数据与界面行为 | 检查边界 |
| --- | --- | --- |
| 一般模型回答 | Utterance + 实际口述 Record，按 input_item_id/response_id 定位；不出现在 /messages.items 的假 Turn 中，不显示企业 citations | answer_kind=general，composition=provider_general，validation_level=provider_only，verification_timing=not_verified |
| Nano D2 知识回答 | EvidenceReady 是内部准备状态，不是 final；任务 awaiting_provider_answer；字幕/音频流出，完成后检查并提交终态，失败保留可见文字并标记失败/清剩余播放 | answer_kind=knowledge，composition=nano_grounded，source_checked / after_audio；不等于全部断言或音频正确 |
| 外置知识回答 | 校验后提交完整文字与来源，并给 VoiceChat 拟口述文本；门户保留实际口述与 canonical 文本的区别 | external_llm / source_checked / before_audio 针对业务文本，不保证逐字朗读 |
| 旧数据 | 缺可选来源字段按 legacy/unknown 或已有信息呈现，不伪造输入关联/已查证/已听到 | 向后兼容，不回填成功证据 |

AnswerBundle 新增的 answer_kind/composition/validation_level/verification_timing 为可选元数据，定义以生成 schema 为准。Utterance 和 DeliveryAttempt 当前是内部表，不新增其独立 HTTP 查询或统一 Presentation API。一般回答从已开始的 response 起有有界结束等待；尚无输出不自动启动检索。D2 失败的 response clear 可经持久 SSE 或 WS 到达，客户端按 ID 去重/抑制，下一条合法回答仍可播放。

本地新增测试覆盖一般回答零知识 Turn、严格拒绝无工具、D2 续答提交/失败、部分数字/单位/条件、超时/取消及旧 clear 不停止下一条回答。真实选择质量、漏调用、字幕/实际音频一致性仍待 [Q07 验收矩阵](qa-routing-design.md#14-验收矩阵)。
