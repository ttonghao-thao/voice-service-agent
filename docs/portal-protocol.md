# HTML 门户与消息/语音接口

更新：2026-09-16。本文确定客户入口和接口边界；D01–D05 的当前实现使用本文 v1 格式，真实服务能力仍按验收记录放行。总架构见 [architecture.md](architecture.md)。

## 1. 简单门户

一个客户页面即可：开始语音、结束语音、连接/聆听/查询/播放状态、用户转写、实际口述字幕、最终答案与可展开引用。麦克风被拒绝、断网或语音不可用时给出明确恢复提示；可选显示文字输入。

- 用户主动点击后申请麦克风，ready 后连续发送音频，包括静音；生产依赖 HTTPS 安全上下文。
- 客户页面不展示工具配置、模型参数、内部运行日志或后台管理菜单。
- 当前交互区分“停止播报”和“取消查询”：前者清除客户端缓冲并由服务端抑制当前 response，保留仍有效业务任务；后者使当前 revision 失效，存在无法安全结清的原生 call 时关闭旧语音连接。
- 显示业务答案与实际语音字幕的区别；来源与版本可查看，工具秘钥和内部地址不可出现在页面。
- 客户入口复用现有 TypeScript/AudioWorklet 采集、重采样和播放模块，并由现有构建链输出静态 HTML；工具管理不进入客户页面。

## 2. “标准消息接口”的含义

使用通用 HTTPS JSON、SSE、WebSocket 传输；`/api/v1` 和 `portal.*` 是本项目公开且版本化的应用契约，不是 NVIDIA 原生 API，也不声称兼容 OpenAI Realtime。

门户只处理会话、音频、字幕、状态、答案与错误。CueKB schema、VoiceChat 原生 function call 和供应商凭据均留在服务端。新增第三方工具不要求门户理解供应商消息。

当前代码依据：`apps/api/app/contracts.py`、`api/routes.py`、`voice/gateway.py`，以及 `apps/web/src/audio/VoiceClient.ts`。生成契约为 [HTTP OpenAPI](../contracts/openapi.json)、[服务端事件联合类型](../contracts/portal-events.schema.json)、[客户端事件联合类型](../contracts/portal-client-events.schema.json)、[答案](../contracts/answer-bundle.schema.json)。网关在收发边界执行同一套严格校验；未知客户端事件、额外身份字段和错误音频格式会被拒绝。

## 3. HTTP 与 SSE（当前路径）

所有业务接口都在 `/api/v1` 下，需认证及会话归属检查。

| 方法和路径 | 用途/关键返回 |
| --- | --- |
| GET `/capabilities` | 配置和适配器声明的能力；部署声明不等于自动实测 |
| POST `/conversations` | 请求 title、locale；返回 id、title、epoch、request_revision、locale；新会话仅接受 `en-US`，旧会话保留原 locale |
| GET `/conversations` | 当前用户会话分页 |
| GET `/conversations/{cid}/messages` | 当前用户的会话历史 |
| POST `/conversations/{cid}/voice-sessions` | 返回 voice_session_id、epoch、request_revision、ws_url（含一次性 ticket） |
| DELETE `/conversations/{cid}/voice-sessions/{sid}` | 关闭语音，保留会话历史；当前同时触发硬中断 |
| POST `/conversations/{cid}/messages` | `{text}`，要求 Idempotency-Key；202 返回 task_id/turn_id、epoch、request_revision、status；新问题 supersede 旧运行任务 |
| GET `/conversations/{cid}/events` | SSE 业务流；`Last-Event-ID` 或 `after` 恢复游标 |
| POST `/conversations/{cid}/playback/stop` | expected_epoch、expected_revision、可选 response_id；停止当前播报，不取消查询 |
| POST `/conversations/{cid}/tasks/current/cancel` | expected_epoch、expected_revision；取消当前查询并拒绝晚到结果 |
| POST `/conversations/{cid}/interrupt` | `{expected_epoch}`；当前是取消业务、失效 epoch、关闭语音的硬中断 |

同一文字幂等键相同正文不重复执行，不同正文返回 409。管理 API 不属于客户协议权限集合。SSO/鉴权策略见 [接入文档](integration.md)。

SSE 的 `id` 是持久化 `server_seq`，不是 JSON `event_id`。仅重放业务事件，不重放实时音频。前端按 `event_id` 去重；不得把 SSE 和 WebSocket 的序号混为一个全局连续序列。

## 4. WebSocket 和音频（当前 v1）

连接签发的 `ws_url`，路径 `/api/v1/voice-sessions/{sid}/stream?ticket=...`，生产必须 WSS。票据有效 60 秒、一次性，绑定认证身份、会话、epoch、Origin；握手失败不能静默切换匿名连接。URL query 与凭据不得写访问日志。

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

`audio` 为示意占位值。实际每帧 PCM16 little-endian、单声道、24 kHz、80 ms，即 1920 samples / 3840 bytes；不是 WAV 文件，不带 WAV header。浏览器设备采样率先经重采样转换。上行 seq 为连接内递增非负整数；重复丢弃，缺帧/过快/持续停顿触发显式错误。连接绑定身份与 conversation，正文不能覆盖。

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
| `portal.transcript.delta` / `done` | WS | item_id、text；done 替换该条最终用户转写 |
| `portal.speech_text.delta` / `done` | WS | response_id、可选 item_id、text；实际模型口述字幕 |
| `portal.audio.delta` / `done` | WS | response_id、delta 的 audio；播放/结束该回复；过期 epoch 一律丢弃 |
| `portal.input.state` | WS | state=speaking/quiet；仅输入状态，不自动等于取消业务 |
| `portal.tool.started` | SSE | 客户可理解的查询状态；不暴露私有工具参数 |
| `portal.answer.final` | SSE | 已校验 AnswerBundle；展示答案、来源、必要卡片 |
| `portal.playback.clear` | SSE | 清理旧 epoch 的排队音频；不可被晚到 clear 清掉新连接 |
| `portal.session.ended` | SSE/连接生命周期 | 显示结束并释放设备；也须处理 WS close，不能只依赖单个消息 |
| `portal.error` | WS；HTTP 使用错误响应 | code、message、retryable、trace_id；按错误恢复，不无限重试 |

下行音频按握手确认的 24 kHz PCM16 播放，实际 delta 长度不要求与上行 80 ms 相同；按字节长度计算 samples。当前 WS 事件 turn_id 可空，不伪造用户转写与工具轮次的关联。持久业务事件主要走 SSE，不能假定所有事件在两个通道重复发布。

### 4.3 时序和背压

1. 创建会话 → 签发票据 → WS 握手 → VoiceChat 握手 → portal.session.ready。
2. 并行持续收发音频；原生工具进入后台 worker，音频任务不等待业务 Runtime。
3. 已接受业务答案走 SSE；工具结果经合法 native call 回传后，语音/字幕走 WS。
4. 停止播报只抑制当前输出；取消查询推进 revision，并在无法安全结清 pending call 时关闭连接；结束/硬打断释放语音资源。
5. 网络恢复重新取票，不重放旧录音；恢复业务历史不等于恢复模型隐藏状态。

保持有界队列、单写入器、发送速率校验和采集 watchdog；过载显式拒绝/恢复，不能无限积压后追赶播放。当前默认 105 秒在输入 quiet 且无 pending call 时触发安全轮换，浏览器清除旧音频并取得新 ticket；宽限期内仍未安全时显式结束，不恢复模型隐藏状态。

## 5. 版本和错误处理

- v1 固定当前音频格式与已定义必需字段；未来二进制音频/不同格式需要明确协商或新版本，不能暗改现有字段含义。
- 兼容性新增先由 capability 声明并提供降级；未知服务端非关键展示事件可忽略，未知客户端控制事件拒绝；格式/鉴权错误不得继续播放。
- 当前已有典型错误：AUTH_REQUIRED、FORBIDDEN、VOICE_UNAVAILABLE、VOICE_CAPACITY_EXCEEDED、VOICE_PROTOCOL_ERROR、VOICE_SESSION_ROTATION_REQUIRED、VOICE_SESSION_EXPIRED、AUDIO_BACKPRESSURE。
- 工具错误在业务层映射为稳定的失败/无依据/澄清状态；不能把 401/403/429/5xx 都显示为“没有知识”。
- 不把服务商 error 原文、密钥或内部堆栈直接转发客户。完整错误集合随实现和契约同步维护。

## 6. 接口验收

D01/D04/D05 必须验证：独立简单客户端可接入、门户不直连供应商、身份/票据/Origin、正确音频格式、序号和去重、硬打断竞态、SSE 恢复、旧连接隔离、字幕与答案分离、窄屏和设备释放。真实语音结论记录于 [验收报告](acceptance-report.md)，不以合成音频替代。
