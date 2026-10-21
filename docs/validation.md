# 统一验证操作手册

这是本项目唯一的验证文档，适用于英文（en-US）全双工知识客服：Web → API → NVIDIA VoiceChat → CueKB → 有来源的语音回答。所有操作、回传内容、判据和当前未解决项均在本页，不需要打开其他文档。

**操作者只执行命令和回传精简输出，不负责判断原因、选择日志、找会话 ID 或调整参数。** 命令失败也保留输出，交给开发者定位；不要反复重跑、提高检索次数、延长会话或删除 WAV 的停顿来获得通过。

## 1. 本次范围与执行顺序

当前首先验证一个完整知识问答闭环，特别是 Golden Yolk 第二段。固定执行一次自动验证、一次 Web 文件验证。需要完整四文件对照时，执行第 5 节；不要同时执行两套方案。

输入唯一来源是本仓库 `artifacts/native-cuekb/` 的 `cases.json` 和四个 WAV。API 镜像内对应 `/app/validation/native-cuekb/`。所有 Python 在容器内执行；宿主机只需 Bash、Docker Compose、Git 和常用 shell 工具。**不使用 docker cp，不在容器创建输入目录，不使用历史 ZIP。** 输出目录由脚本自动创建，API 的只读根文件系统不受影响。

宿主机执行入口是 `bash scripts/validate_deployment.sh`；它自动调用 `docker exec`，寻找 API 容器、检查固定文件、提取本次时间窗口和会话、采集匹配的 VoiceChat sequence、查询已保存答案。不会新增模型请求或 CueKB 请求，只有 `auto` 会发送测试语音。默认 VoiceChat 容器名为 `nemotron-labs-voicechat`。

## 2. 更新本次验证工具并准备环境

所有命令在**宿主机项目根目录**执行，保持同一主机、目录和用户。先结束已有测试通话；本次采集期间不要有其他测试用户通话。

本次统一入口增加了 API 镜像内的精简采集器，因此需要重新制作 API 镜像。下面同时构建 Web，避免浏览器对照使用旧前端。假定 `.env` 和既有部署已经可用；不改变数据库结构、工具预算和 VoiceChat 推理逻辑。

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
  bash scripts/validate_deployment.sh check
else
  printf 'deployment=failed local_build_log=%s\n' "$LOG"
fi
SH
```

正常会输出 `deployment=passed`、`preparation=passed` 和容器镜像/启动时间。运行文件或材料不一致时输出 `version=failed file=... expected=... actual=...`；环境不完整时输出 `collection=failed stage=...`。**失败后停止本次验证，只回传这些行，不自行替换文件或选择日志。** 如需构建失败详情，开发者后续只索取该阶段的必要报错，不能默认索取完整构建日志。

VoiceChat 已部署本仓库完整快照是本次验证前提；该检查会逐个比对六个运行文件，包括 audio_server、模板、model、loader、runtime_setup 和 data_types，并检查 Jinja/诊断开关。若不匹配，交给开发者修正部署，不在验证期间修改推理代码或猜测 GPU、模型挂载参数。本次文档与采集器更新不要求重建已匹配的 VoiceChat。

## 3. 操作一：自动验证一次 Golden Yolk

```bash
bash scripts/validate_deployment.sh auto golden_yolk
```

执行期间等待命令自行结束，通常需一至两分钟。**把本命令终端输出全部回传即可，它已经是精简结果。** 不要另外运行 docker logs、cat report.json 或贴完整日志。

入口会在全新连接中发送原始 WAV，保留中间停顿；完整发送后等待最新输入的非 ACK 回答完成。120 秒是验证器等待上限，不改变服务端会话时限。每次使用独立报告路径，失败不会误读上一次报告。即使验证失败，仍尝试采集这次的摘要。

终端输出包括：版本准备、`preflight`、`case`、`validation`、退出码、`session`、API 预算与检索、`backend_injection`、每输入 `input_timing`、有界 `backend_stages`、保存的输入/回答/业务状态、少量来源节选。`local_artifacts` 指向本次宿主机私有目录；原始日志只保存在这里，不直接打印。

## 4. 操作二：Web 上传同一 WAV 一次

1. 浏览器强制刷新门户，结束旧通话。打开 F12 → Console，开启 Preserve log，过滤 `voice_file`。保持门户前台，不切后台、不锁屏。无需在 Network 中寻找 messages 或手填 ID。
2. 在宿主机执行：

   ```bash
   bash scripts/validate_deployment.sh web-start golden_yolk
   ```

3. 门户选择 **Audio file → Choose WAV**，选择仓库 `artifacts/native-cuekb/golden_yolk.wav`，点击 **Start file call**。不要点 Stop playback/Cancel search，不要在 WAV 中间停顿时结束通话。
4. 从点击 Start file call 起等待 **120 秒**，再点 **End call**。即使页面提前报错，也不重新开始、不重试；把页面提示保留下来。固定等待是为了不要求操作者判断回答是否完整，不是要求服务端必须运行满 120 秒。
5. 在宿主机执行：

   ```bash
   bash scripts/validate_deployment.sh web-end
   ```

6. 回传以下三项：本次 `web-start`/`web-end` 的终端输出；Console 过滤后的 `voice_file` 行；下列简短观察记录。无需导出 HAR、完整 messages、访问令牌或全部服务日志。

   ```text
   test_file=golden_yolk.wav
   heard_audio=有声音 / 没声音
   page_error=页面提示原文（没有则写无）
   ```

同一时间窗口最多自动汇总四个会话，逐个标注 CID；无法匹配时明确输出 unavailable，不挑一个会话冒充本次结果。保存的转写和业务状态由采集器只读获取。音频内容与来源由开发者分析；本页不要求操作者裁决答案是否准确。

`voice_file` 用于补充浏览器的发送、首包和播放开始时刻。服务端首包时间不能替代浏览器开始播放，更不能替代实际扬声器听音。如果终端阶段没有延迟而浏览器仍等待，开发者才依据这些行进一步定位。

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
| 回答是否来自知识库 | turn 的工具、提交状态、引用与 trace，加回答正文/来源节选；数量只能证明关联，节选不能证明所有事实受支持 |

时间基于 Docker 日志观察时刻，不是 GPU 内部精确计时。多次检索间隔可能包含 ACK 和模型决策；没有对应 response 的完成记录时，不把未分类音频当成最终答案。摘要明确标注 truncated/omitted 的部分不作完整证据。需要某条引用或回答的完整内容时，开发者应给出**只提取该 input/response/citation** 的只读命令，不能转而要求全量日志。

### 放行标准

- 普通问候和 TCP/UDP 可以无工具、无来源；产品问题应按注册工具进入 CueKB。
- 每 input 实际检索不超过默认 2 次，无 HTTP 自动重试；精确重复和预算耗尽进入终态；终态后的新调用明确失败，不无限回放。
- 每完整批次一次聚合注入；累计来源与权限/版本检查保留；后续真实新输入拥有新预算。
- 最新输入的非 ACK 文本、非空音频与结束事件齐全，且产品答案提交为 answered、有引用。终态已提交、音频已发送、工具诊断 passed 均不是答案成功的替代条件。
- 内容应回答有据事实、保留数字/单位/限定条件；无法确认的部分明确说明“本次检索材料无法确认”。不能声称事实不存在或整个公司知识库没有该信息。诚实部分回答可以通过，但内容由开发者核对，必要时请求对应音频。
- Web 与自动验证采用同版 WAV、相同 API Relay/工具目录/VoiceChat/CueKB；原始 24 kHz 单声道 PCM16、80 ms 帧，末帧补零，ready 后发送，EOF 后继续静音。浏览器真实播放与回执仍与自动脚本不同，不能宣称逐毫秒等同或模型选择必然一致。

### 后续验收，不混入当前故障轮次

同连接多轮/指代、麦克风打断、等待工具交互、断线轮换恢复、权限撤销和容量仍需独立验收，不因单个知识答案通过而放行全产品。需要多轮/打断时，沿用 `web-start`/`web-end` 采集流程，开发者在执行前提供固定英文问题、说话顺序和等待秒数；不得要求操作者临场决定如何触发问题。自动四文件新连接测试不替代这些专项。

同一假设两次无新增证据即停止；三种假设仍无进展，报告环境、设计或技术路径阻塞。不得盲目重复、扩大预算、调整 WAV 或将诊断通过写成业务通过。

## 8. 当前事实与未解决事项

截至 2026-10-21，以下是历史证据摘要，**不是本次文档整理后的新部署结果**：

- Black Pig 自动单 case 曾现场通过：2026-10-20 15:20 UTC，CID `6d9dfaa1-1f84-4df6-94dc-65a3f711bdf3`，2 次查询、10 条来源、终态 1、终态后调用 0、answer_complete=passed、answer_status=answered。不能推导 Web/所有产品/连续多轮通过。
- Web 最新对照确认 Black Pig 出现 Checking knowledge；先前“始终不调用”的概括已不成立。回答准确性和重复响应仍需分析。
- Golden Yolk 的排队调用曾在终态回填前约 195 ms 到达，被旧实现按出队状态误判；显式批次与聚合结果修正已完成本地协议测试，真实第二段闭环待验收。
- 已观测 CueKB 单次约 0.1 秒，不能把估算的约 30 秒等待全部归因于检索。关联后的时间线尚需现场证据。
- hello 开头缺词按用户要求暂缓；不为本次闭环另开调试分支。

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
