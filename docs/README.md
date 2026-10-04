# 文档导航与维护规则

更新：2026-10-04。当前为 Q07 + CTX1/EVAL1 + 本轮 Live P1/P2；状态只看任务板，默认 legacy、可选 dual_tools。默认只加载一份主题文档的相关段落；不用顺序读完整套文档。

## 按问题定位

| 问题 | 读取位置 | 实现或证据入口 |
| --- | --- | --- |
| 已完成什么、还缺什么 | [任务板](TASK_BOARD.md) §1–3 | 验收 §0 的基线；不要先读历史验证记录 |
| 系统职责与调用流程 | [架构](architecture.md) §2、4 | `apps/api/app/{api,voice,sessions,agent_runtime,tools}` |
| 口述检查、结束/重连、等待进度/更正 | [Live 改进](live-agent-implementation.md) §1–4 | Provider 门槛、呈现/交付状态、两个工具的可选 operation，当前 NVIDIA 等待能力仍关闭 |
| 改问、取消、过期结果 | [架构](architecture.md) §5–6 | `sessions/coordinator.py`、`storage/store.py`、`voice/gateway.py` |
| 建议优化如何实施 | [架构](architecture.md) §10 | Q01–Q03；原件查看 Q04 见接入 §7 |
| 通用问答定位、即时接话、简单直查与复杂推理 | [Q07 详细设计](qa-routing-design.md#0-本轮编码范围与扩展契约) | 基础流程已编码并本地验证；先读实施范围，再查设计章节，D1/D3 等仍为后续能力 |
| 两个原生工具和后台 search_knowledge 的区别、新增工具 | [接入 §5](integration.md#5-第三方扩展d06) | `agent_runtime/dispatch.py` 的 NativeTool 与 `tools/registry.py` 的不同职责 |
| legacy / dual_tools、一般或严格回答、预算 | [部署：Q07 配置](deployment.md#问答模式与预算q07) | `config.py`、会话快照及 Alembic 0009；不是浏览器请求参数 |
| 门户、音频、消息格式 | [门户契约](portal-protocol.md) §3–5 | `contracts.py`、`api/routes.py`、`apps/web/src/audio/VoiceClient.ts` |
| CueKB / VoiceChat / 文本模型 / 身份 | [接入](integration.md) §2 / §3 / §4 / §6 | `tools/adapters.py`、`voice/provider.py`、`agent_runtime/runtime.py`、`api/auth.py` |
| 镜像、URL、迁移与恢复 | [部署](deployment.md) 对应标题 | `deploy/`、`scripts/deploy-cloud.sh`、`apps/api/migrations/` |
| 公网 Web 是否需要独立 Nginx、私网客户端怎样调用 API | [部署](deployment.md)「部署方式边界」；[接入](integration.md) §6 | `deploy/nginx.conf`、`deploy/compose.production.yaml`、`api/auth.py` |
| 云端剩余工作怎么做 | [部署](deployment.md)「D07 分阶段执行设计」 | D07-A–F；验收 V01–V12 |
| 哪些检查真实跑过、如何放行 | [验收](acceptance-report.md) §0、3–5 | §1–2 仅在追溯某次结果时读 |
| 约 3 秒查询与分段耗时 | [接入](integration.md) §4.1；[部署](deployment.md) | Runtime timing hooks、Store/SSE、门户 final 渲染 |
| 无声、48000 Hz、工具 ACK 与逐轮 response | [接入](integration.md) §3；[VoiceChat 补丁](../deploy/voicechat/README.md) | `voice/gateway.py`、`VoiceClient.ts`、独立 speech `audio_server.py` |
| 最新 VoiceChat 论文、离线容器及优化方案 | [论文与容器优化设计](voicechat-research-review.md) | Q05 设计待确认；已转换 Model Repository、工具等待限制与 D07 验收 |
| 语音文字按 GPT 截图展示、ASR 即时气泡与口述保留 | [门户 Q06 契约](portal-protocol.md#7-q06语音文字统一聊天展示) | 本地编码完成；关联、恢复、验收边界与 D07 现场待测项 |
| 本地验证命令 | [项目 README](../README.md) | 根目录运行；默认工作规则见 [AGENTS](../AGENTS.md) |
| 独立服务 API 怎样模拟、全流程覆盖与复现 | [模拟测试](simulated-full-flow.md) | 独立 HTTP/WS 监听、本项目真实适配器、隔离迁移与浏览器；结果见验收 §1 |
| 一般背景怎样用于知识查询、型号纠正和连续评测 | [上下文与评测](task-context-evaluation.md) | CTX1 / EVAL1、内部来源快照、Alembic 0008、十组连续网络场景和报告完整性 |

代码路径未标完整前缀时，以 `apps/api/app/` 为基准。

## 文档职责与读取预算

- **任务板**只维护状态、优先级、依赖和完成条件；**架构/接入/门户/部署**各自维护该主题设计；**验收**只维护证据和放行标准。README 不复制里程碑明细。
- 仓库没有 `task_road.md`；[TASK_BOARD.md](TASK_BOARD.md) 是唯一任务路线图。先按 ID 查任务板，再按表内链接查主题，不创建平行状态文件。
- 状态区分：已编码并本地验证、待真实验收、建议待排期、暂缓。代码和受控测试不能证明真实供应商能力；目标设计不能替代已实现行为。
- 先查标题（`rg -n '^##' docs/architecture.md`）或 ID（`rg -n 'Q01|D07' docs/TASK_BOARD.md`），再截取命中章节。通常一节设计加相关函数/测试足够；发现跨边界依赖才追加邻接章节。
- 避免整读 OpenAPI/事件 JSON；先查 `contracts.py` 和目标 route，必要时只提取对应 schema。测试按场景定位，不默认运行或加载全套。
- 文档优化以减少每次读取范围为目标，不以删除验收证据换取短文档；本文不承诺固定 token 节省比例。

## 更新与历史

修改行为时同步主题设计、任务状态及实际验证记录；没有执行的项保持待验证。任务板 ID 保持稳定，链接尽量指向章节。检查相对链接、标题和 `git diff --check`；文档整理阶段仅编辑 Markdown，发布前另核对已有代码改动。

`archive/2026-09-16/` 保存早期总设计、过程提案和 SHA-256 清单，仅供明确追溯；不作为当前规范，不递归加载、不重写快照。已有历史验证按日期保留在验收记录，只有规模影响按需读取时再归档，不新增并行总设计。
