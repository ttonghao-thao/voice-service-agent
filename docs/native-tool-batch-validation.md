# 原生工具批次：本次范围与验收

## 已知问题与本次边界

2026-10-21 两次对照中，Black Pig 的 Web 已出现 Checking knowledge；不能再把它概括为始终不调用工具。Web 结果是否准确仍需核对答案和来源。Golden Yolk 第二段的第 3 个调用在第 2 个终态结果提交前约 195 ms 已到达 API，旧实现直到出队才检查预算，误把排队中的调用当成终态后的新调用。

这是已确认的应用判断错误。VoiceChat 的一个函数通道输出可以含多个调用，而后端维护单份待注入结果；逐个回填存在覆盖风险，这是代码层面的风险，不能当作现场已经证实的丢结果。本次由 audio_server 显式声明批次，API 结清成员后只回填一份累计结果，不改 model.py、格式化函数或提示词。

本次只修正这个闭环阻塞和诊断关联：同输入最多两次实际查询；同批次超额成员标记 not_executed，不执行新查询；同查询复用仍进入终态；终态结果提交后才到达的新批次继续明确失败。累计证据、权限/版本检查和 Coordinator 唯一答案提交保留。不增加时间窗口猜测、第三次查询、自动重试或根据转写强制选工具。

已有日志显示 CueKB 请求约 0.1 秒，较长等待发生在结果提交后、下一次调用或回答到达前。由于当时时间线未关联到 VoiceChat sequence，尚不能将这段时间全部归因于模型推理或 ACK。本次补全 call_id/response_id 关联和批次阶段，先交付完整回答；重复回答、具体时延优化、hello 缺词、通用打断及跨轮能力不在本次修改范围。

## 部署

1. 按 [Web/API 构建命令](web-wav-comparison.md#1-部署准备宿主机项目根目录) 更新两个镜像并重建容器。
2. 同时按 [VoiceChat 启动命令](voicechat/docker-run.md) 更新挂载的完整 `audio_server.py` 并重建 VoiceChat；若使用镜像内文件，则先同步到 Speech 构建上下文再制作镜像。旧 API 不能正确结清新 VoiceChat 的多成员批次，不能混版验收。
3. 核对 [VoiceChat 独立验证 §11](voicechat/native-tool-selection-validation.md#11-原生批次聚合结果验证)。数据库、模型、Prompt、检索预算不需要调整。

输入仍是镜像内固定 WAV/cases.json。无需在容器创建输入目录或拷文件。保持默认 `VOICE_MAX_RETRIEVALS_PER_INPUT=2`，不要为使测试通过而提高预算或延长连接寿命。

## 操作一：自动验证 Golden Yolk 一次

宿主机项目根目录执行；单 case 新连接，原 WAV 停顿保留。120 秒仅为验证器等待上限，不能覆盖服务端会话时限。

```sh
API_CONTAINER=$(docker compose --env-file .env -f deploy/compose.production.yaml ps -q api)
VOICE_CONTAINER=nemotron-labs-voicechat
RUN_ID=$(docker exec "$API_CONTAINER" python -c 'import time; print(time.time_ns())')
REPORT="/tmp/native-cuekb/batch-$RUN_ID/report.json"
if docker exec "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py \
  --case-id golden_yolk --case-timeout 120 \
  --output "$REPORT" --audio-dir "/tmp/native-cuekb/batch-$RUN_ID/replies"
then
  VALIDATION_EXIT=0
else
  VALIDATION_EXIT=$?
fi
printf 'host_exit_code=%s\n' "$VALIDATION_EXIT"
```

若 preflight 失败或未生成本次报告，停止并保留该错误，不读取旧报告。否则提取这一轮已有日志，不重跑 WAV：

```sh
WINDOW_START=$(docker exec "$API_CONTAINER" python -c 'import json,sys; print(json.load(open(sys.argv[1]))["window_start"])' "$REPORT")
WINDOW_END=$(docker exec "$API_CONTAINER" python -c 'import json,sys; print(json.load(open(sys.argv[1]))["window_end"])' "$REPORT")
CONVERSATION_ID=$(docker exec "$API_CONTAINER" python -c 'import json,sys; print(json.load(open(sys.argv[1]))["conversation_id"])' "$REPORT")
{
  docker logs --timestamps --since "$WINDOW_START" --until "$WINDOW_END" "$API_CONTAINER" 2>&1
  docker logs --timestamps --since "$WINDOW_START" --until "$WINDOW_END" "$VOICE_CONTAINER" 2>&1
} | docker exec -i "$API_CONTAINER" python /app/scripts/summarize_voice_timeline.py \
      --conversation-id "$CONVERSATION_ID"
docker logs --since "$WINDOW_START" --until "$WINDOW_END" "$API_CONTAINER" 2>&1 |
  docker exec -i "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py --summarize-api-log
docker logs --since "$WINDOW_START" --until "$WINDOW_END" "$VOICE_CONTAINER" 2>&1 |
  docker exec -i "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py --summarize-voicechat-log
```

只保留 case/validation/窗口/CID/退出码、上述摘要和时间线。若出现多个 input，按第二段 input_item_id 看业务链路，不能拿第一段普通回答放行。

## 操作二：Web 上传 Golden Yolk 一次

浏览器强制刷新，F12 Console 保留日志并过滤 `voice_file`，门户全程前台。在全新通话中上传仓库 `artifacts/native-cuekb/golden_yolk.wav`，原样发送全部 WAV，等待第二段知识回答或明确失败；不要在中间停顿时 End call。开始/结束时间及 Web conversation_id 的获取与时间线命令按 [Web 操作 §2](web-wav-comparison.md#2-操作一web-逐个上传四个文件)，本次只运行 Golden Yolk。

仅记录：`voice_file` 行；第二段转写与 input_item_id；对应答案文本、状态、reason、引用数/trace_id；实际是否听到完整回答；Sources 是否支持事实、数值及限定条件。不要回传 HAR、令牌或全量服务日志。两次测试分别使用各自窗口和 CID。

## 预期与退出条件

若重现“第一次单调用，第二次一次解码两个调用”的模式，第二段应为：

```text
retrieval_executions=2 retrieval_limit=2
queued_calls_settled=1 tool_batches_submitted=2
terminal_results_submitted=1 post_terminal_calls=0
answer_complete=passed answer_status=answered answer_sources=>0
```

最后一行 `answer_sources=>0` 表示数量必须大于零，不是输出字面值。API 可记录 received=3/results_submitted=3（按 call_id 结清），实际 result_injections=2；VoiceChat function_parsed=3/new_calls=3/function_result=2（两次注入），这不是回填缺失。第二批模型结果保留累计来源和 executed/not_executed 成员状态。

模型输出可能变化，不能强求上述次数。固定判据是：每 input 实际查询不超过 2、每完整批次只注入一次、预先收到的超额成员不误报 VOICE_TOOL_LOOP、第二段真实非 ACK 回答文本/音频/结束事件齐全并提交有来源的答案。内容仍须人工复核，不能以终态已回填代替回答完成。`session_match=unverified` 不是模型零调用。

本地已覆盖预算、重复通知、批次缺失/乱序、累计证据、权限撤销及下一输入预算重置；未进行真实 GPU/CueKB/听音验收。若两次部署测试仍阻塞，利用现有批次和注入时间线报告最早未完成阶段，停止重复试跑，不扩大检索预算或盲改 VoiceChat。
