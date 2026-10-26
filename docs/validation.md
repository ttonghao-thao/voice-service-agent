# 当前问题验证：工具等待、首问触发与回答完整性

更新：2026-10-25。当前唯一执行入口；历史验证方法已归档。**第 8 节为修复前真实 A/B；本次已编码，第 9 节记录范围与本地检查。CueKB 新版按用户确认已支持内容侧优化、待联调。** 更新后按第 2–7 节执行相同 A/B，不把旧结果当作新版本通过。所有命令在**部署主机的本项目根目录**执行，操作者只操作、观察和回传，不需要挑日志、找会话 ID 或判断根因。

## 1. 本轮要回答什么

用户已报告最新 CueKB V2 上线并验证，但仍有以下问题。本轮是定位采集，不是上线或修复后的放行测试。

| 当前问题 | 已有证据 | 本轮验证目标 |
| --- | --- | --- |
| Checking knowledge 长、常见两次调用 | 用户报告检索不到 1 秒，但调用间隔超过 10 秒；代码确认 VoiceChat 逐 token 注入结果，并可能先等待 ACK 结束 | 分开检索、应用交付、ACK/调度、结果注入，以及下一次工具生成或回答的时间；确认第二次是否属于同一输入、查询了什么 |
| Golden Yolk 独立不触发，Black Pig 后触发 | 用户观察；工具描述已覆盖两种产品，Golden WAV 有追问式开头和中间停顿 | 对照全新连接的 Golden 与同连接 Black Pig 后的 Golden，核对实际输入、分轮、模型调用和 query，而不只看界面状态 |
| 回答漏项、数值及条件表达不稳 | Black Pig 一段称未找到重量，另一段报 65 克，尚不知是否同次提问；Golden 漏答运输 | 明确每段答案属于哪次输入，核对价格/重量/层数/最佳食用时间，以及运输/保质期/开封储存；保留真实来源 |

**复测固定为两次通话、三次文件发送：**A 为 Golden 独立首问；B 为新通话先 Black Pig、再在同一连接发送 Golden。不同时运行自动 core、四文件、Hello/TCP、RNNT 定向或后台测试；不改 WAV、停顿、语速、查询预算、模型参数或会话限制。工具描述、schema 与模板已在本次提交配套更新，不在现场临时改写。这样得到的是首次可比较样本，不足以单独证明稳定性或 P95 达标。

## 2. 更新本次代码并检查部署

本节在部署主机执行，不是开发环境已经部署的记录。先将本次完整提交检出到主机项目目录，保持已有 `.env`、已验证的双 GPU（0,1）配置、9000 端口及模型挂载；停止旧测试通话，本次窗口内不要有其他测试用户。CueKB 由其运行方发布新版，本仓库不替它迁移、修改内容或重建索引。

### 2.1 配套更新 API、目录与 VoiceChat

本次 API、采集器、query schema、全局目录默认值和 VoiceChat model/data/template 均有修改。只重启 API 不足以生效。下面命令用于现有本项目单知识工具部署：构建并重新创建 API，应用已检入的目录配置；记录原 VoiceChat 的已验证镜像 ID 和模型目录，再用原 GPU/端口参数及本仓库六个只读挂载重新创建 VoiceChat。Web 源码未改，无需重建 Web；门户后续强制刷新即可。运行库和权重不变，不下载或猜测基础镜像。

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
  docker build -f deploy/Dockerfile.api -t "$API_IMAGE" .
  docker compose --env-file .env -f deploy/compose.production.yaml up -d --no-deps --force-recreate --wait --wait-timeout 180 api
  docker compose --env-file .env -f deploy/compose.production.yaml exec -T api python /app/scripts/configure_native_cuekb.py --apply
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

正常得到 preparation=passed 和 API/VoiceChat 的 runtime 身份行。入口会核对应用、采集器、query schema、目录 YAML、原始 WAV、VoiceChat 六个运行文件及既有诊断配置，并检查 VoiceChat 为真实 Triton/model ready（拒绝 loopback）。出现 version=failed / collection=failed 就停止并回传；不自行复制文件、降低检查或再次重建。文件匹配不证明 GPU 提速、模型首问选工具正确或 CueKB 内容已通过。

浏览器强制刷新门户，打开 F12 → Console，开启 **Preserve log**，过滤 voice_file，清空旧 Console 内容。保持门户前台、声音可播放，不锁屏或切后台。固定使用仓库 artifacts/native-cuekb/golden_yolk.wav 与 artifacts/native-cuekb/black_pig_crisp_heart.wav，选择 **Audio file → Choose WAV**。Python 和采集在容器内执行，宿主机无需 Python，不手工导出 HAR 或运行 docker logs。

## 3. 通话 A：Golden Yolk 独立首问

**假设：**独立首问未触发可能发生在语音识别、分轮或原生工具选择阶段，不能先判为 CueKB 慢。预期取得完整两段问题及该次实际模型/工具事件；是否出现 Checking knowledge 只作观察。

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

### 7.2 首问触发与内容

| 检查项 | 判据 |
| --- | --- |
| Golden 首问 | A 中完整两段实际输入可见、工具目录正确、模型发出真实知识调用；若未触发，使用 `model_boundary` 的 input.bou / agent.bos source 与 query 定位，不宣称 CueKB 没数据 |
| 同连接对照 | B 中两个 file.start 的 conversation/session/epoch 一致，顺序为 Black → Golden；Golden 未发送、未发完或到期时结论是对照不足。A 失败/B 成功只支持上下文或轮次相关，不直接证明冷启动或唯一根因 |
| 两次检索 | 默认每输入最多两次实际查询，无自动 HTTP 重试；第一/第二次 query 与缺失属性对应，不要求为了通过而凑满或强制只查一次 |
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

## 企业RAG重构P0契约同步（进行中）

消费者补齐CueKB `1d483fc`的事实类型、依赖问题、上下文归属/来源与动态选择诊断；继续禁止未知字段。不代表证据充分性评估、新文档树或语音性能重构已经交付。验证结果以本次运行回执为准。

本次P0验证：契约与原生CueKB集成432 passed（1条现有Starlette弃用警告），专项43 passed；Ruff、契约生成、git diff --check通过。原生集成为测试环境，未验证真实GPU性能。首次扩大回归因PYTHONPATH缺仓库根而收集失败，修正为`PYTHONPATH=.:apps/api`后完整通过。
