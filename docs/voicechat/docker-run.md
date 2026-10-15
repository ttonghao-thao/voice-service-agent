# VoiceChat 验证容器启动命令

本命令用于当前 `codex/q1003` VoiceChat 现场验证。它加载本目录收录的 `model.py` 收尾修改，并显式固定与该修改相关的三个已有运行参数。宿主目录 `/home/thao/voice` 中的运行文件必须与本目录对应文件一致；模型、镜像和 GPU 分配沿用现场已经验证的配置。

## 启动前核对

从本项目根目录同步当前完整文件，并核对 backend hash：

```sh
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/model.py /home/thao/voice/model.py
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/data_types.py /home/thao/voice/data_types.py
cp docs/voicechat/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py /home/thao/voice/runtime_setup.py
cp docs/voicechat/s2s/audio_server.py /home/thao/voice/audio_server.py

python3.12 -m py_compile \
  /home/thao/voice/model.py \
  /home/thao/voice/data_types.py \
  /home/thao/voice/runtime_setup.py \
  /home/thao/voice/audio_server.py

sha256sum /home/thao/voice/model.py
```

当前 `model.py` 预期 SHA-256：

```text
bc4271093c732beb70d4bce770b4ac06bdc0d31d97d558cb4e87190c365806a8
```

`load_utils.py` 和 `prompt_template.jinja` 沿用现场已验证文件；若后续修改，也必须先同步到 `docs/voicechat/` 的相同 Speech 相对路径并更新 snapshot manifest。

## 创建容器

先结束现有验证通话。若同名容器存在，停止并删除该容器，然后执行：

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
  nvcr.io/nim/nvidia/nemotron-labs-voicechat:latest
```

测试依赖和 `s2s/tests/` 文件不参与运行时推理，因此不挂载到验证容器。固定发布时应把 `latest` 替换为已经验证并记录的镜像 digest；本命令保留 `latest` 以匹配当前现场启动基线。

## 启动后核对

```sh
docker exec nemotron-labs-voicechat \
  sha256sum /opt/tritonserver/backends/nemotron-voicechat/model.py

docker exec nemotron-labs-voicechat printenv \
  S2S_INFERENCE_CLOSE_ON_PAD_TAIL \
  S2S_INFERENCE_FORCE_SPEECH_SILENCE_ON_PAD \
  S2S_TTS_PAD_TAIL_RATIO \
  RNNT_MAX_AGENT_RESPONSE_SEC

docker logs nemotron-labs-voicechat 2>&1 | grep 'TTS PAD-silence'
```

预期运行文件 hash 与上文一致，环境依次为 `true`、`true`、`3`、`0`，backend 启动日志包含 `force=True`、`pad_tail_ratio=3.0`、`close_on_pad_tail=True`。这些结果只证明新文件和启动配置已加载；回答收尾、ASR、原生工具和真实知识链仍需按 [验证记录](../voicechat-validation-20261014.md) 执行现场测试。
