# 交付文档导航

本项目为仅支持英文（en-US）的全双工知识客服。开发文档使用中文，产品语音和门户交互使用英文。

| 需要了解的内容 | 唯一入口 |
| --- | --- |
| 需求、目的、交付范围、完成与未解决事项 | [交付说明](delivery.md) |
| 实际架构、模块和数据流 | [系统架构](architecture.md) |
| 待办与优先级 | [任务板](TASK_BOARD.md) |
| 已有现场与本地测试证据 | [验收记录](acceptance-report.md) |
| 放行标准 | [验收清单](development/validation.md) |
| 当前批次修复部署与最小验证 | [批次验收](native-tool-batch-validation.md) |
| 完整四 WAV Web/脚本对照 | [对照操作](web-wav-comparison.md) |
| 单 case、报告字段及答案检查 | [知识验证](native-cuekb-validation.md) |
| 应用镜像、配置、迁移、启动与回滚 | [部署](deployment.md) |
| VoiceChat 完整文件/hash/启动/独立验证 | [快照说明](voicechat/README.md)、[启动命令](voicechat/docker-run.md)、[验证](voicechat/native-tool-selection-validation.md) |
| 工具执行、证据和预算 | [知识执行](qa-routing-design.md)、[CueKB 契约](interfaces/cuekb.md) |
| 工具注册、后台目录和批次协议 | [工具契约](interfaces/voice-tools.md) |
| 语音/答案交付及上下文 | [交付行为](live-agent-implementation.md)、[上下文](task-context-evaluation.md) |
| HTTP/SSE/WS 与浏览器 | [门户协议](portal-protocol.md) |
| 外部服务与权限接入 | [接入](integration.md) |
| 故障与时延分段 | [排查](operations/troubleshooting.md) |
| 当前关键取舍 | [架构决策](decisions/architecture-decisions.md) |
| 开发模拟与文档维护 | [模拟](simulated-full-flow.md)、[维护规则](development/documentation.md) |

旧设计、历史排障流水和重复归档已从交付目录删除，需追溯时使用 Git。外部 VoiceChat 完整源码及 manifest 保留，不能用精简说明替代运行文件。不要将兼容测试、模拟结果或已删除旧模式的描述当作本期真实验收。
