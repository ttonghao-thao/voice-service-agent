# 当前验收证据

2026-10-08 云端自测；应用基线 `ee73afb` 加本轮自测修复，分支 `codex/q1003`。本页只维护最近可用证据及未验证边界；旧流水归档，不累加历史测试数。任务状态归 [任务板](TASK_BOARD.md)，放行目标归 [验收清单](development/validation.md)。

## 0. 当前审计与证据索引

| 项目 | 最近实际记录 | 证据与限制 |
| --- | --- | --- |
| 应用回归 | 2026-10-08：253 pytest、14 Node、10 Chromium，Ruff/前端构建通过，契约导出无差异 | Python 含 51 项独立 API 网络测试、十组连续序列及新增 20 项回归；有一条既有 Starlette/AnyIO 弃用警告。不是 ASR/TTS/听音或真实选路质量 |
| 本轮修复 | 拒绝 CueKB 3xx 正文/损坏压缩，部分 HTTP 响应受限重试；VoiceChat 非法握手/事件/音频归协议错误；本地 SQLite WAL | 新增故障用例先复现再修复；基线完整回归曾有两项 SQLite 锁失败，修复后全量通过；未改变生产 PG 配置或启用 NVIDIA 等待能力 |
| Docker/Compose | 2026-10-04：API/Web build、17 项隔离 Compose 检查通过，PG 迁移 0009 | 历史证据：静态音频资源权限 403 修复；PG/Redis/API/Web 启动与 HTTPS 验证。本轮未重新构建/部署，不证明生产恢复/容量 |
| 数据迁移 | 2026-10-08：临时 SQLite upgrade head → check → downgrade base → upgrade head → check 全部通过 | 0009；隔离 PostgreSQL/十列检查仍为 2026-10-04 历史证据，未在历史生产数据和真实负载下执行迁移/回滚 |
| 当前功能范围 | Q07/CTX1/EVAL1/LVA1–LVA3 已本地实现 | NVIDIA 等待期四项能力仍 false；增强模拟的 parent_call_id 不属于生产协议 |
| 历史代码发布 | 3bb0928 与文档 f498fa4 已推送 codex/q1003 并核对远端 | 这是 2026-10-04 的发布记录；本轮自测执行时 HEAD 为 ee73afb 加工作区修复，未执行部署 |
| 文档整理 | 2026-10-07 DOC5：仅 Markdown；链接/锚点、历史保留、代码对照和五类读取路径验证 | 方法、实际字节统计与限制见 [文档审计 §3](development/documentation.md#3-2026-10-07-审计与场景验证)，该次整理未重跑应用测试 |

本轮环境为 Python 3.12.14、Node 24.19.0、系统 Chromium 和独立 SQLite；CueKB HTTP、VoiceChat WS、兼容文本模型 HTTP 均为 loopback 模拟 API。执行根 README 的 pytest、Ruff、Node 单测、前端构建和契约导出命令，并按 [模拟 §3](simulated-full-flow.md#3-复现步骤) 显式设置 `SIMULATED_FULL_FLOW=1` 运行浏览器；修复后的服务已重启验证并关闭。重连回归另外确认最终 audio.done 和唯一工具结果。

本轮产物在忽略目录 `artifacts/simulation/`：python.xml、network.xml、browser.xml、summary.json（源代码/夹具 hash 与结果）。历史产物曾位于 `artifacts/live-agent/`；不保证新环境保留旧文件。独立服务模拟使用合成 PCM 与脚本转写/工具选择。其他浏览器用例的受控路由范围按各自测试区分。

## 1. 历史证据定位

| 要追溯的内容 | 固定日期记录 |
| --- | --- |
| 最近 LVA 发布 | [2026-10-04 发布记录](archive/2026-10-07/acceptance-report.md#2026-10-04-lva-发布至-codexq1003) |
| P1/P2、全量回归、Docker 403 与 17 项检查 | [2026-10-04 LVA 验证](archive/2026-10-07/acceptance-report.md#2026-10-04-live-p1p2口述结束恢复与等待交互) |
| P0 来源上下文和十组连续评测 | [CTX1/EVAL1](archive/2026-10-07/acceptance-report.md#2026-10-04-ctx1--eval1两个-p0-编码与验证) |
| 独立 API 网络模拟 | [当时模拟记录](archive/2026-10-07/acceptance-report.md#2026-10-04-独立-api-全流程模拟测试)；当前复现见 [模拟](simulated-full-flow.md) |
| Q07 基础、D01–D20、早期容器/文档记录 | [按日期索引](archive/2026-10-07/acceptance-report.md#1-按日期记录的编码与部署前验证)，先检索日期或 ID |
| 独立 VoiceChat 补丁/hash/CPU 协议检查 | [交付说明](../deploy/voicechat/README.md)；不是 GPU 行为验收 |

历史中的“当前/本轮/未实施”只适用于其日期，不用旧测试数覆盖最新证据。未执行、失败和不适用不得改写为通过。

## 2. 记录新验证

只有新增有效证据才更新 §0：执行日期、代码/模型/模板/镜像版本、环境、实际命令与结果、证据位置、未覆盖范围。新的复杂故障或发布流水放在对应日期归档，当前页只留结论及定位，不要求每次任务生成全仓总结。纯文档任务记录文档检查，不刷新应用测试日期。

## 3. 当前明确未验证

- 真实 CueKB Key/ACL、知识正确性、文本模型、VoiceChat 工具选择/续答、英文 ASR/TTS、GPU 和实际听音；连接 ready/正确文字不能替代这些证据。
- 指定 NVIDIA 版本的等待期语音交互/更正、安全结清与多 call 明确关联；当前 Adapter 保持关闭。一般模式的漏调用风险不因 Prompt 或工具网关而消失。
- 有序逐轮响应、尾帧、停止后继续、旧输出隔离、真实设备/长会话/轮换与断网；未知迟到 ID 归属和音频/转写一致性不能靠模拟证明。
- 生产数据迁移/锁、备份恢复、Redis/负载均衡故障、多副本粘性路由、真实多人并发/容量、公网证书和设备体验。

D07/V01–V12/Q07-T01–T27 的执行与放行条件统一在 [真实服务验收](development/validation.md)。阈值须基于真实样本/版本冻结；缺测、失败或核心权限/事实/旧结果隔离不通过时不宣称放行。
