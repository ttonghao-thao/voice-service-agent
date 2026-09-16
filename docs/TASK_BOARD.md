# 当前任务板

更新：2026-09-16。目标设计见 [architecture.md](architecture.md)，状态证据见 [acceptance-report.md](acceptance-report.md)。

## 文档定稿任务

| 阶段 | 状态 | 结果 |
| --- | --- | --- |
| 核对现有结构、契约、调用链和讨论结论 | 完成 | 沿用现有模块，保留实际能力限制 |
| 最终架构与门户消息接口设计 | 完成 | 架构、门户契约、服务接入分主题维护 |
| 文档去重与按需阅读 | 完成 | AGENTS/索引精简，旧总设计与过程提案移出活动目录 |
| 文档验证 | 完成 | 35 处本地链接、9 条 HTTP 路由与实现一致、9 份快照 SHA-256 校验、无非文档变更、差异检查通过 |

## 当前实现基础

已有 FastAPI 会话 API、HTTP/SSE/WSS、VoiceChat native bridge、单写入器、票据、epoch/turn 隔离、BusinessRuntime、ToolRegistry、PostgreSQL/Redis 接入及 AudioWorklet。

当前前端是 React 工作台；认证以 operator/admin 为主；RAG 是 `/v1/retrieve` 代理契约；生产配置仍强制天气；硬打断包含取消和断连；默认 105 秒结束语音。以上是待演进现状，不是最终需求。

## 待实施里程碑

D01/D02 已完成代码和本地自动化验证；真实依赖验收仍保留为未完成。D03 可继续独立推进，D04 在其基础上形成主闭环，D05/D06 补齐交互与扩展，D07 作生产验收。

| ID | 任务与影响模块 | 完成条件 | 状态 |
| --- | --- | --- | --- |
| D01 | 冻结门户契约；固定 VoiceChat 镜像/API 和 CueKB 版本；真实能力探测。`voice/`、`contracts/`、探测脚本 | 原生 call/result、格式、5 秒工具等待插话、失效结果结清有事件及音频证据；区分基础模式和增强模式 | 代码完成；真实 VoiceChat 事件/音频验收待环境 |
| D02 | 客户认证与知识权限。`api/auth.py`、API、历史/引用读取 | 客户不具备管理权限；会话隔离、KB 交集和撤权通过；身份不能从请求正文覆盖 | 代码及本地隔离/撤权测试完成；真实 OIDC/CueKB ACL 待验收 |
| D03 | CueKBAdapter、证据契约与引用。`tools/`、`contracts/`、Runtime/存储 | `/v1/search`、UUID KB、trace/status/version/anchor 正确映射；空命中/降级/错误和权限测试通过；真实 CueKB 闭环 | 待实施 |
| D04 | 简单 HTML 客户门户。`apps/web/`、门户消息接口 | 开始/结束、字幕、语音播放、状态、答案/引用、窄屏及可选文字；不含管理工作台；复用现有音频模块 | 待实施 |
| D05 | 任务 revision、停止播报/取消/改问分离、pending call 恢复、长会话。`sessions/`、`voice/`、`storage/` | 晚到旧结果不提交、不播报；旧 call 结清或关闭；新会话无旧音频；附和不误取消；迁移与恢复测试通过 | 待实施；连续交谈受 D01 门槛限制 |
| D06 | 按部署及用户选择启用工具。`config.py`、registry、capabilities/health、部署模板 | CueKB-only 模式不要求天气；仅暴露已启用且授权工具；新增第三方不改语音主流程 | 待实施 |
| D07 | 真实端到端、故障、性能、Docker 与上线。部署及测试模块 | 客户语音→CueKB→实际口述正确；PostgreSQL/Redis/SSO/容器与长会话通过；记录版本、样本和阈值 | 待实施 |

## 实施约束与暂缓项

- D05 需要新增任务状态/版本时使用 Alembic；保留历史数据兼容，不用 create_all 替代迁移。
- schema/协议随实际代码同步导出，不能只改 JSON 声称新接口已经实现。
- KnowledgeAgent 仅在复杂知识样本证明收益后加入；首期不增加递归多 agent 或新队列服务。
- 不更换语音模型、不训练中文、不建设 CueKB 索引，不在本轮增加 WebRTC/SIP、支付交易或公开匿名管理入口。
- 天气/股票供应商待实际业务选择；尚未接入时不能纳入对客户的可用能力。
- 用户确认新需求时修改负责该主题的文档与本表，避免再建立第二份总设计或长期追加聊天纪要。
