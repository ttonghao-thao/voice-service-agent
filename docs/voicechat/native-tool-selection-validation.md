# VoiceChat 独立接入与验证

2026-10-19：本期使用 VoiceChat 原生英文全双工和连接内多轮能力。应用接入采用 [简化设计](../q1003-target-architecture.md)，VoiceChat 不承担 CueKB 业务去重、结果缓存重放或回答授权。

## 1. 已知事实与撤回的修改

用户多轮现场报告已经证明：`search_knowledge_base` 名称正确、两层 Prompt hash 一致、模型能原生调用工具、CueKB 可在约 100ms 返回证据。证据过滤修正后仍未完成知识语音回答。历史汇总中的 `speech=passed` 包含 ACK 音频，`audio_done` 是整个窗口的总数；它们不足以证明业务答案完成。

旧版本把原因归结为“重复 ACK 抢占唯一回答许可”，据此加入按工具名合并参数变体、自动重放缓存结果、隐藏重复 ACK。这些解释没有完整事件时间线证明，并且改变了原生调用含义。本版撤回三项逻辑，保留有序输出、响应 ID、尾帧刷新和不输出正文的诊断。

- 模型产生的新调用逐个送至 API，不因工具名相同而合并。
- API 只对同一个 call_id 的相同通知幂等；不同查询不会复用上次结果。
- 工具结果按原 call_id 回填一次，VoiceChat 不在内部重放。
- ACK 与最终回答均正常发送；API 不以 ACK 分配或恢复发声许可。
- 本次没有修改 Triton model.py、权重或 ASR。补收用户提供的完整 load_utils.py，保持附件代码不变，LF/CRLF 校验关系见 docker-run.md。hello.wav 句首丢词继续暂缓。

## 2. 更新范围与文件身份

完整文件位于本目录 `s2s/audio_server.py`，运行路径 `/s2s/audio_server.py`；测试在 `s2s/tests/test_response_lifecycle.py`。所有 hash 以 [snapshot-manifest.json](snapshot-manifest.json) 为准。[docker-run.md](docker-run.md) 维护构建、挂载和重建命令。

本次更新 API 镜像，以及 VoiceChat 的 audio_server.py 并重建容器。已有 bind mount 会覆盖镜像内同路径文件，必须同时核对宿主挂载文件。Web 本次无需重建。

从宿主机执行，检查容器实际文件：

```sh
VOICE_CONTAINER=nemotron-labs-voicechat

docker exec "$VOICE_CONTAINER" sha256sum /s2s/audio_server.py /s2s/prompt_template.jinja \
  /opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py
docker exec "$VOICE_CONTAINER" printenv USE_JINJA_TEMPLATE_PROMPT
docker exec -e PYTHONPYCACHEPREFIX=/tmp/voice-validation/pycache "$VOICE_CONTAINER" \
  python -m py_compile /s2s/audio_server.py
```

预期 hash 与 manifest 一致，模板环境值为 1，编译退出码为 0。无需复制测试文件到运行容器，编译缓存自动写入 /tmp。

## 3. 本轮工具传输摘要

先运行 [API 单入口](../native-cuekb-validation.md#2-从宿主机运行一次验证)，报告会记录窗口。宿主机执行：

```sh
WINDOW_START=$(docker exec "$API_CONTAINER" python -c 'import json; print(json.load(open("/tmp/native-cuekb/report.json"))["window_start"])')
WINDOW_END=$(docker exec "$API_CONTAINER" python -c 'import json; print(json.load(open("/tmp/native-cuekb/report.json"))["window_end"])')

docker logs --since "$WINDOW_START" --until "$WINDOW_END" "$VOICE_CONTAINER" 2>&1 |
  docker exec -i "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py --summarize-voicechat-log
```

预期最终 Prompt 与 backend 的工具名/hash 匹配，`function_parsed=new_calls=function_result`，旧 `duplicates_coalesced/results_replayed/duplicate_acks_suppressed/replay_limit` 全为 0。模型可能针对一个输入继续查询，不能仅凭调用数多于输入数判错。`LEGACY_TOOL_REPLAY` 表示旧补偿逻辑仍在运行。

摘要新增 `backend_injection` 行，按同一 sequence 汇总接收、ACK 期间缓存、注入启动、返回 idle、取消及 token 长度。`status=complete` 仅表示观测到对应队列注入完成，不证明模型理解或事实正确；无后端日志则为 unverified。末行 `scope=tool_transport` 明确原检查只证明工具事件与回填数量匹配，不证明答案有依据或音频完成。业务完成必须同时满足 API case 的 `answer_complete=passed`。不要用提高 replay 上限或隐藏 ACK 消除错误。

## 4. 绕过应用工作流的协议对照

仅在 API 单入口失败时，使用 [独立 VoiceChat 探针](../deployment.md#voicechat-诊断探针与部署产物核验)。该页命令全部从宿主机 docker exec，在 API 容器读取现有工具目录和凭据，直接连接 VoiceChat；脚本自动创建 /tmp 输出目录，输入使用镜像内的 WAV。

探针返回的是明确标记的合成工具结果，不执行 CueKB，不能当作业务验收。只回传：

```text
probe_status=... phase=... reason=... connected=... tools=...
checks=...
audio_output_samples=... pending_calls=... timeline_dropped=... output_wav_truncated=...
```

对照的意义：直接探针也无工具续答时查 VoiceChat 原生能力；直接探针完成而 API 失败时查真实结果包和 API 接入。两组必须固定镜像、模板、输入和时间窗口，不用旧会话的计数比较。

## 5. 官方客户端工具调用文件

`--function-text-output` 属于官方客户端启动选项，不是 audio_server.py 的服务端开关。先在实际容器确认客户端版本支持：

```sh
docker exec "$VOICE_CONTAINER" sh -c '
  python /opt/voice-validation/nemotron-voicechat-client.py --help |
    grep -F -- --function-text-output
'
```

有输出才证明当前客户端提供此选项；运行该客户端时才会生成其工具调用文件。它不会自动记录 API 发起的另一条 WebSocket 会话，因此当前应用验证使用上述同窗口服务端摘要。不要用另一客户端的 function_calls.jsonl 证明本轮 API 结果。

## 6. 现场放行

四 WAV 连续验证通过后，在同一个 Web 通话中做产品代词追问及播放期间插话。记录同一连接是否保留、查询对象是否正确、回答是否完整、插话是否收到。CPU 契约测试与文件 hash 不能证明模型理解、真实全双工打断或听音质量。

## 7. 2026-10-19 结果注入复核

04:56 UTC 现场：同一输入产生 10 次原生调用，API 检索/回填和 backend 接收均为 10，全部在 ACK 阶段缓存；注入启动 10、返回 idle 与注入结束各 9、取消 1、decoder limit 0。两轮普通回答完成，产品回答未完成。API 约 105 秒触发轮换；取消与轮换的因果仍需同时间线判断，不能把第十次取消当作前九次重新调用的原因。

已提供的 `format_tool_response_for_nemo` 接受 JSON 对象，并添加 `<TOOL_RESPONSE>[...]</TOOL_RESPONSE>` 后调用 `normalize_tool_response`。不能据此要求 API 再加数组或标签，否则可能产生重复封装。随后已取得规范化源码及实际 before/after：三条正文保留，partial 的布尔 false 被转换为字符串 "zero"。API 改用文本标记适配该数词转换边界，未修改本目录 VoiceChat 运行源码。

在已部署的 VoiceChat 容器执行下列源码检查，不需要更新镜像、重建容器或重新跑 WAV。普通 Python 进程不一定具有 Triton backend 的模块路径；不要直接 import infer.utils，也不要为此安装 triton_python_backend_utils。下面只用标准库读取 AST，不加载 Torch/Triton，不执行模型代码。

```sh
docker exec "$VOICE_CONTAINER" python -c '
import ast
from pathlib import Path

path = Path("/opt/tritonserver/backends/nemotron-voicechat/infer/utils.py")
source = path.read_text()
tree = ast.parse(source)
name = "normalize_tool_response"
found = False
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
        print("normalizer_source=" + str(path))
        print(ast.get_source_segment(source, node))
        found = True
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        if any((alias.asname or alias.name) == name for alias in node.names):
            print("normalizer_import=" + ast.get_source_segment(source, node))
            found = True
if not found:
    print("normalizer_definition=not_found")
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            print(ast.get_source_segment(source, node))
'
```

只回传 normalizer_source 和函数源码；若规范化函数来自其他模块，回传 normalizer_import（或 not_found 后的导入列表），用于定位实际文件。本命令不生成 formatted_probe，不能用读取源码成功代替规范化行为已验证。此前直接导入报 ModuleNotFoundError 只证明该诊断进程无法导入 backend 模块，不能据此判断运行中的 Triton 服务缺少依赖。

预期只增加一次工具响应封装，正文与数字保持完整。具体特殊标记映射应核对实际 normalizer 源码，不预设映射正确。若格式化和规范化均正确，再按 §4 的受控短结果探针对照；合成结果探针不能作为真实知识闭环验收。本轮不修改 VoiceChat 推理逻辑。

04:56:50 UTC 的真实 after 证据只覆盖 Black Pig 录音所问四项中的酥皮 18 层；价格、重量、最佳食用时间未出现于这组响应。此事实说明这一组证据覆盖不足，但不证明后续九次响应也相同，或模型重复调用一定由它造成。当前恢复配置的检索数和完整有界来源；不得以 partial=false 推断问题已获充分回答。下一次应核对真实知识回答，而非继续要求用户逐个提供辅助函数。
