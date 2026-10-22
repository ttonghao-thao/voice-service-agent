# VoiceChat 容器部署命令

本命令用于当前 `main` 开发主线的 VoiceChat 容器部署。源码来源为 [NVIDIA-NeMo/Speech 的 nemotron-labs-voicechat 分支](https://github.com/NVIDIA-NeMo/Speech/tree/nemotron-labs-voicechat)，实际基线和完整文件 SHA-256 以本目录 manifest 为准。模型和 GPU 分配沿用现场已经验证的配置。VoiceChat 使用本项目派生镜像，使官方诊断客户端、静态检查器、`cases.json` 和四段固定 WAV 随镜像发布。

六个运行文件直接从本仓库 `docs/voicechat/` 只读挂载到实际加载路径，重建容器即可加载仓库交付文件，不需要复制到另一个宿主目录。`--mount` 在源文件不存在时明确失败，不会自动创建同名目录。更新仓库文件不等于运行进程已经加载新版本；部署身份与行为核对统一见 [验证手册](../validation.md)。

## 确定挂载根目录

在本项目根目录执行，下面的创建命令在同一 shell 中使用该绝对路径：

```sh
VOICECHAT_SNAPSHOT_ROOT=$(cd docs/voicechat && pwd)
```

文件身份、运行配置和工具闭环的核对统一在 [验证操作手册](../validation.md) 维护。

## 创建容器

2026-10-22 的 audio_server.py 新增首 4 秒输入指纹和 ASR 交接哈希，沿用下列已有 `VOICECHAT_DIAGNOSTICS=1`。本次统一挂载来源，不修改模型、模板、RNNT 或 GPU/端口参数。挂载来源变更必须重新创建容器，不能只重启旧容器。具体健康检查及精简回传全部在统一验证手册，不在此复制验证步骤。

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
  --mount "type=bind,source=${VOICECHAT_SNAPSHOT_ROOT:?Set VOICECHAT_SNAPSHOT_ROOT}/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py,target=/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/s2s/audio_server.py,target=/s2s/audio_server.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/s2s/prompt_template.jinja,target=/s2s/prompt_template.jinja,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/data_types.py,target=/opt/tritonserver/backends/nemotron-voicechat/data_types.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/model.py,target=/opt/tritonserver/backends/nemotron-voicechat/model.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py,target=/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py,readonly" \
  --entrypoint /s2s/run_s2s_server.sh \
  "$VOICECHAT_RELEASE_IMAGE"
```

测试依赖和 `s2s/tests/` 文件不参与运行时推理，因此不挂载到验证容器。固定验证材料位于镜像只读 `/opt/voice-validation`，运行结果写入 `/tmp`。记录派生镜像 digest；仅 `docker restart` 不会换用新镜像。

部署后只使用 [统一验证手册](../validation.md)，本页不维护另一套检查步骤。
