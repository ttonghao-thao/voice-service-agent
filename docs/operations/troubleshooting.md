# 故障定位与分段耗时

按症状进入一行，再扩大到实际依赖。配置只查 [部署](../deployment.md)，外部协议查 [接入](../integration.md) / [CueKB](../interfaces/cuekb.md)；输出状态查 [交付](../live-agent-implementation.md)。本页不证明真实服务已通过验收。

## 1. 按症状定位

当前已知问题和本次复测见 [批次验收](../native-tool-batch-validation.md)，最新状态见 [任务板](../TASK_BOARD.md)。工具注册、检索、回填已有现场正面证据，不能沿用“所有 function 全 PAD”的早期结论。

| 症状 | 先查什么 | 代码与下一层证据 |
| --- | --- | --- |
| 页面正常但无收音 | HTTPS/证书、麦克风权限、worklet/dsp 请求是否 200、设备 Hz 与 sent Hz | `apps/web/src/audio/VoiceClient.ts`、`public/audio-worklet.js`；Web 镜像文件/目录权限，不能只测首页 |
| Start file call 后立即断开，VoiceChat 收到 0 块音频 | API 的 voice_registration_rejected / voice_session_failed 和门户错误消息；session.updated 的实际回显 | 音频在 portal.session.ready 后才发送；Adapter 同时支持 tools 数组/JSON 字符串，仍核对内容；不先排查 ASR 或增加工具等待超时 |
| ready、有数据包却无回答 | 最终 ASR → native call → 工具结果 → response/audio.done 的实际事件 | `voice/provider.py` / `gateway.py`；独立 VoiceChat 版本及有序 response 补丁；ready/包数不证明听到 |
| 文字正确、语音偏离 | presentation 的 mode/status/reason、批准文本、实际 Provider 转写 | `agent_runtime/evidence.py`、`Store.presentation`；再用授权录音核对真实音频，事后检查不能撤回已播内容 |
| 停止后下一轮不播/旧回答串入 | epoch/revision、response/input/call 关联、clear/suppressed、finished ACK | Gateway → Store → VoiceClient/worklet；不要凭文本相似度或超时猜关联 |
| 查询慢/超时 | 先判断直查/外置路径，再按 §2 对齐同一 Turn 日志 | Runtime → Registry → CueKB → Coordinator → SSE；各层耗时有包含关系 |
| 401/403 或历史被隐藏 | call token 是否撤销/跨会话、票据与 Origin、当前 KB 交集和工具版本 | `api/auth.py` / `routes.py`、Registry、CueKB Key；不能把权限失败显示为“无知识” |
| 等待中语音更正没有生效 | Adapter 四项能力和会话快照 | 当前 NVIDIA 为 false，无 env 绕过；门户 Check progress 可用，明确取消后重问 |
| 升级后缺字段/readiness 失败 | 镜像版本、migrate 服务完成、Alembic current、PG/Redis 网络 | `deploy/compose.production.yaml`、`scripts/deploy-cloud.sh`；数据库当前目标见部署，不自动删卷修复 |
| 重连/轮换/重复回答 | DeliveryAttempt unknown、租约、旧 epoch、浏览器排空估计 | `sessions/coordination.py` / `coordinator.py`、Store；unknown 不重发，多副本需粘性路由 |

日志仅保留脱敏 ID、枚举、阶段时长及允许的 endpoint 信息；不记录问题正文、音频、token 或 URL 凭据。先匹配版本和统一 UTC，再关联事件。真实录音/敏感供应商日志按授权保存，不提交仓库。

## 2. 延迟分解与可观测边界

一期不使用外置答案模型。按同一 conversation/input/call/response/batch 分开观察：输入结束→原生调用；API 接收→CueKB 返回；结果准备→批次提交；VoiceChat 接收→ACK 等待→后端注入；注入→有效回答首包；API 转发→浏览器起播。ACK 首包不算答案首包。

CueKB duration_ms 是客户端请求测量，不能解释结果提交后的等待。阶段有包含关系，不把多个 duration 重复相加；浏览器与服务器时钟未经校准不能直接相减。时间线缺 sequence 匹配时保留 unverified，不写成模型零调用。

## 3. 精简复测

只使用 [批次验收](../native-tool-batch-validation.md) 或 [Web 对照](../web-wav-comparison.md) 的同窗口摘要/时间线。先找最早缺失阶段，再决定是否修改；已有日志能回答的问题不要重新跑 WAV。当前没有经过真实测量的全链路 SLA，不把配置超时当实际时延。

模型不调用时核对同 session 的 Prompt/解码/事件；已调用则核对实际查询、批次结果及注入，不再次排查注册。终态前排队调用与真正终态后调用必须按接收时间区分。若单轮已完成却后续重复回答，保留同 input 下的 response ID 和结束事件，记录为独立未解决问题，不扩大当前修复。
