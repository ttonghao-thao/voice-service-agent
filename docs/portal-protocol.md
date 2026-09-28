# HTML 门户与消息/语音接口

更新：2026-09-28。本文是当前门户 v1 契约，覆盖独立通话、输入绑定、逐轮音频和停止后继续播放。D19 不新增公开事件字段；浏览器内部 Worklet 的排空标记不暴露为新的服务端协议。D20 使持久事件提交后唤醒 SSE，并直接应用 final 更新已有 Turn；真实服务能力仍按验收记录放行。总架构见 [architecture.md](architecture.md)。

## 1. 简单门户

一个客户页面即可：开始语音、结束语音、连接/聆听/查询/播放状态、用户转写、实际口述字幕、最终答案与可展开引用。麦克风被拒绝、断网或语音不可用时给出明确恢复提示；可选显示文字输入。

- 页面加载不创建 conversation 或读取历史；每个标签页点击 “Start call” 后创建全新的 conversation/call token，再申请语音 session。token 只在该标签页内存中保存，刷新即丢失。
- 开始通话成功以 `Voice ready` 为准；不承诺自动语音欢迎，连接初始未桥接输出被抑制。用户主动点击后申请麦克风，ready 后连续发送音频，包括静音。门户显示浏览器实际选择的设备、采样率、声道及 echo cancellation/noise suppression/auto gain 设置；这些诊断值不等同于收音准确率。门户使用公网 HTTPS，使页面满足浏览器 secure context 前提；证书信任、实际麦克风授权、设备选择和英文识别质量仍须在 D07 实机确认。
- `portal.transcript.delta/done` 与 `portal.speech_text.delta/done` 只作为当前输入/口述的临时 Live captions；用户完成 transcript 保留到同文本的持久 voice Turn 接管，口述字幕在音频结束后清理，新输入或播放清理时收敛，不把字幕另存为聊天历史。文字与语音请求都由持久业务 Turn 按一问一答展示最终答案和引用，不能过滤 voice Turn，也不用业务答案冒充实时口述。
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
| GET `/conversations/{cid}/messages` | 当前用户的会话历史 |
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
| `portal.transcript.delta` / `done` | WS | item_id、text；更新当前用户 Live caption，业务历史由 Turn 提供 |
| `portal.speech_text.delta` / `done` | WS | response_id、可选 item_id、text；更新当前实际口述 Live caption，不合并成历史答案 |
| `portal.audio.delta` / `done` | WS | response_id、delta 的 audio；done 表示服务端发完，客户端仍须排空缓冲；过期 epoch 一律丢弃 |
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
