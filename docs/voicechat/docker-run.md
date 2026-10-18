# VoiceChat 验证容器启动命令

本命令用于当前 `codex/q1003` VoiceChat 现场验证。它加载本目录收录的 `model.py` 收尾修改，并显式固定与该修改相关的三个已有运行参数。示例宿主目录 `/home/thao/voice` 应替换为现场实际挂载目录，运行文件必须与本目录对应文件一致；模型和 GPU 分配沿用现场已经验证的配置。VoiceChat 应使用本项目派生镜像，使官方诊断客户端、静态检查器、`cases.json` 和四段固定 WAV 随镜像发布，验证时不再复制宿主机测试文件。

## 启动前核对

从本项目根目录同步当前完整文件，并核对 backend hash：

```sh
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/model.py /home/thao/voice/model.py
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/data_types.py /home/thao/voice/data_types.py
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py /home/thao/voice/runtime_setup.py
cp docs/voicechat/s2s/audio_server.py /home/thao/voice/audio_server.py
cp docs/voicechat/s2s/prompt_template.jinja /home/thao/voice/prompt_template.jinja

python3.12 -m py_compile \
  /home/thao/voice/model.py \
  /home/thao/voice/data_types.py \
  /home/thao/voice/runtime_setup.py \
  /home/thao/voice/audio_server.py

sha256sum /home/thao/voice/model.py
```

当前运行文件预期 SHA-256：

```text
model.py          c91b3af5b41f515fc1dbfaf7fdbec4ae528a4989f90a144829d2491a9b805990
audio_server.py   f45bd8a6d91e0d7f8e270c3b767706c7e644e690c6f96e63254d70f8380ab74d
prompt_template   a71230010295a363a0fde0e0d86c4786aeb4ef5f458cf4d75ddf0926895bcdb7
```

`load_utils.py` 沿用现场文件。本次模板必须使用上面同步的完整 `prompt_template.jinja`。模板加入会话策略优先、禁止同请求重试/并行工具调用、仅按授权结果发声的约束，并将调用示例收敛为单个工具和对象参数；单工具 search_knowledge_base 使用完整英文工具名和覆盖价格、重量、存储及保质期的 query 示例。已注册工具覆盖请求时，模板要求调用工具，不能直接声称无权访问资料。它修复提示冲突，不代表已修复模型全 PAD。

`audio_server.py` 在模板渲染后记录工具名、Prompt 字符数和 SHA-256；backend `model.py` 在接收 Triton system_prompt 时再次记录工具名、字符数和 SHA-256，并在函数通道形成有效 JSON 后按 sequence 记录模型解码出的工具名。日志不写 Prompt 正文、query、参数或工具结果正文。同一用户输入内，规范化后的相同工具名和参数只向 API 发送一次；模型重复请求时等待首个结果或把缓存结果重新送入 Triton，新用户输入开始后清空该状态。两层 Prompt hash 相同且工具名均为 `search_knowledge_base`，才证明注册名称未在 VoiceChat 转换链路中被改写。

## 创建容器

先按照 [派生镜像构建说明](../../deploy/voicechat/README.md#更新运行镜像并启动) 从已验证的离线基础镜像构建发布镜像。构建命令必须包含 `--build-context agent_repo=/path/to/voice-service-agent`；构建后先确认固定验证材料存在：

```sh
VOICECHAT_RELEASE_IMAGE='YOUR_REGISTRY/voicechat:YOUR_RELEASE'

docker run --rm --entrypoint sh "$VOICECHAT_RELEASE_IMAGE" -c '
  test -r /opt/voice-validation/nemotron-voicechat-client.py &&
  test -r /opt/voice-validation/inspect_voicechat_artifacts.py &&
  test -r /opt/voice-validation/native-cuekb/cases.json &&
  test "$(find /opt/voice-validation/native-cuekb -maxdepth 1 -name "*.wav" | wc -l)" -eq 4 &&
  echo voicechat_validation_bundle=passed
'
```

预期只输出 `voicechat_validation_bundle=passed`。失败时重新构建派生镜像，不在运行容器内创建目录或补拷文件。

然后结束现有验证通话。若同名容器存在，停止并删除该容器，并用同一个 `$VOICECHAT_RELEASE_IMAGE` 创建容器：

```sh
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
  -v /home/thao/model-repo:/data/models \
  -v /home/thao/voice/load_utils.py:/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py:ro \
  -v /home/thao/voice/audio_server.py:/s2s/audio_server.py:ro \
  -v /home/thao/voice/prompt_template.jinja:/s2s/prompt_template.jinja:ro \
  -v /home/thao/voice/data_types.py:/opt/tritonserver/backends/nemotron-voicechat/data_types.py:ro \
  -v /home/thao/voice/model.py:/opt/tritonserver/backends/nemotron-voicechat/model.py:ro \
  -v /home/thao/voice/runtime_setup.py:/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py:ro \
  --entrypoint /s2s/run_s2s_server.sh \
  "$VOICECHAT_RELEASE_IMAGE"
```

测试依赖和 `s2s/tests/` 文件不参与运行时推理，因此不挂载到验证容器。固定验证材料位于镜像只读 `/opt/voice-validation`，运行结果写入 `/tmp`。记录派生镜像 digest；仅 `docker restart` 不会换用新镜像。

## 启动后核对

```sh
docker exec nemotron-labs-voicechat \
  sha256sum \
    /opt/tritonserver/backends/nemotron-voicechat/model.py \
    /s2s/audio_server.py \
    /s2s/prompt_template.jinja

docker exec nemotron-labs-voicechat printenv \
  S2S_INFERENCE_CLOSE_ON_PAD_TAIL \
  S2S_INFERENCE_FORCE_SPEECH_SILENCE_ON_PAD \
  S2S_TTS_PAD_TAIL_RATIO \
  RNNT_MAX_AGENT_RESPONSE_SEC

docker logs nemotron-labs-voicechat 2>&1 | grep 'TTS PAD-silence'

docker logs nemotron-labs-voicechat 2>&1 \
  | grep -E 'Prompt prepared:|System prompt received|Function call decoded from model'
```

预期运行文件 hash 与上文一致，环境依次为 `true`、`true`、`3`、`0`，backend 启动日志包含 `force=True`、`pad_tail_ratio=3.0`、`close_on_pad_tail=True`。一次新会话应出现相同 Prompt SHA-256 的 `Prompt prepared` 与 `System prompt received`，两行均列出 `search_knowledge_base`；模型触发工具时还应出现同一 sequence 的 `Function call decoded from model for sequence ...: tool_names=search_knowledge_base`。这些结果只证明新文件、启动配置和该轮工具名称链路；回答收尾、ASR、原生工具执行和真实知识链仍需按 [验证记录](../voicechat-validation-20261014.md) 执行现场测试。

模板在进程启动时加载；同步宿主文件后按上面的命令重新创建验证容器。门户或 API 单独重启不会更新 VoiceChat 进程内的模板。当前未触发工具故障的最小输出复测按 [原生工具选择验证](native-tool-selection-validation.md)执行。
