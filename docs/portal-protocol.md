# HTML 门户与消息/语音接口

更新：2026-10-02。§1–7 是当前本地实现契约；Q06 的真实服务与设备验收仍归 D07。§8 是 Q07 待编码增量，不能当作当前 API 已支持。D19 不新增公开事件字段；浏览器内部 Worklet 的排空标记不暴露为新的服务端协议。D20 使持久事件提交后唤醒 SSE，并直接应用 final 更新已有 Turn。总架构见 [architecture.md](architecture.md)。

## 1. 简单门户

一个客户页面即可：开始语音、结束语音、连接/聆听/查询/播放状态、用户转写、实际口述字幕、最终答案与可展开引用。麦克风被拒绝、断网或语音不可用时给出明确恢复提示；可选显示文字输入。

- 页面加载不创建 conversation 或读取历史；每个标签页点击 “Start call” 后创建全新的 conversation/call token，再申请语音 session。token 只在该标签页内存中保存，刷新即丢失。
- 开始通话成功以 `Voice ready` 为准；不承诺自动语音欢迎，连接初始未桥接输出被抑制。用户主动点击后申请麦克风，ready 后连续发送音频，包括静音。门户显示浏览器实际选择的设备、采样率、声道及 echo cancellation/noise suppression/auto gain 设置；这些诊断值不等同于收音准确率。门户使用公网 HTTPS，使页面满足浏览器 secure context 前提；证书信任、实际麦克风授权、设备选择和英文识别质量仍须在 D07 实机确认。
- `portal.transcript.delta/done` 以 `(epoch,item_id)` 更新右侧用户气泡；持久 Turn 的 `input_item_id` 使同一气泡原位接管，不按文本相等归并。`portal.speech_text.delta/done` 的答案阶段用 `turn_id`、`response_id`、`segment_index` 更新左侧口述正文，done 后保留。固定工具 ACK 只显示查询状态；业务完整答案和来源在语音 Turn 中展开，文字 Turn 直接显示业务答案。
- 客户页面不展示工具配置、模型参数、内部运行日志或后台管理菜单。
- 当前交互区分“停止播报”和“取消查询”：前者清除客户端缓冲并由服务端抑制当前 response，保留仍有效业务任务；后者使当前 revision 失效，存在无法安全结清的原生 call 时关闭旧语音连接。
- 显示业务答案与实际语音字幕的区别；来源与版本可查看，工具密钥和内部地址不可出现在页面。
- 客户入口复用现有 TypeScript/AudioWorklet 采集、重采样和播放模块，并由现有构建链输出静态 HTML；工具管理不进入客户页面。

当前门户为英文，引用可展开 `context_parts`、截断提示和关系证据；原件下载不在已实现入口中。字段映射见 [接入 §2](integration.md#2-cuekb-当前接入契约d03e01e02)，本页不重复供应商结构。

## 2. “标准消息接口”的含义

使用通用 HTTP JSON、SSE、WebSocket 传输；`/api/v1` 和 `portal.*` 是本项目公开且版本化的应用契约，不是 NVIDIA 原生 API，也不声称兼容 OpenAI Realtime。

门户只处理会话、音频、字幕、状态、答案与错误。CueKB schema、VoiceChat 原生 function call 和供应商凭据均留在服务端。新增第三方工具不要求门户理解供应商消息。

当前代码依据：`apps/api/app/contracts.py`、`api/routes.py`、`voice/gateway.py`，以及 `apps/web/src/audio/VoiceClient.ts`。生成契约为 [HTTP OpenAPI](../contracts/openapi.json)、[服务端事件联合类型](../contracts/portal-events.schema.json)、[客户端事件联合类型](../contracts/portal-client-events.schema.json)、[答案](../contracts/answer-bundle.schema.json)。网关在收发边界执行同一套严格校验；未知客户端事件、额外身份字段和错误音频格式会被拒绝。

## 3. HTTP 与 SSE（当前路径）

所有业务接口都在 `/api/v1` 下。创建 conversation 不需要登录；响应返回一次性展示的高熵 `access_token`。此后该 conversation 的 HTTPS/SSE 请求必须使用 `Authorization: Bearer <call_access_token>`，结束 conversation 后 token 失效。

| 方法和路径 | 用途/关键返回 |
| --- | --- |
| GET `/capabilities` | 配置和适配器声明的能力；部署声明不等于自动实测 |
| POST `/conversations` | 点击开始通话时请求 title、locale；返回 id、epoch、request_revision、locale、access_token；新会话仅接受 `en-US` |
| GET `/conversations/{cid}/messages` | 当前用户的 Turn（含 `input_item_id`）及权限过滤后的转写 Record（含 `created_at`、口述 `turn_id`）；仅本次 capability 可读取 |
| POST `/conversations/{cid}/voice-sessions` | 返回 voice_session_id、epoch、request_revision、ws_url（含一次性 ticket） |
| DELETE `/conversations/{cid}/voice-sessions/{sid}` | 关闭语音，保留会话历史；当前同时触发硬中断 |
| DELETE `/conversations/{cid}` | 结束本标签页通话、关闭语音并撤销 call token |
| POST `/conversations/{cid}/messages` | `{text}`，要求 Idempotency-Key；202 返回 task_id/turn_id、epoch、request_revision、status；新问题 supersede 旧运行任务 |
| GET `/conversations/{cid}/events` | SSE 业务流；`Last-Event-ID` 或 `after` 恢复游标 |
| POST `/conversations/{cid}/playback/stop` | expected_epoch、expected_revision、可选 response_id；停止当前播报，不取消查询 |
| POST `/conversations/{cid}/tasks/current/cancel` | expected_epoch、expected_revision；取消当前查询并拒绝晚到结果 |
| POST `/conversations/{cid}/interrupt` | `{expected_epoch}`；当前是取消业务、失效 epoch、关闭语音的硬中断 |

同一文字幂等键相同正文不重复执行，不同正文返回 409。管理 API 不属于客户协议权限集合。验证身份和知识范围见 [接入文档](integration.md)。

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
| `portal.playback.ack` | response_id、played_samples，附 epoch；仅估计实际播放进度，不证明客户听到 |
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
| `portal.speech_text.delta` / `done` | WS | response_id、segment_index、phase、text；答案阶段事件带 `turn_id`，更新并保留左侧正文；状态阶段不进入正文 |
| `portal.audio.delta` / `done` | WS | response_id、delta 的 audio；done 的 phase 区分状态提示和答案，表示服务端发完，客户端仍须排空缓冲；过期 epoch 一律丢弃 |
| `portal.input.state` | WS | state=speaking/quiet；仅输入状态，不自动等于取消业务 |
| `portal.tool.started` | SSE | 客户可理解的查询状态；不暴露私有工具参数 |
| `portal.answer.final` | SSE | 已校验 AnswerBundle；展示答案、来源、必要卡片 |
| `portal.playback.clear` | SSE / WS | SSE 处理 epoch 失效；WS 同 epoch 停止当前播放。晚到旧 epoch clear 不能清掉新连接 |
| `portal.session.ended` | SSE/连接生命周期 | 显示结束并释放设备；也须处理 WS close，不能只依赖单个消息 |
| `portal.error` | WS；HTTP 使用错误响应 | code、message、retryable、trace_id；按错误恢复，不无限重试 |

下行音频按握手确认的 24 kHz PCM16 播放，实际 delta 长度不要求与上行 80 ms 相同；按字节长度计算 samples。当前 WS 事件 turn_id 可空，不伪造用户转写与工具轮次的关联。持久业务事件主要走 SSE，不能假定所有事件在两个通道重复发布。

### 4.3 时序和背压

普通插话和停顿由 VoiceChat 推理处理，不触发应用自动取消。Stop playback 是显式本地控制，用于立即清空已到达的音频，并不取消查询；Cancel search/新请求替换控制任务有效性，结果提交和写回仍复核。原生 HTML 的连续播放方式不需要逐轮 ID，当前门户的严格抑制才依赖有序输出归属。

建立 conversation/capability → 签发票据 → 连接 WS → VoiceChat 握手 → portal ready 后，采集与播放并行运行。用户 input item 的开始、临时转写和最终转写是同一输入；原生工具只能消费尚未绑定的 input item，网关等待完成态 ASR 后进入 Runtime。工具参数不能替代最终转写，无输入的工具不能创建业务 Turn。

已接受业务答案走 SSE；合法工具 response 可播放固定等待 ACK，工具结果写回后最多放行一个后续回答。ACK 正在播报时结果提前到达，不能消耗最终答案许可；供应商沿用原 response 返回最终答案时须回收多余许可。新的输入活动不取消仍有效的已授权回答，未桥接直接输出仍失败关闭。供应商完整时序见 [接入 §3](integration.md#3-voicechat-接入与能力门槛)。

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
- 当前已有典型错误：AUTH_REQUIRED、FORBIDDEN、VOICE_UNAVAILABLE、VOICE_CAPACITY_EXCEEDED、VOICE_PROTOCOL_ERROR、VOICE_TOOL_REQUIRED、VOICE_SESSION_ROTATION_REQUIRED、VOICE_SESSION_EXPIRED、AUDIO_BACKPRESSURE。`VOICE_TOOL_REQUIRED` 表示用户完整发言后 VoiceChat 试图绕过 `consult_service_agent` 直接回答；该输出被拒绝，不能作为客服答案播放。
- 工具错误在业务层映射为稳定的失败/无依据/澄清状态；不能把 401/403/429/5xx 都显示为“没有知识”。
- `failed` 是服务端运行/工具异常状态，不能由模型覆盖成功检索结果；已有正文与已校验 citations 的答案必须在 Answer、Turn 和来源面板显示同一终态。读取历史矛盾记录时以规范化后的 Answer 终态为准。
- `AGENT_DEADLINE_MS` 是首次模型调用、CueKB 检索和最终模型回答的完整业务 Turn 总预算，不是单个请求超时；验证部署默认并在 `.env.example` 显式填写 `30000` 毫秒，真实延迟分布仍在 D07 测量后冻结。
- 不把服务商 error 原文、密钥或内部堆栈直接转发客户。完整错误集合随实现和契约同步维护。

## 6. 接口验收

必须验证：独立简单客户端可接入、门户不直连供应商、身份/票据/Origin、正确音频格式、序号和去重、硬打断竞态、SSE 恢复、旧连接隔离、字幕与答案分离、窄屏和设备释放。D19 另覆盖固定 ACK 与快速结果竞态、audio.done 后仍在缓冲时停止、停止后下一轮恢复、旧 PCM 拒绝及 44.1/48 kHz 重采样。真实语音结论记录于 [验收报告](acceptance-report.md)，不以合成音频替代。

## 7. Q06：语音文字统一聊天展示

本节保留 2026-09-29 的设计基线与实施验收要求。M1–M4 已本地实现，D07 真实验收待执行；进度见 [任务板](TASK_BOARD.md#3-待完成与建议顺序)。论文与离线容器优化仍由 [Q05](voicechat-research-review.md) 单独维护。

### 7.1 已明确的展示目标与实施前差距

以用户提供的 GPT 语音交互截图为布局参考：用户文字在右侧浅色圆角气泡，助手文字在左侧以无背景正文展示，连续问答按顺序保留。用户的短追问和助手的短回复也各自保留；不把所有转写累积到一个字幕框。截图不改变本期英文知识库客服范围，也不要求复制 GPT 的全部按钮和功能。

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

引用归属于经过业务校验的答案，不能仅因两段文字关联同一 Turn，就声称 VoiceChat 的改写已逐句通过证据校验。口述质量检查仍属 Q02/Q05。文本生成与实际播放也分开：到达的口述转写不证明客户已经听到；精确逐字播放高亮、已听边界不在本方案承诺内。

Stop playback 保留已收到正文并更新播放状态；普通 `speech_started` 保持模型自然插话语义，不自动取消查询。明确取消/替换继续使业务 revision 失效，保留可见的既有文字并注明中断，拒绝后续旧输出。口述失败时仍可查看已验证的完整文字答案，并明确语音失败；不得伪造口述正文。

新消息出现时，仅在用户位于列表底部时自动跟随；用户向上阅读时保留位置并提供返回最新消息入口。窄屏、长文本换行、滚动容器与底部通话控制区需一起验收，不新增客户可见的协议 ID、模型参数或日志。

### 7.5 影响模块与验收条件

已修改 `apps/web/src/App.tsx`、`style.css` 的统一消息渲染及状态归并，API 的 `voice/gateway.py` 输入/口述关联、`storage/` 的持久读取、API 事件与消息契约，以及对应后端和 `tests/e2e/portal.spec.ts` 回归。Alembic 0006 仅增 nullable 输入关联字段；保留 Coordinator → BusinessRuntime → ToolRegistry 及独立 VoiceChat/CueKB 部署边界。

实施验收必须覆盖：

1. 在业务受理/查询人为延迟时，ASR delta/final 直接出现在右侧气泡；done 修正同一消息，持久 Turn 到达后没有重复或消失。
2. 连续两次相同问题、多个不同问题、工具先到/ASR 先到、WS/SSE 交错、重复事件和晚到历史均保持正确身份、顺序及终态。
3. 实际口述逐步出现在左侧正文；口述完成、音频完成、停止播放和下一次输入后仍保留；完整业务答案与来源可展开，主正文不重复。
4. 工具 ACK 与最终回答共用/分用 response、口述失败、断线、取消/替换和 epoch 轮换均不串轮，不接纳已失效输出，不把生成文字标为已听。
5. 新通话/跨标签页隔离、KB 撤权、同通话恢复、文字模式、窄屏和向上阅读时的滚动行为不回退。
6. 本地受控测试、类型检查和构建通过后，另在 D07 用真实英文语音、VoiceChat/CueKB、浏览器设备验证时序、连续问答及可读性。已有测试通过不能作为 Q06 已实施的证明。

### 7.6 实施结果与边界

Turn 通过 Alembic 0006 新增 nullable `input_item_id`，旧行不伪造关联。Gateway 在合法工具调用与最终 ASR 绑定后提交 Turn，把对应 response 的口述事件标记为 `phase=answer` 与 `turn_id`；同一 response 的多段口述用 `segment_index` 分开。固定 ACK 用已配置短语和响应阶段识别为 `phase=status`，不存成答案正文。`speech_text.done` 的受控 Record 保存 `turn_id`，按既有 owner、KB 范围与 revision/epoch 过滤；历史快照和 WS 各自按稳定身份归并。

现有部署升级前的旧 Turn 没有可靠的输入/口述关联，页面保留完整业务答案的展开入口，不将旧 `display_text` 冒充实际口述。正在使用的标签页可通过同一 capability 补取已定稿文字；刷新后的新 capability 不访问旧通话。生成口述文字不表示已听到。受控回归与构建见 [验收记录](acceptance-report.md)，真实 VoiceChat/CueKB、浏览器设备和撤权现场复测仍归 D07。

## 8. Q07：直接检索模式的消息与状态（设计，未实现）

本节是 [架构 Q07](architecture.md#11-q07外置-llm-可选化实施规格尚未编码) 的客户端契约增量。外置模型模式继续 §7 的完整答案与实际口述展示；direct 的完整正文来自 Nano，不能在检索结束时伪造业务答案。

### 8.1 能力、HTTP 与持久事件

`GET /api/v1/capabilities` 新增 `execution_mode: direct|external`、`external_llm_enabled: bool`、`text_available: bool`；direct 为 direct/false/false，external real 为 external/true/true。显式 mock fixture 映射 external/false/true 并保持 is_mock=true。现有 text_configured direct=false，其他字段保持原义，native_tool_phase_barge_in 对当前 speech 基线固定 false。

`GET /conversations/{cid}/messages` 的每条 Turn 新增 execution_mode 与 knowledge_result。前者旧行默认为 external，后者旧行 null。knowledge_result 为接入 §8 KnowledgeBundle 去掉 authorized_kb_ids/tool_version 的公开严格投影（定义 KnowledgeResultView，禁止序列化后临时漏删内部字段）。citations 继续执行 owner/KB 撤权过滤。相同投影用于新增事件：

| 事件 | 传输/负载 | 作用 |
| --- | --- | --- |
| portal.knowledge.ready | 持久 SSE；payload=KnowledgeResultView，turn_id 必需 | 证据已提交且等待 Nano；不表示有最终答案 |
| portal.speech_text.delta/done | 现有 WS；phase、turn_id、response_id、segment_index 不变 | Nano 实际口述正文；done Record 可用于恢复 |
| portal.answer.final | 持久 SSE；payload=扩展后的 AnswerBundle | external 仍提交模型业务答案；direct 在 response 完成后提交实际口述聚合和最终状态 |

KnowledgeReadyEvent 加入 portal_server_event_adapter 判别 union 及导出 schema，`scripts/export_contracts.py` 增 KnowledgeResultView schema 输出；AnswerBundle 扩展字段/voice_completed 一并导出。不要向浏览器发送 NVIDIA function_call_output 或 provider 原始参数。WS 序号与 SSE server_seq 仍分开，不能跨通道数值比较。

`POST /conversations/{cid}/messages` direct 返回 409/TEXT_INPUT_UNAVAILABLE，必须验证会话归属，但不创建 Turn、不 increment revision、不结束当前语音；external 返回原 202。音频、call capability、ticket、SSE 重连接口不变。禁止在消息 body、工具参数或 URL query 接收模式覆盖。

### 8.2 UI 和历史还原规则

| 状态/资料 | direct 展示 | external 展示 |
| --- | --- | --- |
| running | Searching | 原有 Searching |
| awaiting_voice | Preparing voice reply；来源可展开 | 不产生此状态 |
| 有实际 speech_text | 左侧正文按片段流式更新并保留 | 原 Q06 行为 |
| voice_completed | Response completed，不显示 Verified/Answered 认证 | 不产生此状态 |
| failed 且 delivery_status=voice_failed | Voice reply unavailable；保留已收到片段、可用来源及中断标识 | 已验证业务答案仍可展开 |
| direct answer.citations / knowledge_result.citations | Sources consulted，说明是检索上下文，不暗示逐条引用已验证 | 原业务答案 Sources |
| direct 完整正文 | 已有实际口述；不再另放一份 View full answer 重复正文 | 原 View full answer / Sources |

direct 未完成时 answer=null，不能显示“完整答案已就绪”。若没有任何口述且生成失败，显示固定失败说明；不能把证据 source_text 拼接成假答案。已收到部分转写因超时/断线未完成时保留当前页面文字并标 Spoken reply interrupted，不能变为完整 history；刷新只恢复已存 done Record，不伪造丢失 delta。

direct 最终 AnswerBundle.display_text/speech_text 均来自按序拼接的完整实际口述（总计 ≤8000 字符），或服务失败的固定说明；answer_origin=voicechat 表示来源，不意味着使用外置模型验证。恢复时如果 Record 被 100 条读取上限截掉，可用成功 direct AnswerBundle.display_text 还原正文，因为该模式明确记录的是实际转写；external 的业务 display_text 不能这样冒充口述。失败固定说明不得标为实际口述。

前端 selected 来源状态改为同时能承载 Answer 的 citations 和 KnowledgeResultView 的 citations，不为打开 Sources 伪造 Answer。根据 evidence_role 改文案，客户界面不显示 mode、provider、Nano 参数或日志字段。

保持稳定 user input_item_id → Turn 接管，以及 turn/response/segment 的语音归并。新增证据事件只更新资料和 awaiting_voice，不创建第二条助手正文；answer.final 对已有口述只更新终态和兜底快照，不重复追加文字。未知 Turn 先补取 messages，等待期间按 turn_id 缓存最新资料；重放去重用 event_id/server_seq，沿用 finalAnswers 终态缓存并新增知识结果缓存。

状态单调规则：running → awaiting_voice → 终态；晚到 running/awaiting_voice 快照或 knowledge.ready 不能覆盖已收到的 answer.final/取消/过期。相同 revision 先到 final 后到 knowledge 时，仅在尚未撤权且 result_id 一致的条件下补齐来源，不倒退状态。来自更旧 epoch/revision 的实时新事件忽略；已经显示的历史终态按原授权规则保留。KB_ACCESS_REVOKED 优先清理 sources、direct answer、对应 spoken 缓存，后续任何晚到快照不能恢复被撤销正文。

取消按钮在 running/awaiting_voice 均可用，Stop playback 继续只清音频、不删文字或取消任务。活动生成状态不等于已听：生成完成/播放 ACK/播放停止保持独立。

文字输入框和发送按钮 direct 禁用，并展示 `Text input is unavailable in this deployment. Please use voice.`；capabilities 未加载时也不能先开放输入。所有 direct 语音异常提示删除“use text”的不可用建议，改为重启通话或联系人工。external 的文字切换及提示保持原行为。

### 8.3 受控验收

新增 E2E 覆盖 direct 的 ASR→Searching→knowledge.ready/Preparing voice reply→实际口述→Response completed，且没有外置完整答案面板；另测未命中、工具失败、无口述/部分口述、Stop、awaiting_voice 取消、禁用文字输入。external 跑现有 Q06 用例证明行为保留。

API/存储测试覆盖知识证据与口述两阶段、同一 Turn 的 WS/SSE 乱序、旧快照、重复相同问题、历史分页/Record 上限、撤权与新标签页隔离。新增 strict schema 的合法/非法负载契约测试；UI 不得凭 citations 非空把 voice_completed 改成 answered。真实设备 ASR、口述正确性和听音继续留 D07，fixture 只验证协议和状态机。
