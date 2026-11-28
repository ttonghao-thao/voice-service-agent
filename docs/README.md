# 按需文档索引

先选当前问题的一行，用 `rg -n '^##|关键词' 文件` 定位小节，有调用依赖时再继续读取。

| 当前问题 | 首选入口 / 章节 | 按实际依赖扩读 |
| --- | --- | --- |
| 产品要求、验收目标 | [交付需求](delivery.md) | [当前待验](TASK_BOARD.md) |
| 容器、端口、TLS、数据卷 | [运行拓扑与数据](architecture.md#运行拓扑与数据) | [公网证书](deployment.md#公网证书与续期)、[API 私有根](deployment.md#私有-ca-与-api-信任) |
| Start call、入房、控制 WS | [Sophon 通话链路](architecture.md#sophon-通话链路) | [源码入口](architecture.md#源码入口)、[错误排查](deployment.md#日志与-start-call) |
| 原生委派、意图 LLM、输入归属 | [输入与委派绑定](architecture.md#输入与委派绑定)、[原生 task 解析](architecture.md#原生-task-解析) | [Sophon 接入](integration.md#2-sophon-与原生委派) |
| 每轮有没有委派、如何解析和执行 | [逐轮委派诊断](architecture.md#逐轮委派诊断) | [门户协议](portal-protocol.md#逐轮委派诊断)、[日志](deployment.md#日志与-start-call)、[验证 §9](validation.md#9-sophon-逐轮委派诊断) |
| 查询路由、公开兜底、证据 | [查询类型](architecture.md#查询类型)、[公开兜底](architecture.md#公开检索与兜底) | [证据交付](architecture.md#证据与结果交付)、[CueKB 契约](interfaces/cuekb.md) |
| 字幕、文件音频、打断、播放 | [字幕与播放](architecture.md#字幕与播放)、[回执与听音](architecture.md#回执与听音) | [门户协议](portal-protocol.md) |
| 身份授权、角色、多轮 | [会话授权](architecture.md#会话授权)、[角色与多轮](architecture.md#角色与多轮) | [独立通话与知识范围](integration.md#5-独立通话与知识范围) |
| 下一步怎么实现 | [implementation-plan.md](implementation-plan.md) | 状态只查 TASK_BOARD，不能当实现事实 |
| 云端更新、构建镜像、启动和回滚 | [四步部署](deployment.md) | [备份](deployment.md#升级前备份数据库)、[回滚](deployment.md#回滚应用) |
| 现在还缺什么 | [TASK_BOARD.md](TASK_BOARD.md) | 原因再查方案/证据，不默认整读审查 |
| 真实结果或复跑 | [validation.md](validation.md) §1/§8 当前、§2 Sophon 方法、§6 待验 | §3 NVIDIA、§5 已解决历史、§7 旧检查按需跳过 |
| NVIDIA 注册、batch、注入边界 | [interfaces/voice-tools.md](interfaces/voice-tools.md)、[integration.md](integration.md) §3 | 仅本类任务读 VoiceChat 快照 |
| 外部 VoiceChat 快照与运行 | [维护与交付](voicechat/README.md#快照维护与交付) | manifest 定位文件，[docker-run.md](voicechat/docker-run.md) 定位运行参数 |
| 架构取舍、审查原因、文档维护 | [decisions](decisions/architecture-decisions.md)、[review](reviews/duplex-tool-audit.md)、[维护规则](development/documentation.md) | 只有任务涉及该主题才读 |

默认跳过 docs/archive、docs/voicechat 源码快照、生成 contracts、锁文件、夹具和 Git 历史。需要字段、hash 或历史时再定点读取；不为节省上下文省略必要的调用方、权限或部署检查。
