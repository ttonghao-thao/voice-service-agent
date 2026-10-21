# 文档导航

本项目为仅支持英文（`en-US`）的全双工知识客服，支持连接内多轮对话。中文是开发文档语言；产品范围以 [当前架构](architecture.md#1-产品与部署边界) 为准，归档测试不能作为当前语言能力声明。

不明确任务时先读本页；已有明确问题时直接定位代码/主题。2026-10-15 对照 `63e7059` 整理；当前文档区分实现、方案和现场证据，不递归加载全部链接。

## 最小读取路径

- 了解系统： [当前架构](architecture.md)；要安排工作再读 [任务板](TASK_BOARD.md)。
- 继续当前工作： [单工具原生验证](native-cuekb-validation.md) → [VoiceChat 工具选择定位](voicechat/native-tool-selection-validation.md) → [任务板](TASK_BOARD.md)；需要历史严格模式诊断时再查 [故障定位](voicechat-fault-recovery-design.md) 与日期证据。
- 架构/编码规划： [实施方案](q1003-target-architecture.md)；确认受影响的接口和代码后再扩读主题。
- 现场更新： [VoiceChat 启动](voicechat/docker-run.md) 或 [应用部署](deployment.md)；完成后对照 [验收](acceptance-report.md)。

## 按职责查主题

| 唯一职责 | 文档 | 代码入口 / 按需依赖 |
| --- | --- | --- |
| 已实现模块、调用与状态边界 | [架构](architecture.md) | voice、sessions、agent_runtime、tools、storage |
| 改进设计、依赖与架构退出条件 | [方案](q1003-target-architecture.md) | Gateway/Provider/Runtime；原因查 [ADR](decisions/architecture-decisions.md) |
| 未完成工作及优先级 | [任务板](TASK_BOARD.md) | P0–P3；不维护测试计数 |
| 当前故障诊断分支 | [VoiceChat 工具选择定位](voicechat/native-tool-selection-validation.md)、[通用故障定位](voicechat-fault-recovery-design.md) | 当前未触发工具的最小证据；WS/encoder/RNNT/function/EOS/Gateway 通用分支 |
| 固定通话证据与采集步骤 | [10 月 15 日严格复测](voicechat-validation-20261015.md)、[10 月 14 日复测及采集](voicechat-validation-20261014.md) | 同 sequence 的日志事实；不套用早期断线原因 |
| 外部完整文件、hash、启动 | [VoiceChat 快照](voicechat/README.md)、[启动命令](voicechat/docker-run.md) | docs/voicechat；补丁/构建查 [交付](../deploy/voicechat/README.md) |
| 知识执行与证据准入 | [知识执行](qa-routing-design.md) | agent_runtime、Registry；[CueKB 契约](interfaces/cuekb.md) |
| 工具配置/注册/刷新 | [工具目录](interfaces/voice-tools.md) | tool_catalog、dispatch、Provider、routes |
| 条件来源/跨轮修订 | [上下文](task-context-evaluation.md) | task_context、Coordinator、Store |
| 实际口述、交付/恢复、等待能力 | [交付](live-agent-implementation.md) | evidence、Gateway、Coordinator、Store、Web audio |
| HTTP/SSE/WS、播放与页面 | [门户协议](portal-protocol.md) | contracts/routes、apps/web/src、public |
| 外部模型、鉴权与接入 | [接入](integration.md) | Provider、Runtime、auth；供应商原始事件止于 voice |
| 配置/迁移/镜像/启动 | [部署](deployment.md) | config、.env.example、Compose、deploy-cloud.sh |
| 运行故障与阶段时延 | [排查](operations/troubleshooting.md) | 先定位失败边界再读主题 |
| 已执行证据 / 放行要求 | [验收报告](acceptance-report.md) / [验证清单](development/validation.md) | 日期/版本/测试范围明确，不混合 CPU 与真实服务 |
| 网络/浏览器模拟 | [模拟](simulated-full-flow.md) | simulate_full_flow.py、tests；不代表模型能力 |
| 模型原理 / 固定源码依据 | [论文](voicechat-paper-v2-review.md) / [源码核对](voicechat-upstream-analysis.md) | 仅追究模型机制时读取；不默认全文加载 |
| 文档维护 | [维护规则](development/documentation.md) | 单一归属、按需读取、过期信息删除 |

路径相对 docs；代码入口相对 apps/api/app（前端另标）。字段以代码/生成契约为准，默认值以 Settings 为准，.env.example 是部署模板，现场实际值需核验。

## 历史与上下文

历史只在 [归档索引](archive/README.md) 或 Git 中追溯，默认不加载。先用标题/符号检索，再读取命中章节与实际依赖；不要同时全文读架构、方案、任务板、研究和日期流水。文档冲突时核代码/最新现场事实并修正文档，不把旧断言一起带入新任务。

原生工具批次本次部署与最小验收：[批次验收](native-tool-batch-validation.md)。
