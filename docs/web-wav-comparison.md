# Web 上传与自动脚本：同一 WAV 对照操作

当前批次修正和最小复测以 [批次验收](native-tool-batch-validation.md) 为准；下文保留四文件完整对照方法。输入路径已统一，当前继续补全批次和阶段关联。Web 与自动脚本统一使用 WAV 原始 24 kHz/mono/PCM16，1920 samples（80 ms）一帧，末帧补零；session.ready 后开始，EOF 后持续送静音。不重采样、不增益、不加前置静音、不删除 Golden Yolk 的停顿。浏览器发送若停滞超过 500 ms 或网络积压则明确失败，不突发补发。

两者使用同一个 API Relay、工具目录、VoiceChat 和 CueKB。它们不是相同的运行环境：浏览器负责真实播放和播放回执，脚本只记录音频；浏览器计时器也不能保证与 Python 调度逐毫秒一致。统一 PCM 不等于已经证明模型必然做相同选择。麦克风仍保留必要的重采样；当前只调整 VoiceChat audio_server 的批次传输，不改模型、提示词、检索预算或产品问题。

自动验证器原先可能沿用第一段回答的完成标记。本版在新输入开始时重置，只接受最新输入的非 ACK 回答，并且不因 WAV 尚未发送完时结束的回答提前结束验证。不能用 Golden Yolk 第一段普通回答证明第二段知识回答完成。

## 1. 部署准备（宿主机项目根目录）

需要重新构建并重建 **Web 和 API**。API 增加批次结清和阶段诊断，Web 修正输出 item_id 的诊断字段；不改数据库结构。还必须按 [VoiceChat 启动命令](voicechat/docker-run.md) 同步 audio_server.py 并重建 VoiceChat。

```sh
API_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-api:/ {p;q;}')
WEB_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-web:/ {p;q;}')
test -n "$API_IMAGE" && test -n "$WEB_IMAGE" &&
  docker build -f deploy/Dockerfile.api -t "$API_IMAGE" . &&
  docker build -f deploy/Dockerfile.web -t "$WEB_IMAGE" . &&
  docker compose --env-file .env -f deploy/compose.production.yaml up -d --no-deps --force-recreate api web
API_CONTAINER=$(docker compose --env-file .env -f deploy/compose.production.yaml ps -q api)
VOICE_CONTAINER=nemotron-labs-voicechat
test -n "$API_CONTAINER"
```

等待健康检查通过，浏览器强制刷新，避免旧 JS/Worklet 缓存。输入都来自项目 `artifacts/native-cuekb/`；API 镜像内的同版输入位于 `/app/validation/native-cuekb/`。无需复制文件到容器或手动创建目录。不要使用历史 ZIP 内的另一份录音。

本轮每个 WAV 都使用新连接，Web 与脚本保持相同起点；Golden Yolk 的两段仍在一个连接里自然连续发送。避免先跑问候/技术/Black Pig 再让 Golden Yolk 撞上连接时限。该方式不替代最终的多轮验收。

## 2. 操作一：Web 逐个上传四个文件

顺序为 `hello.wav`、`tcp_udp.wav`、`black_pig_crisp_heart.wav`、`golden_yolk.wav`。每份只运行一次：

1. 打开 F12 → Console，启用 Preserve log，过滤 `voice_file`。Network 同时保留 `/messages` 请求。工具必须在开始通话前打开；保持门户前台，不切后台或锁屏。
2. 宿主机记录本次开始时间：
   ```sh
   WINDOW_START=$(docker exec "$API_CONTAINER" python -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')
   ```
3. 门户选择 Audio file → Choose WAV → Start file call。等待 `Recording sent. Waiting for reply`，不要在 Golden Yolk 中间停顿时结束通话。等待第二段回答完成，或记录明确错误后 End call。
4. 记录 `voice_file` 输出（只含时间/ID/阶段，无录音、答案正文或令牌）。其中 `file.start` 的 `conversation_id` 是下面的 `CONVERSATION_ID`。`file.sent` 证明全部输入已发送；`audio.first` 是收到首包，`playback.started` 是 Worklet 开始消耗音频，不是物理扬声器测量。
5. 在最新 `/messages` 响应中仅保留：各 `user_transcript` 的 `item_id/text`；各 `voicechat_transcript` 的 `input_item_id/response_id/text`；`items` 中的 `input_item_id/selected_tool/status/answer.status/answer.reason_code/answer.citations` 的数量和 trace_id。不要导出 HAR、Authorization 或访问令牌。
6. 将 CID 填入以下命令，提取这一次的后端时间线（在宿主机执行）：

```sh
WINDOW_END=$(docker exec "$API_CONTAINER" python -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')
CONVERSATION_ID='替换为这次 file.start 的 conversation_id'
{
  docker logs --timestamps --since "$WINDOW_START" --until "$WINDOW_END" "$API_CONTAINER" 2>&1
  docker logs --timestamps --since "$WINDOW_START" --until "$WINDOW_END" "$VOICE_CONTAINER" 2>&1
} | docker exec -i "$API_CONTAINER" python /app/scripts/summarize_voice_timeline.py \
      --conversation-id "$CONVERSATION_ID"
```

每次执行时间变量和 CID 必须来自同一轮。不要用自动脚本的报告窗口查询 Web 通话。

问候和 TCP/UDP 没有 Sources、没有工具调用是正常预期。Black Pig 需要重点记录是否调用工具、是否出现有引用的答案。Golden Yolk 要按转写顺序区分第一段产品名和第二段运输/保质期/开封存储问题，不能仅记录“该文件调用过工具”。若只观察到一个 input，也如实记录，不强行拆成两段。

## 3. 操作二：自动脚本逐个运行同版四个文件

下面在宿主机运行；所有 Python 均在 API 容器执行。每次批次使用唯一 RUN_ID，各 case 建立新连接，输出路径独立，不因某条失败跳过其他文件。120 秒为本次观测上限，不修改服务端连接时限或检索预算；若服务端更早轮换，保留真实错误，不当作通过。

```sh
RUN_ID=$(docker exec "$API_CONTAINER" python -c 'import time; print(time.time_ns())')
for CASE_ID in greeting general_tech black_pig_crisp_heart golden_yolk
do
  if docker exec "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py \
    --case-id "$CASE_ID" --case-timeout 120 \
    --output "/tmp/native-cuekb/compare-$RUN_ID/$CASE_ID/report.json" \
    --audio-dir "/tmp/native-cuekb/compare-$RUN_ID/$CASE_ID/replies"
  then
    VALIDATION_EXIT=0
  else
    VALIDATION_EXIT=$?
  fi
  printf 'case=%s host_exit_code=%s\n' "$CASE_ID" "$VALIDATION_EXIT"
  if [ "$VALIDATION_EXIT" -eq 2 ] || ! docker exec "$API_CONTAINER" test -s "/tmp/native-cuekb/compare-$RUN_ID/$CASE_ID/report.json"
  then
    break
  fi
  WINDOW_START=$(docker exec "$API_CONTAINER" python -c 'import json,sys; print(json.load(open(sys.argv[1]))["window_start"])' "/tmp/native-cuekb/compare-$RUN_ID/$CASE_ID/report.json")
  WINDOW_END=$(docker exec "$API_CONTAINER" python -c 'import json,sys; print(json.load(open(sys.argv[1]))["window_end"])' "/tmp/native-cuekb/compare-$RUN_ID/$CASE_ID/report.json")
  CONVERSATION_ID=$(docker exec "$API_CONTAINER" python -c 'import json,sys; print(json.load(open(sys.argv[1]))["conversation_id"])' "/tmp/native-cuekb/compare-$RUN_ID/$CASE_ID/report.json")
  {
    docker logs --timestamps --since "$WINDOW_START" --until "$WINDOW_END" "$API_CONTAINER" 2>&1
    docker logs --timestamps --since "$WINDOW_START" --until "$WINDOW_END" "$VOICE_CONTAINER" 2>&1
  } | docker exec -i "$API_CONTAINER" python /app/scripts/summarize_voice_timeline.py \
        --conversation-id "$CONVERSATION_ID"
done
```

若 preflight 失败而未产生本轮报告，先停止此操作处理 preflight；不能读取旧报告冒充新结果。回答 WAV 是各 case 目录中的 `replies/case-01.wav`。如需下载，使用 `docker exec "$API_CONTAINER" cat "/tmp/native-cuekb/compare-$RUN_ID/golden_yolk/replies/case-01.wav" > golden-yolk-reply.wav`，无需 docker cp。

## 4. 仅需回传的结果及判断

每个文件保留一组，不需要全量日志：

- Web：文件名、`voice_file` 行、上述少量 messages 字段、是否实际听到回答和看到 Sources。Black Pig 与 Golden Yolk 保留 `timeline=` / `stage=` 输出。
- 自动脚本：`preflight`、窗口/CID、`case=`、`validation=`、`host_exit_code`；Black Pig 与 Golden Yolk 保留时间线。
- 内容准确性：仅引用数大于零还不够；产品回答须核对 Sources 的原文、数值、条件和缺失信息声明。自动回答完成不代替人工听音。

时间线只选该 CID 的 API 日志，并由 input_item_id、call_id 或 response_id 找对应 VoiceChat sequence。`session_match=unverified` 表示日志证据不够，**不能**当作模型调用数零。`truncated=true` 表示输出不完整；缩小窗口读取已有日志，不重复发送 WAV。时间戳来自 Docker 日志，适合秒级阶段定位；不同机器时钟或日志缓冲会影响精确差值，不能据此推断 GPU 精确执行耗时。

| 观察 | 可以作出的判断 |
| --- | --- |
| 匹配成功、Prompt 工具正确、`model_function_count=0` 且 `api_tool_calls=0` | 本轮模型没有解码出工具调用；不是 CueKB 返回失败 |
| 出现 `model.function`，无对应 `voicechat.function.call` 或 `voice_tool_call_received` | 工具产生后，需定位 VoiceChat 解析/发送或 API 接收链路；不笼统归为模型没选工具 |
| API 收到调用、CueKB 有状态/trace，随后 `voice_tool_result_submitted` | 已执行并回填；继续检查结果注入及回答 |
| Golden Yolk 第二个 input 有 `voice_tool_call_received` | 该段实际进入工具流程；两个测试方式分别确认，不能互相代替 |

对每一段输入、每一个 call/response 分开看这些区间：

| 起止阶段 | 对应等待位置 |
| --- | --- |
| `voice_input_transcribed` → `voice_tool_call_received` | 转写完成后到工具事件到达 API 的时间；不是 CueKB 查询时间 |
| `voice_bridge_started` → `cuekb_response_validated` | 工具桥接/请求阶段；其中 `duration_ms` 单独给出 CueKB 客户端测得的请求耗时 |
| CueKB 返回 → `voice_tool_result_submitted` | 应用处理、证据准备与结果提交 |
| 提交 → `voicechat.function.result` → `backend.inject_started/inject_done` | VoiceChat 排队、ACK 等待和后端结果注入 |
| 注入完成 → 对应回答的 `voice_audio_first_received` | 等待模型后续音频；若先产生下一次工具调用，按下一次 call 继续分析 |
| `voice_audio_first_received` → `voice_audio_first_sent` | API 转发等待 |
| 浏览器 `audio.first` → `playback.started` | 前端缓冲/调度；后者不是扬声器实际发声证明 |

`response_id` 必须一致，并用 `voice_response_completed phase=answer` / `portal.speech_text.done phase=answer` 区分真正回答与 ACK；首包到达时尚不能确定是否 ACK，不把任何首包都当答案。浏览器与服务器的绝对时间不能未经时钟校准直接相减。

输入统一和验证器结束条件已经修正；最新批次修正仍不能宣称解决模型不选工具或 30 秒等待。按上述两组各做一次后，沿最早出现差异的阶段分析，不调整预算、不重写问题、不无限重跑。VoiceChat 的独立说明见 [原生工具验证](voicechat/native-tool-selection-validation.md)。
