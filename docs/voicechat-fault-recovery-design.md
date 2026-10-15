# VoiceChat 当前故障定位与修复边界

2026-10-15 整理，分析代码基线 `b7a04d6`。本文只维护诊断分支与修复条件；最新现场证据见 [10 月 15 日严格模式复测](voicechat-validation-20261015.md)，旧超时现场记录见 [10 月 14 日通话](voicechat-validation-20261014.md)。

当前优先级已调整：用户确认 VoiceChat 是唯一全双工语音后端，且必须由 VoiceChat 决定直接回答或调用工具。当前采用 general_qa，先按 [实施方案](q1003-target-architecture.md) 修正提示、部署模式并验证双路径；不引入独立 ASR/TTS 或转写驱动业务。下面保留严格模式历史故障的诊断依据，未确认的模型根因仍未知。

## 1. 当前判定

目标录音为 `Hello, please introduce MONKEY MOONCAKE products.`。严格策略已由用户确认，新日志显示两次无原生工具调用的回答均被拒绝为 VOICE_TOOL_REQUIRED；WAV 字幕仍报告缺 Hell，麦克风仅得到极短转写并多次主动起答。ASR、工具选择、响应结束应分别定位，事件先后不代表三者互为因果。

## 2. 证据边界

- 已确认：两轮注册成功、function 全 PAD、首段未授权回答被拒绝；WAV 的 model 首次发布全部累计字符；麦克风轮已触发预算 EOS。计数与时序只维护于最新日期记录。
- 未确认：Hell 首次缺失的算子、实际加载权重/融合、全 PAD 原因、无有效输入时主动起答的原因、正常知识回答的自然 EOS 和完整听音。
- 已排除的解释范围：不能把诊断历史淘汰计数当作音频丢包；不能把 RNNT 显示 gate 或 `<s>` 清理当作已证实截词；没有工具调用，不能解释为 CueKB 执行慢。
- 当前 backend 直接读取 function_tokens，缺 key 会报错；公开 wrapper 缺 key 保留 PAD 的行为不等于现场实现。

源 WAV 已取得并检查格式；现场各阶段 PCM 和原始模型 token 尚未取得。计数相符不能单独证明 PCM 语音内容完整；frame_idx 是局部窗口索引，不能仅凭它不变判断 encoder 卡住。

## 3. 两类验证分开

严格知识验证：应用 knowledge_required、新建 conversation，检查 native call→真实执行→回填→授权口述；VOICE_TOOL_REQUIRED 表示保护生效但业务未完成。

收尾对照：同一新版 model.py、同录音/模型/目录/提示/策略，分别关闭和开启 PAD 开关。严格策略可能先拒绝无工具输出，无法观察尾部；这时用隔离协议探针观察响应生命周期。探针合成回填不能充当真实 CueKB 验收。

采集路径、固定 sequence、连续静音、容量及文件含义统一见 [现场记录 §5](voicechat-validation-20261014.md#5-同一次-sequence-的隔离采集)，启动见 [容器命令](voicechat/docker-run.md)。先用当前 1 秒前置静音样本复现；需要判断边界时，再用同录音原始/500 ms 变体，每次新连接并记录 hash，不同时改多项参数。

## 4. ASR 首差异

| 首个不一致位置 | 可实施修复 |
| --- | --- |
| 原 WAV→WS PCM | 浏览器/Gateway 首包、就绪、缓存及样本发送 |
| WS PCM→soxr→Triton PCM | 有状态重采样、缓存、批次提交；按同采样率区间比较 |
| PCM 完整但 encoder 选帧错误 | cache、mel/窗口偏移、首块/后续块索引 |
| 同帧/初态参考 RNNT 完整，当前解码缺失 | predictor 状态、blank/symbol 循环或词表映射 |
| 等价 RNNT 都缺失 | 继续核对共同 encoder/权重/输入条件；不能立即断言纯模型能力失败 |
| 原始 token 正确而累计/事件丢失 | tokenizer/display/发布游标与 reset 世代 |

参考 greedy 必须同权重、同帧、同初态；标点 bias 要做等价控制，reference_error 不能算有效对照。最终 ASR 不直接进入 VoiceChat 主干，但进入本项目业务输入且影响来源条件。不得在门户补词或通过放宽 gate 掩盖识别错误。

## 5. 原生工具首差异

顺序为实际 loader/head/tokenizer/fusion → 最终 prompt/prefill → 原始 function token → parser/WS → Gateway 参数与 input 绑定 → Runtime/Registry → 同 call 回填。

全 PAD 时先核对 parser 之前；有完整 SOTC/EOTC 而无工具事件才修 parser；有事件而业务未启动才查参数、权限、final ASR 关联。契约正确仍漏调用时做单变量提示/目录实验；质量不能达标则按 [架构替代条件](q1003-target-architecture.md#5-架构替代的进入条件) 评审。没有原生调用就不伪造业务调用。

## 6. 响应结束首差异

| 观察 | 结论与下一步 |
| --- | --- |
| 无 EOS，连续 text PAD | 查原始 token、现有静音计数/阈值与保护条件；文字结束不证明语音结束 |
| 预算 EOS 后完整音频/done | 支持保留保护，完成原因单独记录；不证明 ASR/工具已修复 |
| 预算 EOS 后截断 | 关闭开关，重新评估渲染预算；不能计为正常完成 |
| EOS 有、WS done 无 | 查批次解码、response 边界和尾部排空 |
| WS done 有、API 仍超时 | 查 Adapter 映射、response 关联与终态 |
| 未到预算 API 先超时 | 核对两侧时间轴/预算，不能直接认定补丁未加载 |

PAD ratio=3 不是 3 秒；其预算依赖内容 token 数。新补丁默认关闭，完整文件已交付，CPU 回归不能证明真实音频完整。完整生命周期方案见 [目标设计 §3](q1003-target-architecture.md#3-可实施的契约改进)。

## 7. 门户失败恢复

代码已处理空输出误拒绝/续答许可误消耗、错误通知晚于清理、final ASR 排队取消、上游断开无提示和关闭中重复取消。现场已分别看到超时和严格工具拒绝的 code/message；手动重试、断线与晚到结果的全部场景仍需真实验收。

错误先保留允许交付的最终转写并通知，再清理连接；不自动重播录音、提交文字或伪造工具。Stop playback 只影响播放，明确取消/替换才改变业务任务。知识不足、工具漏调用和超时必须区分，不能把受控拒绝记作业务成功。实现证据见 [验收](acceptance-report.md#0-当前审计与证据索引)。
