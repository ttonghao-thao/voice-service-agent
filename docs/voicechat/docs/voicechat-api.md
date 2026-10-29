# VoiceChat 实时语音 API 与逐轮输出设计

更新：2026-09-28。以本源码 `s2s/audio_server.py` 为实现依据；本次修复来自英文客服通话的“有转写、无口述”问题。CPU 协议/重采样测试不能替代 GPU、实际 ASR 或人工听音。模型权重与 Triton 推理逻辑未在本次修改。

## 1. 服务与客户端边界

服务提供 `/v1/realtime`（兼容路径 `/realtime`），默认进程端口由 `--port` 指定，默认值 9000；实际云端端口与代理以部署配置为准。健康入口为 `/v1/realtime/health`，健康不代表工具或语音质量通过。

独立客户端直接连接此 WebSocket。经 voice-service-agent 的客户门户则访问其同源 `/api/v1/voice-sessions/{sid}/stream`，由网关持有 VoiceChat 凭据、执行工具和授权播放；浏览器不能绕过网关直接获得客服工具权限。公网浏览器入口与服务端内网 WS 是两条不同链路。

## 2. 音频与时钟

| 阶段 | 格式 |
| --- | --- |
| 客户端上行 | PCM16 little-endian、单声道；此源码可协商 16000–48000 Hz，英文客服网关固定 24000 Hz、80 ms/3840 bytes |
| 模型输入 | 状态化 soxr 重采样到 16000 Hz；默认 MODEL_STEPS_PER_CALL=2，每次 2560 samples/160 ms |
| 模型输出 | codec 22050 Hz，文本与音频来自相同 decoder-step 区间，每步 80 ms |
| 客户端下行 | 固定 24000 Hz PCM16；常规 80 ms 分帧，最后一个包可以更短 |

输入包括静音并保持实时节奏。设备 48000 Hz 与实际线上 24000 Hz 可以同时成立；不能根据麦克风标签或内部 16000 Hz 日志擅自改网关格式。PCM 正满幅编码必须截断到 32767，不能溢出成负满幅。每轮完成时刷新 soxr 的剩余采样和 PCM 残帧，避免短回答无声或尾音截断。

## 3. 握手与上行消息

连接成功收到 `session.created`，随后发送一次 `session.update` 并等待 `session.updated`。音频格式、instructions 和 tools 在首次配置中提供。英文客服只注册 `consult_service_agent`，示例：

```json
{
  "type": "session.update",
  "session": {
    "audio": {
      "input": {"format": {"type": "audio/pcm", "rate": 24000}},
      "output": {"format": {"type": "audio/pcm", "rate": 24000}}
    },
    "instructions": "Route every completed customer utterance to consult_service_agent before answering.",
    "tools": [{
      "name": "consult_service_agent",
      "description": "Send the complete customer utterance to the business assistant, including greetings and unclear speech.",
      "parameters": {
        "type": "object",
        "properties": {"user_request": {"type": "string"}},
        "required": ["user_request"],
        "additionalProperties": false
      },
      "ack_messages": ["Please wait while I check the knowledge base."]
    }]
  }
}
```

持续发送 `{"type":"input_audio_buffer.append","audio":"<base64 PCM>"}`。收到原生工具调用后，用原 call_id 发送：

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "function_call_output",
    "call_id": "<received call_id>",
    "output": "{\"status\":\"answered\",\"speech_text\":\"Verified answer.\"}"
  }
}
```

结束使用 `session.close` 或断线。此 API 不接受凭空假定可用的 `response.cancel`、任意文本 TTS 或后台结果推送。不同网关对参数、ASCII、身份和业务范围另有严格约束，不能将本机 loopback/示例工具当成生产知识服务。

## 4. 下行事件与身份

| 事件 | 标识及含义 |
| --- | --- |
| `session.created/updated` | 连接创建/配置确认；不是业务回复 |
| `input_audio_buffer.speech_started/stopped` | 用户输入 item 的边界 |
| `conversation.item.input_audio_transcription.delta/completed` | 同 input item 的临时/最终 ASR |
| `response.created` | 实际口述或工具打开的新 response，不由空闲音频数组触发 |
| `response.function_call_arguments.done` | call_id、name、单次 JSON 编码的 arguments；response_id 与该工具的 ACK 关联 |
| `response.output_audio.delta` | 生成时固定 response_id/item_id 的 24 kHz PCM |
| `response.output_audio_transcript.delta/done` | 实际口述文字，区别于后台知识答案 |
| `response.output_audio.done` | 该轮所有 PCM（含尾帧）已经排在它之前，不代表客户已听完 |
| `response.content_part.done/output_item.done/response.done` | 该轮输出生命周期完成；之后下一轮使用新 ID |
| `error/session.end` | 显式错误或会话结束，不能假定错误后仍可继续 |

同一模型批次内先处理 ASR，再处理新工具，最后处理该工具的 ACK；这样消费方可以先绑定输入并授权。若该批次结束的是此前口述，其音频/EOS 先结束旧 response，再打开新工具 response；不能把初始欢迎语的 ID 用于客户工具。新 BOS 可结束被打断的旧口述，但保留尚未开始说话的工具 response 给第一段 ACK。

本项目适配的音频、口述字幕和工具事件还包含有界关联元数据：`input_item_id` 为主输入，`tool_call_ids` 为该回答上下文已交付的原生调用，`related_input_item_ids` 为等待期间的补充输入（两个列表最多各 128 项）。函数调用自身绑定当时输入；结果送入 Triton 后，下一正式回答保留工具所属输入及补充输入，固定 ACK 不清除待回答关联。关联在 response 打开/事件入队时固定，不能由发送时的最新输入改写。它记录工具消费上下文，不认证模型已回答每个问题；消费者仍须检查实际口述与证据。旧桥接授权路径不把这些字段当作新的发声许可。

连接开始后，模型可在客户约三秒未发声时生成一次简短欢迎询问。这是会话级 response，没有 `input_item_id`、工具调用或知识许可，不创建客户 Turn。客户开始发声后，消费者清除该欢迎语的剩余播放并继续同一连接。ASR 的 `conversation.item.input_audio_transcription.*` 始终只用自身 `item_id`；即使上一个回答刚结束，也不得携带或继承回答关联元数据。只有带非空且匹配当前 response ID 的助手输出事件才能捕获回答关联。

## 5. 输出队列和 response 生命周期

所有模型产生的 ASR、工具、音频、字幕与 done 事件进入有界 FIFO。事件在入队时捕获所属 ID；单发送器只发送事件快照，不从可变全局状态读取 ID。慢连接不能把旧音频改标为新回答。队列溢出或发送失败显式终止，不能静默丢弃边界。

空闲 codec 数组仍用于模型内部时钟，但不打开/发送一个占满整场的音频 response。BOS、有效口述或工具调用打开 response；EOS 刷新音频尾部并完成该轮。固定 ACK 结束后，后续业务答案使用新的 response；消费者必须分别处理 ACK 和答案许可。

默认两步模型输出的相邻 `</s><s>` 对应两个明确的 80 ms codec 帧，服务端分别结束旧轮和开启新轮。帧长度不匹配或更大批次包含无法确定采样边界的多轮输出时显式失败；不能用振幅阈值切音或静默猜测。修改 MODEL_STEPS_PER_CALL、codec 或 decoder 输出方式后必须重新验证此约定。

主动 session.close 会先处理已接收输入、完成当前输出尾部并等待输出 FIFO 排空，再结束；意外断线直接释放，不伪造客户已收到完整输出。浏览器应在 audio.done 后继续排空自身播放缓冲，不立即丢弃尾音。

底层引擎返回 finished 或生成器结束后停止追加，报告 `error.code=voice_session_rotation_required`；未知请求失效明确结束连接，不能用同一 ID 重放工具或重新初始化来恢复未知状态。Triton END 直接清理 LLM/TTS/序列并刷新最终显示 ASR，不再执行常规推理；LLM abort 超时后仍继续其他清理。文件通话由用户显式开始下一通，不自动重播录音。

## 6. 工具模板与业务路由

知识客服部署必须在启动 WebSocket 进程前设置 `USE_JINJA_TEMPLATE_PROMPT=1`，使用已有 `s2s/prompt_template.jinja`。该分支保留工具定义、调用/返回格式和独立 ACK 元数据，不追加默认模板的“常识直接回答”路由。默认模板维持原样，避免影响其他 speech 客户端；应用 instructions 决定哪些请求走工具。客服网关要求每个完整客户发言走统一业务 bridge。修改环境后须重启进程/重建容器，不能仅在页面重发 session.update。

模型 arguments 可能是对象或 JSON 字符串，服务端解析为对象后在原生事件中编码一次。畸形工具 JSON 显式失败；不能把双重编码的字符串传给业务参数验证。

`session.updated` 只回显配置，不能证明模型遵守提示词。客服网关必须继续执行后置条件、最终 ASR 绑定和输出授权，真实失败不能回退假数据。

## 7. 本地验证与发布

在独立 Python 3.12 venv 中安装 `s2s/tests/requirements.txt`，执行：

```sh
python -m pytest s2s/tests/test_response_lifecycle.py -q
python -m py_compile s2s/audio_server.py
```

测试运行实际服务端 dispatch、soxr 和 PCM 编解码，注入受控模型输出；不连接 Triton、GPU 或真实上游。覆盖静音、ASR/工具/ACK 顺序、跨轮 ID、慢发送、短尾帧、EOS/BOS 边界、初始欢迎与工具切换、参数编码、模板和背压。

发布保留源码 hash、独立镜像 digest 和对应网关版本。重新构建 VoiceChat，并与 voice-service-agent API/Web 修复版本一起部署。其整轮业务默认 30 秒不等于本模型工具等待预算；工具超时需按实际模型配置与延迟探针对齐。

GPU 验收至少包括两轮英文问答、问候与澄清、固定 ACK 后的最终口述、快速/延迟工具结果、停止后继续、真实 48 kHz 麦克风、实际扬声器与录音对照。Sent 包数、ready、EOS 或 CPU 测试通过均不能替代人工听音和 ASR 准确性结论。
