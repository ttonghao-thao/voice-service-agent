# 当前待实施方案

类型：方案，非实现或通过记录。需求以 [delivery.md](delivery.md) 为准，实际行为看 [architecture.md](architecture.md)，状态只在 [TASK_BOARD.md](TASK_BOARD.md)，操作和证据只在 [validation.md](validation.md)。

## 1. 容器发布与三类查询对齐

2026-11-23 确认：系统同时提供天气、通用知识、专业知识库查询，通用知识检索承担受控兜底；不存在仅启用 search_knowledge 的产品部署场景。尚未接入某个依赖是交付缺口，旧 Provider 能力限制是兼容事实，均不能改写成另一个产品模式。

| 顺序 | 实施内容 | 涉及现有模块 | 完成条件 |
| --- | --- | --- | --- |
| 1 | 统一三类查询的配置模板与部署检查，移除把旧单知识工具当作产品范围的假设 | config.py、.env.example、scripts/deploy-cloud.sh、scripts/verify_deployment.py | 对同一发布检查 QWeather、Tavily、CueKB；缺项明确阻止完整放行，不填虚假凭据/KB UUID |
| 2 | 区分 Provider 原生入口与业务能力；核对当前全局目录，避免部署时无审查覆盖后台配置 | voice/gateway.py、agent_runtime/tool_catalog.py、scripts/configure_native_cuekb.py | Sophon client delegate 与三类 Router 的检查一致；不以 NVIDIA 知识工具名称/数量认定 Sophon 注册或全系统能力 |
| 3 | 补齐 Sophon 容器验收运行时和宿主单入口 | 复用 scripts/validate_sophon_web.mjs；新增必要的验收 Dockerfile/Compose profile/宿主编排 | 浏览器、ffmpeg、固定官方材料在验收容器内；日志按窗口采集，输出有界摘要；不挂 Docker socket，不在宿主安装验收依赖 |
| 4 | 执行真实容器构建、迁移与发布验证 | 现有 API/Web Dockerfile、Compose、Alembic 0011 | 镜像/source hash 可核对，PostgreSQL 升级保留旧数据和角色；失败可恢复匹配的旧镜像/备份 |
| 5 | 完成三类查询、受控兜底和语音闭环验收 | 现有 Provider → Coordinator → Runtime → Registry | 真实 CueKB、天气、Tavily、ASR 开关、多轮打断、参数保真和实际听音各有独立证据 |

API/Web 发布独立镜像，migrate 复用 API 镜像；LiveKit、Sophon、CueKB 保持部署边界。AgentSession 已在 API 内，不另建第二套 Agent Runtime。验收容器是一次性任务，不是新增业务服务。

不新增第二个 LLM、ASR 关键词补调、强制 tool_choice、通用失败后无条件外发或猜测协议字段。当前 Router 的公开兜底约束按架构记录；文档整理不扩展运行时兜底条件。

## 2. 模型与验收缺口

原生委派漏触发与已委派任务解析必须分别定位、统计和修复。官方 14 项的原始 1/7 失败基线与后续整组复测均保留在验证记录，不能用规则单测或挑选成功样本替代真实原生触发。

型号保真使用固定音频、完整原生 task 和目标型号金标验证；F7005 → F705 不在 Router 中用 ASR 猜改。角色名字生效不等于完整 Persona 遵循；Gateway 回执、候选字幕、首段语音均不等于已核实完整回答。

ASR 启停由独立 Sophon Gateway 管理。本项目只验证字幕展示及业务解耦，不把开启/关闭切成新的业务模式。

## 3. 本轮文档整理边界

只修改 Markdown：将已确认需求写入根入口和交付说明；将当前代码事实、部署操作、待实施方案、待办与执行证据分开；删除重复及已替代的长流水。外部 VoiceChat 源码、manifest、应用代码、Dockerfile、配置和依赖均不因整理而修改。

检查本地链接/锚点、代码路径、Shell 片段语法、内容一致性及 diff；不重跑应用测试，也不刷新已有测试的日期或通过状态。完成后按根 AGENTS.md 交付 origin/main。
