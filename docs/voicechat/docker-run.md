# VoiceChat 容器部署命令

本命令用于当前 `main` 开发主线的 VoiceChat 现场验证。它加载本目录收录的 `model.py` 收尾修改，并显式固定与该修改相关的三个已有运行参数。示例宿主目录 `/home/thao/voice` 应替换为现场实际挂载目录，运行文件必须与本目录对应文件一致；模型和 GPU 分配沿用现场已经验证的配置。VoiceChat 应使用本项目派生镜像，使官方诊断客户端、静态检查器、`cases.json` 和四段固定 WAV 随镜像发布，验证时不再复制宿主机测试文件。

2026-10-21 批次修正必须同时更新 API、Web 和本目录完整 `s2s/audio_server.py`。model.py/Prompt 未新增修改。下面仍将宿主 `/home/thao/voice/audio_server.py` 挂载到实际加载路径 `/s2s/audio_server.py`；更新该宿主文件后重建容器。若不用挂载，须先将完整文件同步到 Speech 构建上下文，不能只更新本项目 docs 就认为运行镜像已改变。批次预期见 [统一验证手册](../validation.md)。

## 同步部署文件

从本项目根目录同步当前完整文件：

```sh
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py /home/thao/voice/load_utils.py
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/model.py /home/thao/voice/model.py
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/data_types.py /home/thao/voice/data_types.py
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py /home/thao/voice/runtime_setup.py
cp docs/voicechat/s2s/audio_server.py /home/thao/voice/audio_server.py
cp docs/voicechat/s2s/prompt_template.jinja /home/thao/voice/prompt_template.jinja
```

文件身份、运行配置和工具闭环的核对统一在 [验证操作手册](../validation.md) 维护。

## 创建容器

先按照 [派生镜像构建说明](../../deploy/voicechat/README.md#更新运行镜像并启动) 从已验证的离线基础镜像构建发布镜像。构建命令必须包含 `--build-context agent_repo=/path/to/voice-service-agent`；发布镜像名写入以下变量：

```sh
VOICECHAT_RELEASE_IMAGE='YOUR_REGISTRY/voicechat:YOUR_RELEASE'
```

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

部署后只使用 [统一验证手册](../validation.md)，本页不维护另一套检查步骤。
