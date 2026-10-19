# 英文全双工与 CueKB 验证

本期路径为 Web ⇄ API relay ⇄ VoiceChat；VoiceChat 原生调用 `search_knowledge_base` 后，API 查询 CueKB 并按原 call_id 回填。普通回答和多轮上下文由同一 VoiceChat 连接承载。

## 1. 部署与输入

配置为 `VOICE_PROVIDER=nvidia`、`AGENT_PROVIDER=disabled`、`QA_EXECUTION_MODE=dual_tools`、`QA_ANSWER_POLICY=general_qa`。新建会话使用当前策略，旧 knowledge_required 会话不能作为本期验收。

API 镜像内置脚本及 `artifacts/native-cuekb/` 的 cases.json、hello.wav、tcp_udp.wav、black_pig_crisp_heart.wav、golden_yolk.wav；镜像路径为 `/app/validation/native-cuekb/`。输入更新须重建镜像。所有 Python 命令在容器执行，不使用 docker cp，不手动创建输入目录。脚本自动在可写 `/tmp/native-cuekb/` 生成报告和回答音频；`--audio-dir` 是回答输出目录。

本次需要重建 API 镜像，更新 VoiceChat 的完整 audio_server.py 并重建容器；Web 没有协议变更，不要求重建。VoiceChat 独立步骤见 [验证说明](voicechat/native-tool-selection-validation.md)，镜像/挂载命令见 [启动说明](voicechat/docker-run.md)。部署时沿用迁移与 `configure_native_cuekb.py --apply` 流程。

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

`LEGACY_TOOL_REPLAY` 表示仍运行旧的合并/重放/隐藏 ACK 补丁，应核对部署文件。不同 call_id 可以属于同一个 input，数量本身不是故障；有调用无结果才是回填不完整。没有最终回答时，不能仅根据 audio_done 总计认定 EOS 故障。

## 4. 同一通话的麦克风验收

固定 WAV 通过后，在 Web 门户保持同一通话，依次说：

1. “Hello, how are you?”
2. “Please introduce Black Pig Crisp Heart.”
3. “How should I store it?”
4. “Thank you. What is the difference between TCP and UDP?”
5. 回答播放期间自然插话，询问 Golden Yolk。

预期：无需每轮重新开始；产品追问能利用会话上下文并查询相应知识；普通问答仍可直接回答；收音与播放同时工作。实际模型打断、完整听音和口述忠实性必须记录为现场结果，不能由 CPU 回归推定。

仅回传：是否重连、追问是否识别正确产品、产品回答是否完整、插话是否被接收及对应错误码。hello.wav 缺 Hell 问题继续暂缓，不通过改写转写掩盖。
