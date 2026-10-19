# 英文全双工与 CueKB 验证

本期路径为 Web ⇄ API relay ⇄ VoiceChat；VoiceChat 原生调用 `search_knowledge_base` 后，API 查询 CueKB 并按原 call_id 回填。普通回答和多轮上下文由同一 VoiceChat 连接承载。

## 1. 部署与输入

配置为 `VOICE_PROVIDER=nvidia`、`AGENT_PROVIDER=disabled`、`QA_EXECUTION_MODE=dual_tools`、`QA_ANSWER_POLICY=general_qa`。新建会话使用当前策略，旧 knowledge_required 会话不能作为本期验收。

API 镜像内置脚本及 `artifacts/native-cuekb/` 的 cases.json、hello.wav、tcp_udp.wav、black_pig_crisp_heart.wav、golden_yolk.wav；镜像路径为 `/app/validation/native-cuekb/`。输入更新须重建镜像。所有 Python 命令在容器执行，不使用 docker cp，不手动创建输入目录。脚本自动在可写 `/tmp/native-cuekb/` 生成报告和回答音频；`--audio-dir` 是回答输出目录。

本次按输入限制检索及累计证据的改动只需要重建 API 镜像；已部署的 Web、VoiceChat 无需更新或重建。VoiceChat 独立只读步骤见 [验证说明](voicechat/native-tool-selection-validation.md)。旧版本升级仍按原部署流程处理迁移和工具目录。

在宿主机项目根目录构建并仅重建 API（复用 `.env` 当前 IMAGE_TAG，避免影响同标签的 Web 镜像）：

```sh
API_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-api:/ {p;q;}')
test -n "$API_IMAGE" && docker build -f deploy/Dockerfile.api -t "$API_IMAGE" . &&
  docker compose --env-file .env -f deploy/compose.production.yaml up -d --no-deps --force-recreate api
```

`VOICE_MAX_RETRIEVALS_PER_INPUT` 默认 2，可在 `.env` 设为 1 或 2；生产 Compose 已通过 env_file 传入。沿用默认值不必编辑 `.env`。修改后须重建 API 容器；它不受旧 `QA_MAX_RETRIEVAL_CALLS=1` 控制。健康检查通过后运行下面命令。回滚/镜像分发沿用部署手册；上述命令会替换本机当前 API 镜像标签。

本期原生语音路径关闭 CueKB HTTP 自动重试：超时、网络异常、响应中断或 5xx 均只发送一次请求，进入明确失败处理，不以“可能已送达”的请求再次尝试。默认两次预算也约束实际 HTTP 请求数；其他路径的重试行为不变。计数按同一连接的 input_item_id，不把 ACK 或新 call_id 当成新问题；实际插话的输入归属仍需要 §4 麦克风验收。

## 2. 从宿主机运行一次验证

在宿主机项目根目录执行：

```sh
API_CONTAINER=$(docker compose --env-file .env -f deploy/compose.production.yaml ps -q api)
test -n "$API_CONTAINER"
if docker exec "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py; then
  VALIDATION_EXIT=0
else
  VALIDATION_EXIT=$?
fi
printf 'host_exit_code=%s\n' "$VALIDATION_EXIT"
```

默认在 API 容器访问 `http://127.0.0.1:8000`，Origin 读取容器 PUBLIC_ORIGIN。预检通过后，四条 WAV 在同一个 conversation/VoiceChat 连接中顺序发送，WAV 后持续送静音推动推理。

预期：

- `preflight=passed ... cases=4`。
- 问候、TCP/UDP 两行 `actual=- selection=passed`。
- 两个产品 case 的工具为 search_knowledge_base，`retrieval=passed voice_evidence=passed`。
- 四行均有 `answer_complete=passed`，最终 `validation=passed ... exit_code=0`。
- 产品行 `retrieval_executions<=retrieval_limit`（默认 2），`post_terminal_calls=0`；`duplicate_queries` 可以为 1，相同查询不再次访问 CueKB。
- `terminal_results_submitted=1` 表示已提交结束检索的结果，不证明模型服从。第一次查询即正常回答时可以为 0，无需人为耗尽预算。
- 第二次查询为空时，`latest_retrieval=not_found` 可以与 `retrieval=passed cumulative_sources>0` 并存：前者是最近一次查询，后者表示累计仍有可用证据。原始失败/空结果保留在审计中。
- `arguments=manual_review_required` 仍需核对名称/指代语义，不代表已确认正确。

`answer_complete` 要求当前 input 对应的非 ACK 完整转写、非零回答音频和音频结束。`speech=passed` 仅表示收到过音频，ACK 也可能产生该结果，不能单独验收。

只需回传四条 `case=...` 行和 `validation=...`。若只出现三条，第四条尚未执行，不能算四用例通过。报告 JSON 和 WAV 留在容器内，无需全量回传。

## 3. 失败时提取精简诊断

验证入口将时间窗口写入报告。宿主机取得变量时仍由容器 Python 读取：

```sh
WINDOW_START=$(docker exec "$API_CONTAINER" python -c 'import json; print(json.load(open("/tmp/native-cuekb/report.json"))["window_start"])')
WINDOW_END=$(docker exec "$API_CONTAINER" python -c 'import json; print(json.load(open("/tmp/native-cuekb/report.json"))["window_end"])')
VOICE_CONTAINER=nemotron-labs-voicechat

docker logs --since "$WINDOW_START" --until "$WINDOW_END" "$API_CONTAINER" 2>&1 |
  docker exec -i "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py --summarize-api-log

docker logs --since "$WINDOW_START" --until "$WINDOW_END" "$VOICE_CONTAINER" 2>&1 |
  docker exec -i "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py --summarize-voicechat-log
```

只回传摘要。API 必须同时看到调用、原调用回填和对应输入的非 ACK 回答完成；`TOOL_ANSWER_NOT_COMPLETED` 不能再显示为局部成功。VoiceChat 摘要仅检查工具传输，不证明模型已完整消费结果或已完成回答。

`LEGACY_TOOL_REPLAY` 表示 VoiceChat 仍运行旧的合并/重放/隐藏 ACK 补丁，应核对部署文件。API 的同输入相同参数缓存是显式受限行为，不是 VoiceChat 内部重放。`api_search_budget` 提供本窗口累计检索、重复查询、终态结果和终态后调用数量；判定每个问题的上限以 case 行为准。

`TOOL_LOOP_AFTER_TERMINAL` / `VOICE_TOOL_LOOP` 表示模型在收到终态结果后仍调用：API 不再检索，关闭异常连接结清调用。该结果是查询受控但业务失败，不能验收；VoiceChat 摘要此时可能显示最后一条调用没有回填，因为 API 已明确终止连接。保留这些失败信息，不无限重放终态结果。

## 4. 同一通话的麦克风验收

固定 WAV 通过后，在 Web 门户保持同一通话，依次说：

1. “Hello, how are you?”
2. “Please introduce Black Pig Crisp Heart.”
3. “How should I store it?”
4. “Thank you. What is the difference between TCP and UDP?”
5. 回答播放期间自然插话，询问 Golden Yolk。

预期：无需每轮重新开始；产品追问能利用会话上下文并查询相应知识；普通问答仍可直接回答；收音与播放同时工作。实际模型打断、完整听音和口述忠实性必须记录为现场结果，不能由 CPU 回归推定。

仅回传：是否重连、追问是否识别正确产品、产品回答是否完整、插话是否被接收及对应错误码。hello.wav 缺 Hell 问题继续暂缓，不通过改写转写掩盖。

## 5. 会话轮换与结果注入

ACK 的 audio.done 不是知识回答完成。API 的空闲轮换必须同时满足没有待观察的知识回答、工具已结清、输出队列已发送完；不会仅因 ACK 已结束而轮换。原有连接最大时间及 30 秒宽限仍保留，持续无回答可最终报 VOICE_SESSION_EXPIRED，不代表业务成功。

04:56 UTC 现场已确认 10 次检索/回填、9 次结果队列注入完成，产品回答仍未完成。该循环的根因尚未确定，不能用本次轮换修正宣称已解决。进一步只读核对见 [VoiceChat 结果注入复核](voicechat/native-tool-selection-validation.md#7-2026-10-19-结果注入复核)。已有镜像可以执行该检查，无需为获取信息再次部署。

## 6. 来源覆盖与布尔值兼容

04:56:50 UTC 的第一组真实工具响应保留了三条来源文本，但 `partial:false` 被 VoiceChat 数词转换为 `partial:"zero"`。API 已将发给 VoiceChat 的该标记改为文本 "true"/"false"，内部审计仍是布尔值。无需修改 VoiceChat。

Black Pig WAV 问价格、重量、酥皮层数及最佳食用时间；这组响应只有 18 层的资料，其余三项未出现。证据是原文完整片段，不等于覆盖了问题。已撤回原生检索强制 top_k<=3 的旧限制；遵守 CUEKB_TOP_K（默认 5）并在现有字节预算内保留来源，省略项如实标记 partial。不能保证第五条就足够，也不把这一改动当作工具循环已解决的证据。

本次仅需重建 API 镜像，Web/VoiceChat 无须重建。沿用 §2、§3 的入口；回传四条 case、validation 及两份摘要。selected 不再固定等于 3；即使 answer_complete=passed，也须核对价格/重量/层数/最佳食用时间分别有依据回答或明确说明缺少资料，不能用猜测补齐。保留原 WAV，不降低用例要求。

## 7. 本次验证的事实、假设与退出条件

已知事实：历史产品轮反复查询且未完成回答；原代码每次新调用只用本次证据校验，可能遗漏早先已返回的事实。本次将累计证据与校验、落库统一，并限制检索。

待现场验证的假设：有界检索、累计证据与结束检索要求能让当前 VoiceChat 完成英文回答。代码和 CPU 协议夹具不能证明模型会服从，也不能断言过去循环只由证据不足引起。已有后端负责 `<TOOL_RESPONSE>` 包裹，本次不修改其逻辑。

验收允许诚实的部分回答：有来源的事实正确口述，无法确认的字段明确说明“本次检索资料不足以确认”；不能说整个知识库没有，更不能编造或引导到未经确认有资料的门户页面。`answer_complete` 仍要求非 ACK 文本、非零音频和结束事件；内容准确性及听音仍人工核对。

首次部署先运行一次 §2；失败仅回传四条 case（若中止则已出现的 case）、validation 与 §3 精简摘要。若终态后仍调用，不重复跑二十轮或调大预算；转入一次有明确对照条件的 VoiceChat 续答能力分析。同一思路两次无新增证据即停止，三种假设无解则评估设计/环境/技术路径。hello 缺词仍暂缓。
