# 当前待实施方案

类型：方案，非实现或通过记录。需求以 [delivery.md](delivery.md) 为准，实际行为看 [architecture.md](architecture.md)，状态只在 [TASK_BOARD.md](TASK_BOARD.md)，操作和证据只在 [validation.md](validation.md)。

## 1. 容器发布剩余实施

2026-11-23 确认：系统同时提供天气、通用知识、专业知识库查询，通用知识检索承担受控兜底；不存在仅启用 search_knowledge 的产品部署场景。尚未接入某个依赖是交付缺口，旧 Provider 能力限制是兼容事实，均不能改写成另一个产品模式。

LiveKit/Redis 同 Compose、完整三类模板/启动检查和停止覆盖 NVIDIA 目录已实现，操作见 [deployment.md](deployment.md)，检查证据见 validation §7；不再列作待编码步骤。剩余顺序如下：

| 顺序 | 实施内容 | 涉及现有模块 | 完成条件 |
| --- | --- | --- | --- |
| 1 | 补齐 Sophon 容器验收运行时和宿主单入口 | 复用 scripts/validate_sophon_web.mjs；新增必要的验收 Dockerfile/Compose profile/宿主编排 | 浏览器、ffmpeg、固定官方材料在验收容器内；日志按窗口采集，输出有界摘要；不挂 Docker socket，不在宿主安装验收依赖 |
| 2 | 执行真实容器构建、迁移与发布验证 | 现有 API/Web Dockerfile、Compose、LiveKit、Alembic 0011 | 镜像/source hash 可核对，WSS/媒体/NAT 实际可达，PostgreSQL 升级保留旧数据和角色；失败可恢复匹配的旧镜像/备份 |
| 3 | 完成三类查询、受控兜底和语音闭环验收 | 现有 Provider → Coordinator → Runtime → Registry | 真实 CueKB、天气、Tavily、ASR 开关、多轮打断、参数保真和实际听音各有独立证据 |

API/Web 发布独立镜像，migrate 复用 API 镜像；LiveKit 与 PostgreSQL/Redis 同属一份 Compose，Sophon/CueKB 保持独立部署。AgentSession 已在 API 内，不另建第二套 Agent Runtime。验收容器是一次性任务，不是新增业务服务。

不新增第二个 LLM、ASR 关键词补调、强制 tool_choice、通用失败后无条件外发或猜测协议字段。当前 Router 的公开兜底约束按架构记录，不因部署调整而扩展。

## 2. 模型与验收缺口

原生委派漏触发与已委派任务解析已在 b60028d 的固定官方录音集分别修复并复测，详见 validation 的已解决章节；不再作为当前待修复任务。更大样本稳定性、多轮、参数保真与完整口述仍单独验收，不能用该组通过替代。

型号保真使用固定音频、完整原生 task 和目标型号金标验证；F7005 → F705 不在 Router 中用 ASR 猜改。角色名字生效不等于完整 Persona 遵循；Gateway 回执、候选字幕、首段语音均不等于已核实完整回答。

ASR 启停由独立 Sophon Gateway 管理。本项目只验证字幕展示及业务解耦，不把开启/关闭切成新的业务模式。

## 3. 交付与验证边界

每阶段同步部署操作、实际架构、任务板和唯一验证记录；已经修复并通过对应验证的问题移入已解决章节，不让历史失败继续驱动当前工作。外部 VoiceChat 配套源码/镜像的变更须另按根 AGENTS.md 交付，不能随应用发布自动升级。

仓库检查包括部署契约、Compose 展开、Shell/文档语法、链接和 diff。真实部署在容器执行；缺少 Docker Engine 时不得声称镜像/真实网络通话验收通过，不刷新历史语音结果。完成后按根 AGENTS.md 交付 origin/main。
