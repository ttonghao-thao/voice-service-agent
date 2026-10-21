# VoiceChat 独立验证

本期使用私有 VoiceChat 的原生英文语音和工具能力；CueKB 查询预算与累计证据由 API 执行。VoiceChat 仅保持音频/响应顺序、工具批次边界和结果注入，不做业务去重或缓存重放。

## 1. 文件与部署核对

完整运行文件见本目录，SHA-256 与修改来源见 [manifest](snapshot-manifest.json)，实际挂载/启动命令见 [docker-run.md](docker-run.md)。本次只修改 audio_server.py 的批次传输，API 必须同时升级；model.py、load_utils.py、Prompt 与格式化函数不新增修改。

宿主机执行，不复制测试材料、不导入 Torch/Triton：

```sh
VOICE_CONTAINER=nemotron-labs-voicechat
docker exec "$VOICE_CONTAINER" sha256sum /s2s/audio_server.py
docker exec -e PYTHONPYCACHEPREFIX=/tmp/voice-validation/pycache "$VOICE_CONTAINER" \
  python -m py_compile /s2s/audio_server.py
```

预期 hash 与启动手册一致、编译退出 0。这只证明文件身份/语法，不证明模型运行正确。API 镜像单入口为 `python /app/scripts/run_native_cuekb_validation.py`；业务复测按 [批次验收](../native-tool-batch-validation.md)。

## 2. 原生批次聚合结果验证

Golden Yolk 自动/Web 各一次，各自取得窗口与 conversation_id，按批次验收命令提取时间线及 `--summarize-voicechat-log`。不要回传完整 `docker logs`，不要用另一个客户端的日志证明本次 API 会话。

预期：

- 同次 session 的 Prompt 工具名与 SHA-256 在 audio_server/backend 两侧一致；这只证明传递一致。
- 同一解码输出的 function.call 共享 native_batch_id，size/index 连续；一个完整结果对应一次 function.batch_result、function.result 和 backend 注入。
- 若一批有两个调用，function_parsed/new_calls 增加 2，function_result 增加 1，不是回填缺失。成员缺失/错误或重复结清明确拒绝，不静默等待。
- 时间线可用 input_item_id、call_id 或 response_id 关联 sequence；未关联仍为 unverified，不能报告模型零调用。
- 工具传输 passed 不证明第二段回答完成或准确；还要有非 ACK 文本、非空音频、结束事件及答案/来源提交，随后听音核实。

VoiceChat 后端已调用 format_tool_response_for_nemo，对 API JSON 添加既有 TOOL_RESPONSE 包裹。不要重复添加。API partial 使用字符串以适配现有数词规范化；不据此前 false→zero 日志推定用户已经听到 zero。

## 3. 官方客户端日志边界

可以在容器只读确认选项存在：

```sh
docker exec "$VOICE_CONTAINER" sh -c '
  python /opt/voice-validation/nemotron-voicechat-client.py --help |
    grep -F -- --function-text-output
'
```

`--function-text-output function_calls.jsonl` 只记录该客户端自己的连接，不会旁路记录门户/API 的另一连接。当前业务验证使用同窗口服务端摘要，不需要再启动官方客户端重跑。

CPU 回归覆盖批次完整性、单次注入及拒绝重复。真实模型在聚合结果后是否完整回答、等待发生在 ACK/注入/生成的哪一段，仍需本次部署证据。未取得证据前不调整模型逻辑。
