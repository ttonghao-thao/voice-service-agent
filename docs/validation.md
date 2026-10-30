# 当前问题验证：工具等待、首问触发与回答完整性

更新：2026-10-30。当前唯一执行入口；历史验证方法已归档。**第 15 节记录 CueKB `30970e9` 问题级证据接入、单查询终态和欢迎语 EOS 修复的最新验收标准。** 第 8—14 节保留已有现场事实用于对照；部署后以第 15 节的新契约判据为准，首问工具选择、真实内容和 GPU 时延仍待验收。所有命令在**部署主机的本项目根目录**执行，操作者只操作、观察和回传，不需要挑日志、找会话 ID 或判断根因。

## 1. 本轮要回答什么

本轮验证完整工具结果消费、多轮补充输入关联、原任务结清、同连接下一问以及引擎结束的正常收尾。最新 CueKB 返回 5 条证据只用 122 ms，后续 17.216 秒主要是模型消费；不把 Searching 文案等同于 CueKB 仍在执行。

| 当前问题 | 已有证据 | 本轮验证目标 |
| --- | --- | --- |
| 请求消失后仍向旧 ID append，END 再次异常 | 99.673 秒连接时长时出现 request not found；旧封装忽略 output.finished | 不再追加终止请求；摘要给出 backend.request_finished 的 reason/prompt/appended/output 计数；END 独立清理，不复现 500 清理异常 |
| 第一段一直 Searching，补充输入显示一般回答 | 工具属于第一段，注入后回答关联第二段；原回答 future 没结清 | 保留工具所属输入和补充输入关联；原知识任务结清，补充段显示 Included in the knowledge reply above，同连接下一问正常 |
| 查询时 ACK 中断，播放积压或 VOICE_UNAVAILABLE | 两次注入均开始后约 39 ms 退出、未回 idle；本地已复现跨线程写 inference tensor 的 RuntimeError，补处理 ACK 存在突发输出 | 完整听到 ACK；无连接异常，backend_injection=complete 且 returned_idle/done_idle 各一次，随后出现正式回答；核对播放与服务器完成时间 |
| Checking knowledge 长、模型可能继续调用 | 旧样本中 CueKB 约 0.1—0.2 秒，主要耗时在 VoiceChat 逐 token 注入；新契约每输入只允许一次问题级查询 | 分开 query、validate、应用交付、ACK/调度和结果注入；若模型继续调用，应记录 settled 且 CueKB query 数不增加、连接不关闭 |
| Golden Yolk 独立不触发，Black Pig 后触发 | 用户观察；工具描述已覆盖两种产品，Golden WAV 有追问式开头和中间停顿 | 对照全新连接的 Golden 与同连接 Black Pig 后的 Golden，核对实际输入、分轮、模型调用和 query，而不只看界面状态 |
| 回答漏项、数值及条件表达不稳 | Black Pig 一段称未找到重量，另一段报 65 克，尚不知是否同次提问；Golden 漏答运输 | 明确每段答案属于哪次输入，核对价格/重量/层数/最佳食用时间，以及运输/保质期/开封储存；保留真实来源 |

**复测固定为两次通话、三次文件发送：**A 为 Golden 独立首问；B 为新通话先 Black Pig、再在同一连接发送 Golden。不同时运行自动 core、四文件、Hello/TCP、RNNT 定向或后台测试；不改 WAV、停顿、语速、查询预算、模型参数或会话限制。工具描述、schema 与模板已在本次提交配套更新，不在现场临时改写。这样得到的是首次可比较样本，不足以单独证明稳定性或 P95 达标。

## 2. 更新本次代码并检查部署

本节在部署主机执行，不是开发环境已经部署的记录。先将本次完整提交检出到主机项目目录，保持已有 `.env`、已验证的双 GPU（0,1）配置、9000 端口及模型挂载；停止旧测试通话，本次窗口内不要有其他测试用户。CueKB 由其运行方发布新版，本仓库不替它迁移、修改内容或重建索引。

### 2.0 先同步执行目录

`version=failed` 的 expected 来自**执行脚本所在目录的宿主文件**，actual 来自容器磁盘；expected 不等于远程 main 的最新版本。先在当前本项目目录执行下段，再使用更新后的本页执行 2.1 和 2.2；不要从旧目录构建覆盖新 API。命令保留 `.env` 和数据卷，不丢弃本地修改；受跟踪文件有修改或本地 main 与远端不一致时明确停止，回传失败行。

```bash
bash <<'SH'
set -euo pipefail
if ! git diff --quiet || ! git diff --cached --quiet; then
  printf 'checkout=failed reason=LOCAL_TRACKED_CHANGES_PRESERVED\n'
  exit 1
fi
git fetch origin main
git switch main
git merge --ff-only origin/main
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
  printf 'checkout=failed reason=LOCAL_MAIN_DIFFERS_FROM_ORIGIN\n'
  exit 1
fi
printf 'checkout=updated root=%s commit=%s\n' "$(pwd)" "$(git rev-parse --short HEAD)"
SH
```

`git pull` 只更新文件，不会重建 API，也不会重新加载 VoiceChat Python 进程。后续重新创建 VoiceChat 时，第 2.1 节的六个绝对路径挂载统一指向这个目录。即便磁盘文件 hash 已匹配，也必须完成进程重载；不能以在旧容器里复制文件代替部署。

### 2.1 配套更新 API、Web 与 VoiceChat

下面命令从同一目录构建并重新创建 API 与 Web，再用原 VoiceChat 的已验证镜像 ID、模型目录、GPU/端口参数及六个只读挂载重新创建 VoiceChat。本次 model/data/audio_server 必须配套加载；Web 页面阶段、输入关联和文件播放完成状态有实际行为修改，必须重建 Web 后强制刷新浏览器。运行库、权重、工具目录和识别门槛不变；CueKB 的数据库/审核升级由其项目负责。

```bash
bash <<'SH'
set -uo pipefail
umask 077
RELEASE_LOG=$(mktemp /tmp/voice-consumption-release-XXXXXX.log)
if (
  set -e
  VOICECHAT_SNAPSHOT_ROOT=$(cd docs/voicechat && pwd)
  VOICECHAT_RELEASE_IMAGE=$(docker inspect --format '{{.Image}}' nemotron-labs-voicechat)
  VOICECHAT_MODEL_ROOT=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data/models"}}{{.Source}}{{end}}{{end}}' nemotron-labs-voicechat)
  test -n "$VOICECHAT_MODEL_ROOT" && test -d "$VOICECHAT_MODEL_ROOT"
  docker image inspect "$VOICECHAT_RELEASE_IMAGE" >/dev/null
  API_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-api:/ {p;q;}')
  test -n "$API_IMAGE"
  WEB_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-web:/ {p;q;}')
  test -n "$WEB_IMAGE"
  docker build -f deploy/Dockerfile.api -t "$API_IMAGE" .
  docker build -f deploy/Dockerfile.web -t "$WEB_IMAGE" .
  docker compose --env-file .env -f deploy/compose.production.yaml up -d --no-deps --force-recreate --wait --wait-timeout 180 api
  docker compose --env-file .env -f deploy/compose.production.yaml exec -T api python /app/scripts/configure_native_cuekb.py --apply
  docker compose --env-file .env -f deploy/compose.production.yaml up -d --no-deps --force-recreate --wait --wait-timeout 180 web
  docker stop nemotron-labs-voicechat
  docker rm nemotron-labs-voicechat
# Paired model/data/template runtime; recreate the container to load all three.
docker run -d --name=nemotron-labs-voicechat \
  --runtime=nvidia \
  --gpus '"device=0,1"' \
  --shm-size=16GB \
  -e USE_JINJA_TEMPLATE_PROMPT=1 \
  -e S2S_INFERENCE_CLOSE_ON_PAD_TAIL=true \
  -e S2S_INFERENCE_FORCE_SPEECH_SILENCE_ON_PAD=true \
  -e S2S_TTS_PAD_TAIL_RATIO=3 \
  -e RNNT_MAX_AGENT_RESPONSE_SEC=0 \
  -e VOICECHAT_DIAGNOSTICS=1 \
  -e NIM_HTTP_API_PORT=9000 \
  -e TTS_CUDA_VISIBLE_DEVICES=1 \
  -e LLM_GPU_MEM_UTIL=0.65 \
  -p 9000:9000 \
  -v "${VOICECHAT_MODEL_ROOT:?Set VOICECHAT_MODEL_ROOT}:/data/models" \
  --mount "type=bind,source=${VOICECHAT_SNAPSHOT_ROOT:?Set VOICECHAT_SNAPSHOT_ROOT}/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py,target=/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/s2s/audio_server.py,target=/s2s/audio_server.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/s2s/prompt_template.jinja,target=/s2s/prompt_template.jinja,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/data_types.py,target=/opt/tritonserver/backends/nemotron-voicechat/data_types.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/model.py,target=/opt/tritonserver/backends/nemotron-voicechat/model.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py,target=/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py,readonly" \
  --entrypoint /s2s/run_s2s_server.sh \
  "$VOICECHAT_RELEASE_IMAGE"
) >"$RELEASE_LOG" 2>&1; then
  printf 'release=created commit=%s build_log=local_only\n' "$(git rev-parse --short HEAD)"
else
  printf 'release=failed local_build_log=%s\n' "$RELEASE_LOG"
  exit 1
fi
SH
```

成功输出 release=created 只说明完成创建。若失败，停止并回传失败行，原始构建日志留在本机；不要继续测试、猜测镜像、修改绑定文件或反复重启。已有离线镜像必须在删除旧容器之前记录，本命令不会安装新的 GPU 依赖。

### 2.2 等待就绪并核对版本

VoiceChat 重载模型可能需要几分钟。创建完成后等待 **5 分钟**，然后在同一终端/目录执行：

```bash
git rev-parse --short HEAD
bash scripts/validate_deployment.sh check
```

正常得到 checkout 的目录/提交/分支、preparation=passed 和 API/VoiceChat 的 runtime 身份行。入口会核对应用、采集器、query schema、目录 YAML、原始 WAV、VoiceChat 六个运行文件及既有诊断配置，并检查 VoiceChat 为真实 Triton/model ready（拒绝 loopback）。哈希失败时额外标注 version_source expected=host_checkout actual=container_disk，并输出容器 image/start 时间。出现 version=failed / collection=failed 就停止并回传；不自行复制文件、降低检查或再次重建。文件匹配不证明已运行的 Python 加载了新代码，也不证明 GPU 提速、模型首问选工具正确或 CueKB 内容已通过。

浏览器强制刷新门户，打开 F12 → Console，开启 **Preserve log**，过滤 voice_file，清空旧 Console 内容。保持门户前台、声音可播放，不锁屏或切后台。固定使用仓库 artifacts/native-cuekb/golden_yolk.wav 与 artifacts/native-cuekb/black_pig_crisp_heart.wav，选择 **Audio file → Choose WAV**。Python 和采集在容器内执行，宿主机无需 Python，不手工导出 HAR 或运行 docker logs。

## 3. 通话 A：Golden Yolk 独立首问

**假设：**独立首问未触发可能发生在语音识别、分轮或原生工具选择阶段，不能先判为 CueKB 慢。预期取得完整两段问题及该次实际模型/工具事件；是否出现 Checking knowledge 只作观察。

A、B 均须先得到 `preparation=passed` **和** `web_capture=started`，再点击门户 Start file call。任何 version/collection 失败都停止，不播放录音、不继续 B，也不执行第 5 节采集；web-start 版本检查失败时没有创建窗口，随后 web-end 只会返回 web_capture_not_started。

1. 确认已 End call，宿主机开始一个新的采集窗口：

   ```bash
   bash scripts/validate_deployment.sh web-start golden_yolk
   ```

2. 门户 Choose WAV 选择 `artifacts/native-cuekb/golden_yolk.wav`，点击一次 **Start file call**。本次通话不要先发 Hello、Black Pig、文字或其他问题。
3. 完整发送原文件，保留中间停顿。预期原问题为：

   > What about the Monkey Golden Yolk mooncake? Can it be shipped, how long is its shelf life, and how should it be stored after opening?

4. 不在第一小句、文件中间停顿或 ACK 后结束。WAV 完整发送后，等回答停止 **Speaking**，显示 **Reply complete** 或 **Answer not verified**，再观察 **5 秒**。观察期间若再次进入 Checking knowledge / Speaking，继续等待其结束后重新观察；不要点 Stop playback / Cancel search。
5. 从 Start file call 起最多等 **120 秒**；连接提前结束、报错或录音未发完就保留现象，不重试。既有 **105 秒连接上限及固定 30 秒活动宽限**继续生效，120 秒只是本次人工等待上限，不延长服务端寿命。
6. 按第 5 节完成 End call 和采集；本次观察标记为 `case=A_golden_first`。保存本次全部用户转写、回答与来源面板；没有回答就写“无”，不只保留最后一句。

## 4. 通话 B：Black Pig 后同连接发送 Golden Yolk

**假设：**前一轮对话会改变模型历史和轮次状态；对照 A/B 可定位上下文相关性，但不能单凭 B 成功证明 CueKB 冷启动。预期两次 file.start 属于同一个 conversation/session/epoch，且各次 query 能对应正确产品。

1. A 已结束并完成采集后，再开启新的窗口。窗口标签使用第一个文件名，实际采集包含两份输入：

   ```bash
   bash scripts/validate_deployment.sh web-start black_pig_crisp_heart
   ```

2. 门户 Choose WAV 选择 `artifacts/native-cuekb/black_pig_crisp_heart.wav`，点击一次 **Start file call**。新通话会清空上一通话的模型历史。原问题为：

   > What does the product documentation say about the price, weight, pastry layers, and best consumption time for the Monkey Black Pig Crisp Heart fresh pork mooncake?

3. Black Pig 文件发送完、Choose WAV 恢复可用后，可以先选好 `golden_yolk.wav`；**选择文件本身不会发送，不点击 Send recording**。等待 Black Pig 回答停止 Speaking 并出现终态，再观察 5 秒；如有新活动继续等，不按内容正误决定何时结束。
4. 通话仍连接时点击 **Send recording** 一次，发送已选的 Golden Yolk。**不点 End call / Start file call，不刷新页面**；发送后按 A 相同规则等待 Golden 最新回答结束和 5 秒观察。B 的两次 Start/Send 操作顺序只做一次，不添加其他问题。
5. B 从第一次 Start file call 起总计最多 **120 秒**，发送第二份文件不重置人工计时或连接寿命。Black Pig 未结束、连接已到期或第二次发送按钮不可用时，不强行发送、不中途新建连接代替；到上限就结束并记录 Golden 未发送/未发完/未答完。本轮如实标为对照证据不足，不因失败反复重跑或调大限制。
6. 按第 5 节结束并采集，标记 `case=B_black_then_golden`。End call 后、下一次 Start file call 前，保留 B 中两份输入及**所有**回答，分别查看 Black Pig、Golden 的来源面板；不要仅复制最后一条 Golden 答案。尤其保留“没找到重量”和“65 克”是否在同一通话出现的信息。

## 5. 每次通话的统一结束与采集

**A、B 各执行一次：**先在门户点击 **End call**，随后在宿主机执行下面整段。不要在 A/B 之间留下未关闭的采集窗口。

即使 A 提前报错，也必须先执行本节采集，再开始 B。如果 web-start 显示 `web_capture_already_started`，说明上个窗口尚未结束；先执行本节保存现有窗口，不删除状态、不覆盖窗口。当前窗口可能包含两次通话，采集器会按会话分别输出；这个采集保护错误不代表 Black Pig 的模型或 CueKB 调用失败。

该命令先调用现有 web-end，再自动使用本窗口的文件和会话输出阶段时间线与分轮元数据。只读已有记录，不发送录音、不发起模型或 CueKB 请求；原始日志仍留在本机。采集失败也保留本次输出，不读取上一次结果。

```bash
bash <<'SH'
set -uo pipefail
umask 077
END_SUMMARY=$(mktemp /tmp/voice-current-end-XXXXXX.txt)
bash scripts/validate_deployment.sh web-end >"$END_SUMMARY" 2>&1
END_STATUS=$?
cat "$END_SUMMARY"
ARTIFACT_DIR=$(sed -n 's/^local_artifacts=\(.*\) raw_logs=not_printed$/\1/p' "$END_SUMMARY")
CASE_IDS=$(sed -n 's/^session={"conversation_id":"\([a-f0-9-]\{36\}\)".*/\1/p' "$END_SUMMARY" | sort -u)
if [[ -z "$ARTIFACT_DIR" || -z "$CASE_IDS" || ! -f "$ARTIFACT_DIR/api.log" || ! -f "$ARTIFACT_DIR/voicechat.log" ]]; then
  printf 'timeline=unavailable reason=NO_CURRENT_WINDOW_OR_SESSION\n'
  exit 1
fi
API_CONTAINER=$(docker compose --env-file .env -f deploy/compose.production.yaml ps -q api 2>"${END_SUMMARY}.setup")
if [[ -z "$API_CONTAINER" || "$API_CONTAINER" == *$'\n'* ]]; then
  printf 'timeline=unavailable reason=API_CONTAINER\n'
  exit 1
fi
TIMELINE_STATUS=0
while IFS= read -r CASE_ID; do
  cat "$ARTIFACT_DIR/api.log" "$ARTIFACT_DIR/voicechat.log" | docker exec -i "$API_CONTAINER" python -c '
import json, re, sys
from scripts.summarize_voice_timeline import summarize
limit = 8 * 1024 * 1024
text = sys.stdin.read(limit + 1)
if len(text.encode()) > limit:
    print("timeline=unavailable reason=LOG_WINDOW_TOO_LARGE")
    raise SystemExit(1)
try:
    header, rows = summarize(text.splitlines(), sys.argv[1])
except ValueError:
    print("timeline=unavailable reason=TOO_MANY_EVENTS")
    raise SystemExit(1)
print("timeline=" + json.dumps(header, separators=(",", ":")))
for row in rows:
    print("stage=" + json.dumps(row, separators=(",", ":")))
sequences = set(header["sequence_ids"])
wanted = {"input.bou", "agent.bos", "function.start", "function.valid", "function.invalid",
          "function.extract.stop", "function.cancel", "function.inject", "sequence.end"}
allowed = ("event", "sequence_id", "at_ms", "step", "source", "state", "phase", "reason",
           "silent_frames", "effective_bou_min", "dropped_events", "tokens", "completed",
           "preparation_ms", "append_wait_ms")
boundaries = []
for line in text.splitlines():
    if "voice_model_diagnostic " not in line:
        continue
    try:
        event = json.loads(line.split("voice_model_diagnostic ", 1)[1])
    except ValueError:
        continue
    if str(event.get("sequence_id")) not in sequences or event.get("event") not in wanted:
        continue
    stamp = re.match(r"^(\d{4}-\d\d-\d\dT\S+Z)\s", line)
    boundaries.append({"at": stamp[1] if stamp else "unknown",
                       **{key: event[key] for key in allowed if key in event}})
boundaries.sort(key=lambda row: row["at"])
for row in boundaries[:40]:
    print("model_boundary=" + json.dumps(row, separators=(",", ":")))
print("model_boundaries=" + json.dumps({"conversation_id": sys.argv[1], "total": len(boundaries),
    "omitted": max(0, len(boundaries) - 40), "basis": "docker_log_not_speaker"}, separators=(",", ":")))
' "$CASE_ID" || TIMELINE_STATUS=1
done <<< "$CASE_IDS"
if [[ "$END_STATUS" != 0 || "$TIMELINE_STATUS" != 0 ]]; then exit 1; fi
SH
```

## 6. 每个窗口回传什么

回传本次准备检查、web-start 和第 5 节的**全部终端输出**，它们已按窗口/会话限制范围；另附 Console 当前过滤出的 `voice_file` 行、门户的完整用户转写/回答与来源面板。原始日志、录音、令牌、HAR 和 report.json 不用贴。Console 没有对应行或来源面板不可用就写“无”，不要从旧通话补齐。

A、B 各填一次下面的观察记录；时间只记大约秒数，不需要人工计算阶段耗时或检查事实正确性：

```text
case=A_golden_first / B_black_then_golden
checking_knowledge=A 的 Golden，或 B 的 Black Pig / Golden 各写出现或未出现
heard_audio=有声音 / 没声音
text_during_speech=逐步出现 / 结束才出现 / 无文字
second_file=B 写已发送 / 未发送 / 未发完，A 写不适用
end_reason=正常完成 / 120秒等待上限 / 页面或连接提前结束
page_error=页面提示原文，没有则写无
portal_answers=按显示顺序保留全部答案；每段标注 A、B-Black 或 B-Golden
```

服务端摘要包含 `session`、`input_timing`、`backend_injection`、`backend_stages`、`query`、`record`、`turn`、`voice_evidence`、`evidence_delivery`、`answer_check`、`admitted_evidence`；新增只读输出为 `timeline` / `stage` / `model_boundary`。采集器会同时标注截断和 omitted。`admitted_evidence` 只展示窗口中最新有证据答案的有界样本，B 中通常是 Golden，**不能拿它证明先前 Black Pig 的来源**，因此 B 的两种产品来源面板都要保留。

## 7. 开发者如何判断（操作者不执行选择）

### 7.1 时间花在哪里

按 `conversation_id → sequence_id → input_item_id → call_id/response_id` 对齐 A/B。两个问题、两个 input、两个调用和两次注入不是同一个计数；Gold WAV 被分轮后还须核对 query 是否带回完整产品名与全部属性。

| 阶段 | 证据与判断 |
| --- | --- |
| 是否生成工具调用 | `session_match`、`backend_prompts`、`model_function_count` 和 `model.function`，再对照 `voice_tool_call_received`。0 与无法关联的 null 分开；Checking knowledge 未显示不等于调用数为 0 |
| 实际检索 | `voice_tool_call_received`、`cuekb_response_validated.duration_ms` 与实际 trace。工具耗时只覆盖其计时范围，不等于 VoiceChat 已消费结果 |
| 应用交付 | `cuekb_response_validated` → `voice_tool_result_submitted`；若这一段长，核查应用 DB/锁、权限复核与发送，而非一概归为模型慢 |
| VoiceChat 排队 | `voice_tool_result_submitted` → `voicechat.function.batch_result` / `voicechat.function.result` → `backend.result_received` |
| 剩余 ACK 与调度 | `backend.result_received` → `backend.inject_started`；结合 `backend.buffered_ack`，不能将这整段都称为 ACK 播放 |
| 结果逐 token 注入 | `backend.inject_started` → `backend.inject_done`，结合 `backend_injection` 的 token 范围。代码确认逐 token 模型步骤，实测后才可说本轮是否仍主要耗在这里 |
| 第二次调用间隔 | 同 input 的第一次 `voice_tool_result_submitted` → 第二次 `voice_tool_call_received`，进一步拆 ACK、注入、后续工具生成；结合所有正式音频/完成事件检查中间是否已有一段回答 |
| 真实播放等待 | 服务端 `voice_audio_first_sent` 与浏览器 `voice_file.audio.first` / `playback.started` 分开；Docker 时间不是扬声器听音时间，跨主机时钟不直接相减 |

`input_timing.answer_audio` 选择最后结果之后的首个完成回答，不保证包含之前曾出现的正式回答；不能单凭该指标排除“先答后查”。完整 `stage` 中的各次 response 才用于核对这条路径。`backend_injection` 的 token 值是 min/max 范围，不能假设每次注入长度相同。若有 truncated、omitted、缺边界或无法关联，只报告已测区间，不推算缺失阶段。

`backend.inject_done` 来自 finally，只代表线程退出。必须同时见 `backend.returned_idle` 和 done 的 `state=idle` 才算完成。新增 `backend.inject_failed` 输出安全异常类型及剩余 token 数，backend_injection 的 `failed` 计数和 `status=failed` 标识明确失败；不打印异常正文、工具正文或凭据。失败的 39 ms 不能算作消费数百 token 的提速数据。PCM 发送现在按样本时钟进行，服务端 ACK done 仍不能代替浏览器实际听音。

### 7.2 首问触发与内容

| 检查项 | 判据 |
| --- | --- |
| Golden 首问 | A 中完整两段实际输入可见、工具目录正确、模型发出真实知识调用；若未触发，使用 `model_boundary` 的 input.bou / agent.bos source 与 query 定位，不宣称 CueKB 没数据 |
| 同连接对照 | B 中两个 file.start 的 conversation/session/epoch 一致，顺序为 Black → Golden；Golden 未发送、未发完或到期时结论是对照不足。A 失败/B 成功只支持上下文或轮次相关，不直接证明冷启动或唯一根因 |
| 问题级检索 | 每个稳定输入恰好一次 `/v2/evidence/query` 和一次 `/v2/evidence/validate`，无自动 HTTP 重试；完整 ASR/facets 保留，内部补召回不产生第二个模型工具查询 |
| Black Pig 内容 | 价格、重量、层数、最佳食用时间逐项作答，缺证据的项明确合并说明；不以肉源或好评介绍代替所问属性。65 g、比例、冷藏/密封期限必须有实际来源与适用条件 |
| 比例与期限 | 若实际原文为 3:7，不得说成 3.7%；肌内脂肪百分比与肥瘦比例分别核对。“最佳食用”不能改成硬保质期，储存条件不能跨产品借用 |
| Golden 内容 | 运输、密封保质期、开封后储存分别回答或明确无法确认；若来源写 15 天最佳食用，保留“最佳”限定，不只罗列 45/15/3 三个数字 |
| 回答数量 | 按实际输入及 response 区分 ACK、停顿分轮与同输入重复正式回答；没有新输入时的额外回答须记录，不能把不同输入的答案直接算为重复 |
| 验证边界 | Reply complete / answered / collection=complete 均不等于事实、听音或时延验收。当前没有据此设定新的秒数达标线，本轮先定位每段耗时 |

收到这两次窗口后先分析，不自动追加复测。相同方案连续两次无新增证据就停止；若三种假设仍无进展，重新评估设计、环境和技术路径，不放宽查询、来源或时限标准。

## 8. 已收到的现场结果与下一步（2026-10-25）

### 8.1 本次证据范围

用户提供 A 的独立 Golden 门户结果、B 的 Black → Golden 门户结果，以及一次包含两个会话的 web-end 摘要与完整 stage 输出。窗口标签是 golden_yolk，实际覆盖 UTC 13:23:35–13:32:24（北京时间 **21:23:35–21:32:24**），不是只有 Golden 的记录。按 CID/sequence 可区分 A/B，无需仅因共用窗口重测；窗口长度也不能当作单条连接时长。

- A：CID `812e2a6b-3888-48cc-b19e-f88f171d68e7`，sequence `2131184838`。
- B：CID `105fab11-b2fa-4934-9f0f-9e0a550d3a1b`，sequence `1311217920`。
- 两个后端 prompt 均为 8615 字符，SHA-256 均为 `33e653e03db6f172e87cba6a00a86b130cfbe7709988d184c52505d5afeb41d6`，工具名均含 search_knowledge_base。这证明观测到的提示词输入一致，不证明模型遵守了指令。
- 本次附件没有准备检查/runtime 身份行、voice_file 浏览器发送/播放记录；两会话的 model_boundaries.total 均为 0。可判断阶段耗时与真实调用，不能判定每个 BOS 来自 model_native 还是 forced_eou，也不能证明浏览器完整发送或实际扬声器播放时间。不能把边界元数据缺失解释为没有发生分轮。

### 8.2 时延已定位到结果消费

下表按 stage 的调用顺序关联。毫秒检索值来自 cuekb_response_validated；秒数按同窗口 Docker 时间戳相减。结果消费不是全部 GPU 算时，也包括逐步传输与调度。

| 调用 | 所属输入及实际 query | CueKB | API 收到调用→提交结果 | 后端收到结果→注入开始 | 结果注入 |
| --- | --- | --- | --- | --- | --- |
| 1 | Black 同一输入初查：Monkey Black Pig Crisp Heart Fresh Pork Moon Cake | 89 ms | 0.180 s | 1.670 s | **21.905 s** |
| 2 | Black 同一输入补查：price, weight, storage requirements, and shelf life of Monkey Fresh Pork Mooncake | 78 ms | 0.177 s | 1.683 s | **12.590 s** |
| 3 | Golden 第一段：Monkey Golden Yolk Moon Cake | 78 ms | 0.177 s | 1.689 s | **14.521 s** |
| 4 | Golden 第二段：shelfl life of Monkey Golden Yolk Moon Cake | 79 ms | 0.177 s | 1.695 s | **15.415 s** |

四次结果提交到后端接收仅约 0.033–0.051 秒；四次均收到、注入完成、回 idle，无取消或 decoder limit，结果规模为 362–634 tokens 的 min/max 范围。不能把这个范围当作每次精确 token 数。

Black 两次检索合计 **0.167 秒**，两次注入合计 **34.495 秒**。转写完成→首个正式回答音频 **40.798 秒**，注入占约 **84.6%**。第一次工具结果提交→第二次工具调用 **25.717 秒**，可拆为：传递到后端 0.033 秒、ACK/调度 1.670 秒、注入 21.905 秒、注入后到下一次 API 调用 2.109 秒。本次 Black 在第二次结果之后才出现唯一正式回答，中间两段均为 status ACK，**不支持本次“先正式回答再补查”**。

B 中 Golden 两次合计检索 0.157 秒、注入 29.936 秒，但它们属于两个 input_item_id，各只执行一次，不能称为同输入的第二次补查。原先“每次必定两次检索”的解释须按输入区分。

B 四次检索累计 **0.324 秒**，四次注入累计 **64.431 秒**。这个样本应先优化结果消费与无关查询/负载，不能把本次主要瓶颈归给 CueKB 检索或 API 交付。

### 8.3 Golden：独立首问确实未调用；上下文测试仍有输入缺失

A 中实际出现两个输入：第一段为产品名问题，第二段包含运输、保质期、开封储存。它们分别收到普通回答；model_function_count=0、api_tool_calls=0、CueKB responses=0、queries=0。不是查不到，也不是仅未显示 ACK。第二段以 I do not have access… 开头并给出一般月饼储存建议，违反当前产品问题必须检索、不得以一般知识替代的会话指令。由于 A 第二段实际文字完整，不能只用 ASR 缺字解释全部漏触发；需定位 VoiceChat 原生工具选择和上下文/分轮。

B 第一段 Golden 已成功检索并完成注入，随后该 Turn 因 request_revised 变为 superseded。第二段输入记录仅为 **? How long is its shelf life**，query 也只搜 shelf life；运输和开封储存未保留为本次请求属性。首段从北京时间 21:31:04.676 开始注入，到 21:31:19.197 完成；第二段 voice_input_started 已在 21:31:03.711 出现，说明该段输入与注入区间交叠。model.py 的 infer 在抽取/注入期间于保存音频之前 continue，确定存在跳过输入的代码路径；结合此交叠和残缺转写，属于需优先修复的全双工缺陷。没有 voice_file 和模型边界记录时，尚不能认定具体缺词全部由此造成，或反推哪个音频帧被跳过。

本次 api_diagnostic=TOOL_ANSWER_NOT_COMPLETED 是有工具的三个输入中仅两个完成正式回答，首段 Golden 被修订取代；不是 CueKB 超时或工具结果未回来。Superseded 本身是应用的修订结清结果，不能用重试检索消除。

### 8.4 查询、来源与回答检查

- **Black query 发生属性漂移：**转写含价格、重量、层数、最佳食用时间；首查只有产品名，补查改成价格、重量、储存要求和保质期，并把精确 Black Pig 名称缩成 Monkey Fresh Pork Mooncake。实际 query 未保留全部所问属性，且补查属性组合与模板/目录示例接近；示例偏置是待验证原因，属性漂移本身已可确认。不能把每次补查都视为正确补齐缺失项。
- **来源归属未建立：**四个 V2 响应及四次语音选择均为 resolved=0、unknown=5；没有发生 API 预算或非 ASCII 省略，不能把来源缺失归因于本次预算。最终 Golden 的五块样本含产品名、标题、评论及无归属的保质期条目，未附加 required 邻块；required_context_chunks=0 本身不等于错误，也不能证明产品归属已完整。V2 接口/RRF 返回正常不等于产品归属数据准备完成；章节归属与发布需要 CueKB 侧处理，不能在应用中按 query 猜成目标产品。Black 的逐块来源未在附件展示，不能据此证明其三天/四十五天已串用。
- **Golden 的期限回答在本次与 C4 文本相符：**45 天密封、15 天最佳食用、开封冷藏三天，且这次保留了 best consumed。该项较之前用户文本更完整，但 C4 applicability 仍 unknown；运输仍未回答，而请求/query 中运输已缺失，不能单独归罪于回答合成漏答。
- **Black 的未验证原因已明确：**VOICE_MISSING_CONDITION 命中 C5 中 ordinary white pigs mostly have **only** 1% to 1.8% 的对比说明，答复没有复述 only。此为词法 source_signal 检查，不是对 3:7、储存产品归属或用户全部属性的实际认证；该条存在无关对比触发误报的问题，应改善条件关联，不能直接改为一律通过。
- Black 本次只有一段正式回答，仍未答价格或明确说价格未确认；three point seven percent fat-to-lean ratio 仍需用本次原文核对，英文数字未被有限词法检查充分覆盖。Answer not verified 与事实问题并非同一层；Golden 的 Reply complete 也没有证明运输/适用产品已验证。

### 8.5 下一步顺序与复测边界

不再重复未修改版本的 A/B。当前证据足以进入针对性修改，按以下依赖顺序处理：

1. VoiceChat 先保证抽取/注入期间真实音频不被跳过，审查快路径 decoder/audio 步数与用户新输入的处理；不能仅把跳过的音频拼到已用零音频推进的缓存后面就宣称正确。保留原生选工具与已有提交边界，不新增语义分类器、强制工具选择或额外答案模型。
2. 对结果注入做保持同一 sequence/cache/function 通道的性能方案验证；批量消费需要实际 vLLM 能力与 GPU 证据。同步减少与所问属性无关的来源和错误补查，但不截掉事实归属、必要条件或按一次查询强行掩盖漏答。
3. 修正原生 query 对完整请求及精确产品名的保留，检查模板示例偏置和独立首问的工具策略遵循。A 的零调用问题不能靠 CueKB 重试、提高查询预算或已有 Black 历史掩盖。
4. CueKB 处理当前 unknown 产品归属与必要章节关系；应用侧改善来源条件与回答实际使用事实的关联检查，分别核对价格/比例/储存条件，不只追求页面变绿。

涉及 VoiceChat 的修改仍须完整文件同步、manifest/启动身份核对及受影响回归；第 9 节说明本次已落地的修改与待测边界，更新后按第 2–7 节复测同样 A/B；浏览器发送记录和模型边界的采集缺口也应在下一轮前补齐。不改变原 WAV、105 秒上限、固定 30 秒宽限或查询预算。每种方案两次无新增证据即停止，三种假设无进展则重新评估设计与环境。

### 8.6 文档检查与历史方法

此前文档检查已验证本地链接、围栏、4 个 Bash 命令块及内嵌 Python 语法，并用合成日志验证时间线排序、会话隔离与异常分支。这些检查不替代本节真实样本，也不证明模型细分边界在实际日志中可用。当前新增耗时根据用户提供的 stage 数据计算，未自行重跑生产请求；用户附件不作为仓库产物提交。

开发者补充入口仅在明确需要排除 Web 文件发送差异时使用：`bash scripts/validate_deployment.sh auto core`。它独立发送 Golden、Black，并在 Golden 后自动追加 Hello，不包含 Black → Golden 产品对照；本轮操作者不要额外运行。其他旧轮次的方法、部署操作和历史测试记录统一保存在[历史验证方法归档](archive/2026-10-25/validation-before-current-issues.md)，不作为本轮前置操作。


## 9. 本次编码范围与待联调项

用户已将内容侧问题交给 CueKB，本次按其新版已支持、待真实联调处理，不在本仓库重做其内容或归属算法。第 8 节 unknown/数值/条件等仍是修复前样本，不能标成新 CueKB 结果。

本次 VoiceChat 改为：真实音频消费游标独立于 decoder 工具步数；抽取/注入期间保留输入和待处理结果；每次请求最多消费四个积压窗口，保留原两帧语义边界及输出顺序。工具线程异常明确失败，部分未知写入不重放。模型函数结果预先计算 embedding 并一次复制到 CPU，单协程顺序 append/anext；跳过未使用的正文 logits 采样、每 token 的重复传递和跨线程等待。**仍然每 token 一次引擎步骤，未使用未经确认的多 token append；不宣称已消除全部串行模型耗时。**

本项目同步加强独立首问产品事实必须检索、精确产品名及首查完整属性；query schema/目录/模板示例配套，格式示例不替换实际问题属性。服务端不按转写分类或偷偷重写模型 query；提示约束的真实遵循仍需 Golden 独立 A 验证。

下一轮额外查看 backend.audio_buffered / backend.audio_resumed 的 received_samples、processed_frames，以及 backend.inject_profile 的 tokens、completed、preparation_ms、append_wait_ms。后者是模型 append/anext 与协程执行区间，不当作纯 GPU 算时。即便模型细分边界日志仍缺失，这些计数阶段也会作为常规后端日志采集。backend_stages 的有界行数提高到 32，完整 stage 仍标注截断。

本次本地检查：API 全量 682 passed；新增健康检查及时间线/完成边界收尾专项 71 passed；VoiceChat 独立 CPU 44 passed，含 float32/bfloat16 通道递推、逐样本保留与两帧 EOS/BOS 报文边界；契约生成、Ruff、12 个快照 hash/完整文件编译、补丁逆向和启动命令语法通过。本环境没有 GPU、真实 CueKB 新版或实际听音；未部署，不把 CPU 测试或提示文本当作真实原生工具质量/时延验收。更新后只执行同样 A/B 一轮，保留原始停顿和 105 秒/固定 30 秒宽限。


开发回归仅由开发者执行，API 与 VoiceChat CPU 环境分开，CPU 依赖不装入 GPU 容器：

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check apps/api tests scripts docs/voicechat/s2s/tests/test_tool_consumption.py
PYTHONPATH=apps/api .venv/bin/python scripts/export_contracts.py
VOICECHAT_CPU_ENV=../voicechat-cpu-env
python3.12 -m venv "$VOICECHAT_CPU_ENV"
"$VOICECHAT_CPU_ENV/bin/python" -m pip install --disable-pip-version-check -r docs/voicechat/s2s/tests/requirements.txt
"$VOICECHAT_CPU_ENV/bin/python" -m pytest -q docs/voicechat/s2s/tests
```

## 10. 最新现场：ACK 中断与本次修复（2026-10-28）

### 10.1 已知结果

现场准备检查均通过，两个会话收到相同 9893 字符提示词、同一 SHA-256 `94a98aef2ce00991dd4009ec741a805a41e8fb40d7c7717d20a936e2ac9e0bc6`，工具注册为 search_knowledge_base。准备通过只证明健康与文件身份，不代表推理逻辑正确。采集窗口 UTC 14:09:43–14:13:47 同时覆盖下面两个会话；第二个 web-start 因旧窗口未结束而拒绝，不抹掉窗口内已经发生的第二通话。

| 会话 | 原生工具与 CueKB | 注入与最终结果 |
| --- | --- | --- |
| Golden 独立，CID b660297b-a251-41f9-b598-b5286cc6fe4b / sequence 2608292347 | 产品首句未查并开始一般回答；运输/保质期/开封储存第二段调用一次，query 错写为 Monkey Golden Yeast Moon Cake 且丢失属性。CueKB 129 ms，5 条 resolved 证据，状态 degraded，reranker 未配置/执行 | 691 tokens；UTC 14:10:02.276204 开始，14:10:02.315103 finally 退出，约 38.9 ms；未回 idle、无正式回答。附件 1 显示 Audio playback fell behind；连接关闭后 Turn canceled |
| Black → Golden，CID 6dac64a0-34b5-4fc6-ab97-a1269361be37 / sequence 4021977475 | Black 完整问题未查，模型回答无文档访问；随后 Golden 产品句调用一次，query Monkey Golden Yolk Moon Cake。CueKB 208 ms，5 条 resolved 证据，状态 degraded，reranker 未配置/执行 | 531 tokens；UTC 14:12:58.176658 开始，14:12:58.214924 finally 退出，约 38.3 ms；未回 idle、无正式回答。附件 2 显示 VOICE_UNAVAILABLE，Golden 后半段转写中断；连接关闭后 Turn canceled |

两次调用都是不同会话的一次调用，不能算同输入检索两次。CueKB 服务内部总耗时分别约 122/197 ms，应用工具接收到提交结果约 236/313 ms；此次检索成功返回候选，不再出现 409。resolved=5 表示上游归属字段已给出，不认证模型错拼名称仍找对产品；degraded 和 reranker 未配置本身不等于调用失败。没有完整口述答案，本轮无法验收证据充分性、事实准确率或成功消费的 GPU 耗时。

### 10.2 复现、修改与边界

假设一：工具注入新协程违反 PyTorch 线程局部模式。方法：在 inference_mode 下创建与 execute() 相同的历史 tensor，使用真实后台事件循环线程提交注入协程。修复前第一步引擎返回后写历史抛 RuntimeError，decoder 步数未提交、线程退出仍处于 process_response；同线程 asyncio.run 夹具不会复现。修复在 await **之后**的历史写入周围开启窄 inference_mode，不跨 await 持有模式，避免影响同事件循环其他任务。成功与第二次 append 失败分别验证；部分失败仍明确结束且不重放。本地复现确认此缺陷，现场摘要未保留异常类型，不能声称已从生产原始堆栈证实同一个异常。

假设二：保留真实音频后的补处理使 ACK 输出超过实时播放速度。方法：将超过两秒的补处理 ACK 和相邻回答一次入队，经实际 sender 和受控时钟发送，核对字节/ID/顺序和播放领先量。原 sender 不按样本时钟限速，可能冲破浏览器原有一秒 ring；现将等待放在独立的每会话有序 writer，按实际 PCM24k 字节数控制发送，空闲或网络阻塞不积累突发额度。输入接收、模型调度继续工作，队列上限和浏览器保护保持原值。不以删音频、加快播放或扩大缓冲掩盖。现场没有浏览器队列明细，不能把所有中断归到这一条；另一张截图的模型异常可由假设一解释。

首问未查、Golden 错拼和漏掉全部查询属性仍有真实失败证据。此前已加强提示，连续同一路径未改善，本轮停止继续堆提示词，不添加转写分类/强制工具选择；先恢复工具结果到完整回答的链路，再评估原生模型遵循能力和设计路径。Golden 独立两段的第二段在本次完整转写，另一会话第二段在断连时中断，不能都归因于输入丢失。

按第 2 节部署后只复测相同 A/B 一轮：完整 ACK、注入回 idle、正式回答及同连接下一段均需观察；任何异常保留输出并结束当前窗口再开下一个。未实际进行 GPU、浏览器听音或 CueKB 联调的本地测试不作为放行证据。

本轮开发检查：VoiceChat 独立 CPU 全量 **48 passed**；本项目受影响的时间线/采集器/原生工具验证/完整 backend 补丁回归 **130 passed**；Ruff、12 个快照 SHA-256、完整 Python 编译、4 个外部修改文件同步核对通过。上述样本节奏测试使用受控时钟和 PCM，不是浏览器现场听音。本环境未部署、无 GPU，也未发送新的真实 CueKB 请求。

## 11. aba0b53 回溯与 CueKB 最新接口核对（2026-10-28）

### 11.1 aba0b53 是否引入 ACK 中断

结论：**不支持把 aba0b53 的严格字段补齐认定为中断根因。** 单提交不修改 VoiceChat backend/audio_server、API relay、浏览器 Worklet、CueKB Adapter 或证据投影方法。它新增可解析字段、默认 ranked 选择参数和测试/生成契约；不改变 GPU、音频输出节奏或工具注入算法。完整升级到该提交时也会带入更早的 fac1b7a，不能把部署后的现象直接归因到最后一条提交。

方法一：把 986f442（该提交父版本）与 aba0b53 的 API 源码分别解包到临时目录，在同一固定依赖环境执行实际 Adapter → NativeCueKBExecutor → NativeInputSearch → voice_result，用同一份旧版公开合成响应、相同查询/KB/预算和受控 HTTP 传输。实际模型结果均为 **576 字节**，排序 JSON 的 SHA-256 均为 `157c05b22e2fd7cf909ae70d218459d652bc654c21d6249bbfd3eeb2d06fee87`，逐字段相同。请求只增加 `selection_mode=ranked`、`selection_query=null`、`query_facets=[]`、`filters.product_entity_id=null`；最新 CueKB DTO 接受这些默认值，未自动切换 adaptive。新增默认结构会增加 API 内部元数据体积，但本样本没有增加实际模型结果或改变播放；临近预算时的完整组取舍仍由预算保护负责。

方法二：分别载入三个提交的实际 backend 注入方法，在 inference_mode 下创建历史 tensor，再交给真实后台事件循环线程。受控引擎相同、待注入 token 相同，不运行 GPU：

| backend 版本 | 引擎调用数（共 3 tokens） | 结束状态 | 结果 |
| --- | --- | --- | --- |
| 986f442（aba0b53 之前） | 1 | process_response | 注入失败，历史写入抛 RuntimeError |
| aba0b53 | 1 | process_response | 同上 |
| 87f5ef7（用户确认尚未部署） | 3 | idle | 全部完成，无错误 |

缺陷可追溯到 fac1b7a 将历史 tensor 写入移入后台协程；Triton execute() 的线程局部 inference_mode 不会传播到后台线程。同线程 asyncio.run 测试会掩盖它。此前“432 项契约与集成通过”不包含真实后台 inference tensor 写入和浏览器输出节奏，所以不能排除此缺陷。现有现场时序与它吻合，但未取得现场异常堆栈；播放缓冲溢出仍是第 10 节的另一条输出路径问题，不能把所有截图都归成同一个异常。回退 aba0b53 既不会修复该代码缺陷，还会失去新版字段兼容。

另查出 aba0b53 确实影响了旧验证脚本：连续对话评估两处、自然修订测试一处仍按不含 product_entity_id 的旧 filters 字典精确比较。新增 null 字段令这些检查误报；在未修改的 87f5ef7 临时副本运行 C01 同样失败，已确认属于本次编码前的验证兼容缺口。本轮更新预期为显式 product_entity_id=null，仍精确检查服务端产品/版本条件，没有放宽授权或结果标准。这不是音频运行代码，不能用它解释浏览器溢出或 backend 注入异常；432 项子集通过也不能代替完整持续对话回归。

### 11.2 CueKB 8a5aacc 接口影响

本轮读取 CueKB main 的 `8a5aacce1184b95cacd391347183491a0f075d9f`，对照 schemas/domain、检索实现、生成契约及接入文档。最新请求/响应/命中/上下文/归属/选择诊断字段集与本项目同步后的 DTO 一致；本地仍保持未知字段拒绝和原有 UUID/非空证据检查。最新新增审核分类、复选产品及草稿状态字段，以及源树分页接口，属于管理链路；本项目仅调用 /v2/search，不需要引入这些管理 DTO 或额外请求。没有新检索必填参数、协议重试或新模型调用要求。

8a5aacc 将可选相关性校准文件无效从构造检索服务时抛错，改为继续未校准检索并标记 `retrieval_status=degraded` / `degraded_reasons=[relevance_profile_invalid]`；有效旧词法 revision 可体现为 adaptive `calibration_status=binding_mismatch`。已有字符串原因字段可接收，不能把 HTTP 200/degraded+hits 当作工具错误或证据已充分；无证据的 degraded 仍明确不可用。本轮用 CueKB 实际 V2SearchResponse 序列化合成 ranked/adaptive 降级响应，再由本项目 DTO 解析通过；实际 ranked 请求也经其 V2SearchRequest 验证。此前 409 没有响应体，本次不倒推它必由校准文件引起。

需要修补的是先前 DTO 补齐之后的消费缺口，而非另一次搜索字段扩展：

- `fact_kind`、`dependency_issues` 此前只进入搜索 DTO，Adapter 构造 Citation 时丢失。现保留到 Citation/生成契约/门户类型；存在未解决依赖的组不会因 context_state=complete 而被采用，历史 Citation 用默认值兼容。
- 必要上下文此前合为字符串放在主事实的归属下。现实际模型结果的 context 为必要片段列表，每段保留原文、角色和自己的 applicability/reason_codes，unknown 不继承主事实的 resolved 产品；原始 link_provenance、来源和完整结构仍留在 Citation 审计。非默认事实类型进入模型证据，不把比较/套件默认为普通单品陈述。
- 请求 DTO 补齐与上游相同的“选择专用参数仅供 adaptive、非空白”校验；实际请求继续 ranked，不自动启用动态选择。原生结果按实际投影后的 JSON 计费，同一预算内完整组取舍，不截断条件、不提高预算。

这次修补只涉及本项目 API、契约、门户 TypeScript 类型和测试；VoiceChat 完整运行文件沿用 87f5ef7，不另改其模型/提示/音频。先部署最新 main，再按 A/B 听完整 ACK、确认注入 idle 和正式回答；首问路由、错拼/漏项及成功注入的 GPU 时延仍单独验收。

本轮检查：接口与原生 CueKB 集成 **116 passed**；完整回归首轮 **691 passed / 6 failed**，6 项均为上述旧 filters 预期；修正后连续对话/修订 **26 passed**，覆盖全部 6 项，其他 691 项的结果来自前述全量运行，期间生产代码未再变动。VoiceChat 独立 CPU **48 passed**；Web TypeScript/生产构建、契约导出、Ruff、12 个完整快照 hash、文档链接/命令语法与差异检查通过。CueKB 工作区保持无修改；未部署、未调用新的真实 CueKB/语音模型、未做 GPU 或现场听音。

## 12. 最新现场：执行目录与运行版本不一致（2026-10-28）

假设：此次仍在使用混合部署，尚未验证已交付的注入修复。方法：将用户提供的七组 expected/actual SHA-256 与 Git 提交文件逐一比对，再核对异常文案、源码行号和 web-start 的执行顺序。结果：

| 项目 | 核对结果 | 含义 |
| --- | --- | --- |
| 两个采集脚本 run_native_cuekb_validation / summarize_voice_timeline | expected 匹配 986f442/aba0b53，actual 匹配 87f5ef7/63a7400 | 宿主执行目录的采集脚本较旧，容器文件较新 |
| cuekb_evidence / adapters / schemas / contracts / evidence_types | expected 为 63a7400 之前的文件，actual 五项全部精确匹配 63a7400 | API 容器已包含本轮接口修补；不能把 expected 当作最新版本、从旧目录重建降级 API |
| VoiceChat RuntimeError | `model.py:1923` 的旧异常文案、1927 的 finally 行与 aba0b53 backend 精确一致；87f5ef7 对应行号和文案已改变，应包含 error_type/remaining_tokens | 日志对应修复前已加载代码。可能是旧挂载来源或进程未重载；现有材料不能区分，也不能由磁盘 hash 通过证明加载成功 |
| VoiceChat 运行数据 | 38.9 ms 后仍 process_response，推理返回 500 后终止连接、abort 请求；该连接 Dropped=0 | 符合此前注入异常退出路径，不能算新修复失败；Dropped=0 也不代表工具结果已消费 |
| web_capture_not_started / NO_CURRENT_WINDOW_OR_SESSION | web-start 在版本校验时退出，先于创建 web-pending | 没有本次窗口可供 web-end 或时间线采集；不是第二个独立检索故障 |

本轮不再修改推理算法。部署命令经核对：Dockerfile 会复制上述 API 文件，Compose API 无源码 bind mount，按第 2.1 节正确目录 build+force-recreate 会加载构建内容；VoiceChat 则须用本仓库 docs/voicechat 六个绝对路径重新创建。未发现这段命令本身漏装上述文件的缺陷，现场缺少的是统一执行目录与进程加载身份。

处理顺序固定为第 2.0 节完整同步 main → 第 2.1 节重建 API/重新创建 VoiceChat → 第 2.2 节等待就绪并 check → 通过后才 web-start/播放。已有已验证的 GPU 镜像 ID、模型目录、双 GPU 配置和数据卷保留；不复制单文件或绕过版本检查。此次仅增强宿主检查输出的目录/提交、哈希方向和失败时容器身份，保护逻辑保持；真实部署/听音仍由现场复测，不把本地工具模拟当作已修复生产。

本轮开发检查：采集/部署专项 **24 passed**，包含版本不匹配时不创建窗口、不启动自动音频或采集的回归；Ruff、shell 语法、文档链接与围栏、部署命令语法、差异检查通过。未修改 VoiceChat 运行文件或 API 推理逻辑，未进行生产部署。

## 13. 请求结束、多轮回答与结果负载修复（2026-10-29）

### 13.1 现场证据与定位

b6960e0 的 Golden 独立 preparation 通过，API 于 04:41:52、VoiceChat 于 04:33:24 重建。本次不能继续归因为旧文件。Golden 的 sequence 1310895159 在连接时长 99.673 秒报 append_request request not found；当前 backend 未读取 output.finished，保留旧 generator/start 状态继续追加；END 也先走正常推理，所以清理重复遇到异常。CPU 真实方法复现表明 terminal output 被误作普通 token。底层到底为何结束仍缺现场 finish_reason，不能把 99.673 秒当 105 秒 watchdog，也不能仅凭 max_model_len=6144 认定现场必达上限。

Black → Golden 的 sequence 2420721760 中：CueKB 04:46:48.500 返回 5 条，耗时 122 ms；工具结果 48.550 提交；backend 48.763 收到、49.518 开始注入，04:47:06.739 完成 530/530 tokens，准备 5 ms、append_wait 17216 ms。后半段 input f1c4e089… 于 49.635 开始、08.661 完成转写，等待约 19.026 秒；新回答 703c0ee7… 被关联该段，首段 bc6da698… 的回答观察器悬挂。黑猪采集准备另报 voicechat_health，因此这一段是可用于定位的现场片段，不能记作健康准备通过的独立 B 验收。

图 1 已有未核验口述答案，但文件底部仍 waiting reply；图 2 原任务 Searching 与第二段一般回答并存。running→Searching、回答首次出现时绑定 latest_input、文件发完后状态不随实际播放完成更新，分别导致这三种显示。未核验标签继续保留，首问缺工具、漏答保质期和事实条件检查不因显示修复自动通过。

### 13.2 本次实现及约束

1. VoiceChat 处理引擎 finished/生成器耗尽和 missing-request，保存终止状态，后续不再 append、不重放；正常结束给会话轮换码，未知失效明确关闭。END 独立终止 LLM/TTS、释放序列并刷新最终显示 ASR，不调用正常 decoder；LLM abort 等待上限 1 秒，即使出错或不返回也继续清理 TTS/序列。常规日志增加 finished reason、prompt/appended/output token 与 decoder 步计数，摘要输出 backend.request_finished；这些计数用于定位，不把三项简单相加当实际 context 长度。
2. 服务在函数输出送入 Triton 时保留原生调用→输入关联；等待期间的补充输入与后续正式回答一起快照，ACK 不结清工具回答义务。API 复核已知调用/输入与响应内关联不变；普通语音不取消查询、不新增模型选择器。仅旧协议无法区分交错回答时明确失败，不按最新或最早输入猜归属。正式回答仍由原 Coordinator 观察器按实际证据核验和唯一提交；related inputs 表示该段处于同一待回答上下文，不认证每个属性均已覆盖。
3. 工具结果提交后，原生回答默认最多等待 60 秒，包含注入与回答完成；由 VOICE_NATIVE_ANSWER_TIMEOUT_SECONDS 在 1–120 秒内配置。超过后明确 VOICE_ANSWER_TIMEOUT，关闭连接并结清 observer，不触发重试。检索时限、105 秒连接上限和固定 30 秒宽限保持。
4. API 只把多来源完全相同的 title/applicability/fact_kind 提取为 source_defaults，补充一句应用默认字段的说明。原始来源、必要上下文、各自 unknown/ambiguous 归属、审计 Citation 与检查证据不变；去重仍使用完整来源、预算沿用原有保守上限。三组合成完整证据从 **1066→727 字节（减少 31.8%）**，还原后逐字段相同。这是负载数据，不是实际 Golden 530 tokens 的提速测量；引擎仍逐 token 执行，未启用未经 GPU/实际引擎验证的批量追加。
5. 门户依据 awaiting_provider_answer 显示 Preparing reply，补充输入标为 Included in the knowledge reply above，历史刷新保留关联和知识回答分类。文件在实际 answer 播放 ACK 完成后显示 Reply finished，状态不以固定 ACK/仅 audio.done 为准；发送下一文件后旧回答不改变新文件状态，通话保持。

### 13.3 部署后验证效果

只按第 2 节配套部署 API、Web、VoiceChat，然后执行第 3–5 节同一 A/B，保留原文件、停顿和预算；无需新参数或额外用例。出现 preparation/health 失败就停止，不能继续播放并记为通过。

回传本次有界后台摘要与两次实际门户结果：原任务应得到终态、补充输入应有明确关联，同连接下一问仍能回答；完整 ACK/注入/回答分别核对。记录 tokens、completed、append_wait_ms 和 result→answer 时间，与本节 530/17.216 秒比较，不能由字节下降直接换算 GPU 秒数。若触发引擎结束，应有明确会话结束/轮换，END 不再报同一追加异常；摘要中的 reason 和计数用于确定剩余的长度/引擎原因。Answer not verified 仍按具体 answer_check 原因及实际来源评估，不改成成功以通过验收。

本轮开发验证：API 全量 **710 passed**；随后旧桥接新增上下文字段兼容补充，相关完整专项 **65 passed**。VoiceChat 独立 CPU **56 passed**；Web 音频/文件回归 **39 passed**，真实 Chromium 的关联/历史与一般回答检查 **2 passed**，TypeScript/生产构建通过。首轮 API 全量曾因两处手工夹具缺新增状态字段而失败，补齐夹具后上述全量通过，未放宽运行检查；浏览器初次启动使用了不允许 mock 的生产入口，改用仓库显式隔离模拟入口后通过。存在既有 Starlette 弃用警告。Ruff、契约生成、完整快照 hash/编译、模型补丁逆向及文档/命令检查通过。浏览器使用受控服务端输出，本环境未部署生产，无真实 GPU/CueKB/原音频听音结论。

## 14. 566e414 转写关联回归与会话欢迎语（2026-10-29）

### 14.1 现场根因

566e414 的部署身份、文件和健康检查均通过。Golden 与 Black 会话都在 CueKB 调用前结束，`model_function_count=0`、`api_tool_calls=0`，因此本次错误不能用于判断检索或注入是否恢复。VoiceChat 回答结束后把当前 `response_id` 清为 null，却暂留上一回答的输入关联；下一条 ASR 转写本身没有 response ID，旧条件把两边的 null 误判为同一回答，把旧 `input_item_id`、`tool_call_ids`、`related_input_item_ids` 附到新用户转写。API 又把这些回答字段透传给严格的门户转写契约，产生三项 `extra_forbidden` 并关闭连接。

麦克风静默约三秒时，模型按会话行为开始欢迎询问；API 过去要求每个回答都属于已存在的用户输入，因而报 `Response has an unknown input association`。欢迎询问不是客户问题、检索任务或知识回答，不能伪造输入归属，也不能因客户随后开口而改标。

### 14.2 本次实现与验证目标

VoiceChat 只在事件具有非空且匹配当前 response ID 时捕获回答关联，回答完成后清理可变关联；用户转写始终只保留自己的 `item_id`。Provider 再按事件类型过滤，ASR 即使收到旧端错误附带的回答字段也不向门户传播。API 允许连接开始、尚无用户输入和工具上下文时出现一次会话级 `greeting`；它不创建 Turn、不调用 CueKB。客户开始发声时清除欢迎语剩余播放，继续同一连接；正常模型 EOS 和 PAD-tail 完成都进入等待新输入状态。首次欢迎语之后若仍出现无用户归属的主动输出，API 有界消费并抑制，不创建 Turn、不断开连接；显式未知输入、未知工具调用和中途改变归属仍严格拒绝。会话提示要求先等待约三秒，只在客户未开始发声时询问一次。

本轮开发验证：API 全量 **714 passed**；其中真实 FastAPI/WebSocket 受控 Provider 覆盖欢迎询问、随后首个输入和欢迎语中途开口，门户未收到协议错误。VoiceChat 独立 CPU 全套 **57 passed**，包含完整回答后下一 ASR 不携带旧关联。Web 音频/文件回归 **39 passed**且 TypeScript/生产构建通过；契约重新生成，Ruff、快照 hash、外部 Speech 三个修改文件逐字节同步及 `git diff --check` 通过。存在既有 Starlette 弃用警告。本环境没有生产 GPU、真实麦克风、CueKB 或原始 WAV 听音结果。

部署后先执行一次麦克风静默场景：连接后约三秒应听到一次简短询问，页面不报错；询问期间开口应停止余音并保留连接。随后按第 3–5 节执行原 Golden 与 Black→Golden，不改变 WAV 或停顿。转写事件不得再含上一回答的关联字段，且只有实际工具调用后才评估 CueKB、注入、正式回答和首问路由。开发环境没有生产 GPU、真实麦克风或原始 WAV 听音，这些仍以现场结果为准。

## 15. CueKB 2.1 单查询终态与正常 EOS 验收（2026-10-30）

### 15.1 部署身份

本轮必须同时部署本仓库最新 `main`、CueKB `main@30970e9` 和按 `docs/voicechat/snapshot-manifest.json` 重建的 VoiceChat。API 环境至少确认：

```text
VOICE_MAX_RETRIEVALS_PER_INPUT=1
CUEKB_VOICE_TOKEN_BUDGET=120
CUEKB_BASE_URL=<CueKB base URL，不带接口路径>
```

`preparation=passed` 后，API 日志应出现 `cuekb_evidence_request_started` 和 `cuekb_evidence_response_validated`；出现旧语音 `/v2/search`、旧 `cuekb_response_validated` 或 manifest hash 不一致，均表示版本未配套，不进入业务验收。

### 15.2 固定场景

1. 麦克风连接后保持静默约三秒：只能出现一次欢迎询问，欢迎语正常结束后连接继续等待。
2. 在欢迎语播放期间开口：页面应收到 playback clear，余音停止，当前用户转写和随后回答保留；不得出现第二欢迎语或协议错误。
3. 新通话执行 Golden 独立文件；另起新通话执行 Black 后同连接 Golden，文件、停顿和顺序仍按第 3—5 节。
4. 受控回归中强制模型在第一个终态结果后再发一个或两个新 call，用于验证结清边界；生产 A/B 不人为修改 prompt 或 WAV 来制造调用。

### 15.3 必须同时满足的控制流判据

- 每个 `input_item_id` 最多一个 `voice_bridge_started`、一个 CueKB evidence query、一个 validate、一个 `voice_tool_result_submitted` 和一个业务 Turn；`retrieval_executions=1`、`retrieval_limit=1`。
- 请求 `question` 是完整最终 ASR，`requested_facets` 是其精确跨度；`search_query/filters` 只能增加服务器确认上下文，不能把组合问题缩成产品名或模型泛化词。
- `cuekb_evidence_response_validated` 记录 decision、facet/group/token 数；注入载荷只含 provider `model_view` 与 `search=finished`，完整来源留在 Citation/门户。
- 模型终态后新 call 记录 `voice_search_post_terminal_settled` 和 `post_terminal_calls_settled`；不得新增 CueKB query、Turn 或 result injection，不得出现 `VOICE_TOOL_LOOP`，原 Turn 最终为 answered/明确业务失败而非 superseded/canceled。
- 欢迎语或完成回答后的无归属自说记录 `voice_unsolicited_response_suppressed`，不进入门户答案、不创建 Turn、不断开；真实下一次用户输入仍可正常回答。
- 显式伪造/未知 input、call 或工具关联仍应得到 `VOICE_PROTOCOL_ERROR`，不能因抑制自说而放宽。

### 15.4 内容和时延判据

Golden 的 shipping、密封保质期、最佳食用期和开封储存逐 facet 对照 CueKB decision/evidence/missing；Black 的价格、重量、层数和最佳食用时间同样核对。`partial_answer/clarify/insufficient/conflict` 必须保留 provider 语义；禁止用 Black 的鲜肉期限支持 Golden。

时间线新增区分 CueKB query+validate 与 VoiceChat 注入。`cuekb_evidence_response_validated.duration_ms` 不是最终回答时延；继续使用 `backend.inject_profile.append_wait_ms` 和 result→answer 时间定位 GPU 消费。通过控制流不等于内容准确、实际扬声器播放或 P95 达标。

### 15.5 开发侧已完成的证据边界

CueKB `30970e9` 源码、公开 schema/fixture 和四个端点契约已核对；其源码与测试可编译，完整依赖安装因固定 `torch==2.14.0+cpu` 在当前索引不可用而未在本环境重跑全套。本项目 API 契约 **399 passed**、集成 **314 passed**，其中严格 consumer、FastAPI/WebSocket 受控 Provider、正常 EOS/PAD-tail 和 post-terminal 结清测试用于证明控制流及契约；VoiceChat/部署受影响契约 **70 passed**。这些结果不证明生产 GPU、真实 CueKB 内容或麦克风听音。

## 企业RAG重构P0契约同步（进行中）

消费者补齐CueKB `1d483fc`的事实类型、依赖问题、上下文归属/来源与动态选择诊断；继续禁止未知字段。不代表证据充分性评估、新文档树或语音性能重构已经交付。验证结果以本次运行回执为准。

本次P0验证：契约与原生CueKB集成432 passed（1条现有Starlette弃用警告），专项43 passed；Ruff、契约生成、git diff --check通过。原生集成为测试环境，未验证真实GPU性能。首次扩大回归因PYTHONPATH缺仓库根而收集失败，修正为`PYTHONPATH=.:apps/api`后完整通过。
