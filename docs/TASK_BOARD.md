# 待优化项与验证状态

## LiveKit 与 Sophon 升级

1. Phase 0 · 已核对现有 Provider、门户音频、WeatherAdapter、Registry 和 CueKB 2.1。真实 Sophon 已通过 24 kHz 握手、委派、校验码回传与打断换题。部署侧未发出用户输入转写，CueKB 原问题和委派归属仍待核对。详见 [Phase 0 验收](validation.md#livekit-sophon-phase-0)。
2. Phase 1 · 待 Phase 0 通过。将 Room Token、AgentSession 和 LiveKit Web SDK 接入现有入口，验证连续对话、打断与退出。
3. Phase 2 · 待 Room 语音链路验证通过。实现 Client Delegation Handler 和单层 Tool Router，按权限接入 WEATHER、CUEKB、WEB_SEARCH。
4. Phase 3 · 覆盖重复及漏委派、实时查询、中断、并发和断开，记录路由准确率与时延后交付 `origin/main`。

2026-11-21 Sophon Live 审计修复（已实现，待真实服务验收）

1. S4：多段可能输入时拒绝延迟委托；无可确认的任务类型时不查询。保留单一明确输入的 CueKB 执行。
2. S5：分别记录 Commentary 发送、Gateway 回执和候选语音。保留获取的授权来源，候选字幕和播放结束均不提交为已核实回答。
3. S6：用官方 Gateway 协议复现和受控夹具复测。独立部署的 32B Flash 4-bit 实际听音和端到端验收见 [Sophon Live 验证](validation.md#sophon-live-验证)。

2026-11-21 Sophon Live 接入（已实现，待真实服务验收）

1. S1：独立 WebSocket Provider、音频、字幕和同连接多轮在协议夹具中通过。
2. S2：中文 ASR 原文经现有 CueKB 查询和校验；有证据、无证据、重复委托和证据失效均有夹具结果。
3. S3：完成受控停顿、播放打断、查询期间换题和浏览器语音回归。真实 24 kHz 会话、自然对话、路由指标和 CueKB 听音待按 [Sophon Live 验证](validation.md#sophon-live-验证) 执行。

更新：2026-11-06。审查代码基线 `2a8ba8b`；CueKB main `30970e9`。2026-11-07 已实施 F1/F2/F3/F4 的下述限定修复，未部署。以下是当前未完成项，历史流水由 Git 追溯；详细代码原因见[审查](reviews/duplex-tool-audit.md)。

| ID / 优先级 | 待优化项 | 证据状态 | 完成条件 |
| --- | --- | --- | --- |
| F3 / P0 | related 输入过早视作已消费/已回答 | API 已取消 related 自动完成和门户完成暗示；半段输入集成回归，现场待验 | 完整消费与实际覆盖分开；半段补充问题不被 completed/settlement 吞掉 |
| F1 / P0 | 口述矛盾和全部漏项仍可通过检查 | 增加属性覆盖与单极性检查；完整语义核验仍待优化 | 主张极性/主体/属性/条件正确关联；未覆盖 facet 不认证 answered |
| F2 / P1 | don't 等缩写撤回仍绑定旧实体 | 缩写规范化已修复，回归覆盖直/弯撇号和属性否定 | 缩写否定、换题、更正、属性否定分别正确；不查询被撤回产品 |
| F4 / P1，需 CueKB 协作 | 组合 facet 无法联合多条正确证据 | 简单属性并列已拆跨度；复杂组合仍需 CueKB 协作 | 保留原跨度并正确表达多属性覆盖，不用词项重叠放宽 direct |
| F8 / P1 | 终态 settlement 仍依赖模型停止调用 | 代码有 call/答案/连接上限，最终会断连 | 有界结清与业务失败边界明确；可安全继续下一问，不留下 native pending call |
| F7 / P1 | validate 到真正写回之间没有 CueKB 复验门禁 | 源码确认窗口；现场撤权/过期待验 | 最终发送前检查包/修订，失败不注入旧包、不自行重查 |
| F5 / P1 | 原生首问选工具稳定性 | 历史失败；当前版本未做真实金标 | 独立 Golden/Black 与追问真实选工具，不靠前置上下文掩盖漏调 |
| F6 / P1 | 逐 token 注入和预检全过程等待预算 | 源码确认串行；新版本 GPU 未验 | 分阶段实测，输入可见与模型理解分别验收；有完整等待预算 |
| F9 / P2 | 固定连接寿命/文件结束体验 | 现存 105+30 秒边界 | 安全轮换、明确恢复语义，不重放未知结果 |
| P2，暂缓 | 后台持续播报 | 按用户决定暂缓 | 后续按设备验证；不阻塞前台功能，勿声称隐藏页面等同后台可用 |

已编码待配套现场验证：欢迎语正常 EOS、欢迎语/普通回答打断、ACK 前后结果续答、迟到帧抑制、suppressed done 排空、单 input 一次 query/validate、终态后不重查、完整包精确计数、注入期显示 ASR。上述实现不是本轮新修复，也不代表生产放行。

已替代且不得重新引入的旧设计：每输入两次 search、第三次直接 VOICE_TOOL_LOOP 断连、模型 query 覆盖 ASR 原问题、只把 model_view token 数当最终注入预算、注入时完全暂停显示 ASR、用两次旧通话通过代替当前麦克风/多轮验收。仍存在且影响功能的旧取舍已分别标为 F1/F3/F6/F8/F9。

下一轮先处理 F3/F1，再 F2/F4，随后 F8/F7；F5/F6 需真实配套版本验证。操作和放行判据只有[验证手册](validation.md)一份。本次不新增模型、工具重试、识别阈值或超时配置。
