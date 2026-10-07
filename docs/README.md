# 文档导航

先选一行，只读取所列章节；知道主题时可直接跳过本页。实现基线 `f498fa4`（2026-10-07 核对）；状态归任务板，证据归验收。代码路径相对 `apps/api/app/`，前端路径另标。

## 按任务选主题

| 文档 / 职责 | 何时读取 / 首查章节 | 对应代码 | 必要时追加的依赖 |
| --- | --- | --- | --- |
| [系统架构](architecture.md)：模块与数据流 | 跨模块/架构改动；§2–4 | voice、sessions、agent_runtime、tools、storage | 决策 → 受影响主题 |
| [知识执行](qa-routing-design.md)：模式、工具、直查/升级 | 修改选路、注册或证据处理；§1–4 | agent_runtime、tools/registry.py | 上下文 → CueKB；口述变更再读交付 |
| [上下文与评测](task-context-evaluation.md)：来源条件/跨轮纠正 | 修改参数继承或历史；§1–3；评测查 §4–5 | task_context.py、sessions、storage | 知识执行；等待修订再读交付 §3 |
| [口述与交付](live-agent-implementation.md)：检查、结束/恢复、等待交互 | 分别查 §1、§2、§3 | evidence.py、voice、sessions、storage；Web audio | 门户协议；真实等待门槛再读接入 |
| [门户协议](portal-protocol.md)：HTTP/SSE/WS 与显示语义 | 前端/接口/播放；§1–4 | contracts.py、api/routes.py；apps/web/src、public | 改呈现/恢复读交付，供应商事件读接入 |
| [外部接入](integration.md)：VoiceChat、文本模型、权限 | Adapter/注册/鉴权；§3/4/5/6 | voice/provider.py、runtime.py、api/auth.py | CueKB 契约或对应交付；故障读排查 |
| [CueKB 契约](interfaces/cuekb.md)：请求/证据/错误 | 检索 Adapter/映射；§1–3 | tools/adapters.py、tools/schemas.py | 影响证据判定再读知识执行 |
| [部署](deployment.md)：唯一配置与运维操作 | 改配置、镜像、迁移、租约；按标题 | config.py、migrations；deploy、scripts/deploy-cloud.sh | 排查 → 真实验收 |
| [故障排查](operations/troubleshooting.md)：症状/日志/时延 | 故障先查 §1；耗时查 §2–3 | Gateway、Runtime、Registry、Store、Web | 表中实际失败边界对应主题 |
| [模拟全流程](simulated-full-flow.md)：网络夹具与复现 | 新增集成/E2E；§1–3 | scripts/simulate_full_flow.py、tests | 连续序列读上下文 §4–5；命令读根 README |
| [关键决策](decisions/architecture-decisions.md)：原因与代价 | 改边界或评估替代方案；按 ADR ID | 涉及模块列在各决策中 | 当前主题；历史方案仅在追溯时读 |
| [任务板](TASK_BOARD.md)：实现/计划状态 | 排期或确认剩余工作；§1–3 | ID 对应主题/测试 | 验收报告；不要默认追历史 |
| [验收报告](acceptance-report.md)：已执行的证据 | 判断版本可用性；§0/§3 | 本地测试/部署记录 | [验收清单](development/validation.md)：D07/V/Q07 标准 |
| [文档维护](development/documentation.md)：职责/更新规则 | 文档重组、归档或维护范围不清；§1–2 | 仅 Markdown | §3 为本轮审计，平常不加载 |

独立 Speech 补丁的文件/hash/构建命令仍由 [交付 README](../deploy/voicechat/README.md) 唯一维护。其版本匹配不代表 GPU/实际音频已验证。

## 信息归属与历史

字段定义以 `contracts.py` / 工具 schema / routes 为准，生成契约只查目标对象；参数默认值以 `config.py` / `.env.example` 为准，操作说明只在部署维护。索引记录职责和依赖，不复制协议、配置、实现状态或测试计数。

历史研究、完整旧方案和日期流水见 [归档索引](archive/README.md)，默认不读；旧方案中同名字段/能力不覆盖当前代码。默认不同时加载全部链接；完整审计等任务可按实际需要扩大范围。
