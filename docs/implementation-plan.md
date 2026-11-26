# 当前待实施方案

类型：方案，非实现或通过记录。需求以 [delivery.md](delivery.md) 为准，实际行为看 [architecture.md](architecture.md)，状态只在 [TASK_BOARD.md](TASK_BOARD.md)，操作和证据只在 [validation.md](validation.md)。

本轮目标（2026-11-23 用户确认）：启用 qwen3.7-flash 解析 Sophon 已产生的原生 task，替代自然语言规则解析；结构化任务仍快速解析，ASR 仅展示，原生触发与服务端授权保持不变。用户明确允许本机验证，本次不接入真实 CueKB；这只是本轮验收范围，不改变产品三类交付或正式容器发布要求。真实凭据只留服务端私密配置，不入 Git/报告。

本轮上述接入与验证已完成，当前实现看 architecture，实际结果与未验边界只看 validation §8；不再重复列作待编码任务。接口冒烟不代替原生语音测试，固定集通过不代替完整生产放行。剩余正式交付顺序如下。

续办补充（2026-11-23）：Sophon/NVIDIA 是互斥语音后端，VOICE_PROVIDER 必须单选，默认 Sophon；本期只验证 Sophon。已完成配置/工厂/Compose 选择边界、按所选后端检查和启动依赖（Sophon 的 LiveKit 仍在同一 Compose）、模板/手册同步；必要的双后端配置契约、Sophon 定向回归和最新源码真实天气语音复测见 validation §8。此前误纳入的跨后端全量结果只作为历史旁证，不列为本期验收阻塞；续办未运行 NVIDIA 服务/GPU 验证。

## 1. 容器发布剩余实施

2026-11-26 用户确认 Sophon 部署可省略专业知识库或天气，保留 Tavily 公开检索。模板显式填写 `ENABLED_TOOLS`，启动脚本按启用项核对依赖。部署手册包含固定公网 IPv4 加私有 CA 的签发、信任与验证步骤。目标容器和实际语音验收按下表继续。

LiveKit/Redis 同 Compose。部署配置和启动操作见 [deployment.md](deployment.md)，验证见 validation §7。生产验收剩余顺序如下。

| 顺序 | 实施内容 | 涉及现有模块 | 完成条件 |
| --- | --- | --- | --- |
| 1 | 补齐 Sophon 容器验收运行时和宿主单入口 | 复用 scripts/validate_sophon_web.mjs；新增必要的验收 Dockerfile/Compose profile/宿主编排 | 浏览器、ffmpeg、固定官方材料在验收容器内；日志按窗口采集，输出有界摘要；不挂 Docker socket，不在宿主安装验收依赖 |
| 2 | 执行真实容器构建、迁移与发布验证 | 现有 API/Web Dockerfile、Compose、LiveKit、Alembic 0011 | 镜像/source hash 可核对，WSS/媒体/NAT 实际可达，PostgreSQL 升级保留旧数据和角色；失败可恢复匹配的旧镜像/备份 |
| 3 | 完成三类能力及可选配置的语音验收 | 现有 Provider → Coordinator → Runtime → Registry | 已启用的真实服务、可选服务停用、受控兜底、ASR 开关、多轮打断、参数保真和实际听音各有证据 |

API/Web 发布独立镜像，migrate 复用 API 镜像；LiveKit 与 PostgreSQL/Redis 同属一份 Compose，Sophon/CueKB 保持独立部署。AgentSession 已在 API 内，不另建第二套 Agent Runtime。验收容器是一次性任务，不是新增业务服务。

仅引入本轮批准的原生 task 意图解析 LLM，不增加 ASR 分类模型、第二套 Agent Runtime、强制 tool_choice、通用失败后无条件外发或猜测协议字段。原有公开兜底约束保持；LLM 不读取 ASR、历史、证据或权限，私有/敏感 task 不自动发给外部模型。

## 2. 模型与验收缺口

原生委派漏触发与已委派任务解析已在 b60028d 的固定官方录音集分别修复并复测，详见 validation 的已解决章节；不再作为当前待修复任务。更大样本稳定性、多轮、参数保真与完整口述仍单独验收，不能用该组通过替代。

型号保真使用固定音频、完整原生 task 和目标型号金标验证；F7005 → F705 不在 Router 中用 ASR 猜改。角色名字生效不等于完整 Persona 遵循；Gateway 回执、候选字幕、首段语音均不等于已核实完整回答。

ASR 启停由独立 Sophon Gateway 管理。本项目只验证字幕展示及业务解耦，不把开启/关闭切成新的业务模式。

## 3. 交付与验证边界

每阶段同步部署操作、实际架构、任务板和唯一验证记录；已经修复并通过对应验证的问题移入已解决章节，不让历史失败继续驱动当前工作。外部 VoiceChat 配套源码/镜像的变更须另按根 AGENTS.md 交付，不能随应用发布自动升级。

仓库检查包括部署契约、Compose 展开、Shell/文档语法、链接和 diff。真实部署在容器执行；缺少 Docker Engine 时不得声称镜像/真实网络通话验收通过，不刷新历史语音结果。完成后按根 AGENTS.md 交付 origin/main。
