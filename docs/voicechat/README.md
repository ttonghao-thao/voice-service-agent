# VoiceChat 本地修改源码快照

本目录保存为本项目维护的完整 VoiceChat/Speech 文件，保持 Speech 根目录相对路径。2026-10-22 修正 `model.py` 中 RNNT 显示转写的重复零输入初始化，改用 NeMo 标准贪心解码的 `add_sos=False`；不改变分轮/打断状态、识别门槛或权重。audio_server 的首 4 秒指纹与 ASR 交接哈希保留，不保留原始音频或记录 ASR 正文；显式批次及每批一次聚合注入保留，不做查询重放。参考上游为 NVIDIA-NeMo/Speech 的 nemotron-labs-voicechat 分支，基线 commit 为 `097dfe9e2f55baf653b83035868bdc89849f1b47`；实际发布身份以 manifest/镜像/挂载 hash 为准。

当前 `model.py` 已包含音频消费/结果注入优化，以及 PAD 与 RNNT 补丁，可直接用于更新独立 VoiceChat/Speech 的同路径文件，**不要再对它应用 patch**。部署顺序和运行参数仍以 [`deploy/voicechat/README.md`](../../deploy/voicechat/README.md) 为准；精确 SHA-256 见 [`snapshot-manifest.json`](snapshot-manifest.json)。PAD 保护仍需在 Triton backend 启动前设置 `S2S_INFERENCE_CLOSE_ON_PAD_TAIL=true`；RNNT 转写修正随完整文件加载，不新增开关。

完整 `docker run` 命令见 [`docker-run.md`](docker-run.md)：重建容器时直接从本目录的绝对路径只读挂载六个运行文件，不复制到其他宿主目录。以后修改本目录中的 VoiceChat 运行文件、环境变量、挂载、镜像、端口或 entrypoint 时，必须在同一提交中同步更新该命令。运行文件挂载到 Speech 的实际加载路径，CPU 测试文件只收录、不挂载。

所有验证步骤与精简回传统一见 [验证操作手册](../validation.md)，本目录不再保留独立验证文档。

PAD 补丁前的完整快照保存在 Git commit `b924c72a295bdca428ea3c841fdfca8a26aac53a`；[`pad-tail-completion-manifest.json`](../../deploy/voicechat/pad-tail-completion-manifest.json) 保留不可变的 PAD-only before/after hash，当前完整文件的 hash 只以 snapshot manifest 为准。[RNNT 独立差异](../../deploy/voicechat/rnnt-transcript-bootstrap.patch) 仅用于离线回归/追溯，部署直接挂载完整文件。当前收录文件不是已部署到 GPU 的证明。

本次工具消费修改将真实音频帧游标与工具步数分开，抽取/注入期间保留输入与结果；积压按有界窗口消费，语义边界仍按原两帧报文交付。函数结果一次准备 embedding、单协程顺序注入，避免每步正文采样与重复传递；保留现有 append/anext 引擎接口。错误/未知部分写入明确失败，不自动重放。独立首问及复合 query 的策略在 API、目录 schema 和模板中同步加强；真实模型行为与 GPU 时延仍待部署验证。

## 快照范围

| Speech 相对路径 | 内容 |
| --- | --- |
| `s2s/audio_server.py` | WebSocket 响应生命周期、隔离采集、Prompt 工具名/hash、首 4 秒输入及 ASR 哈希诊断，以及原生调用批次声明/聚合结果校验；无业务查询重放 |
| `s2s/prompt_template.jinja` | 用户提供的现场完整模板，加入会话策略优先、search_knowledge_base 单工具调用约束及消费品/商业事实检索示例；基线来源及 hash 见 manifest |
| `docs/voicechat-api.md` | 外部 VoiceChat API 说明更新 |
| `s2s/tests/test_response_lifecycle.py` | 外部 Speech 响应生命周期测试 |
| `s2s/tests/requirements.txt` | 独立 CPU 测试固定依赖，含 CPU PyTorch；不安装到 GPU 推理环境 |
| `s2s/tests/test_voice_diagnostics.py` | 外部 Speech 诊断测试，夹具保持新开关默认关闭 |
| `s2s/tests/test_tool_consumption.py` | CPU ramp 音频逐样本/隔离/结果保留、两帧边界、函数通道递推及部分失败检查 |
| `s2s/tests/test_rnnt_transcript.py` | 实际显示转写方法配合 CPU LSTM，验证单次零输入、blank 状态保留及标准贪心递推；受控反例不代表真实音频准确率 |
| `opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py` | 2026-10-19 用户补充的完整 loader，原样保留 LF 换行；与现场 CRLF 版本等价，两种校验值见 manifest |
| `opt/tritonserver/backends/nemotron-voicechat/model.py` | RNNT 显示转写初始化修正；保留 Prompt hash、模型函数名称诊断、隔离采集与默认关闭的 PAD 尾部完成 |
| `opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py` | backend 诊断运行设置 |
| `opt/tritonserver/backends/nemotron-voicechat/data_types.py` | 真实音频消费游标、待处理结果/输出和明确错误状态，及既有 backend 诊断数据结构 |

`s2s/tests/` 是外接 Speech 项目的 CPU 测试文件，不需要复制到 GPU 生产运行目录。本项目 `tests/contract/test_voicechat_pad_tail_completion.py` 核验完整文件 hash，并在临时副本上依次撤销 RNNT/PAD 补丁再按顺序应用，分别核对固定 PAD 基线和当前完整文件，不改写旧补丁 hash。

本目录用于云端留存、完整文件交付与代码审查。文件/hash 一致只证明收录源码与 manifest 一致，不代表 GPU 推理、真实音频、原生工具调用或 CueKB 链路已通过现场验收。
