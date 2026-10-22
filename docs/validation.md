# 统一验证操作手册

这是本项目唯一的验证文档，适用于英文（en-US）全双工知识客服：Web → API → NVIDIA VoiceChat → CueKB → 有来源的语音回答。所有操作、回传内容、判据和当前未解决项均在本页，不需要打开其他文档。

**操作者只执行命令和回传精简输出，不负责判断原因、选择日志、找会话 ID 或调整参数。** 命令失败也保留输出，交给开发者定位；不要反复重跑、提高检索次数、延长会话或删除 WAV 的停顿来获得通过。

## 1. 本次范围与执行顺序

本轮修复已编码，尚未真实部署验收。按第 2、2.1 节更新 API/Web 和 VoiceChat，随后仅执行第 3 节的 `auto core` 一次，以及第 4 节两个产品文件的 Web 上传各一次。不要同时执行四文件全量对照、RNNT 定向测试或后台测试。Hello/TCP 缺词按多次正常的现场反馈收敛。

本轮目标：结束后静音不再重复开口；同连接下一问仍可回答；准确区分检索失败与口述校验失败；在两次查询预算内覆盖复合问题，并减少重复来源的逐 token 注入。自动结束后保留 5 秒静音观察，不能首个 done 即放行。GPU 首包时延和回答覆盖度必须用本轮真实结果核对；本地通过不代表现场已解决。

2026-10-22 补充的 RNNT 显示转写修正已部署，用户报告 Hello/TCP 多次正常。`model.py` 的显示转写 predictor 改用一次零输入初始化，与 NeMo 标准贪心解码一致；不改 WAV、前置静音、权重、识别门槛或控制打断的 RNNT 状态。第 4.2 节仍保留定向检查方法，当前不再执行；该反馈不代表麦克风或全部语音准确率验收。

后台持续播报按用户决定暂缓，作为非阻塞待优化项；保留页面隐藏不主动结束及回前台尝试恢复的现有实现。本轮 Web 验证保持前台，不要求切后台或锁屏，不将后台连续播放作为放行条件。

输入唯一来源是本仓库 `artifacts/native-cuekb/` 的 `cases.json` 和四个 WAV。API 镜像内对应 `/app/validation/native-cuekb/`。所有 Python 在容器内执行；宿主机只需 Bash、Docker Compose、Git 和常用 shell 工具。**不使用 docker cp，不在容器创建输入目录，不使用历史 ZIP。** 输出目录由脚本自动创建，API 的只读根文件系统不受影响。

宿主机执行入口是 `bash scripts/validate_deployment.sh`；它自动调用 `docker exec`，寻找 API 容器、检查固定文件、提取本次时间窗口和会话、采集匹配的 VoiceChat sequence、查询已保存答案。采集不会新增模型请求或 CueKB 请求；`auto` 和 `prefix` 会发送测试语音。默认 VoiceChat 容器名为 `nemotron-labs-voicechat`。

## 2. 更新本次验证工具并准备环境

所有命令在**宿主机项目根目录**执行，保持同一主机、目录和用户。先结束已有测试通话；本次采集期间不要有其他测试用户通话。

本轮需重建 API/Web 镜像，并加载 VoiceChat 完整 `model.py` 和 `data_types.py`；原 `audio_server.py` 沿用仓库版本。无数据库迁移。API 增加同输入的已发送来源去重，完整累计证据仍用于校验/引用；门户保留失败状态并显示正确阶段，不放宽内容检查。假定 `.env` 和既有部署已经可用。

```bash
bash <<'SH'
set -uo pipefail
umask 077
LOG=$(mktemp /tmp/voice-validation-build-XXXXXX.log)
if (
  set -e
  git pull --ff-only origin main
  API_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-api:/ {p;q;}')
  WEB_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-web:/ {p;q;}')
  test -n "$API_IMAGE" && test -n "$WEB_IMAGE"
  docker build -f deploy/Dockerfile.api -t "$API_IMAGE" .
  docker build -f deploy/Dockerfile.web -t "$WEB_IMAGE" .
  docker compose --env-file .env -f deploy/compose.production.yaml up -d --no-deps --force-recreate --wait --wait-timeout 180 api web
) >"$LOG" 2>&1; then
  printf 'deployment=passed commit=%s build_log=local_only\n' "$(git rev-parse --short HEAD)"
else
  printf 'deployment=failed local_build_log=%s\n' "$LOG"
fi
SH
```

正常先输出 `deployment=passed`，再执行第 2.1 节。**失败后停止本次验证，只回传失败行，不自行替换文件或选择日志。** 如需构建失败详情，开发者后续只索取该阶段的必要报错，不能默认索取完整构建日志。

VoiceChat 已加载本仓库完整快照是本次验证前提；检查会逐个比对六个运行文件，包括 audio_server、模板、model、loader、runtime_setup 和 data_types，并检查 Jinja/诊断开关。本轮必须同时加载配套的 model、data_types 和 audio_server；其他文件不匹配时停止并回传，不猜测 GPU、模型挂载参数。会话提示由 API 在新通话注册，因此更新后必须新建通话。

## 2.1 更新现有 VoiceChat 文件挂载并检查就绪

重建 VoiceChat 容器时，运行文件必须直接从本仓库 `docs/voicechat/` 的绝对路径只读 bind mount 到下表路径；挂载来源变化必须重新创建容器。源码来源固定为 NVIDIA-NeMo/Speech 的 nemotron-labs-voicechat 分支，实际修改基线及完整文件哈希由快照 manifest 记录，不自动升级到上游最新 HEAD。测试文件只收录，不挂载到推理路径。

| `docs/voicechat/` 下的相对路径 | 容器实际加载路径 |
| --- | --- |
| `s2s/audio_server.py` | `/s2s/audio_server.py` |
| `s2s/prompt_template.jinja` | `/s2s/prompt_template.jinja` |
| `opt/tritonserver/backends/nemotron-voicechat/model.py` | `/opt/tritonserver/backends/nemotron-voicechat/model.py` |
| `opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py` | `/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py` |
| `opt/tritonserver/backends/nemotron-voicechat/data_types.py` | `/opt/tritonserver/backends/nemotron-voicechat/data_types.py` |
| `opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py` | `/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py` |

先结束所有通话。以下命令适配已按单文件 bind mount 加载 `model.py`、`data_types.py` 和 `audio_server.py` 的现有容器：自动找到三个宿主机源文件，备份后原位更新不同内容，再重启整个 VoiceChat 容器，使 Triton 和 WebSocket 进程都重新加载。源文件已是本仓库路径时跳过写入。不改变镜像、GPU、环境、端口或挂载，不创建容器目录、不复制验证文件进容器、不使用 docker cp。未来重建仍须直接从本仓库挂载上述六个文件；镜像、环境或挂载变化不能用重启代替。

若现场不是这种挂载方式，命令明确输出失败并停止，由开发者提供对应镜像发布操作，操作者不需要自行判断或改路径。

```bash
bash <<'SH'
set -uo pipefail
umask 077
VOICE_CONTAINER=${VOICE_CONTAINER:-nemotron-labs-voicechat}
LOG=$(mktemp /tmp/voicechat-runtime-update-XXXXXX.log)
stop() { printf 'voicechat_update=failed stage=%s details=local_only\n' "$1"; exit 2; }
API_CONTAINER=$(docker compose --env-file .env -f deploy/compose.production.yaml ps -q api 2>>"$LOG") || stop discover_api
test -n "$API_CONTAINER" || stop missing_api
MOUNTS=$(docker inspect "$VOICE_CONTAINER" 2>>"$LOG" | docker exec -i "$API_CONTAINER" python -c '
import json, sys
mounts = json.load(sys.stdin)[0]["Mounts"]
for relative in ("s2s/audio_server.py", "opt/tritonserver/backends/nemotron-voicechat/model.py", "opt/tritonserver/backends/nemotron-voicechat/data_types.py"):
    found = [m for m in mounts if m["Type"] == "bind" and m["Destination"] == "/" + relative]
    if len(found) != 1 or any(c in found[0]["Source"] for c in "\t\r\n"):
        raise SystemExit(2)
    print(relative + "\t" + found[0]["Source"])
' 2>>"$LOG") || stop runtime_file_bind_not_found
BACKUP=$(mktemp -d /tmp/voicechat-runtime-before-XXXXXX) || stop backup_path
while IFS=$'\t' read -r RELATIVE HOST_FILE; do
  test -f "$HOST_FILE" && test -f "docs/voicechat/$RELATIVE" || stop missing_host_source
  cat "$HOST_FILE" >"$BACKUP/${RELATIVE##*/}" 2>>"$LOG" || stop backup
done <<< "$MOUNTS"
UPDATED=0
while IFS=$'\t' read -r RELATIVE HOST_FILE; do
  if ! cmp -s "docs/voicechat/$RELATIVE" "$HOST_FILE"; then
    if ! cat "docs/voicechat/$RELATIVE" >"$HOST_FILE" 2>>"$LOG"; then
      while IFS=$'\t' read -r RESTORE_RELATIVE RESTORE_FILE; do
        cat "$BACKUP/${RESTORE_RELATIVE##*/}" >"$RESTORE_FILE" 2>>"$LOG" || true
      done <<< "$MOUNTS"
      stop update_source
    fi
    UPDATED=$((UPDATED + 1))
  fi
done <<< "$MOUNTS"
docker restart "$VOICE_CONTAINER" >>"$LOG" 2>&1 || stop restart
READY=0
RUNTIME_DEADLINE=$((SECONDS + 180))
while (( SECONDS < RUNTIME_DEADLINE )); do
  if docker exec "$VOICE_CONTAINER" python -c '
import json, os, urllib.request
port = os.getenv("SERVER_PORT", "9000")
with urllib.request.urlopen("http://127.0.0.1:" + port + "/v1/realtime/health", timeout=5) as response:
    assert json.load(response)["status"] == "ok"
' >>"$LOG" 2>&1; then READY=1; break; fi
  sleep 3
done
test "$READY" = 1 || stop health_timeout
printf 'voicechat_update=ready runtime_files=3 updated_sources=%s completion_wait=enabled rnnt_transcript=single_zero_bootstrap backup=local_only\n' "$UPDATED"
bash scripts/validate_deployment.sh check
SH
```

回传 `voicechat_update`、`preparation`、`runtime` 行。失败时只回传 `voicechat_update=failed` 或 `version=failed`/`collection=failed` 行，不反复重启或索取完整日志。元数据指纹沿用已有 `VOICECHAT_DIAGNOSTICS=1`，每连接每音频阶段仅输出一次首 4 秒哈希，ASR 只输出长度与哈希，不开启原始录音采集。

## 3. 操作一：一次命令完成两份产品录音和同连接下一问

```bash
bash scripts/validate_deployment.sh auto core
```

执行期间等待命令自行结束，通常需两至四分钟。**把本命令终端输出全部回传即可，它已经是精简结果。** 不要另外运行 docker logs、cat report.json 或贴完整日志。

入口先在一条全新连接发送原始 Golden Yolk WAV，保留中间停顿；完整发送后等待最新输入的非 ACK 回答完成，再观察 5 秒，然后在同一连接发送原始 hello.wav 并完成同样观察。随后为 Black Pig 建立另一条新连接测试一次。一份失败仍采集其结果并继续另一份，不自动重试。120 秒是验证器等待上限，不改变服务端会话时限。每次使用独立报告路径，失败不会误读上一次报告。即使验证失败，仍尝试采集这次的摘要。

终端输出包括：版本准备、`preflight`、`case`、`validation`、退出码、`session`、API 预算与检索、`backend_injection`、每输入 `input_timing`、有界 `backend_stages`、保存的输入/回答/业务状态、模型 query、`answer_check`、`evidence_delivery` 和实际准入证据 `admitted_evidence`。证据总长度最多 6000 字符、8 个块，超出明确标记 omitted；不要另外打印全量检索响应。`local_artifacts` 指向本次宿主机私有目录；原始日志只保存在这里，不直接打印。

## 4. 操作二：Web 上传两份产品 WAV，各一次

1. 浏览器强制刷新门户，结束旧通话。打开 F12 → Console，开启 Preserve log，过滤 `voice_file`。保持门户前台，不切后台、不锁屏。无需在 Network 中寻找 messages 或手填 ID。
2. 在宿主机执行：

   ```bash
   bash scripts/validate_deployment.sh web-start golden_yolk
   ```

3. 门户选择 **Audio file → Choose WAV**，选择仓库 `artifacts/native-cuekb/golden_yolk.wav`，点击 **Start file call**。不要点 Stop playback/Cancel search，不要在 WAV 中间停顿时结束通话。
4. 完整发送 WAV 后，等待最新一条回答的 **Speaking** 结束，显示 **Reply complete** 或 **Answer not verified**；声音停止后再静待 **5 秒**，然后点 **End call**。Golden Yolk 以第二段 “Can it be shipped…” 对应的最后一行回答为准，不在第一段或文件中间停顿时结束。没有出现上述状态时最多等待 **120 秒**再 End call；页面提前报错则保留提示后 End call，不重新开始。只需按界面状态和时间操作，不判断内容正误。既有 105 秒上限及活动宽限仍可能触发，到期如实回传，不调大限制。
5. 在宿主机执行：

   ```bash
   bash scripts/validate_deployment.sh web-end
   ```

6. 回传以下三项：本次 `web-start`/`web-end` 的终端输出；Console 过滤后的 `voice_file` 行；下列简短观察记录。无需导出 HAR、完整 messages、访问令牌或全部服务日志。

   ```text
   test_file=golden_yolk.wav
   heard_audio=有声音 / 没声音
   page_error=页面提示原文（没有则写无）
   text_during_speech=播放时已逐步出现 / 播放结束才出现 / 没有文字
   ```

随后再测试 Black Pig 一次：先执行下面的开始命令，再按以上步骤 3–6 上传 `artifacts/native-cuekb/black_pig_crisp_heart.wav`，观察记录中的 test_file 同时改为该文件名。两个产品使用独立通话和采集窗口，不在同一条通话连发两个长产品问题。

```bash
bash scripts/validate_deployment.sh web-start black_pig_crisp_heart
```

Black Pig 来源面板若显示 **Answer not verified**，截图保留原因码和来源数量即可；不要把它改记成查询失败。Golden Yolk 的 **Reply complete** 仅代表已交付回答，不代表运输、保质期、开封储存都已查到。若方便，同时保存两份最终来源面板截图；回答/来源是否相符由开发者根据摘要判断。

同一时间窗口最多自动汇总四个会话，逐个标注 CID；无法匹配时明确输出 unavailable，不挑一个会话冒充本次结果。保存的转写和业务状态由采集器只读获取。音频内容与来源由开发者分析；本页不要求操作者裁决答案是否准确。

`voice_file` 用于补充浏览器的发送、首文字（speech_text.first）、首音频包和播放开始时刻。服务端首包时间不能替代浏览器开始播放，更不能替代实际扬声器听音。如果终端阶段没有延迟而浏览器仍等待，开发者才依据这些行进一步定位。

## 4.1 补充：同一通话发送两个文件

本操作单独运行一次，不与上面的 Golden Yolk 窗口混用。仍使用仓库固定文件，整个测试只点一次 Start file call，不要求操作者判断回答准确性。约一分钟内结束，避免故意撞上原有 105 秒限制。

1. 先 End call 结束旧通话，宿主机执行以下命令。采集以第二个文件 general_tech 命名，窗口会包含两份输入。

   ```bash
   bash scripts/validate_deployment.sh web-start general_tech
   ```

2. 门户 Audio file → Choose WAV，选择 `artifacts/native-cuekb/hello.wav`，点击 Start file call。等待 **10 秒**。
3. 同一页面点击 Choose WAV，选择 `artifacts/native-cuekb/tcp_udp.wav`，点击新按钮 **Send recording**。不要再点 Start file call 或 End call。
4. 保持门户前台，等待 **35 秒**，点击 End call。不切后台、不锁屏。
5. 宿主机执行：

   ```bash
   bash scripts/validate_deployment.sh web-end
   ```

6. 回传终端精简输出、Console 过滤的 `voice_file` 行，以及下面的观察记录。按钮不能点击或页面报错时，保留现状和提示，不重新测试。

   ```text
   files=hello.wav,tcp_udp.wav
   history=两轮都在 / 第一轮消失 / 其他页面现象
   page_error=页面提示原文（没有则写无）
   heard_audio=有声音 / 没声音
   ```

开发者检查两次 file.start/file.sent 的 conversation_id、voice_session_id、epoch 一致，file_id 不同，后端有两份输入记录，原历史未被清空。query 是否出现依具体问题；hello/TCP 都可正常无工具回答。选择下一文件只在当前 WAV 全部发送后开放；下一份只在明确点击 Send recording 后发送，选择文件本身不发送、不重建会话，不自动重播。

文件由 AudioWorklet 渲染时钟发送相同 80 ms 原始 PCM，不通过麦克风/重采样。后台持续播报暂缓，不在本操作验证；系统冻结音频、真实断流、网络错误及原来的连接上限仍生效。出现提示只回传对应精简行，本轮不修改保护阈值、不自动补发旧录音。

## 4.2 定向验证：RNNT 修复后的 Hello / TCP 句首

**故障假设：**显示转写使用 `predict(y=None, add_sos=True)`，其中 `y=None` 已提供一次零输入，再加 SOS 会让 LSTM 多走一步；当前取最后一步并缓存，初始状态因而偏离 NeMo 标准贪心解码。这可能导致音频完整但句首漏识别。已改为 `add_sos=False`，不改变控制分轮/打断的解码状态。上游同一基线的 VoiceChat wrapper 也有原写法，不能据此认定是本项目首次引入，或已经唯一证明现场根因。

**方法与预期：**固定使用 `hello.wav` 和 `tcp_udp.wav`，自动/Web 各一次，每份文件都从新通话开始；不加静音、不改语速、不运行产品用例。保持门户前台。预期最终转写包含完整的 “Hello, how are you today?” 和 “What is the difference between TCP and UDP?”，下述音频和文字交接一致。标准贪心参考固定为 Speech `097dfe9e2f55baf653b83035868bdc89849f1b47` 的 `RNNTDecoder.predict` 与 `GreedyRNNTInfer._pred_step`；没有修改或升级权重。

**自动侧：**在宿主机项目根目录执行以下一条命令。脚本依次为两份 WAV 新建连接，执行失败仍保留该次摘要并采集下一份，不重试同一份录音。

```bash
bash scripts/validate_deployment.sh prefix
```

回传终端输出即可；它已经限定为准备状态、单用例传输结果及每 case 三条定位摘要：

- `prefix_audio`：会话/sequence、源文件到 VoiceChat 解码后的首 4 秒是否一致、重采样后到提交给 Triton 的首 4 秒是否一致，以及输入队列丢弃数。
- `prefix_transcript`：预期句子、实际短转写（每条最多 160 字符、最多 8 条）、句首与全句的词序检查、原始 ASR 到最终文字/API/落库的哈希比较。标点和大小写不用于判定本次缺词。
- `prefix_diagnostic`：定位结果与检查边界。证据缺失、混入多个 sequence、截断或输入条目不齐时明确 unverified，不能当作识别成功或模型故障。

`transport_exit_code=0` 只表示原有传输验证通过；本命令最终以 `prefix_run` 和 `prefix_diagnostic` 为准。首次 4 秒比较包括 WAV 尾部的正常补零/静音。不会将 24 kHz 与 16 kHz 的哈希直接比较，也不据此声称重采样算法、模型实际编码帧或 Nano 理解已正确。

**Web 侧：**打开 F12 → Console，开启 Preserve log，过滤 `portal.transcript.done`；先 End call。下面两次都按“开始采集 → 上传指定文件并 Start file call → 等待 20 秒 → End call → 结束采集”执行，不沿用上一个通话。

第一次开始采集：

```bash
bash scripts/validate_deployment.sh web-start greeting prefix
```

上传 `artifacts/native-cuekb/hello.wav` 并 Start file call，等待 **20 秒**，End call 后执行：

```bash
bash scripts/validate_deployment.sh web-end
```

第二次开始采集：

```bash
bash scripts/validate_deployment.sh web-start general_tech prefix
```

上传 `artifacts/native-cuekb/tcp_udp.wav` 并 Start file call，等待 **20 秒**，End call 后执行：

```bash
bash scripts/validate_deployment.sh web-end
```

回传两次开始/结束命令的精简终端输出、Console 当前过滤出的行，以及每次页面上的用户转写原文。Console 行含 `transcript_chars` 和 `transcript_preview`，代表到达浏览器的最终转写；没有对应行就写“无”。不要求导出 Network/messages、HAR、原始音频或全量日志，也不要求操作者选择故障原因。中途报错照常结束采集，不重新上传或重跑。

开发者按以下边界解释结果，操作者无需判断：

| fault | 证据含义 |
| --- | --- |
| `INPUT_AUDIO_MISMATCH` | 原文件与 VoiceChat 解码后的首段不同；先核对文件身份、发送或解码，不归罪于 ASR |
| `VOICECHAT_INPUT_QUEUE_MISMATCH` | 重采样后的首段与实际提交首段不同；检查缓冲/排队丢弃 |
| `TRANSCRIPT_HANDOFF_MISMATCH` | 原始 ASR 累计文字或 VoiceChat 完成事件与 API 不一致 |
| `TRANSCRIPT_STORAGE_MISMATCH` | API 收到的转写与落库不一致 |
| `UPSTREAM_ASR_PREFIX_MISMATCH` | 已检查的音频和文字交接一致，但 ASR 句首仍缺；本次初始化修复未解决该次现象，停止重复测试并重新评估重采样/编码帧/识别路径，不据此断言模型没听见 |
| `TRANSCRIPT_CONTENT_MISMATCH` | 句首满足预期，句子其他部分仍不一致 |
| `INCOMPLETE_EVIDENCE` / `SESSION_UNVERIFIED` | 缺少可判定证据，停止本轮并报告缺口 |
| `PASS` | 本次固定句子的转写比对通过；不代表整体问答、所有语音或真实听音已验收 |

同一会话的所有最终输入条目一起核对，避免把拆成两轮的句首误报为丢失。API、落库和浏览器预览一致时，不通过门户补写 Hello 或问句前缀。两文件自动/Web 均通过则停止本次缺词调试；这不代表整体业务验收或长期准确率已完成。仍缺词或证据不齐就记录该次结果并停止同一方案复测，不重复改参数、不扩大到 20 轮。CPU 受控 LSTM 的首 token 反例不能作为现场识别成功的证明。

## 4.3 本轮修复、预期输出与判定

**已知事实：**此前 Golden Yolk CID `7546bdcd-ec6d-41ea-85dc-8b130b18ea88` 的第二段有五个正式 response，全部以 `render_budget` 强制 EOS 结束，其后四次在无新输入/结果时接纳原生 BOS。旧修复只补齐结束事件，没有覆盖结束后持续解码。该轮 CueKB 两次合计 189 ms，结果注入合计约 22.46 秒；转写完成到**首次**正式音频约 29.28 秒，最后一次注入到首次音频约 345 ms；四次额外回答又占约 56.64 秒。旧摘要 71012 ms 选中了最后一段首包，不能当作初始等待。

**本轮已编码，待现场验收：**

- VoiceChat 保留既有 PAD 完成保护，强制结束后等待新输入；静音时的 BOS/正文/function token 在 TTS、解析及后续模型输入前抑制。首个新 RNNT 非空白语音帧或待处理调用的有效回填解除等待；不提高 BOU 门槛，不清空历史、不关闭整条连接，不改权重、显示转写修复或正常 ACK/工具注入。不能仅隐藏页面字幕来宣称解决。
- 同一 input/连接中，成功写回过的**完全相同**来源正文/标题/上下文/条件不再次注入；新来源完整发送。一次批次中尚未提交的来源不会被误判已发送，下一 input 不共享去重状态。第二次 sources 为空、status=ok 时明确沿用本问题先前证据；不是撤销证据或知识不存在。API 校验/落库仍保留累计来源及权限/版本检查。
- 英文提示要求一个 query 覆盖产品和全部所问属性，补查合并缺失项；缺失信息合并说明一次，保留产品/包装/制作状态归属，不反复道歉或重新询问已经明确的产品。它只是模型引导，不是语义正确性证明。
- 内容校验失败仍为 failed；有权限的已检索来源可供复核，页面显示 **Answer not verified** 和原因码，不再一律叫 Search failed。权限/版本失效继续隐藏来源；实际检索错误才显示 Search failed。成功状态显示 **Reply complete**，不能理解为所有字段均已查明。
- 自动验证完整发送 WAV 后等待最新输入的正式文本、非空音频及 done，再保留 **5 秒**静音观察。出现同输入不同 response 的新正文即报 **REPEATED_ANSWER**，不等待再次完成再冒充通过；没有可分类文字的额外音频也明确报告，不能当作静音通过。WAV 仅剩尾部静音时，最新输入已经完成的回答会在 EOF 后进入观察，不要求模型再回答一次。到期打断观察仍失败，不提高 105 秒上限。

以下为开发者核对字段，操作者回传命令精简输出即可，无需自行选择行：

| 输出 | 预期与含义 |
| --- | --- |
| Golden Yolk preflight | cases=2：原 Golden Yolk + 同连接 hello.wav，不是新增产品输入 |
| case | `extra_answers=0 quiet_observation=passed`；产品查询 <=2；内容检查失败仍明确 failed/reason，不能通过改标签放行 |
| 同连接下一问 | 同一 conversation_id 中出现 greeting，非空音频及 answer_complete=passed；Hello 不应触发工具 |
| input_timing | `question_to_first_answer_ms`、`last_result_to_audio_ms` 按末次结果之后的**首个**正式 response 计；`answer_responses` 和 `additional_answers_after_result` 不隐藏后续重复 |
| evidence_delivery | `new_sources` 是本批新注入数，`retained_sources` 是本问题累计已交付来源数；重复来源无须再次消费 function token |
| backend_injection / backend_stages | 对比实际 token、ACK 缓冲和每次注入耗时，不以字节下降宣称 GPU 延迟已达标 |
| answer_check / turn | input/response、reason、最多三条 condition_failures，以及 answer_sources/retrieved_sources；定位 Black Pig 具体拒绝点，不索取全量日志 |
| 门户 | 检索/校验标签对应真实阶段；正文在播报结束前逐步出现；来源面板不把检索命中当作所有口述均已证实 |

内容验收继续逐项核对事实和条件。检索材料没提供某项不代表整个知识库没有，也不能把两条产品线的 fresh-baked 与 sealed 合并成一个产品属性。当前 Black Pig 截图未提供具体 reason，不能预先认定是条件遗漏；新摘要会直接给出对应检查结果。

每份原 WAV 在自动/Web 各执行一次。本轮后若同一方案两次无新增证据，停止复测；三种假设仍无进展则报告阻塞。CPU 回归证明控制边界和传输契约，不能替代 GPU 续答、听音、内容或真实时延验收。

## 5. 完整四文件对照（开发者要求时执行）

自动侧只需一条命令；四份主文件分别建立新连接，一份失败仍采集其输出并继续其他文件；Golden Yolk 额外带同连接 Hello 追问：

```bash
bash scripts/validate_deployment.sh auto all
```

Web 侧按以下顺序，每个文件重复第 4 节的全部动作，只替换开始命令及上传文件；结束命令固定不变。

| 顺序 | 开始命令 | 上传的仓库文件 |
| --- | --- | --- |
| 1 | `bash scripts/validate_deployment.sh web-start greeting` | `artifacts/native-cuekb/hello.wav` |
| 2 | `bash scripts/validate_deployment.sh web-start general_tech` | `artifacts/native-cuekb/tcp_udp.wav` |
| 3 | `bash scripts/validate_deployment.sh web-start black_pig_crisp_heart` | `artifacts/native-cuekb/black_pig_crisp_heart.wav` |
| 4 | `bash scripts/validate_deployment.sh web-start golden_yolk` | `artifacts/native-cuekb/golden_yolk.wav` |

每份文件只执行一次，回传格式同第 4 节。固定四文件隔离连接是为了比较相同起点，不等于同连接多轮已验收。Golden Yolk 的两段始终保留在同一次上传、同一连接中。

## 6. VoiceChat 独立核对

**文件与配置检查**已包含在 `check`、`auto` 和 `web-start` 中；无需人工运行 hash、编译或导入 Triton 模块。检查成功只证明磁盘文件匹配和诊断配置，不能证明模型行为。模板在进程启动时加载，因此仅覆盖挂载文件而不重建进程，不能算已部署。

**原生工具传输检查**已经由同次日志采集输出：`session.session_match`、`backend_prompts`、`model_function_count` 和 `backend_injection`。只统计通过 input/call/response ID 关联的 sequence；无法关联时模型次数为 null，不能解释为 0。后端 Prompt 中应包含 `search_knowledge_base`；文件身份和 Prompt 传递均不能证明回答内容准确。

**批次预期：**同一解码批次的多个调用由 API 分别结清，VoiceChat 接收一次聚合结果。若第一次单调用、第二次两个调用，可能出现 API received=3、实际检索=2、result_injections=2；不是“三次调用必须三次注入”。累计证据必须保留，已排队超额成员不应误报为终态之后的新调用。模型实际调用数可能变化，不能强求某个固定次数。

**官方客户端能力检查**只在开发者明确要求确认 `--function-text-output` 时执行一次，不重跑 WAV：

```bash
bash <<'SH'
set -uo pipefail
umask 077
LOG=$(mktemp /tmp/voice-client-help-XXXXXX.log)
if docker exec "${VOICE_CONTAINER:-nemotron-labs-voicechat}" python /opt/voice-validation/nemotron-voicechat-client.py --help >"$LOG" 2>&1; then
  if grep -q -- '--function-text-output' "$LOG"; then
    echo official_client_function_log=supported
  else
    echo official_client_function_log=not_advertised
  fi
else
  echo official_client_function_log=unavailable
fi
SH
```

回传这一行即可。该参数是官方客户端保存自身连接的工具事件的能力，不是 Web/API 连接的旁路抓包开关；不据此启动另一条连接替代本次证据。

## 7. 开发者判据（操作者无需执行判断）

### 核心问题与最少证据

| 问题 | 使用已有摘要的判定边界 |
| --- | --- |
| Black Pig 是否由模型发出工具 | session_match=passed 时结合 model_function_count；0 表示本窗口未观察到模型函数，不等于注册失败 |
| 模型发出但应用没执行 | 对比模型函数、API received、实际检索和批次结清；先排除去重、预算及错误，不机械要求次数相等 |
| 工具结果是否回来 | api_cuekb 状态、命中、trace、耗时；results_submitted/result_injections 与 backend_injection 分层核对 |
| Golden Yolk 第二段是否调用 | 按 record 的输入正文和 input_item_id 关联工具、预算、回答；不拿第一段答案放行 |
| 时间卡在哪里 | input_timing 给出转写→首次调用、转写→首次正式音频、末次结果→首个非 ACK 首包与重复回答数；backend_stages 给出 ACK 缓冲/注入/idle；结合浏览器 voice_file 的播放阶段 |
| 回答是否来自知识库 | turn、trace、query、实际准入的 admitted_evidence，与回答逐项对照；omitted 大于零时不能声称已完整核验 |
| 流式文字 | input_timing.answer_text 为正式回答首文字发送时间；text_before_done_ms 应大于零；浏览器 speech_text.first 应早于对应 speech_text.done；最后用操作者的文字出现观察确认显示 |

时间基于 Docker 日志观察时刻，不是 GPU 内部精确计时。多次检索间隔可能包含 ACK 和模型决策；没有对应 response 的完成记录时，不把未分类音频当成最终答案。摘要明确标注 truncated/omitted 的部分不作完整证据。首文字时间不是全文完成时间，也不是 DOM 已渲染的证明。需要某条引用或回答的完整内容时，开发者应给出**只提取该 input/response/citation** 的只读命令，不能转而要求全量日志。

### 放行标准

- 普通问候和 TCP/UDP 可以无工具、无来源；产品问题应按注册工具进入 CueKB。
- 每 input 实际检索不超过默认 2 次，无 HTTP 自动重试；精确重复和预算耗尽进入终态；终态后的新调用明确失败，不无限回放。
- 每完整批次一次聚合注入；新增完整来源补充到模型本问题历史，已发送相同来源不重复注入；累计校验/引用与权限/版本检查保留；后续真实新输入拥有新预算和独立去重集合。
- 最新输入的非 ACK 文本、非空音频与结束事件齐全，结束后 5 秒无无输入重复正文，同连接下一问正常，且产品答案提交为 answered、有引用。终态已提交、音频已发送、工具诊断 passed 均不是答案成功的替代条件。
- 内容应回答有据事实、保留数字/单位/限定条件；无法确认的部分明确说明“本次检索材料无法确认”。不能声称事实不存在或整个公司知识库没有该信息。诚实部分回答可以通过，但内容由开发者核对，必要时请求对应音频。
- Web 与自动验证采用同版 WAV、相同 API Relay/工具目录/VoiceChat/CueKB；原始 24 kHz 单声道 PCM16、80 ms 帧，末帧补零，ready 后发送，EOF 后继续静音；Web 文件时钟使用 Worklet，不依赖后台窗口定时器。浏览器真实播放与回执仍与自动脚本不同，不能宣称逐毫秒等同或模型选择必然一致。

### 后续验收，不混入当前故障轮次

同连接多轮/指代、麦克风打断、等待工具交互、断线轮换恢复、权限撤销和容量仍需独立验收，不因单个知识答案通过而放行全产品。需要多轮/打断时，沿用 `web-start`/`web-end` 采集流程，开发者在执行前提供固定英文问题、说话顺序和等待秒数；不得要求操作者临场决定如何触发问题。自动四文件新连接测试不替代这些专项。

同一假设两次无新增证据即停止；三种假设仍无进展，报告环境、设计或技术路径阻塞。不得盲目重复、扩大预算、调整 WAV 或将诊断通过写成业务通过。

## 8. 当前事实与未解决事项

截至 2026-10-22，代码完成与现场放行严格区分：

- 本轮等待新输入、同输入来源去重、阶段标签、累计证据复核和有界结束观察已编码；真实 GPU、CueKB、页面及听音结果尚未获得，不能宣称重复回答和 40–50 秒等待已现场解决。
- 最新已关联现场证据及明确控制缺口集中在第 4.3 节，不重复历史排障流水。两次提示音可能对应两批正常检索；不以提示次数断定工具循环或检索失败。
- 用户最新截图：Black Pig 播出产品资料但来源面板 Search failed；代码确认 failed 同时被用于检索错误和播后检查错误，具体该轮 reason 尚未知。Golden Yolk 有五条引用及缺失声明，但引用不能证明全部所问字段被回答。修复标签不降低内容要求。
- Hello/TCP 录音缺词按用户多次正常反馈收敛；不扩大为麦克风或全部语音识别准确率验收。
- 105 秒连接上限、固定 30 秒活动宽限及后台持续播报按用户决定保留/暂缓，本轮不调时限，不做后台或锁屏验收。
- 内容覆盖、产品属性归属、真实时延、完整多轮及麦克风打断仍须依次取得真实证据；不能由传输计数、Prompt 匹配或单次 answered 推定全部通过。

历史逐轮记录、旧设计和重复验证手册已删除，Git 保留追溯。外部 VoiceChat 源码、manifest 和接口快照保留，属于运行文件与协议资产，不是另一份操作手册。

## 9. 开发回归（开发者在开发环境执行）

生产操作者不执行本节。这里只列本仓库回归入口；CPU 夹具不代表真实 GPU/知识库/听音通过。开发环境需 Python 3.12、Node/npm；依赖版本沿用仓库固定文件，按实际修改选择测试。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-dev.txt
npm ci --prefix apps/web
.venv/bin/python -m pytest -q
.venv/bin/ruff check apps/api tests scripts
npm test --prefix apps/web
npm run build --prefix apps/web
PYTHONPATH=apps/api .venv/bin/python scripts/export_contracts.py
```

浏览器文件入口夹具：先在一个终端执行 `npm run dev --prefix apps/web -- --port 5173`，在另一个终端执行 `cd apps/web && npm run test:e2e -- voice-file.spec.ts`。若使用系统 Chromium，给后一条命令设置 `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium`。供应商和 PCM 由夹具生成，不是真实模型准确率。

VoiceChat 外部 CPU 回归在独立环境执行，避免改 GPU 容器依赖：

```bash
python3.12 -m venv /tmp/voicechat-cpu-tests
/tmp/voicechat-cpu-tests/bin/python -m pip install -r docs/voicechat/s2s/tests/requirements.txt
/tmp/voicechat-cpu-tests/bin/python -m pytest -q docs/voicechat/s2s/tests
```

本轮本地结果：VoiceChat 实际方法的生命周期回归 56 项通过，覆盖强制 EOS 后继续静音、BOS/正文/function 抑制、短语音恢复和有效工具回填。API、工具目录、批次、累计证据、权限撤销与采集相关回归通过；分组存在重叠，不累加成全仓测试总数。新增真实 HTTP/WS 夹具验证单 WAV 两段输入、尾部静音、结束后重复正文或无字幕音频的检出，以及同连接下一问与到期观察失败。

前端 Node 38 项通过、生产构建通过；Chromium 两项定向回归通过，分别覆盖知识字幕/失败原因/授权引用与撤销隐藏，以及同一通话两份 WAV 保留历史。Ruff、Python 编译、Shell 语法、11 个外部快照文件 hash 和 Markdown 链接检查通过。以上是受控服务/CPU/浏览器夹具，不是实际 VoiceChat GPU、真实 CueKB 内容、麦克风或扬声器验收；真实注入提速和口述事实尚待本页第 3、4 节的部署结果。
