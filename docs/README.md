# 按需文档索引

先读根 AGENTS.md，`git status --short` 后用 `rg -n '^##|关键词' 文件` 定位章节，只读当前任务需要的段落。链接不是递归加载指令；遇到需求与实现不一致，按下面的信息归属判断，不能用旧实现缩小需求。

| 当前问题 | 首选入口 / 章节 | 按实际依赖扩读 |
| --- | --- | --- |
| 系统应该提供什么、兜底边界 | [delivery.md](delivery.md) §1–3 | 不先读旧 Provider 的工具数量 |
| 谁调用谁、ASR/权限归谁 | [architecture.md](architecture.md) §1–5 | 按表定位源码；参数再查 interfaces |
| 意图 LLM、超时、输入/数据边界 | [architecture.md](architecture.md) §3、[deployment.md](deployment.md) §5 | 使用 DELEGATION_ROUTER_*；新验收见 validation §8 |
| 下一步怎么实现 | [implementation-plan.md](implementation-plan.md) | 状态只查 TASK_BOARD，不能当实现事实 |
| 云端更新、构建镜像、签证书、迁移、Web 入口 | [deployment.md](deployment.md) §1–8 | 按顺序执行，失败与证书续期见 §9 |
| 现在还缺什么 | [TASK_BOARD.md](TASK_BOARD.md) | 原因再查方案/证据，不默认整读审查 |
| 真实结果或复跑 | [validation.md](validation.md) §1/§8 当前、§2 Sophon 方法、§6 待验 | §3 NVIDIA、§5 已解决历史、§7 旧检查按需跳过 |
| Sophon/外部工具/身份接入 | [integration.md](integration.md) §1–2、§5 | CueKB 参数查 [interfaces/cuekb.md](interfaces/cuekb.md) |
| NVIDIA 注册、batch、注入边界 | [interfaces/voice-tools.md](interfaces/voice-tools.md)、[integration.md](integration.md) §3 | 仅本类任务读 VoiceChat 快照 |
| 门户事件、文件音频、角色 API | [portal-protocol.md](portal-protocol.md) | VoiceClient、LiveKitMedia、App |
| 外部 VoiceChat 完整源码/运行 | [voicechat/README.md](voicechat/README.md)、[docker-run.md](voicechat/docker-run.md) | manifest 定位文件；不是三类查询发布入口 |
| 架构取舍、审查原因、文档维护 | [decisions](decisions/architecture-decisions.md)、[review](reviews/duplex-tool-audit.md)、[维护规则](development/documentation.md) | 只有任务涉及该主题才读 |

默认跳过 docs/archive、docs/voicechat 源码快照、生成 contracts、锁文件、夹具和 Git 历史。需要字段、hash 或历史时再定点读取；不为节省上下文省略必要的调用方、权限或部署检查。
