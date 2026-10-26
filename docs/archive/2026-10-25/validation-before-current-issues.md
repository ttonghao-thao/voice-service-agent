# 历史验证方法归档（2026-10-25 重整前）

> **归档文件，不作为当前执行入口。** 按用户要求保存重整前的验证方法与排查记录。当前操作只使用[最新验证手册](../../validation.md)，不再执行本页中的旧轮次流程。

本页保留旧手册的 CueKB V2 部署、自动 core、独立 Web 文件、同连接 Hello/TCP、RNNT 句首、四文件对照、VoiceChat 核对、内容判据和开发回归方法。旧日期、测试数量和现场结论均属于当时的记录，不自动成为当前版本的通过证据。

除本归档说明与相对链接重定位外，以下保留重整前正文；原正文 SHA-256：`5524d1ded016099e1284a596ad1cb84010b3a7a526f3b4879bf1ac840a472cb1`。

---

# 统一验证操作手册

这是本项目唯一的验证文档，适用于英文（en-US）全双工知识客服：Web → API → NVIDIA VoiceChat → CueKB → 有来源的语音回答。所有操作、回传内容、判据和当前未解决项均在本页，不需要打开其他文档。

**操作者只执行命令和回传精简输出，不负责判断原因、选择日志、找会话 ID 或调整参数。** 命令失败也保留输出，交给开发者定位；不要反复重跑、提高检索次数、延长会话或删除 WAV 的停顿来获得通过。

## 1. 本次范围与执行顺序

本轮接入 CueKB V2；2026-10-25 用户反馈新版本已上线并基于最新版本验证，仍有 Checking knowledge 等待长和 Golden Yolk 首问不触发的现象。当前开发环境没有该轮现场日志或生产容器，不能将上线反馈扩大为内容、时延和独立首问全部验收。第 4.4 节记录本次代码分析及证据边界。以下保留部署与采集入口：先由 CueKB 管理员完成其 V2 数据准备，再按第 2 节更新本项目 API/Web、第 2.1 节检查现有 VoiceChat；随后仅执行第 3 节的 `auto core` 一次，以及第 4 节两个产品文件的 Web 上传各一次。不要同时执行四文件全量对照、RNNT 定向测试或后台测试。

本轮目标：让实际模型证据保留产品归属、包装/开封等必要条件，按完整证据组预算选择，减少无关注入；保留两次查询预算、终态和累计证据。V2 评分是相关性，不是事实正确率。必要依赖不完整时明确拒绝，未知归属明确标记，不能借用另一产品的数值或宣称整个知识库没有资料。自动完成后的 5 秒静音观察和同连接下一问继续保留；本轮不修改 VoiceChat、RNNT、会话寿命，不新增 MMR/reranker/答案模型。

2026-10-22 补充的 RNNT 显示转写修正已部署，用户报告 Hello/TCP 多次正常。`model.py` 的显示转写 predictor 改用一次零输入初始化，与 NeMo 标准贪心解码一致；不改 WAV、前置静音、权重、识别门槛或控制打断的 RNNT 状态。第 4.2 节仍保留定向检查方法，当前不再执行；该反馈不代表麦克风或全部语音准确率验收。

后台持续播报按用户决定暂缓，作为非阻塞待优化项；保留页面隐藏不主动结束及回前台尝试恢复的现有实现。本轮 Web 验证保持前台，不要求切后台或锁屏，不将后台连续播放作为放行条件。

输入唯一来源是本仓库 `artifacts/native-cuekb/` 的 `cases.json` 和四个 WAV。API 镜像内对应 `/app/validation/native-cuekb/`。所有 Python 在容器内执行；宿主机只需 Bash、Docker Compose、Git 和常用 shell 工具。**不使用 docker cp，不在容器创建输入目录，不使用历史 ZIP。** 输出目录由脚本自动创建，API 的只读根文件系统不受影响。

宿主机执行入口是 `bash scripts/validate_deployment.sh`；它自动调用 `docker exec`，寻找 API 容器、检查固定文件、提取本次时间窗口和会话、采集匹配的 VoiceChat sequence、查询已保存答案。采集不会新增模型请求或 CueKB 请求；`auto` 和 `prefix` 会发送测试语音。默认 VoiceChat 容器名为 `nemotron-labs-voicechat`。

## 2. 更新本次验证工具并准备环境

所有命令在**宿主机项目根目录**执行，保持同一主机、目录和用户。先结束已有测试通话；本次采集期间不要有其他测试用户通话。

CueKB 运行方须先确认以下三项，本项目命令不会代为写入或迁移其知识库：

- 已部署支持 `/v2/search` 的版本，并完成 CueKB 自身的 0004 数据库迁移、可用 Embedding 配置和 V2 索引/generation；未配置 reranker 是正常配置。
- 两种月饼原文已按 `evidence-v2` 重处理，按具体章节审核产品归属并发布；单纯重建向量索引不会补齐旧切块/归属。同一文档不能默认全部属于一个产品。
- 受限服务端 Key/KB 可读范围和 `.env` 中 `CUEKB_BASE_URL` 保持正确；base URL 不含 `/v1/search` 或 `/v2/search`。本项目固定调用 V2，不自动回退 V1。

本轮重建 API/Web 镜像并新建通话，**不需要重建或重启已匹配版本的 VoiceChat**。本项目数据库无需新增迁移；CueKB 的迁移由其管理员单独执行。API 使用严格 V2 DTO，模型包仍为约 6000 bytes 的独立预算；上游 hits 预算默认 24000 bytes，并非直接发送完整 V2 JSON 给 Nano。沿用已有效的每输入两次查询及原生 HTTP 无重试。

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

VoiceChat 已加载本仓库完整快照是本次验证前提。会话提示由 API 在新通话注册，因此更新后必须新建通话。本轮没有新增 VoiceChat 运行文件或环境变量，不执行源码替换、容器重启或权重调整。

## 2.1 检查现有 VoiceChat 和镜像内文件

```bash
bash scripts/validate_deployment.sh check
```

正常输出 `preparation=passed` 和两条 `runtime` 身份行。入口自动核对 API 的 V2 Adapter/严格 schema/证据类型/原生选择与验证脚本，以及四份原 WAV 的哈希；六个 VoiceChat 挂载文件、Jinja/诊断及既有完成保护配置也必须匹配。检查不发送 WAV，不新增 CueKB 查询。

回传精简终端输出即可。出现 `version=failed` 或 `collection=failed` 时停止，由开发者针对该项给出处理；不要自行复制文件、关闭校验或反复重启。此检查只证明容器文件与配置匹配，**不证明上游 V2 已就绪或产品文档已正确重处理**；实际 V2 响应、数据归属与索引错误由第 3 节首次真实查询的摘要提供。

VoiceChat 后续重建时，六个维护文件仍须从本仓库 `docs/voicechat/` 的绝对路径只读挂载到以下容器位置。本轮已经匹配时保持现有容器，不使用 `docker cp`：

| 仓库 `docs/voicechat/` 下的相对路径 | 容器实际加载路径 |
| --- | --- |
| `s2s/audio_server.py` | `/s2s/audio_server.py` |
| `s2s/prompt_template.jinja` | `/s2s/prompt_template.jinja` |
| `opt/tritonserver/backends/nemotron-voicechat/model.py` | `/opt/tritonserver/backends/nemotron-voicechat/model.py` |
| `opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py` | `/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py` |
| `opt/tritonserver/backends/nemotron-voicechat/data_types.py` | `/opt/tritonserver/backends/nemotron-voicechat/data_types.py` |
| `opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py` | `/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py` |

## 3. 操作一：一次命令完成两份产品录音和同连接下一问

```bash
bash scripts/validate_deployment.sh auto core
```

执行期间等待命令自行结束，通常需两至四分钟。**把本命令终端输出全部回传即可，它已经是精简结果。** 不要另外运行 docker logs、cat report.json 或贴完整日志。

入口先在一条全新连接发送原始 Golden Yolk WAV，保留中间停顿；完整发送后等待最新输入的非 ACK 回答完成，再观察 5 秒，然后在同一连接发送原始 hello.wav 并完成同样观察。随后为 Black Pig 建立另一条新连接测试一次。一份失败仍采集其结果并继续另一份，不自动重试。120 秒是验证器等待上限，不改变服务端会话时限。每次使用独立报告路径，失败不会误读上一次报告。即使验证失败，仍尝试采集这次的摘要。

终端输出包括：版本准备、`preflight`、`case`、`validation`、退出码、`session`、API 预算与检索、`cuekb_v2`、`voice_evidence`、`backend_injection`、每输入 `input_timing`、有界 `backend_stages`、保存的输入/回答/业务状态、模型 query、`answer_check`、`evidence_delivery` 和实际准入证据 `admitted_evidence`。证据总长度最多 6000 字符、8 个块，超出明确标记 omitted；不要另外打印全量检索响应。`cuekb_v2` 最多四条，只含排序方法、归属/省略计数及载荷；`voice_evidence.selected_payload_bytes` 是单次选择阶段准备的语音结果 UTF-8 大小；`evidence_delivery.payload_bytes` 是最终批次实际提交大小，二者都不是 Nano token 数。`local_artifacts` 指向本次宿主机私有目录；原始日志只保存在这里，不直接打印。

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

## 4.3 本轮 V2 接入、预期输出与判定

**已知基线：**2026-10-22 现场 `auto core` 中，两份产品录音均两次检索、终态后调用零、单次正式回答和 5 秒观察通过，Golden Yolk 后同连接 Hello 正常。Golden Yolk 转写到首个正式音频约 22.86 秒、两次注入合计约 16.73 秒；Black Pig 分别约 36.24 秒、29.96 秒。两份 CueKB 检索均合计约 0.2 秒，主要等待是 function 结果注入，不是知识库 HTTP 检索。

**内容未放行：**Golden Yolk 漏答运输，未保留 15 天最佳食用限定，并无依据地区分同一产品的两个名称。Black Pig 有 `3:7` 被说成小数、65 g 漏答、“最佳两小时”变为保质期，以及三天条件疑似跨产品使用；其 `VOICE_MISSING_CONDITION` 来自否定条件的有限词法检查。自动 passed 不证明这些事实准确，不能只放宽检查或减少来源数量使结果变绿。

**本轮已实现的接入边界：**严格选择 `/v2/search`，保留 applicability 和必要依赖；unknown/ambiguous 不补成查询产品，unresolved 组不直接肯定作答。完整组按排名和预算取舍；可选周边省略不损坏必要组。精简模型包保留可读归属、正文和必要条件，评分/位置/内部 ID 留在审计。V2 状态、配置错误和选择受限不能一律降为“没有资料”。旧历史仍可读，在线不回退 M3/V1。

以下为开发者核对字段，操作者只回传命令精简输出：

| 输出 | 预期与含义 |
| --- | --- |
| preflight | `cuekb_api=v2` 表示本项目配置；Golden Yolk `cases=2` 包含同连接 Hello。不是上游 V2 就绪证明 |
| cuekb_v2 | `schema_version=2`、实际 `ranking_method`、service_hits/adapter_retained/adapter_omitted、scope_counts、required_parts/unresolved_groups、omission_counts、上游 hits 与 Adapter 大小；无 reranker 可正常执行 |
| voice_evidence / evidence_delivery | 前者给候选/选中组、归属/必要上下文计数、选择阶段 `selected_payload_bytes`、拒绝与可选省略；后者 `payload_bytes` 是含指令/批次结构的实际提交大小。二者不当作 Nano token 数 |
| case | 产品实际检索 <=2；`extra_answers=0 quiet_observation=passed`；内容失败保留 failed/reason，不把提交终态当作回答成功 |
| 同连接下一问 | 同一 conversation_id 中 greeting 有非空音频及完成；Hello 不应触发工具 |
| admitted_evidence | 每条可读 applicability、完整必要 context、context_state、required_context_chunks 与正文；最多 8 组/6000 字符，超出明确 omitted |
| input_timing / backend_injection | 转写→首个正式音频、最后结果→首音频、实际 token、每次注入开始/结束。以本轮真实结果比较，不由 API 字节数宣称时延达标 |
| answer_check / turn | input/response、具体 reason、最多三条条件失败和引用数；只索取确有必要的对应片段，不打印完整日志 |
| 门户 | 归属未知/歧义、条件与必要上下文状态可见；正文在播报结束前逐步出现；Answer not verified 不等于检索失败 |

`CUEKB_CONFIGURATION` 优先定位 CueKB 的模型/索引/已发布处理版本；`CUEKB_CONTRACT` 定位严格 V2 响应不兼容；`CUEKB_DEPENDENCY` 表示 HTTP/服务依赖失败；`CUEKB_RETRIEVAL_LIMITED` 表示上游 degraded 空结果或选择受限，不能冒充正常查无资料。`CUEKB_VOICE_EVIDENCE` 再结合 unresolved、unknown/ambiguous 和 budget 区分原因。开发者处理，操作者不修改参数、不选字段。单个 `context_state=complete` 也只证明已识别必要依赖完整，不代表全部所问属性已找到。

每份原 WAV 自动/Web 各一次。若同一方案两次无新增证据则停止复测，三种假设仍无进展则报告阻塞。CPU 契约通过不能替代真实知识语料、GPU 续答、听音和时延验收。本轮不调整 105 秒上限、固定 30 秒活动宽限或后台播放。

## 4.4 Checking knowledge 长等待与首问差异（2026-10-25 代码分析）

**本次反馈与方法：**用户反馈 Black Pig 每次有结果但慢，Golden Yolk 独立发送未见 Checking knowledge，在 Black Pig 回答后发送则有结果；每次两次工具调用，检索不到 1 秒但调用间隔超过 10 秒。本任务 fetch 并核对最新 `origin/main=0da3234`，工作分支与其一致。分别核查应用执行、VoiceChat 结果接收/注入和原生工具触发；不修改运行代码、模型参数、检索次数或原始 WAV。以下耗时实测仍来自第 4.3 节的 2026-10-22 旧版记录，不能冒充本次 V2 的精确测量。

**已确认的等待机制：**VoiceChat 收到 function result 后，先将结果 tokenize 成队列。若仍处于 `speaking_ack`，结果先缓存，ACK 的文本和尾部 PAD 全部结束后才进入 `process_response`。当前默认 ACK 为 `Let me check the documentation.`，尾部至少 17 个 80 ms 帧，即仅尾部就有 1.36 秒，另加正文及边界帧；这不是固定 10 秒等待。

进入 `process_response` 后，[model.py](../../voicechat/opt/tritonserver/backends/nemotron-voicechat/model.py) 的 `_fast_inject_response_tokens` 对每个结果 token 执行一次 `_llm_step`，每步只 `popleft()` 一个 token，并非一次整段 prefill。每步包含 embedding 的 CPU 传递、跨事件循环等待、vLLM `append_request` / `anext` 和模型执行。`fast` 只表示不按实时音频到达速度推进，不表示跳过这些逐 token 步骤。注入时 `infer` 直接返回静音，结束后才恢复正常模型生成，决定补查还是回答。工具返回快与结果注入慢可以同时成立；证据是否准确也不改变这条处理路径。实际注入耗时近似为 token 数乘以平均每步耗时，不能在没有 profiler 时把全部时间归为 GPU 计算。

两次调用之间应按下表拆开，而非把整个间隔算为 CueKB HTTP 时间：

| 阶段 | 可核对边界与判据 |
| --- | --- |
| 应用侧检索及交付 | `voice_tool_call_received` → `cuekb_response_validated` → `voice_tool_result_submitted`；`tool_run_finished` 计时不包括后续全部 DB 记录、权限复核与 WS 发送 |
| VoiceChat 接收与音频请求调度 | `function.batch_result` → `function.result` → `response_chars`；audio_server 接收后先排队，在下一次音频推理请求携带结果，提交不等于模型已接收 |
| 剩余 ACK | `backend.result_received` → `backend.inject_started`，结合 `backend.buffered_ack` 判断；该段也可能含调度，不能一律算为 ACK |
| 逐 token 注入 | `backend.inject_started` → `backend.inject_done`，核对 `backend_injection` 的 token 数；这是旧版现场记录中的主要等待 |
| 模型后续决策与输出 | `backend.inject_done` → 下一次 `model.function` / `voice_tool_call_received`，或正式 `answer_audio`；补查的 query 本身也需模型生成和工具 token 抽取 |

**为什么常见两次：**[native_input.py](../../../apps/api/app/agent_runtime/native_input.py) 的 `combine` 首次成功时返回 `search_state=available`，指令为 `Search again only for a missing requested detail.`；达到每输入默认两次预算或重复 query 时返回终态要求回答。上限不是强制执行次数；若这两次属于同一个 `input_item_id`，需要比较 query 和实际交付来源判断是合理补查还是模型多查。若 Golden Yolk 停顿前后形成不同输入，则不能仅凭同一连接的总次数称为同一问题补查。原生 CueKB HTTP `retry=False`，本项目在该路径不调用另一答案模型；10 秒答案超时在 native relay 路径被绕开。VoiceChat 的 10 秒 function request/response timeout 也仅用于等待状态的故障复位，不是成功结果的定时等待。

**用户补充的门户回答文本：**本轮没有调用/时序日志，用户提供两段 Black Pig 和一段 Golden Yolk 回答；未说明两段 Black Pig 是否同一次提问连续输出，不据此认定重复回答或两段分别对应两次调用。该文本是当前输出证据，不是 CueKB 原文、检索 trace 或实际听音验证。

| 输出 | 可确认的问题与证据边界 |
| --- | --- |
| Black Pig 第一段 | 明确称未找到 price/weight，但给出十八层、肉源、肌内脂肪及评价；没有回答固定问题中的最佳食用时间。新增产品介绍并未补齐所问属性 |
| Black Pig 第二段 | 出现 `sixty five grams`、十八层、两小时及储存/包装期限，但没有价格或价格未确认的说明。与第一段的重量可用性表述不同；若是同一次提问连续输出，应优先核查是否先答后补查再答，若是不同运行则体现输出覆盖不稳定，不能反推两次调用的时序 |
| Black Pig 比例 | 当前说法为 `three point seven percent fat-to-lean ratio`，与第 4.3 节已记录的 `3:7` 口述问题相似，但没有本次来源不能认定原文仍为 `3:7`。第一段的 `three percent intramuscular fat content` 属于不同指标，不能仅因数值不同就判定两句直接矛盾。三天冷藏/四十五天密封是否属于 Black Pig 仍需来源，不能用另一产品恰有同数字判定串用已证实 |
| Golden Yolk | 给出密封四十五天、十五天食用及开封冷藏三天；未回答固定问题中的运输，也没有说明运输未确认。`should be consumed within fifteen days` 没有保留旧基线记录的“最佳食用”限定；本次来源不可见，不能仅凭这句话确认新证据是否仍包含该限定 |

这些文本说明当前仍有属性漏答、数值表达及条件归属待核对。它们与“模型拿到部分证据后可能继续补查或重新组织回答”的机制相容，但不足以证明先答后查、同输入重复回答、最新证据是否正确或十多秒的具体阶段占比。不能因为没有时序日志就忽略这些已观察到的内容问题，也不能用答案中的知识细节代替原生工具触发证据。

**Golden Yolk 首问尚未唯一定位：**[工具描述](../../../config/voice-tools.yaml) 和[会话指令](../../../config/voice-qa-prompt.txt) 已覆盖产品名、运输、保质期、开封储存及追问，没有产品专属开关或先查 Black Pig 才启用工具的逻辑。固定 Golden WAV 以 `What about…` 开头，而 Black Pig 明确说 `What does the product documentation say…`，与工具说明及示例更接近。对原始 PCM16 WAV 按不重叠 20 ms 帧计算 RMS，以 RMS < 160 判低能量并合并连续帧，仅观察至少 0.4 秒的区间：Golden 全长约 8.208 秒，中间约 2.16–3.12 秒有低能量停顿，Black Pig 全长约 9.696 秒、没有达到 0.4 秒的中间停顿。该声学检查不等同 RNNT blank 或模型 EOU。上下文敏感和提前分轮是合理假设，不是已证实的根因；强制 EOU 默认 3.2 秒也不阻止模型提前产生 `model_native` BOS。先 Black Pig 后 Golden 同时改变历史上下文和轮次状态，不能据此证明 CueKB 冷启动。

Checking knowledge 是 ACK/UI 状态，不等于实际 HTTP 起点，也不能仅凭未显示就断定没有调用。预期应以同窗口的实际转写、`input.bou`、`agent.bos`（`model_native` / `forced_eou`）、`model_function_count`、`voice_tool_call_received`、`native_tool_arguments` 和 CueKB trace 区分：没有模型调用、已调用但未显示状态、或停顿后指代/产品名丢失。现有第 3/4 节入口已采集时间边界和 query，`backend_stages` 给出接收/ACK/注入边界；无需让操作者挑选原始日志或新增模型调用。若实际转写完整、工具目录身份正确但独立首问没有模型 function，定位应停留在 VoiceChat 原生选工具/分轮，而非 CueKB 搜索速度。

另一个已确认的边界是：`infer` 在抽取/注入期间返回静音的分支早于保存本次真实音频。因此若文件尚未发送完就开始工具抽取/注入，期间后半段音频会被跳过；正常麦克风打断也受此影响。它不能单独解释“从未触发工具”的 Golden 首问，但核对停顿前后是否分轮时必须同时检查此边界，不能以连接保持活动推定音频已被模型消费。

**后续方向：**先按实际 query/覆盖情况减少无必要补查；保持完整事实、产品归属和必要条件，继续减少重复或无关的模型负载。VoiceChat 的根本性能优化需评估已有流式会话中批量消费 function-channel embeddings 的可行性，同时保持 KV cache、text/function 通道和步数对齐；不能未经真实 GPU 验证就把当前循环改为普通文本 prefill。缩短 ACK 尾部只能改善其中一段，不足以消除十多秒逐 token 注入。当前环境无运行的生产 API/VoiceChat 容器，本次不能给出最新每次调用的精确时间占比，也未声称已修复时延或 Golden 独立首问。

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

- CueKB V2 代码评估已确认真实 RRF、可选 reranker、审核归属、required 上下文及有界诊断机制。2026-10-25 用户报告已上线验证；本任务未获得该轮现场明细，时延和 Golden 独立首问仍未解决，内容/文档重处理/听音也不能由该反馈或合成契约样例推定通过。
- 旧版最近一次 `auto core` 已有单次正式回答、5 秒无额外回答和同连接 Hello 的正面证据；该样本不代表任意输入都不会重复。其时间与内容问题见第 4.3 节，不重复历史排障流水。
- 产品归属需有明确结构或审核映射；未知归属不能从模型 query 猜测。任意文档的跨段例外和复合属性完整召回没有无条件保证。
- 当前数字/条件检查是有限检查，英文数词、比例和事实归属不能由一个 answered 标记认证。`3:7` 口述及价格/重量/运输覆盖继续人工逐项核对；本轮不以新增答案模型掩盖失败。
- Hello/TCP 录音缺词按用户多次正常反馈收敛，不扩大为麦克风或全部语音识别准确率验收。
- 105 秒连接上限、固定 30 秒活动宽限及后台持续播报按用户决定保留/暂缓；本轮不修改 VoiceChat，不做后台或锁屏验收。

历史逐轮记录、旧设计和重复验证手册由 Git 追溯。外部 VoiceChat 源码、manifest 和接口快照是运行文件与协议资产，不是另一份操作手册。

## 9. 开发回归（开发者在开发环境执行）

生产操作者不执行本节。这里只列本仓库回归入口；CPU 夹具不代表真实 GPU/知识库/听音通过。开发环境需 Python 3.12、Node/npm；依赖版本沿用仓库固定文件，按实际修改选择测试。

2026-10-25 代码分析复核：累计证据/来源交付、原生批次、时间线、模板与注入摘要相关既有 CPU 契约共 35 passed；VoiceChat manifest 的 11 个文件 hash、本文新增本地链接及 `git diff --check` 通过。首次模板测试收集因环境缺少 Jinja2 失败，按既有 `requirements-dev.txt` 补齐固定的 `jinja2==3.1.6` 后通过，未修改依赖文件。只修改本文的分析与当前状态，没有部署或真实 GPU/时延复测。

本轮本地记录（2026-10-23）：Python 全量执行为 672 passed、9 failed。同步 V2 过滤字段断言，并把条件缺失用例改为明确由用户提供条件后，对失败项及数字/单位相关用例单独复核，11 passed；其中两项短时限测试隔离通过，未放宽运行超时。Ruff、生成契约一致性、Web 38 项单元测试、构建和 1 项来源展示/权限撤销浏览器回归通过。这些是本地应用与合成接口证据，真实 CueKB/GPU 及产品内容仍待上述部署验证。

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

本轮重点回归严格 V2 公开样例、旧响应拒绝、必要依赖不完整、未知/歧义归属、完整组预算、累计证据与权限撤销，以及有界诊断不泄漏原文。前端构建及来源显示回归需同步检查。实际执行数量以本次交付结果为准，不把历史测试数写成本轮执行。

模型结果 `partial` 不认证内容覆盖，也不表示没有省略可选邻居；该类省略见 `voice_evidence.omitted_optional_context_count`。实际提交大小以 `evidence_delivery.payload_bytes` 为准，包含终态指令和批次结清。选择阶段先预留这些开销，再按完整组取舍，不能依赖提交时报错后截断正文。

本轮不改外部 VoiceChat 文件，不为 V2 接入要求重装其 CPU 测试依赖或重跑正常 RNNT。以上检查均不代表真实 CueKB 内容、GPU 首包、麦克风或扬声器验收；现场只按第 3、4 节执行一次并回传精简结果。
