# 当前任务板

更新：2026-10-18；本次原生单工具实现基于 `codex/q1003`。本页唯一维护状态；具体实施及依赖见 [方案 §4](q1003-target-architecture.md#4-实施计划与决策门槛)，实际证据见 [验收](acceptance-report.md)。部署准备与真实验证分开记录。

## 1. 当前结论与审计基线

应用控制链已有实现。2026-10-18 14:27 现场窗口已证明 VoiceChat 最终 Prompt 与 Triton 接收 hash 一致、Nano 9B 解码出 `search_knowledge_base`、API selection 和 CueKB 检索通过；API 只收到并执行 1 次调用，VoiceChat 合并 2 次重复解码，两个诊断均通过。当前唯一阻塞是 5 个 `evidence_status=sufficient` 命中因应用省略周边 context 而全部被语音证据过滤；准入已改为接受 CueKB 明确判定充分的完整主命中，同时继续拒绝真正截断和未评估且无完整块证明的证据，待重建 API 复测。故障二是 `hello.wav` 句首 `Hello` 丢失，已确认独立存在并暂缓定位。真实知识语音链路仍未放行，详见 [VoiceChat 工具选择定位](voicechat/native-tool-selection-validation.md)。

当前需求已确认：VoiceChat 自主决定直接回答或调用工具，后端只使用 VoiceChat，不引入独立 ASR/TTS，不根据最终转写自动启动业务。本次实现以 dual_tools/general_qa 为部署目标；旧 knowledge_required 现场证据属于严格模式，不能作为当前目标配置。先打通功能再优化时延，详见 [实施顺序](q1003-target-architecture.md#4-实施计划与决策门槛)。

本期进一步收敛到一个原生业务工具 search_knowledge_base(query)，公开名称和内部可信 implementation 完全同名，后端由 CueKB 提供检索。默认目录、执行器和录音验证脚本已编码；AGENT_PROVIDER=disabled，不需外置模型配置。旧 lookup/reason、SDK 工作流仅保留兼容覆盖，不能用于本期放行。部署及错误分界见 [单工具验证](native-cuekb-validation.md)。

## 2. 已完成的编码里程碑

| 范围 / 旧 ID | 已实现及边界 |
| --- | --- |
| 知识执行 Q07-A–D、CTX1/EVAL1 | 可配置原生多工具、direct/reasoned、单次升级、来源上下文及模拟评测；真实选择质量待验 |
| 会话/交付 LVA1–LVA3、D01–D20 | capability、证据/权限、epoch/revision/租约、交付台账、播放控制、进度；NVIDIA 等待语音修订仍关闭 |
| WEB-FILE、TOOL-CATALOG | WAV 实时输入、后台全局目录与刷新重连；dual_tools 不限制工具数量 |
| D21-HANDSHAKE / RECOVERY | 字符串 tools 回显、空事件边界、错误/转写交付与重试；现场确认注册及可见超时，其余恢复矩阵待验 |
| D21-PROBE / END | 连续送音探针、静态产物检查、WS/backend 隔离采集、可选 PAD 结束保护；CPU 证据不替代 GPU |
| VoiceChat 交付规则 | 完整外部修改文件、manifest、容器命令已在 docs/voicechat；AGENTS 要求后续同步 |
| 镜像内置验证材料 | API 内置单入口、底层验证/探针、case 配置和四段 WAV；VoiceChat 派生镜像内置客户端、静态检查器和相同输入。一次 `docker exec` 完成 API 项目验证并只返回窗口、ID、四条 case 和故障分类；同一 Python 可将 VoiceChat 日志窗口归纳为五行、API 工具生命周期归纳为三行 |
| P1-A 严格提示一致性 | Provider/两份 voice prompt 统一授权口述与关联固定 ACK，legacy 保持 bridge；未经授权的问候/澄清/限制说明不放行，沉默时自动澄清与真实模型行为另行验证 |

## 3. 待完成与建议顺序

| 任务 | 当前状态 | 下一步完成条件 |
| --- | --- | --- |
| VoiceChat 一期原生单工具 | 工具注册、Prompt 传输、Nano 9B function 解码、API selection、CueKB 调用及重复调用合并已通过现场验证。语音证据现接受 CueKB 明确 `sufficient` 且仅省略周边 context 的完整主命中，继续拒绝真正截断和未评估证据。公开名称统一为 search_knowledge_base(query)，旧拼写严格拒绝。问候首词故障暂缓 | 仅重建 API 后运行单入口；放行要求产品 case `cuekb_evidence=sufficient voice_evidence=passed`、退出码 0。若仍需链路复核，API 应保持 `received=1 unique_calls=1 unbound=0 results_submitted=1`，VoiceChat 摘要通过 |
| P0-A/B：现场部署与两类验证 | 严格配置已确认；两类现场日志已分析 | 补运行 hash；严格知识闭环与 PAD A/B 分开报告 |
| P1-A：严格提示与授权一致 | 编码及本地回归完成；现场拒绝保护生效 | 保留显式严格模式；当前自主回答部署改用 general_qa，不要求每次先调用工具 |
| P1-B：ASR/function 首差异修复 | 原生路径追加排查暂停；算子未确定 | 仅在重新投入原生路径时恢复，不阻塞提示与应用契约修正 |
| P1-C：响应终态与阶段预算 | 预算 EOS 现场已触发；完整听音及原因贯通未完成 | 正常知识回答的结束与 PAD A/B 验证，不把预算结束当正常完成 |
| P2：Gateway 状态管理提取 | 计划，后于状态语义收敛 | 保持单写入器/提交权；交错、取消、迟到与超时回归 |
| P3：真实放行 D07 / Q07-E | 未通过 | 知识、口述、完整结束、恢复/权限/并发证据齐全 |
| social_only / quarantine（旧 Q1003-SAFE） | 推迟评审 | 输入分类不足以放行开放式回答；先验证受控话术/播前输出约束 |
| 独立 ASR→业务→受控 TTS | 用户明确排除；本轮未提交代码已撤销 | 不作为当前实现或自动 fallback |

后续 Q01/Q02（完整恢复/语义与听音）、Q03（端到端指标）、Q04（原件下载）、D1/D3、其他 Provider 和 LVA3-R 等待能力均不抢占上述故障闭环。详细验收仍保留于 [验证清单](development/validation.md)；范围外能力不得因旧任务 ID 存在而声明已实现。

## 4. 维护规则

状态变化才更新本板；测试日期/计数只写验收，日志事实只写日期证据，命令只写部署。重复过程和已失效方案从活动文档删除，追溯使用 Git 历史及 [归档](archive/README.md)，不为每次整理再复制一份旧正文。
