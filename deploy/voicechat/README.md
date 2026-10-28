# VoiceChat 构建与源码交付

当前发布以 [完整源码快照](../../docs/voicechat/README.md) 和 [snapshot-manifest.json](../../docs/voicechat/snapshot-manifest.json) 为准。外部项目独立部署，不加入本项目 Compose 或 Python 依赖。模型权重、GPU 分配、离线转换沿用现场验证配置。

## 同步运行文件

2026-10-28 补充交付 `model.py` 的跨线程 inference tensor 写入修复和 `s2s/audio_server.py` 的 PCM 输出节奏修复。完整文件与 CPU 回归同步收录；`tool-output-recovery.patch` 只供 backend 固定基线逆向检查，不代替两个运行文件。部署仍直接挂载本仓库六文件、重新创建容器，无新依赖/权重；现场注入成功和实际听音仍待复测。

本次配套更新 `model.py` / `data_types.py` 的工具期间音频保留、独立消费游标、原两帧输出边界和函数结果注入热路径；`prompt_template.jinja` 同步保留完整问题与独立首问策略。沿用已生效的 RNNT 显示初始化、原识别/打断门槛、PAD 保护及权重，不新增引擎能力或开关。audio_server.py 的输入/ASR 哈希诊断和显式批次仍保留，必须与 API 配套。CueKB 内容优化按用户提供的新版本已支持、等待真实联调处理，本仓库不修改 CueKB。

先保存现场源码，再将 `docs/voicechat/` 中受维护的完整文件同步到 Speech 的相同相对路径。不要覆盖整个 Speech 工程，也不要只应用早期 response-lifecycle.patch 代替最新完整文件。历史 patch/manifest 仅保留给固定基线回归和追溯，不作为当前运行文件 hash。

容器创建命令直接从本仓库 `docs/voicechat/` 的绝对路径只读挂载六个运行文件，不维护另一个宿主目录中的副本。完整挂载命令见 [启动手册](../../docs/voicechat/docker-run.md)。挂载来源变更必须重新创建容器；更新镜像不会覆盖现有 bind mount。

## 更新运行镜像并启动

沿用现场已验证的离线基础镜像。默认构建上下文为已同步完整文件的 Speech 目录；named context 为本项目根目录：

```sh
voicechat_base_image='YOUR_VALIDATED_OFFLINE_IMAGE@sha256:YOUR_DIGEST'
voicechat_release_image='YOUR_REGISTRY/voicechat:YOUR_RELEASE'
docker build --build-arg VOICECHAT_BASE_IMAGE="$voicechat_base_image" \
  --build-context agent_repo=/path/to/voice-service-agent \
  -f /path/to/voice-service-agent/deploy/voicechat/Dockerfile \
  -t "$voicechat_release_image" /path/to/speech
```

[Dockerfile](Dockerfile) 只覆盖 WebSocket 文件、模板并打包官方客户端/验证材料，不安装依赖或下载权重。backend 完整文件按启动手册挂载；不能假设这个 Dockerfile 已覆盖 model.py/load_utils.py。

固定客户端、静态检查器、cases.json、四段 WAV 位于 `/opt/voice-validation`。验证时不复制文件进容器或创建输入目录，输出写可写 `/tmp`。API/Web 镜像另行构建，使用匹配版本一起发布。

在 [启动手册](../../docs/voicechat/docker-run.md) 指定镜像和现有 GPU/卷参数后重建容器。仅 docker restart 不更新镜像、环境或挂载配置。启动后记录 image digest、实际文件 hash、Jinja 环境与诊断开关。

## 已有 backend 保护的边界

完整 model.py 已包含 PAD 尾部完成保护，不再次正向应用 pad-tail-completion.patch。该保护默认关闭，现场验证命令显式设置开关；只有已有 PAD 渲染条件及安全状态满足才插入结束。它不证明内容完整，也不解决工具选择或 hello 首词缺失。源码身份以 snapshot-manifest 为准，旧补丁 before/after hash 只用于补丁回归。

## 本地与现场验证

外部 CPU 测试依赖固定在 `docs/voicechat/s2s/tests/requirements.txt`，包含仅用于 RNNT 回归的 CPU PyTorch；不得安装到 GPU 推理环境。生命周期/诊断及 RNNT 测试文件不作运行挂载。所有验证只使用 [统一验证手册](../../docs/validation.md)，不在本目录维护独立检查步骤。

注册/Prompt 一致、CPU 回归、容器启动均不能替代有据语音回答。API 收到的调用、实际查询、每批注入、答案完成和人工内容核对分别记录；不增加查询上限、不给模型追加未经验证的控制参数。回滚使用已记录的 API/Web/VoiceChat 组合，不能混版测试批次协议。
