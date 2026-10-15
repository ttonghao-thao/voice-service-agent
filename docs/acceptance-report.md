# 当前验收证据

整理日期：2026-10-15，代码基线 `63e7059`。**真实知识语音闭环尚未放行。**现场最新报告仍是 2026-10-14 15:54 通话；新 PAD 收尾版本及 knowledge_required 切换尚无新结果。状态见 [任务板](TASK_BOARD.md)，要求见 [验证清单](development/validation.md)。

## 0. 当前审计与证据索引

以下是分别执行的验证，不累加成一个“当前全量通过数”，也不更新历史测试的执行日期。

| 日期 / 版本 | 实际结果 | 限制与证据位置 |
| --- | --- | --- |
| 2026-10-14 / c6bba4e | 应用完整回归：340 Python、26 Node、18 Chromium；Ruff、TypeScript、前端构建通过 | API/VoiceChat/CueKB/文本模型为受控网络夹具；artifacts/q1003-release/{python,browser}.xml。Python 3.12.14、Node 24.19、Chromium 154；非 GPU/真实知识/听音 |
| 2026-10-14 / 用户现场日志 | tools_count=2/type=str；202 输入块；function PAD=200；最终转写缺 Hell；无结束事件后 VOICE_ANSWER_TIMEOUT，门户显示错误 | [完整判定](voicechat-validation-20261014.md)。无本轮 CueKB 调用证据；未取得实际 PCM、权重和运行 hash |
| 2026-10-14 / b924c72 | PAD 补丁 46 CPU 边界 + 22 QA 回归通过 | artifacts/voice-recovery/pad-tail-completion.xml；受控 backend/WS 方法，不保证触发时刻或真实语音完整 |
| 2026-10-14 / 2af7566 | 完整 backend 文件交付验证 48 + 8 CPU 诊断通过；hash、补丁反向/正向、编译检查通过 | artifacts/voice-recovery/bundled-backend*.xml；NumPy 2.3.5，未安装整套 GPU 依赖；hash 以 snapshot-manifest 为准 |
| 2026-10-15 / 63e7059 | VoiceChat 相关 62 项定向测试通过；四个运行文件编译、启动命令 shell 语法、diff 检查通过 | 对应 pad_tail_completion、snapshot_handshake、artifacts 三模块；仅交付与规则文档，未执行 GPU docker run |
| 2026-10-13 / 工具目录发布验证 | API/Web 构建、Compose config、SQLite 0006→0010 及降升级/检查通过 | 仅本地；不证明生产 PG 迁移、TLS、Redis、并发或云端部署 |
| 2026-10-13 / 论文与静态产物审计 | 固定上游 097dfe9、HF config 443794e 的融合声明 1/1/2；相关诊断 30 项通过 | artifacts/voicechat-paper/；未验证现场权重实际加载。研究细节按需读 [论文](voicechat-paper-v2-review.md)、[源码](voicechat-upstream-analysis.md) |

工具注册兼容的早期 124 项等定向结果已被对应完整应用回归覆盖，过程不再重复列入当前页。只有输入/函数/回答/业务/播放关联到同一轮的证据，才能判断端到端成功。

## 1. 历史证据定位

需要追溯早期 P1/P2、CTX1、模拟与发布记录时查 [历史验收](archive/2026-10-07/acceptance-report.md)；此次精简前的详细流水可查 Git `63e7059:docs/acceptance-report.md`。历史中的“当前/本轮”只属于其记录日期，不能覆盖新证据。

## 2. 记录新验证

每条新证据必须带执行日期、代码/模型/模板/镜像版本、策略/目录、环境、命令/样本、结果及未覆盖范围。失败、缺测和不适用分别记录；探针合成工具结果不当作 CueKB 成功，预算 EOS 不当作自然完整回答。普通日志无正文，授权原始采集放忽略目录。

本次文档整理只做代码事实、链接/锚点、围栏与 diff 核验，不重跑应用测试、不增加现场通过项；检查结果随本次提交说明记录。

## 3. 当前明确未验证

- ASR 首差异、全 PAD 根因、自然 EOS 缺失及新预算保护的触发/完整听音。
- 严格策略实际新会话、真实 CueKB/ACL/外置模型、同 call 回填及授权口述闭环。
- 工具等待期语音修订、未知迟到 response 归属、转写与实际音频一致性；NVIDIA 四项等待能力仍为 false。
- 真实播放、长会话/轮换/断网、生产数据迁移/备份、Redis/多副本故障与容量。

完整 D07/V01–V12/Q07-T01–T27 要求保留在 [验证清单](development/validation.md)。本地 fixture、容器 ready、文本正确及收到音频包均不能替代真实验收。
