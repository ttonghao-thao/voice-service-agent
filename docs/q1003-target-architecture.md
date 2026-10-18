# q1003 架构改进与实施方案

当前需求以一期单工具原生验证为准：后端必须是 VoiceChat；由 VoiceChat 自主决定使用自身知识回答或调用 search_knowledge_base(query)，内部仍执行 CueKB 检索；不引入其他 ASR/TTS、意图分类器或额外答案生成模型，不根据转写自动启动业务。先完成语音输入、工具执行和语音回复，时延优化后置。代码分支 `codex/q1003`；状态见 [任务板](TASK_BOARD.md)，现场证据见 [验收](acceptance-report.md)。

## 1. 目标与架构判断

保留 `门户 ↔ Gateway ↔ VoiceChat` 的全双工音频连接，以及 `Gateway → Coordinator → NativeCueKBExecutor → Registry → CueKB` 的工具执行链。

- 直接回答：VoiceChat 生成文字/音频；Gateway 关联当前输入后交付，记录为 provider_general / provider_only，不标为知识库验证结果。
- 工具回答：VoiceChat 原生发出工具名、参数、call_id；应用验证目录、权限和最终输入关联，保留模型 query，直连 CueKB 并精简证据，按原 call_id 回填；VoiceChat 继续生成回复。
- 应用不选择是否调用工具、不伪造原生调用、不增加分类模型或强制 WS tool_choice。
- 原生调用不可靠、ASR 句首缺失等历史问题尚未证明修复；提示或应用回归通过不能代替真实模型测试。

## 2. 本阶段选择及推迟的方案

目标部署采用 `dual_tools + general_qa` 和 `AGENT_PROVIDER=disabled`，VoiceChat 是唯一语音 Provider 和答案生成模型。dual_tools 为兼容名称，一期只注册 search_knowledge_base(query)；旧双工具实现仅保留兼容测试。保留 knowledge_required 的显式策略实现及回归，但它会拒绝没有工具授权的直接回答，不能用于当前自主回答部署。

此前推荐的独立 ASR→业务→TTS 方案已撤回，相关未提交应用代码已撤销；应用根据转写自动发起检索也不在当前实现范围。最终转写只用于绑定和校验已发生的原生调用。

已有会话快照不会随环境变量更新；策略变更后必须新建 Conversation。不能通过放宽旧严格会话的 Gateway 门禁代替正确的模式切换。

## 3. 可实施的契约改进

### 3.1 策略与提示一致

API general_qa 提示明确让 VoiceChat 决定直接回答或调用注册工具，不要求每句话都调用工具。私有、实时或需要授权来源的请求仍按工具说明处理；业务失败不能转而冒充查询成功。

现场完整 `s2s/prompt_template.jinja` 纳入版本管理，采用用户指定的三句约束：

```text
Follow the session instructions and tool contracts when handling tool results.
Do not retry the same tool request or issue multiple tool calls simultaneously.
Speak only as permitted by the session answer policy and the returned tool result.
```

工具调用示例使用单个工具和对象参数；保留完整 schema 及独立 ACK 元数据。不再在模板尾部通用授权额外调用/错误重试。一期提示版本为 cuekb-native-v1；完整文件、来源/hash 和运行挂载见 [VoiceChat 部署](voicechat/docker-run.md)。

### 3.2 显式响应终态

保留输入、工具任务、回答和播放的分别关联。语音回答需要实际音频及结束事件；工具返回成功、字幕正确不等于声音已播放。保持已有终态、交付审计和停止播放规则，本次不调整 PAD 算法或新增全局缓冲。

### 3.3 分阶段预算

沿用既有业务和回答期限机制，部署例值分别为 60/30 秒；不以无限延长超时掩盖缺少结束事件。功能链完成前不启动时延优化，也不改变模型融合/采样参数。

### 3.4 输入与原生工具契约

保留最终转写绑定、目录/权限复核、同 call 回填、重复事件幂等和迟到结果隔离。普通输入活动不直接取消业务；停止播放与取消任务仍是独立操作。工具结果不允许通过虚构 call_id 送给 VoiceChat，现有接口也不是任意文本 TTS。

## 4. 实施计划与决策门槛

| 工作 | 本次内容 | 完成依据 |
| --- | --- | --- |
| 模式与部署 | general_qa、disabled、单 search_knowledge_base；迁移后 CAS 应用目录并检查实际 capabilities | 无需文本模型 key；旧目录可显式升级；新会话生效 |
| 提示与模板 | 明确自主决策；用户指定三句约束、单调用示例、完整模板和 manifest | 渲染保留各策略、0/1/2/3 工具 schema 及 ACK；容器模板 hash 一致 |
| 应用双路径 | 直接回答不检索；query-only 原生工具只检索/整理完整 ASCII 证据，不升级答案模型 | 追问 query 保留、服务端 KB/鉴权、同 call 回填、重复幂等、失败不冒充无事实、音频及结束 |
| 现场验收 | 固定 VoiceChat、真实 CueKB；用门户麦克风/WAV 或 validate_native_cuekb.py | 选择、参数、检索和口述分别判定；实际听音单独确认 |

自动化测试用受控上游事件验证应用接线，不能声明模型实际完成工具决策。没有现场通过结果时，原生选工具及完整听音继续标为未验证。

## 5. 架构替代的进入条件

本轮不启用架构替代。VoiceChat 是不可更换的全双工语音后端，工具决策必须由 VoiceChat 完成。未来任何超出该边界的方案都不属于本轮任务。

## 6. 放行条件

新建 general_qa 会话分别验证：普通问候/一般问题直接回答；明确请求查询授权知识库时原生调用成功；原 call_id 回填后 VoiceChat 正常语音回复。保持工具权限、输入归属、结果版本与停止播放校验。

直接回答是模型自身知识，不是已验证企业证据。工具后口述仍需单独验证完整性和内容；现有播后检查不能保证已经播放的音频绝对准确。验收遵循 [真实验证清单](development/validation.md)，不以模拟测试或容器 ready 代替现场成功。
