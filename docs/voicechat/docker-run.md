# VoiceChat 容器部署命令

本命令用于当前 `main` 开发主线的 VoiceChat 容器部署。源码来源为 [NVIDIA-NeMo/Speech 的 nemotron-labs-voicechat 分支](https://github.com/NVIDIA-NeMo/Speech/tree/nemotron-labs-voicechat)，实际基线和完整文件 SHA-256 以本目录 manifest 为准。模型和 GPU 分配沿用现场已经验证的配置。VoiceChat 使用本项目派生镜像，使官方诊断客户端、静态检查器、`cases.json` 和四段固定 WAV 随镜像发布。

六个运行文件直接从本仓库 `docs/voicechat/` 只读挂载到实际加载路径，重建容器即可加载仓库交付文件，不需要复制到另一个宿主目录。`--mount` 在源文件不存在时明确失败，不会自动创建同名目录。更新仓库文件不等于运行进程已经加载新版本；部署身份与行为核对统一见 [验证手册](../validation.md)。

## 确定挂载根目录

在本项目根目录执行，下面的创建命令在同一 shell 中使用该绝对路径：

```sh
VOICECHAT_SNAPSHOT_ROOT=$(cd docs/voicechat && pwd)
VOICECHAT_MODEL_ROOT=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data/models"}}{{.Source}}{{end}}{{end}}' nemotron-labs-voicechat)
test -n "$VOICECHAT_MODEL_ROOT" && test -d "$VOICECHAT_MODEL_ROOT"
```

文件身份、运行配置和工具闭环的核对统一在 [验证操作手册](../validation.md) 维护。

## 创建容器

本次 `model.py` / `data_types.py` 分离真实音频消费与工具 decoder 步数，工具期间保留音频和待处理结果，并有界追赶积压；正文/ASR/函数边界保持原两帧传输，不把 EOS/BOS 拼进任意长度音频。结果注入预先计算函数通道 embedding、使用单一协程顺序 append，跳过未使用的正文采样和重复 CPU 传递。引擎仍逐 token 执行，不能由 CPU 通过宣称 GPU 已提速。`prompt_template.jinja` 同步更换为忠实保留用户问题的格式示例，不能继续加载旧模板。

这三个完整文件必须配套加载，并保留已有 RNNT 显示修复、PAD 完成保护、正常识别/分轮门槛和权重。仍为原六个只读挂载，不新增环境变量、端口或 GPU 参数。沿用已验证离线派生镜像并按下面命令重新创建容器，让 Triton backend 与模板一同重载；仅更新磁盘文件或重启 API 不会加载 VoiceChat 修复。健康、文件身份和下一轮 A/B 回传统一见验证手册。

本次运行库、官方客户端和权重没有改变，可沿用现有已验证派生镜像的不可变 image ID；先在删除旧容器前记录：

```sh
VOICECHAT_RELEASE_IMAGE=$(docker inspect --format '{{.Image}}' nemotron-labs-voicechat)
docker image inspect "$VOICECHAT_RELEASE_IMAGE" >/dev/null
```

六个 bind mount 提供本次运行代码，因此不需要为本次修改下载基础镜像或权重。确需更新派生镜像时再按[构建说明](../../deploy/voicechat/README.md#更新运行镜像并启动) 构建并记录新的 digest。

然后结束现有验证通话。若同名容器存在，停止并删除该容器，并用同一个 `$VOICECHAT_RELEASE_IMAGE` 创建容器：

```sh
# Paired model/data/template runtime; recreate the container to load all three.
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
  -v "${VOICECHAT_MODEL_ROOT:?Set VOICECHAT_MODEL_ROOT}:/data/models" \
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
