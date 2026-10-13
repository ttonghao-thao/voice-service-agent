# VoiceChat 本地修改源码快照

本目录保存 `/Users/snowking/Documents/speech` 中为本项目 D19/D21 故障定位与恢复而实际修改的 VoiceChat/Speech 文件，目录结构保持相对于 Speech 源码根目录不变。参考上游为 `NVIDIA-NeMo/Speech` 的 `nemotron-labs-voicechat` 分支，审计基线 commit 为 `097dfe9e2f55baf653b83035868bdc89849f1b47`；本快照日期为 2026-10-13。

这些文件是本地最终修改状态的逐字节副本，不是新的补丁应用入口。部署顺序、运行参数和离线验证仍以 [`deploy/voicechat/README.md`](../../deploy/voicechat/README.md) 为准；精确 SHA-256 见 [`snapshot-manifest.json`](snapshot-manifest.json)。

## 快照范围

| Speech 相对路径 | 内容 |
| --- | --- |
| `s2s/audio_server.py` | WebSocket 响应生命周期、诊断与隔离采集 |
| `docs/voicechat-api.md` | 外部 VoiceChat API 说明更新 |
| `s2s/tests/test_response_lifecycle.py` | 外部 Speech 响应生命周期测试 |
| `s2s/tests/requirements.txt` | 外部 Speech 测试依赖 |
| `s2s/tests/test_voice_diagnostics.py` | 外部 Speech 诊断测试 |
| `opt/tritonserver/backends/nemotron-voicechat/model.py` | Triton backend 诊断与隔离采集 |
| `opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py` | backend 诊断运行设置 |
| `opt/tritonserver/backends/nemotron-voicechat/data_types.py` | backend 诊断数据结构 |

`s2s/tests/` 是外接 Speech 项目自身已修改的测试文件，因此随源码一并快照；本次没有修改 voice-service-agent 项目已有的 D21 故障测试代码、探针或验证方法，也没有移动 `docs/voicechat-fault-recovery-design.md`。

本目录用于云端留存与代码审查。文件/hash 一致只证明本地 Speech 修改已完整保存，不代表 GPU 推理、真实音频、原生工具调用或 CueKB 链路已通过现场验收。
