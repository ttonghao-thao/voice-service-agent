# VoiceChat 原生工具选择故障：定位进展与验证步骤

更新日期：2026-10-18。本页只维护故障一：产品/企业知识问题未稳定触发 VoiceChat 原生工具。故障二是 `hello.wav` 的 `Hello` 开头被识别为 `o` 或只保留 `how are you today`；该问题已确认独立存在，本期暂缓，不用它解释工具选择结果，也不在本页增加 ASR 调参。

## 1. 多轮定位进展

| 轮次 | 已观察证据 | 可以确认 | 仍不能确认 |
| --- | --- | --- | --- |
| 早期错误名称 | API 审计出现 `emitted_tool=search_cueb`、`reason_code=VOICE_TOOL_UNKNOWN`、`stage=selection` | 模型/VoiceChat 至少一次产生了 function call；API 在名称校验阶段拒绝错误名称，未进入参数或 CueKB | 错误名称由 Prompt 注册、VoiceChat 转换还是模型生成造成 |
| 20 轮未调用 | 产品录音重复验证均未出现工具；控制用问候和 TCP/UDP 能直接回答 | 音频连接和普通回答路径可工作；产品选择问题可稳定复现 | 未调用发生在模型选择还是 function decoder 输出阶段 |
| 注册与预发送 | API `voice_registration_confirmed tools_count=1`；VoiceChat 的 session update、Jinja Prompt 准备、预发送和完成均为 21/21，失败为 0 | API 向 VoiceChat 注册了一项工具，Jinja 与 prompt pre-send 路径执行 | `tools_count=1` 不证明最终 Prompt 中名称/内容正确，也不证明 Nano 9B 收到的字节一致 |
| 跨层诊断编码 | `audio_server.py` 记录最终 Prompt 工具名、字符数、SHA-256；Triton backend `model.py` 记录收到的相同字段及模型函数通道解码名称 | 可把注册/Prompt 构造、VoiceChat→Triton 传输、Nano 9B/function decoder、WebSocket 封装分层判定 | 需要当前部署同一时间窗口的三类现场行才能下结论 |
| 完整英文名称 | 应用、目录、模板、可信 implementation 和用例统一为 `search_knowledge_base`；旧缩写和拼写不再兼容 | 后续不会由 API 模糊纠错掩盖模型原始输出 | 名称统一本身不能保证模型主动选择工具 |
| 一次已进入 CueKB 的现场轮次 | 产品 case 曾出现 `selection=passed`、`actual_tools=['search_knowledge_base']`、CueKB trace；随后因语音证据过滤/结束边界超时未完成 | 至少一个部署轮次曾从原生选择进入真实 CueKB | 该结果不能覆盖当前“未触发”的部署和新轮次，也不能证明选择稳定 |
| 2026-10-18 现场轮次 | 最终 Prompt 与 Triton 收到的 Prompt 均为 `search_knowledge_base`、7082 字符、SHA-256 `01e7a462...7493`；sequence `4205027636` 解码出正确工具名；API selection 通过并取得 CueKB trace | 注册、Prompt 构造/传输、Nano 9B function 解码、VoiceChat→API 工具事件和 CueKB 请求均已通过；原来的“未触发工具”不再是当前阻塞点 | 同一 sequence 在约 76 秒内重复解码 15 次，产品 case 在 90 秒超时；需确认工具结果停在 API 提交前还是 VoiceChat/Triton 接收后 |
| 2026-10-18 13:13 现场轮次 | CueKB 102 ms 返回 5 个命中；旧 `context:5` 聚合计数显示 5 项都在上下文完整性阶段被拒绝，但不能区分上游 `context_truncated` 与应用 `context_omitted`。API 只启动一次 bridge/CueKB，却收到 15 个不同 call ID，其中 14 个没有新 input；API 提交 15 个结果 | 证据适配器把两类 context 状态合并诊断；VoiceChat 每次重复解码都生成新 call ID；旧摘要的 `MODEL_FUNCTION_WRONG_NAME` 是解析误报，模型实际名称仍正确 | 本期修复同输入规范调用去重/结果复用、按当前 sequence 汇总和工具名字符边界，并拆分 context 诊断 |
| 2026-10-18 14:27 现场轮次 | API `received=1 unique_calls=1 unbound=0 results_submitted=1`；VoiceChat `duplicates_coalesced=2 response_timeouts=0`，两份诊断均通过；CueKB 102 ms 返回 5 个命中，但旧聚合计数仍显示 5 项上下文拒绝 | 重复调用问题已修复；工具注册、Prompt、模型 function、API bridge 和 CueKB 检索完整通过。剩余根因限定在语音证据的截断/省略准入，但旧计数不能判定现场具体分支 | 同时安全处理两种分支并拆分计数，避免先后部署多个猜测性修复 |
| 2026-10-19 00:03（北京时间）现场轮次 | `cuekb_evidence=unassessed voice_evidence=passed candidates:5 selected:3 truncated_ok:5 limit:2`，证明截断准入已修复。随后出现 `VOICE_ANSWER_TIMEOUT/VOICE_TOOL_REQUIRED`；模型解码 3 次、VoiceChat 仅合并 1 次；API `received=1 unique_calls=2 unbound=1 results_submitted=2` | 5→3 使 evidence envelope 标为 `partial`，模型改写 query 重调；现有完整参数签名把变体当成新调用。第二份无用户输入的失败结果破坏回答授权；旧验证器又误报 `INCOMPLETE` | 原生语音 top_k 与 3 个证据槽位一致；单工具会话按当前输入和工具名合并参数变体；诊断按 input 统计并把该错误归为 `VOICE_RESPONSE_COMPLETION` |
| 2026-10-19 00:45（北京时间）现场轮次 | `candidates=selected=3 limit=0`；VoiceChat `function_parsed=2 new_calls=1 function_result=1 duplicates_coalesced=1`；API `received=inputs=unique_calls=1 unbound=0`，两份诊断通过；产品轮仍以 `VOICE_ANSWER_TIMEOUT/VOICE_TOOL_REQUIRED` 结束 | top_k 对齐、参数变体合并、真实 CueKB 与结果回送均已通过。剩余故障是重复 function call 产生的新 response ACK 消耗了唯一回答 continuation grant；ACK 结束未归还 grant，真正答案随后被当作未授权输出拒绝 | Gateway 在已绑定工具结果的精确固定 ACK 结束后恢复同一个 grant；摘要拆分等待合并与缓存结果重放，重新部署 API 验证最终回答完成 |
| 当前状态 | continuation grant 修复已编码，等待重新部署 API | 截断证据、检索条数和 API 工具调用无需再调整；本轮不改 VoiceChat 运行文件 | 产品 case 需无 timeout/tool-required；完整四用例通过，API 仍为每个产品 input 一个调用 |

当前判断顺序固定为：先证明 API 当前目录和用例正确，再证明 VoiceChat 最终 Prompt 与 backend 收到内容一致，再看模型 function 通道是否输出，最后核对 API 提交工具结果和 Triton 收到结果。旧轮次、相邻日志或单独的 `tools_count=1` 不代替同一窗口证据。

## 2. 本项目与 VoiceChat 的修改边界

本项目 API 镜像内的 `/app/scripts/run_native_cuekb_validation.py` 是唯一业务验证入口。它完成 readiness、capabilities、镜像材料、四段 WAV、Portal、原生工具、CueKB 和语音传输判定；详细结果写 `/tmp`，stdout 只有定位字段。

VoiceChat 修改以本目录完整文件为准：

| 文件 | 与故障一有关的修改 |
| --- | --- |
| `s2s/prompt_template.jinja` | 单工具调用规则、完整 `search_knowledge_base` 名称和消费品/商业事实示例 |
| `s2s/audio_server.py` | 记录最终 Prompt 的工具名、字符数和 SHA-256；记录 function 新调用、合并、结果与输出边界诊断；单工具会话按用户输入和工具名合并参数改写并复用首个结果 |
| `opt/tritonserver/backends/nemotron-voicechat/model.py` | 记录 Triton 收到的 Prompt 名称/hash，以及同 sequence 的模型 function 通道解码名称 |
| `docker-run.md` | 派生镜像、挂载、环境变量和启动后 hash 核对 |

完整文件 hash 由 [snapshot-manifest.json](snapshot-manifest.json)维护。修改这些 VoiceChat 文件时，必须同步完整文件、manifest 和 [容器启动命令](docker-run.md)，重新构建并重新创建 VoiceChat 容器；仅重启旧容器不会更新镜像或环境变量。

## 3. 先运行项目单入口

以下命令在宿主机执行，不复制脚本、JSON 或 WAV：

```sh
COMPOSE='docker compose --env-file .env -f deploy/compose.production.yaml'
API_CONTAINER=$($COMPOSE ps -q api)
test -n "$API_CONTAINER"

set +e
docker exec "$API_CONTAINER" \
  python /app/scripts/run_native_cuekb_validation.py
VALIDATION_EXIT=$?
set -e
echo "host_exit_code=$VALIDATION_EXIT"
```

预期只出现预检、时间窗口/会话 ID、四条 case、最终分类和 `host_exit_code`。新验证器把 CueKB 充分性、检索和语音证据分开显示；本次修复部署后的产品 case 应接近：

```text
case=black_pig_crisp_heart expected=search_knowledge_base actual=search_knowledge_base selection=passed arguments=manual_review_required retrieval=passed cuekb_evidence=unassessed voice_evidence=passed evidence_filter=candidates:3,selected:3,truncated_ok:3,truncated_rejected:0,omitted_ok:0,omitted_rejected:0,non_ascii:0,limit:0,budget:0 speech=passed reason=- portal_error=- ... input_ids=I trace_ids=T
validation=passed fault=PASS focus=human_transcription_and_spoken_faithfulness error_type=- exit_code=0
host_exit_code=0
```

保存输出中的 `window_start`、`window_end`、`conversation_id` 和失败 case 的 `input_ids`。不提供 `/tmp/native-cuekb/report.json`，除非这些核心字段互相矛盾。

## 4. 核对当前 VoiceChat 镜像和运行文件

沿用现场容器名，从宿主机执行：

```sh
VOICE_CONTAINER=nemotron-labs-voicechat

docker exec "$VOICE_CONTAINER" sh -c '
  test -r /opt/voice-validation/nemotron-voicechat-client.py &&
  test -r /opt/voice-validation/native-cuekb/black_pig_crisp_heart.wav &&
  sha256sum \
    /opt/tritonserver/backends/nemotron-voicechat/model.py \
    /s2s/audio_server.py \
    /s2s/prompt_template.jinja &&
  printf "env=" && printenv USE_JINJA_TEMPLATE_PROMPT
'
```

预期仅有三条 hash 和 `env=1`：

```text
c91b3af5b41f515fc1dbfaf7fdbec4ae528a4989f90a144829d2491a9b805990  /opt/tritonserver/backends/nemotron-voicechat/model.py
f28ada577f84956c46b3ce04f4cbebf9c0e6405773ce4486105aef97958bb17b  /s2s/audio_server.py
a71230010295a363a0fde0e0d86c4786aeb4ef5f458cf4d75ddf0926895bcdb7  /s2s/prompt_template.jinja
env=1
```

任一 hash 或环境不一致时停止模型归因，按 [docker-run.md](docker-run.md)重建并重新创建容器。

## 5. 用同一个 Python 入口归纳 VoiceChat 日志

VoiceChat 主进程历史只在 Docker stdout 中，容器内没有可由 `docker exec` 读取的标准日志文件。因此宿主机只负责按第 3 节的时间窗口读取 stdout，再把该窗口通过标准输入送入 API 镜像中的同一个 Python；脚本不会回显原始日志。执行前停止其他验证流量，保证窗口只包含这一条顺序会话。

将下面两个时间替换为单入口输出的精确值：

```sh
WINDOW_START='2026-10-18T00:00:00+00:00'
WINDOW_END='2026-10-18T00:05:00+00:00'

set +e
docker logs --since "$WINDOW_START" --until "$WINDOW_END" \
  "$VOICE_CONTAINER" 2>&1 \
  | docker exec -i "$API_CONTAINER" \
      python /app/scripts/run_native_cuekb_validation.py \
        --summarize-voicechat-log
VOICE_DIAGNOSTIC_EXIT=$?
set -e
echo "voicechat_exit_code=$VOICE_DIAGNOSTIC_EXIT"
```

正常工具调用只输出五条诊断和退出码：

```text
voicechat_prompt count=1 tools=search_knowledge_base chars=N sha256=H
backend_prompt sequence=S tools=search_knowledge_base chars=N sha256=H match=passed
model_function status=passed count=1 unique_sequences=S tools=search_knowledge_base
voicechat_events function_parsed=P new_calls=N new_call_inputs=I function_result=R duplicates_coalesced=D results_replayed=Y response_timeouts=W audio_done=A
voicechat_diagnostic=passed fault=NONE
voicechat_exit_code=0
```

实际第三行会输出当前 backend Prompt 对应 sequence 的 `count=N unique_sequences=S`，不会混入同一日志窗口的旧 sequence。第四行要求 `new_calls=new_call_inputs` 且 `new_calls+duplicates_coalesced+results_replayed=function_parsed`；`duplicates_coalesced` 表示首个结果尚未交付时合并，`results_replayed` 表示结果已送入 Triton 后模型再次调用并重放缓存结果。完整四用例通常有两个产品 input，所以允许两个新调用，但每个 input 只能有一个。`R>=N`、`W=0`、`A>=1`；缓存结果重放可使 `R>N`。`voicechat_diagnostic=passed` 表示 Prompt、function 选择、单输入去重和工具结果接收链完整，最终回答仍以第 3 节 case 为准。

当前故障若发生在模型/function decoder，前两条仍应匹配，而第三条为：

```text
model_function status=absent count=0 unique_sequences=- tools=-
voicechat_diagnostic=failed fault=MODEL_FUNCTION_ABSENT
voicechat_exit_code=1
```

只需回传第 3 节 Python 七行及 `host_exit_code`，再回传本节五行诊断及 `voicechat_exit_code`。不要回传完整 `docker logs`。

## 6. 用同一个 Python 入口归纳 API 工具生命周期

使用与第 3 节完全相同的 `WINDOW_START`、`WINDOW_END` 和 `API_CONTAINER`，在宿主机执行。脚本只保留工具生命周期、CueKB 状态和 trace，不输出 query、证据正文或工具结果正文：

```sh
set +e
docker logs --since "$WINDOW_START" --until "$WINDOW_END" \
  "$API_CONTAINER" 2>&1 \
  | docker exec -i "$API_CONTAINER" \
      python /app/scripts/run_native_cuekb_validation.py \
        --summarize-api-log
API_DIAGNOSTIC_EXIT=$?
set -e
echo "api_diagnostic_exit_code=$API_DIAGNOSTIC_EXIT"
```

正常的一次工具调用只输出三条摘要和退出码：

```text
api_tool_flow conversations=C received=2 inputs=2 duplicate_inputs=0 unique_calls=2 bridges_started=2 bridges_finished=0 bridges_failed=0 unbound=0 results_submitted=2 ack_grants_restored=G
api_cuekb responses=2 statuses=ok hits=3,3 traces=T1,T2 durations_ms=N1,N2
api_diagnostic=passed fault=NONE
api_diagnostic_exit_code=0
```

判定方式：

- `results_submitted=0`：结果停在 API 内，查 `CUEKB_RESPONSE_ABSENT`、`TOOL_RESULT_NOT_SUBMITTED` 或 bridge failure。
- `results_submitted>0` 且 VoiceChat `function_result=0`：WebSocket 已从 API 写出，但 VoiceChat 未把结果送入 Triton，查 `audio_server.py` 的 function output 队列与 inference 调度。
- `function_result>=1` 且 `response_timeouts>0`：Triton 收到过结果，但 function 状态机没有及时进入或保持 `process_response`。
- `duplicate_inputs>0` 或 `unbound>0`：同一个产品 input 产生多次新 function 事件，或工具事件已没有可绑定输入。完整四用例允许 `received=2 inputs=2`，分别对应两个产品录音。
- `ack_grants_restored>0`：模型重调后产生了新的固定 ACK，Gateway 已在该 ACK 结束时把同一个 continuation grant 归还给最终答案；没有重调时该值可以为 0。

只回传三条摘要和 `api_diagnostic_exit_code`。若摘要与第 3 节报告矛盾，才需要按 `conversation_id` 提取对应的单条原始生命周期日志。

## 7. 结果判定

| 最小输出 | 判定与下一步 |
| --- | --- |
| `preflight=failed` | API 配置、目录或镜像材料问题；尚未验证 VoiceChat 模型 |
| `INPUT_NOT_OBSERVED` | 产品输入没有形成 input ID；停在音频/ASR 传输层，不判断工具选择 |
| `voicechat_prompt absent` | session update/Jinja/VoiceChat 会话入口未进入，核对当前容器与时间窗口 |
| Prompt 工具名错误 | API→VoiceChat 注册或 Prompt 构造问题 |
| Prompt 正确但 `backend_prompt ... match=failed` | VoiceChat→Triton 传输/转换问题 |
| 两层 Prompt 匹配且 `MODEL_FUNCTION_ABSENT` | Nano 9B 未输出有效 function call，或 backend function decoder 未形成 JSON；下一步只采同 sequence 的 function token 诊断 |
| `MODEL_FUNCTION_WRONG_NAME` | 模型/function decoder 输出了其他名称；用同 sequence 原始名称定位，不在 API 做模糊纠错 |
| model function 正确但 API `TOOL_NOT_CALLED` | VoiceChat WebSocket function 事件封装/发送或 API 接收关联问题 |
| API selection 通过但 `CUEKB_RETRIEVAL` | CueKB 没有返回可用命中或服务失败，查对应 trace 的检索状态 |
| `retrieval=passed voice_evidence=unavailable` | CueKB 已命中；同时查看 `cuekb_evidence` 与 `evidence_filter`。`truncated_rejected` 表示缺少完整主命中块证明，`omitted_rejected` 表示应用省略后缺少充分性/完整块证明；非 ASCII 或字节预算也可导致拒绝 |
| API `results_submitted=0` | 工具结果未从 API 写回 VoiceChat，继续查 API bridge/delivery |
| API `results_submitted=1`、VoiceChat `function_result=0` | 结果越过 API，停在 VoiceChat function output 队列或 Triton inference 输入 |
| `function_result>=1`、`response_timeouts>0` | 结果已进 Triton，但 backend function 状态机未正常消费 |
| `VOICE_RESPONSE_COMPLETION` | 工具选择与回答结束分开处理，检查 function result 和 audio done/EOS 计数 |

若两层 Prompt 匹配且 function 缺失，下一轮证据必须限定到该 `sequence=S`，只输出 function token 类型/计数和首个非 PAD 位置；不要输出 Prompt、query、工具参数、音频正文或全量 token 数组。

## 8. 故障二暂缓边界

`hello.wav` 已出现句首 `Hello` 丢失，但加前置静音后的 `how are you today` 可以完整识别。该现象记录为独立 ASR 首差异，不修改本期产品用例、不调整工具 Prompt，也不以问候转写问题否定产品 case 的工具选择证据。恢复定位时从同 sequence 的输入样本计数、RNNT raw token 和 final transcript 开始，另立验证窗口。
