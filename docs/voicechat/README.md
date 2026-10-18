# VoiceChat 本地修改源码快照

本目录最初保存 `/Users/snowking/Documents/speech` 中为本项目 D19/D21 故障定位与恢复而实际修改的 VoiceChat/Speech 文件。2026-10-19 更新完整 `audio_server.py`、`model.py` 和 Prompt 模板：保留默认关闭的 PAD 尾部完成补丁，公开工具名改为完整英文，增加不含 Prompt/参数正文的跨层名称与 hash 诊断，按用户输入合并重复规范工具调用、复用首个 API 结果，并把缓存结果重放限制为一次。目录结构保持相对于 Speech 源码根目录不变。参考上游为 `NVIDIA-NeMo/Speech` 的 `nemotron-labs-voicechat` 分支，审计基线 commit 为 `097dfe9e2f55baf653b83035868bdc89849f1b47`。

当前 `model.py` 已包含补丁，可直接用于更新独立 VoiceChat/Speech 的同路径文件，**不要再对它应用 `pad-tail-completion.patch`**。部署顺序、运行参数和离线验证仍以 [`deploy/voicechat/README.md`](../../deploy/voicechat/README.md) 为准；精确 SHA-256 见 [`snapshot-manifest.json`](snapshot-manifest.json)。更新后仍需在 Triton backend 启动前设置 `S2S_INFERENCE_CLOSE_ON_PAD_TAIL=true` 才开启新逻辑。

当前现场验证容器的完整 `docker run` 命令、宿主文件同步和启动后核对步骤见 [`docker-run.md`](docker-run.md)。以后修改本目录中的 VoiceChat 运行文件、环境变量、挂载、镜像、端口或 entrypoint 时，必须在同一提交中同步更新该命令。

当前原生工具未触发故障的多轮证据、最小输出命令、VoiceChat 独立验证步骤和预期结果见 [`native-tool-selection-validation.md`](native-tool-selection-validation.md)。故障定位只回传该页规定的核心行，不回传全量容器日志。

补丁前的完整快照保存在 Git commit `b924c72a295bdca428ea3c841fdfca8a26aac53a`；`model.py` 的 before/after hash 仍由 [`pad-tail-completion-manifest.json`](../../deploy/voicechat/pad-tail-completion-manifest.json) 记录。当前收录文件不是已部署到 GPU 的证明。

## 快照范围

| Speech 相对路径 | 内容 |
| --- | --- |
| `s2s/audio_server.py` | WebSocket 响应生命周期、隔离采集、最终 Prompt 工具名/hash 诊断，以及同输入重复工具调用合并、一次结果复用和重放上限 |
| `s2s/prompt_template.jinja` | 用户提供的现场完整模板，加入会话策略优先、search_knowledge_base 单工具调用约束及消费品/商业事实检索示例；基线来源及 hash 见 manifest |
| `docs/voicechat-api.md` | 外部 VoiceChat API 说明更新 |
| `s2s/tests/test_response_lifecycle.py` | 外部 Speech 响应生命周期测试 |
| `s2s/tests/requirements.txt` | 外部 Speech 测试依赖 |
| `s2s/tests/test_voice_diagnostics.py` | 外部 Speech 诊断测试，夹具保持新开关默认关闭 |
| `opt/tritonserver/backends/nemotron-voicechat/model.py` | Triton backend Prompt hash、模型函数名称诊断、隔离采集与默认关闭的 PAD 尾部完成 |
| `opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py` | backend 诊断运行设置 |
| `opt/tritonserver/backends/nemotron-voicechat/data_types.py` | backend 诊断数据结构 |

`s2s/tests/` 是外接 Speech 项目的 CPU 测试文件，不需要复制到 GPU 生产运行目录。本项目 `tests/contract/test_voicechat_pad_tail_completion.py` 核验完整文件 hash，并在临时副本上反向/正向应用 PAD 补丁，确认诊断基线与当前收录版本逐字节一致。

本目录用于云端留存、完整文件交付与代码审查。文件/hash 一致只证明收录源码与 manifest 一致，不代表 GPU 推理、真实音频、原生工具调用或 CueKB 链路已通过现场验收。
