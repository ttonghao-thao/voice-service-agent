# 统一验证操作手册

这是本项目唯一的验证文档，适用于英文（en-US）全双工知识客服：Web → API → NVIDIA VoiceChat → CueKB → 有来源的语音回答。所有操作、回传内容、判据和当前未解决项均在本页，不需要打开其他文档。

**操作者只执行命令和回传精简输出，不负责判断原因、选择日志、找会话 ID 或调整参数。** 命令失败也保留输出，交给开发者定位；不要反复重跑、提高检索次数、延长会话或删除 WAV 的停顿来获得通过。

## 1. 本次范围与执行顺序

本次包含尚未部署的回答覆盖度与结果注入时延、Web 流式文字展示，并补充同一通话连续发送 WAV、页面转后台不主动结束。Golden Yolk 自动/Web 第二段已有闭环现场证据；本轮在同一 WAV 上各执行一次，对比实际回答、结果 token/注入时间，以及文字是否在回答结束前出现。需要完整四文件对照时，执行第 5 节；不要同时执行两套方案。

2026-10-22 新增 Hello/TCP 句首缺失的定向定位。本次只验证该问题时，完成第 2、2.1 节后执行第 4.2 节；不同时执行产品用例或四文件全测。不修改 WAV、前置静音、RNNT 门槛或模型参数；该版是定位交付，尚未证明缺词修复。

后台持续播报按用户决定暂缓，作为非阻塞待优化项；保留页面隐藏不主动结束及回前台尝试恢复的现有实现。本轮 Web 验证保持前台，不要求切后台或锁屏，不将后台连续播放作为放行条件。

输入唯一来源是本仓库 `artifacts/native-cuekb/` 的 `cases.json` 和四个 WAV。API 镜像内对应 `/app/validation/native-cuekb/`。所有 Python 在容器内执行；宿主机只需 Bash、Docker Compose、Git 和常用 shell 工具。**不使用 docker cp，不在容器创建输入目录，不使用历史 ZIP。** 输出目录由脚本自动创建，API 的只读根文件系统不受影响。

宿主机执行入口是 `bash scripts/validate_deployment.sh`；它自动调用 `docker exec`，寻找 API 容器、检查固定文件、提取本次时间窗口和会话、采集匹配的 VoiceChat sequence、查询已保存答案。采集不会新增模型请求或 CueKB 请求；`auto` 和 `prefix` 会发送测试语音。默认 VoiceChat 容器名为 `nemotron-labs-voicechat`。

## 2. 更新本次验证工具并准备环境

所有命令在**宿主机项目根目录**执行，保持同一主机、目录和用户。先结束已有测试通话；本次采集期间不要有其他测试用户通话。

本轮累计修改 API/Web，并新增转写哈希与门户有界预览，必须重新制作 API 和 Web 镜像；下面命令同时完成两者。随后执行第 2.1 节更新 VoiceChat audio_server.py 的元数据诊断。假定 `.env` 和既有部署已经可用；不改变数据库结构、工具预算和 VoiceChat 推理逻辑。

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

VoiceChat 已加载本仓库完整快照是本次验证前提；检查会逐个比对六个运行文件，包括 audio_server、模板、model、loader、runtime_setup 和 data_types，并检查 Jinja/诊断开关。本轮仅 audio_server 的元数据诊断更新；其他文件不匹配时停止并回传，不猜测 GPU、模型挂载参数。会话提示由 API 在新通话注册，因此更新后必须新建通话。

## 2.1 更新现有 VoiceChat 文件挂载并检查就绪

先结束所有通话。当前部署按单文件 bind mount 加载 `/s2s/audio_server.py`；以下命令自动找到它的宿主机原文件，备份后更新原文件内容并重启现有容器进程，不改变镜像、GPU、环境、端口或挂载。不创建容器目录，不复制验证文件进容器，也不使用 docker cp。这里重启足以加载同一文件的内容变化；镜像、环境或挂载变化仍需重新创建容器。

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
HOST_FILE=$(docker inspect "$VOICE_CONTAINER" 2>>"$LOG" | docker exec -i "$API_CONTAINER" python -c '
import json, sys
mounts = [m for m in json.load(sys.stdin)[0]["Mounts"]
          if m["Type"] == "bind" and m["Destination"] == "/s2s/audio_server.py"]
if len(mounts) != 1:
    raise SystemExit(2)
print(mounts[0]["Source"])
' 2>>"$LOG") || stop audio_server_bind_not_found
test -f "$HOST_FILE" || stop missing_host_source
BACKUP=not_needed
if ! cmp -s docs/voicechat/s2s/audio_server.py "$HOST_FILE"; then
  BACKUP=$(mktemp /tmp/voicechat-audio-server-before-XXXXXX.py) || stop backup_path
  cat "$HOST_FILE" >"$BACKUP" || stop backup
  if ! cat docs/voicechat/s2s/audio_server.py >"$HOST_FILE"; then
    cat "$BACKUP" >"$HOST_FILE" 2>>"$LOG" || true
    stop update_source
  fi
fi
docker restart "$VOICE_CONTAINER" >"$LOG" 2>&1 || stop restart
READY=0
RUNTIME_DEADLINE=$((SECONDS + 180))
while (( SECONDS < RUNTIME_DEADLINE )); do
  if docker exec "$VOICE_CONTAINER" python -c '
import json, os, urllib.request
port = os.getenv("SERVER_PORT", "9000")
with urllib.request.urlopen("http://127.0.0.1:" + port + "/v1/realtime/health", timeout=5) as response:
    assert json.load(response)["status"] == "ok"
' >"$LOG" 2>&1; then READY=1; break; fi
  sleep 3
done
test "$READY" = 1 || stop health_timeout
printf 'voicechat_update=ready source=%s backup=%s inference_logic=unchanged\n' "$HOST_FILE" "$BACKUP"
bash scripts/validate_deployment.sh check
SH
```

回传 `voicechat_update`、`preparation`、`runtime` 行。失败时只回传 `voicechat_update=failed` 或 `version=failed`/`collection=failed` 行，不反复重启或索取完整日志。元数据指纹沿用已有 `VOICECHAT_DIAGNOSTICS=1`，每连接每音频阶段仅输出一次首 4 秒哈希，ASR 只输出长度与哈希，不开启原始录音采集。

## 3. 操作一：自动验证一次 Golden Yolk

```bash
bash scripts/validate_deployment.sh auto golden_yolk
```

执行期间等待命令自行结束，通常需一至两分钟。**把本命令终端输出全部回传即可，它已经是精简结果。** 不要另外运行 docker logs、cat report.json 或贴完整日志。

入口会在全新连接中发送原始 WAV，保留中间停顿；完整发送后等待最新输入的非 ACK 回答完成。120 秒是验证器等待上限，不改变服务端会话时限。每次使用独立报告路径，失败不会误读上一次报告。即使验证失败，仍尝试采集这次的摘要。

终端输出包括：版本准备、`preflight`、`case`、`validation`、退出码、`session`、API 预算与检索、`backend_injection`、每输入 `input_timing`、有界 `backend_stages`、保存的输入/回答/业务状态、模型 query 和实际准入证据 `admitted_evidence`。证据总长度最多 6000 字符、8 个块，超出明确标记 omitted；不要另外打印全量检索响应。`local_artifacts` 指向本次宿主机私有目录；原始日志只保存在这里，不直接打印。

## 4. 操作二：Web 上传同一 WAV 一次

1. 浏览器强制刷新门户，结束旧通话。打开 F12 → Console，开启 Preserve log，过滤 `voice_file`。保持门户前台，不切后台、不锁屏。无需在 Network 中寻找 messages 或手填 ID。
2. 在宿主机执行：

   ```bash
   bash scripts/validate_deployment.sh web-start golden_yolk
   ```

3. 门户选择 **Audio file → Choose WAV**，选择仓库 `artifacts/native-cuekb/golden_yolk.wav`，点击 **Start file call**。不要点 Stop playback/Cancel search，不要在 WAV 中间停顿时结束通话。
4. 从点击 Start file call 起等待 **120 秒**，再点 **End call**。即使页面提前报错，也不重新开始、不重试；把页面提示保留下来。固定等待是为了不要求操作者判断回答是否完整。当前连接默认约 105 秒空闲轮换，所以可能在等待期间出现 session limit 提示；照常记录即可，不重新测试。本轮按用户要求保留该限制，120 秒并非引擎固有限制。
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

## 4.2 定向定位：Hello / TCP 句首转写缺失

假设只有两个边界方向：进入 VoiceChat/提交推理前音频已经不同，或者原始 ASR 及后续文字交接发生缺失。本轮不先修改识别逻辑。固定使用 `hello.wav` 和 `tcp_udp.wav`，自动/Web 各一次，每份文件都从新通话开始；不加静音、不改语速、不运行产品用例。保持门户前台。

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
| `UPSTREAM_ASR_PREFIX_MISMATCH` | 已检查的音频和文字交接一致，但 ASR 句首已缺；继续定位重采样/编码帧/RNNT，不据此断言模型没听见 |
| `TRANSCRIPT_CONTENT_MISMATCH` | 句首满足预期，句子其他部分仍不一致 |
| `INCOMPLETE_EVIDENCE` / `SESSION_UNVERIFIED` | 缺少可判定证据，停止本轮并报告缺口 |
| `PASS` | 本次固定句子的转写比对通过；不代表整体问答、所有语音或真实听音已验收 |

同一会话的所有最终输入条目一起核对，避免把拆成两轮的句首误报为丢失。API、落库和浏览器预览一致时，不通过门户补写 Hello 或问句前缀。一次自动通过不能排除 Web/模型的非确定性；本轮只取得一组对应边界证据，不扩大到 20 轮复测。

## 5. 完整四文件对照（开发者要求时执行）

自动侧只需一条命令；四份文件分别建立新连接，一份失败仍采集其输出并继续其他文件：

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
| 时间卡在哪里 | input_timing 给出转写→首次调用、末次结果→非 ACK 首包；backend_stages 给出 ACK 缓冲/注入/idle；结合浏览器 voice_file 的播放阶段 |
| 回答是否来自知识库 | turn、trace、query、实际准入的 admitted_evidence，与回答逐项对照；omitted 大于零时不能声称已完整核验 |
| 流式文字 | input_timing.answer_text 为正式回答首文字发送时间；text_before_done_ms 应大于零；浏览器 speech_text.first 应早于对应 speech_text.done；最后用操作者的文字出现观察确认显示 |

时间基于 Docker 日志观察时刻，不是 GPU 内部精确计时。多次检索间隔可能包含 ACK 和模型决策；没有对应 response 的完成记录时，不把未分类音频当成最终答案。摘要明确标注 truncated/omitted 的部分不作完整证据。首文字时间不是全文完成时间，也不是 DOM 已渲染的证明。需要某条引用或回答的完整内容时，开发者应给出**只提取该 input/response/citation** 的只读命令，不能转而要求全量日志。

### 放行标准

- 普通问候和 TCP/UDP 可以无工具、无来源；产品问题应按注册工具进入 CueKB。
- 每 input 实际检索不超过默认 2 次，无 HTTP 自动重试；精确重复和预算耗尽进入终态；终态后的新调用明确失败，不无限回放。
- 每完整批次一次聚合注入；累计来源与权限/版本检查保留；后续真实新输入拥有新预算。
- 最新输入的非 ACK 文本、非空音频与结束事件齐全，且产品答案提交为 answered、有引用。终态已提交、音频已发送、工具诊断 passed 均不是答案成功的替代条件。
- 内容应回答有据事实、保留数字/单位/限定条件；无法确认的部分明确说明“本次检索材料无法确认”。不能声称事实不存在或整个公司知识库没有该信息。诚实部分回答可以通过，但内容由开发者核对，必要时请求对应音频。
- Web 与自动验证采用同版 WAV、相同 API Relay/工具目录/VoiceChat/CueKB；原始 24 kHz 单声道 PCM16、80 ms 帧，末帧补零，ready 后发送，EOF 后继续静音；Web 文件时钟使用 Worklet，不依赖后台窗口定时器。浏览器真实播放与回执仍与自动脚本不同，不能宣称逐毫秒等同或模型选择必然一致。

### 后续验收，不混入当前故障轮次

同连接多轮/指代、麦克风打断、等待工具交互、断线轮换恢复、权限撤销和容量仍需独立验收，不因单个知识答案通过而放行全产品。需要多轮/打断时，沿用 `web-start`/`web-end` 采集流程，开发者在执行前提供固定英文问题、说话顺序和等待秒数；不得要求操作者临场决定如何触发问题。自动四文件新连接测试不替代这些专项。

同一假设两次无新增证据即停止；三种假设仍无进展，报告环境、设计或技术路径阻塞。不得盲目重复、扩大预算、调整 WAV 或将诊断通过写成业务通过。

## 8. 当前事实与未解决事项

截至 2026-10-22，以下是历史证据摘要，**不是本次更新后的新部署结果**：

- Black Pig 自动单 case 曾现场通过：2026-10-20 15:20 UTC，CID `6d9dfaa1-1f84-4df6-94dc-65a3f711bdf3`，2 次查询、10 条来源、终态 1、终态后调用 0、answer_complete=passed、answer_status=answered。不能推导 Web/所有产品/连续多轮通过。
- 2026-10-21 新部署的 Golden Yolk 自动（CID 7337b531-c299-41e0-87e1-7826b8518807）和 Web（CID afbf9a21-b167-4160-a673-b95ba2e78caf）均完成两次查询、两批注入、一个超额成员结清、零终态后调用，提交 5 条引用。该版批次闭环在这两轮通过。
- 同两轮第二段到正式音频首包分别 35.43/35.56 秒，两次注入合计 28.54/28.47 秒；CueKB 合计 201/192 ms。注入结果为 390–437 tokens，1573–1726 字符，作为本轮对照基线，不承诺线性提速。
- 同两轮回答没有回应运输；只有两条标题性来源节选，尚不能核定其余内容。新采集器输出已保存的 query 和准入证据，开发者判断逐项覆盖，不能以 answered 冒充内容通过。
- 用户要求保留单连接上限：默认 105 秒，繁忙时固定额外 30 秒，超过约 135 秒强制结束。120 秒只是测试等待。该机制标为待优化，本轮不改时长配置、watchdog、到期轮换和对应错误提示；另按补充需求移除页面隐藏触发的主动结束。
- 后台持续播报按用户决定暂缓，标记为非阻塞待优化项；页面隐藏不主动结束和回前台尝试恢复的代码保留。当前事件测试不证明手机切应用或锁屏仍持续播放，后台等待结果后开始播报也未实机验收；本轮只做前台功能验证。
- Web 最新对照确认 Black Pig 出现 Checking knowledge；先前“始终不调用”的概括已不成立。回答准确性和重复响应仍需分析。
- Golden Yolk 的排队调用曾在终态回填前约 195 ms 到达，被旧实现按出队状态误判；该历史误判经批次修正后，上述两次部署测试已完成第二段闭环；内容与时延另行核定。
- 已观测 CueKB 单次约 0.1 秒，不能把估算的约 30 秒等待全部归因于检索。上述关联时间线已将主要等待定位到逐 token 结果注入，而非 CueKB 请求。
- Hello/TCP 开头缺词按 2026-10-22 新需求进入定向定位；已增加指纹与转写交接检查，尚无新现场结果，不宣称已修复。

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

兼容的旧双工具模拟脚本不属于本期验收，不再维护独立操作手册。本次采集入口的定向回归使用 `.venv/bin/python -m pytest -q tests/contract/test_validation_collection.py tests/contract/test_deployment.py`。本次本地执行 18 项通过，Shell 语法、Markdown 链接与 VoiceChat manifest 检查通过；未执行现场 GPU/CueKB 验证。定向回归验证会话隔离、摘要边界、失败保留与镜像打包，不验证真实推理。

本轮本地回归覆盖原生 ACK/正文流式发送、回答完成前不提交、两次预算/累计证据及权限；浏览器用受控 HTTP/WS 夹具检查结束事件前正文可见、补充查询 Turn 更替、历史不重复与权限撤销。稳定事实规则保留在会话提示，结果内只发送逐项覆盖和搜索状态说明，删除 Source excerpt 通用标题，保留所有准入正文、有效标题、上下文和条件。实际 token、时延与语义覆盖只能由新部署测试证明。

本轮累计代码的本地结果：上一未部署提交 0e9946a 已执行后端相关回归 159 项通过；本次仅新增 Web 行为，未重跑后端。最新 Node 音频测试 35 项通过；Chromium 文件入口 12 个独立场景通过（全组 11 个通过后修正按钮可访问名称，双文件场景定向复测通过）；Web 构建通过。后续针对时间摘要的定向检查与上述组有覆盖重叠，不累加为全量计数。终态说明本地长度由 488 降至 199 字节，约减少 59%；这是说明文本的字节比较，不是整个结果或实际注入时延降低 59%。本轮新代码尚未部署到现场，不能继承旧版 answered 作为本轮内容验收。

本轮补充的自动检查：以真实 Worklet 代码配合可控时钟核对两个 WAV 的原始字节、末帧补零、EOF 静音、跨文件 seq 连续、重复 ready 不重播、Stop playback 不重置文件位置、隐藏事件不结束会话；Chromium 使用受控 HTTP/WS 验证双文件同 CID/SID、历史保留与后台事件。它们不代表实际手机操作系统后台能力或真实供应商对每份文件的分轮都已通过。

2026-10-22 句首定位代码本地检查：后端/部署/采集相关 170 项通过，外部 VoiceChat CPU 31 项通过，Web 音频 36 项通过，Web 构建通过；后续 35 项定向复查与前述组重叠，不累加。覆盖真实 PCM 解码、soxr 重采样与提交边界哈希、样本不被诊断修改、ASR 跨条目累计、无内容日志、缺证据不误报、Web 预览有界、宿主机入口每文件只执行一次、时间窗口排除旧记录及镜像打包。manifest、编译、链接及手册 Shell 语法检查通过。未执行真实 GPU/现场缺词复现，不能将这些本地结果写成 Hello/TCP 已修复。
