# 文档导航与维护规则

更新：2026-09-23。默认只加载一份主题文档的相关段落；不用顺序读完整套文档。

## 按问题定位

| 问题 | 读取位置 | 实现或证据入口 |
| --- | --- | --- |
| 已完成什么、还缺什么 | [任务板](TASK_BOARD.md) §1–3 | 验收 §0 的基线；不要先读历史验证记录 |
| 系统职责与调用流程 | [架构](architecture.md) §2、4 | `apps/api/app/{api,voice,sessions,agent_runtime,tools}` |
| 改问、取消、过期结果 | [架构](architecture.md) §5–6 | `sessions/coordinator.py`、`storage/store.py`、`voice/gateway.py` |
| 建议优化如何实施 | [架构](architecture.md) §10 | Q01–Q03；原件查看 Q04 见接入 §7 |
| 门户、音频、消息格式 | [门户契约](portal-protocol.md) §3–5 | `contracts.py`、`api/routes.py`、`apps/web/src/audio/VoiceClient.ts` |
| CueKB / VoiceChat / 文本模型 / 身份 | [接入](integration.md) §2 / §3 / §4 / §6 | `tools/adapters.py`、`voice/provider.py`、`agent_runtime/runtime.py`、`api/auth.py` |
| 镜像、URL、迁移与恢复 | [部署](deployment.md) 对应标题 | `deploy/`、`scripts/deploy-cloud.sh`、`apps/api/migrations/` |
| 公网 Web 是否需要独立 Nginx、私网客户端怎样调用 API | [部署](deployment.md)「部署方式边界」；[接入](integration.md) §6 | `deploy/nginx.conf`、`deploy/compose.production.yaml`、`api/auth.py` |
| 云端剩余工作怎么做 | [部署](deployment.md)「D07 分阶段执行设计」 | D07-A–F；验收 V01–V12 |
| 哪些检查真实跑过、如何放行 | [验收](acceptance-report.md) §0、3–5 | §1–2 仅在追溯某次结果时读 |
| 本地验证命令 | [项目 README](../README.md) | 根目录运行；默认工作规则见 [AGENTS](../AGENTS.md) |

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
