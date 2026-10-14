# 当前验收证据

2026-10-14 发布检查基于 `codex/q1003@cce8e75` 加本次恢复、握手及诊断修复；已保留远端新增的 VoiceChat 源码快照。最新完整结果为 **340 项 Python、26 项 Node、18 项 Chromium 全通过**，Ruff/TypeScript/前端构建/diff 与文档链接检查通过。本轮提交用于推送应用代码，没有构建或发布生产容器。任务状态归 [任务板](TASK_BOARD.md)，放行目标归 [验收清单](development/validation.md)。

用户随后报告 c6bba4e 升级后的 15:54 通话已确认字符串 tools 回显、收到 202 音频块；握手故障恢复。句首 Hell 仍缺失、function 200 步全 PAD、普通回答未出现结束边界，页面明确显示 VOICE_ANSWER_TIMEOUT。运行文件 hash、同 sequence 原始 PCM/模型输出与真实 CueKB/听音仍待取得；完整分析与下一轮采集见 [本次复测](voicechat-validation-20261014.md)。

## 0. 当前审计与证据索引

| 项目 | 最近实际记录 | 证据与限制 |
| --- | --- | --- |
| 完整 backend 文件交付 | 2026-10-14：按用户要求将修补后的完整 model.py 收录到 docs/voicechat；46 项后端边界、2 项真实 WS 握手与 8 项归档 CPU 诊断测试共 56 项通过；hash、补丁反向/正向应用、编译与 Ruff/diff 检查通过 | `artifacts/voice-recovery/bundled-backend.xml`、`bundled-backend-diagnostics.xml`；model.py hash 为 bc427109…806a8，旧文件保留于 Git b924c72。CPU 诊断使用已有 NumPy 2.3.5，未安装整套独立 Speech 依赖或进行 GPU 验证；只更新完整文件交付方式，不扩展已验证行为 |
| 15:54 现场复测 | 用户提供 API/VoiceChat 日志：注册 tools_count=2/type=str，Triton 100 次、submitted/completed=256000；ASR 最终 44 字符、function PAD=200/SOTC=EOTC=0；40.054 开始回答、50.961 超时；queued=sent=167 | 这是远端日志报告，不是本工作区 GPU/人工听音验证。缺少实际 PCM/权重与目录采集；没有证据证明 classic/Pro 来自 CueKB。已确认超时的直接原因是缺结束边界，ASR/function 具体根因未闭合 |
| PAD 尾部完成补丁 | 2026-10-14：46 项新 CPU 契约测试 + 22 项 QA 策略/执行回归共 68 项通过；Ruff/diff/文档链接和原始 8 文件 hash 检查通过 | `artifacts/voice-recovery/pad-tail-completion.xml`；先严格 apply/reverse/hash，再执行实际 backend generate/turn/TTS/parser 和 WS 收尾方法，I/O 为受控夹具。默认关闭，仅修既有强制静音与回合结束的缺口；不保证 10 秒内结束、实际 TTS 完整、ASR 或工具选路正确。未部署 GPU/构建生产镜像；68 项不与前次完整 340 相加 |
| 握手回显兼容 | 2026-10-14：官方 session.updated.tools JSON 字符串场景先复现 legacy/自定义目录 2 项失败；修复后字符串/数组同等校验，篡改/重复/非法 JSON 均仍拒绝，成功后首个 80 ms PCM 可发送；四模块定向 124 项通过，Ruff/diff 检查通过 | `artifacts/voice-recovery/handshake.xml`；模块为 native_tool_catalog、protocol、voicechat_probe、voice。定向数不与此前完整 318 相加；无真实 GPU/CueKB/听音；补丁 `serialized-tools-handshake.patch` 已核对可应用于 d0dea88。门户错误已确认 tools 类型/数量检查；cce8e75 的实际服务源码以字符串回显，新增 CPU/WS 回归已覆盖。124 项是此前定向记录，最终纳入完整 340 项；运行文件 hash 及修复后的送音待现场确认 |
| 应用回归 | 2026-10-14：最终完整 pytest 340、26 Node、18 Chromium 全通过；Ruff/TypeScript/前端构建/diff 检查通过 | Python 含独立 API 网络、连续序列、全部恢复/握手和静态产物检查，以及 2 项归档真实握手方法回归；JUnit 使用 xunit1，仅 1 条既有 Starlette/AnyIO 弃用警告。Chromium 使用实际 AudioWorklet 与显式模拟 API，未跳过用例。本轮未改 schema，不导出重复契约；不是 GPU/真实选路/听音验收 |
| 论文 v2 与融合审计 | 2026-10-13：深读 v2 全文/附录并逐节对比 v1；固定 HF commit `443794e` 的配置检查为 1/1/2 matches；新增 7 项融合审计用例，相关 30 项通过，Ruff/diff 检查通过 | 产物为 `artifacts/voicechat-paper/` 的 source-manifest.json、official-fusion-audit.json、diagnostics.xml；本轮只扩展独立诊断脚本，不修改应用运行时/模型，未重跑上一轮完整 318 项或前端测试，不累加计数。没有现场 GPU/模型权重/原始 WAV 验证；见 [v2 设计](voicechat-paper-v2-review.md) |
| NVIDIA 源码/诊断 | 已克隆并固定 `097dfe9`，核读官方部署/API、模板、转换、wrapper、cache、RNNT、pipeline/Triton 示例；修复探针停供音频及多工具注册不一致，新增 CPU 产物检查；上游真实方法受控执行确认缺失 function key 与实际 token 的不同映射 | 23 项新增回归使用 loopback WS/合成产物，纳入最终 318 项；AST 受控检查无 GPU/tensor 推理；现已取得 cce8e75 的 WS/backend 快照；实际模型产物、运行 hash 与授权测试 WAV 未取得，不能宣称 ASR/function 根因已修复；见 [源码核对](voicechat-upstream-analysis.md) |
| 本轮修复 | 空输出不触发工具许可拒绝/不消耗续答许可；拒绝或上游断开时补送 final ASR、提前发送错误、保护关闭审计；门户显示错误 code 和断线重试提示，清除等待状态 | 受控用例先复现空边界误拒绝、续答许可丢失、转写发送取消、错误晚于清理、无错误断线和错误后 DELETE 中断关闭。非空未授权文本/PCM 仍拒绝；未自动提交或重播，未改会话策略或启用 NVIDIA 等待能力 |
| Docker/Compose | 2026-10-13：API/Web Docker build 及 Compose config --quiet 通过 | 本轮未启动生产 Compose 或验证 PG/Redis/TLS/容量；17 项隔离启动检查仍为 2026-10-04 的历史证据 |
| 数据迁移 | 2026-10-13：SQLite 从 0006 升级 head=0010、Alembic check、降级 0007/0006 后再升级 head 的回归通过，旧会话历史保留 | 0010 增加全局工具目录；隔离 PostgreSQL/十列检查仍为 2026-10-04 历史证据，未在生产数据和真实负载下执行本轮迁移/回滚 |
| 当前功能范围 | Q07/CTX1/EVAL1/LVA1–LVA3 已本地实现 | NVIDIA 等待期四项能力仍 false；增强模拟的 parent_call_id 不属于生产协议 |
| 历史代码发布 | 3bb0928 与文档 f498fa4 已推送 codex/q1003 并核对远端 | 这是 2026-10-04 的发布记录；本轮自测执行时 HEAD 为 ee73afb 加工作区修复，未执行部署 |
| 文档整理 | 2026-10-07 DOC5：仅 Markdown；链接/锚点、历史保留、代码对照和五类读取路径验证 | 方法、实际字节统计与限制见 [文档审计 §3](development/documentation.md#3-2026-10-07-审计与场景验证)，该次整理未重跑应用测试 |
| q1003 目标设计 | 2026-10-13：目标方案原基线 `56d7774`，按当前实现 `1158760` 刷新工具目录边界；整理业务目标、`social_only`/响应隔离、诊断路径、里程碑和离线/真实门槛 | 仅 Markdown，未编码 `social_only`/quarantine，未验证真实 VoiceChat/CueKB/GPU/浏览器听音；见 [目标架构](q1003-target-architecture.md) |
| D21 当前语音故障 | 2026-10-13：Gateway/门户恢复已本地实现并覆盖 legacy/dual_tools、非空文本/PCM 拒绝、空边界、上游结束/断开、错误后立即停止与手动重试；目标语音与句首缺失描述继续保留 | 现已提供 0 音频块握手日志、门户错误和源码快照；真实测试 WAV、VoiceChat 运行 hash 与同 sequence 音频推理捕获仍缺；ASR/function 根因尚未闭合，未验证真实 CueKB/GPU/听音；见 [D21 §7](voicechat-fault-recovery-design.md#7-门户失败恢复) |
| 预录 WAV 输入 | 2026-10-13：26 项 Web 单测、生产构建和 Web 镜像构建通过；完整 Chromium 14 通过、1 项按条件跳过，其中新增文件输入 5 项全通过；既有独立 API 模拟矩阵 51 项通过 | Chromium 使用实际 AudioContext/AudioBufferSource/AudioWorklet；HTTP/WS 与独立 CueKB/VoiceChat/文本模型均为受控夹具。覆盖格式/大小/时长、ready 前不发送、实时输入/EOF、无麦克风、错误/轮换不重播、启动竞态及文字互斥；未验证真实 VoiceChat/CueKB、GPU、ASR/TTS 或听音 |
| 全局工具目录与注册 | 2026-10-13：三工具真实应用分派及原始 call_id 回填、六工具无固定数量截断、重启保留、全局可见/调用授权、CAS、启停/reload、活动连接刷新、无效候选和错误回显拒绝均通过；SQLite 0010 升降级及 Alembic check 通过 | 配置更新使麦克风重新注册，文件停止且不重播，新增两个 Chromium 场景通过。API/Web 镜像构建及 Compose config --quiet 通过；未执行生产 Compose 启动、PG 数据迁移、真实 VoiceChat/GPU/模型工具质量验收。统一契约见 [工具目录](interfaces/voice-tools.md) |

上述完整应用回归的 D21 环境为 Python 3.12.14、Node 24.19.0、系统 Chromium 和独立 SQLite；CueKB HTTP、VoiceChat WS、兼容文本模型 HTTP 均为 loopback 模拟 API。执行根 README 的 pytest、Ruff、Node 单测和前端构建命令，并按 [模拟 §3](simulated-full-flow.md#3-复现步骤) 显式设置 `SIMULATED_FULL_FLOW=1` 运行全部浏览器用例；没有真实服务连接或听音验收。

最终发布产物在忽略目录 `artifacts/q1003-release/`：python.xml（340 项完整回归）、browser.xml（18 项浏览器回归）。命令为 `.venv/bin/python -m pytest -q --junitxml=artifacts/q1003-release/python.xml -o junit_family=xunit1`；浏览器按 [模拟 §3](simulated-full-flow.md#3-复现步骤) 显式设置 `SIMULATED_FULL_FLOW=1`。主机 Python 3.12.14、Node 24.19.0、Chromium 154；这些文件不提交 Git。

此前 D21 产物在 `artifacts/voice-recovery/`，论文与静态配置证据在 `artifacts/voicechat-paper/`；此前计数不与最新完整结果累加。`test_voicechat_snapshot_handshake.py` 从 hash 核验后的归档源码提取实际会话更新方法，执行其 JSON 字符串回显和后台 prefill 控制流，再用真实 API Adapter 发送首个 80 ms PCM；模板、Triton 和音频内容为受控夹具，无 GPU 推理。b924c72 交付时未修改原始 8 文件；随后按用户要求更新完整 model.py 与 CPU 诊断夹具，当前 8 文件 hash 以更新后的 snapshot manifest 为准，旧快照可由 Git b924c72 恢复。

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
