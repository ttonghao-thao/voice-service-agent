# speech 启动更新与 VoiceChat 接入边界

更新：2026-10-04。补丁基线仍为 2026-09-28。本目录交付独立 speech 的 WebSocket 传输修复。本项目代码和补丁由 voice-service-agent 仓库发布，speech 本机源文件已同步；云端尚未部署。它不改变模型权重、离线转换、Triton 推理或自然插话/停顿逻辑。

## 为什么原生 HTML 正常，本项目仍需检查传输边界

`voicechat-client.html` 收到 PCM 就播放，按 input transcription.completed 与 output transcript.done 分气泡，不按 response ID 授权；speech 自带 agent 网关也透传音频。原服务可以正常对话和插话，不应把其可用性否定为“模型没有打断”。

本项目另有显式 Stop playback、取消查询和失效结果拒绝。原基线 WebSocket 层对静音也打开 response，只有断线才发 audio.done，工具另造 response ID；音频在 send_loop 中读取全局 ID，而字幕在推理协程中直接发。慢发送时 transcript.done 可以早于该轮缓存 PCM，且同一 ID 跨轮复用。仅在本项目把 transcript.done 换成 audio.done 或随机改 ID，不能恢复缺失的采样边界；直接放行所有 PCM 又会撤销现有业务授权保证。

因此，此份确认同源的 speech 基线保留有界输出队列、事件生成时固定 ID、逐轮尾帧/结束以及工具先于同批 ACK 的修复。它是当前业务隔离要求下选定的 WebSocket 接入方案，不是 NVIDIA 模型实现自然打断的必要条件，也不代表所有 VoiceChat API 都须修改。其它部署版本若已提供等价的有序归属和结束事件，应在 adapter 验证后直接接入。

## 实际需要更新什么

| 文件或配置 | 部署动作 | 用途 |
| --- | --- | --- |
| `s2s/audio_server.py` → 容器 `/s2s/audio_server.py` | 应用 [response-lifecycle.patch](response-lifecycle.patch) 并更新运行文件 | 音频/工具/字幕有序发送、每轮 ID、尾帧、PCM16 满幅与工具参数编码 |
| `s2s/prompt_template.jinja` → `/s2s/prompt_template.jinja` | 保留现有源码版本，并确保镜像中存在同份文件 | 使用原有 Jinja 模板；本次未修改它 |
| `USE_JINJA_TEMPLATE_PROMPT=1` | **在 WebSocket Python 进程启动前设置** | 按既有 Jinja 构造当前 tools/instructions；分别核对 legacy 强制 bridge 和 dual_tools 可选工具规则，不是 tool_choice/auto 开关 |
| `docs/voicechat-api.md` | 更新说明 | 当前传输契约、原生能力与业务控制边界 |
| `s2s/tests/test_response_lifecycle.py`、`s2s/tests/requirements.txt` | 仅 CPU 测试环境使用 | 不安装到现有 GPU 运行环境 |

`opt/tritonserver/backends/nemotron-voicechat/model.py`、模型仓库/权重、`deploy_s2s_model.sh` 和 `run_s2s_server.sh` 不需要因本次接入改动而更新；不需要重新转换模型。不改 GPU、端口、共享内存、模型卷等已验证参数。自然插话由模型处理，应用不会在每次 speech_started 时自动取消查询。

## 源码核对与本地测试

[source-manifest.json](source-manifest.json) 给出补丁涉及文件的前后 SHA-256。针对原始基线，在 speech 源码目录执行；路径替换为实际值：

```sh
git apply --check /path/to/voice-service-agent/deploy/voicechat/response-lifecycle.patch
git apply /path/to/voice-service-agent/deploy/voicechat/response-lifecycle.patch
python3.12 -m venv .venv-d19
. .venv-d19/bin/activate
python -m pip install -r s2s/tests/requirements.txt
python -m pytest s2s/tests/test_response_lifecycle.py -q
python -m py_compile s2s/audio_server.py
```

`git apply` 可用于非 Git 源码目录。已应用本轮最终版本时用 after hash 核对，不重复应用。上一轮临时 D19 文件与本轮最终版不同，尤其默认工具模板已恢复；应使用保存的原始基线重新应用最终补丁。先备份已有修改，不用 `--reject` 或跳过冲突。

## 更新运行镜像并启动

沿用现场已经验证的离线镜像。可使用本目录 [Dockerfile](Dockerfile) 在该镜像之上只覆盖 WebSocket 文件与既有模板（构建上下文必须是应用补丁后的 speech 目录）：

```sh
voicechat_base_image='YOUR_VALIDATED_OFFLINE_IMAGE@sha256:YOUR_DIGEST'
voicechat_release_image='YOUR_REGISTRY/voicechat:YOUR_RELEASE'
docker build --build-arg VOICECHAT_BASE_IMAGE="$voicechat_base_image" \
  -f /path/to/voice-service-agent/deploy/voicechat/Dockerfile \
  -t "$voicechat_release_image" /path/to/speech
```

该派生镜像不安装依赖或下载权重，继承原镜像 entrypoint、command 和运行环境，并设置 Jinja 为默认。随后在**现场原有启动命令/Compose**中替换镜像，显式加入 `-e USE_JINJA_TEMPLATE_PROMPT=1`（Compose 使用 environment），保留全部原参数并重建容器。仅 `docker restart` 不会应用新镜像或新环境变量。若 `/s2s` 已被 bind mount 覆盖，镜像内 COPY 不生效，须同步更新挂载源文件。

在已配置的容器 shell 中直接运行现有脚本时，对应命令为：

```sh
USE_JINJA_TEMPLATE_PROMPT=1 /s2s/run_s2s_server.sh
```

不要在已有服务进程运行时再启动一份。启动完成后核对环境、实际文件 hash 与镜像：

```sh
voicechat_container='YOUR_VOICECHAT_CONTAINER'
docker inspect --format '{{.Image}}' "$voicechat_container"
docker exec "$voicechat_container" printenv USE_JINJA_TEMPLATE_PROMPT
docker exec "$voicechat_container" sha256sum /s2s/audio_server.py /s2s/prompt_template.jinja
```

本项目也需发布匹配的 API/Web 镜像。发生回退时使用已记录的源码/镜像组合；回到旧 WebSocket 边界后不能继续宣称本项目的逐轮停止/授权已通过。

## 复测与边界

先用原生 HTML 复测问答、停顿、插话和字幕，再用门户测试知识查询、快速/延迟工具结果、Stop playback 后下一轮、取消旧任务、短音频尾部和断线。默认 `MODEL_STEPS_PER_CALL=2` 的相邻 EOS/BOS 按两个 80 ms codec 帧验证；不同批次或无法确定的多轮边界须重新验收，不靠幅值猜测。

VoiceChat 内部工具等待与本项目 30 秒业务预算独立，不能认为本项目 env 会改变 GPU 服务。CPU/Chromium 夹具不证明真实 ASR 或听音质量。部署/性能步骤见 [部署](../../docs/deployment.md)，证据见 [验收](../../docs/acceptance-report.md)。

## Q07 双工具接入与模板核对

Q07 不新增此补丁文件或修改 GPU 推理/权重。本项目 API 按会话选择 config/voice-prompt.txt（legacy 单 bridge）或 config/voice-qa-prompt.txt（dual_tools，仅 lookup_knowledge / reason_over_knowledge）；严格知识模式另追加证据要求。原生 tools/instructions 在 session.update 注册，不发送 tool_choice。

保留上述 Jinja 启动配置并检查镜像内最终模板：只包含本轮授权工具，dual_tools 不残留每句强制 consult_service_agent 的规则，legacy 不追加绕过 bridge 的规则。原生自动选择能力不等于当前工具定义的选择质量；本地 WebSocket/CPU 测试不替代 GPU。

除原有两轮工具、ACK、尾帧、停止/取消外，还须按 [Q07-E](../../docs/qa-routing-design.md#14-验收矩阵) 验证无工具一般回答、lookup 证据续答、reasoned 外置回答及混合会话漏/误调用。D2 事后检查不是播前逐字批准；工具等待自由交谈与 D3 仍未实现。模式配置和迁移见 [部署](../../docs/deployment.md#问答模式与预算q07)。
