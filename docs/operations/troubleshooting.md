# 故障定位与分段耗时

按症状进入一行，再扩大到实际依赖。配置只查 [部署](../deployment.md)，外部协议查 [接入](../integration.md) / [CueKB](../interfaces/cuekb.md)；输出状态查 [交付](../live-agent-implementation.md)。本页不证明真实服务已通过验收。

## 1. 按症状定位

| 症状 | 先查什么 | 代码与下一层证据 |
| --- | --- | --- |
| 页面正常但无收音 | HTTPS/证书、麦克风权限、worklet/dsp 请求是否 200、设备 Hz 与 sent Hz | `apps/web/src/audio/VoiceClient.ts`、`public/audio-worklet.js`；Web 镜像文件/目录权限，不能只测首页 |
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


历史反馈曾约 3 秒，没有对应 turn 的完整分段日志；这不是当前性能基线。默认按提交文字到完整答案分析：HTTP/鉴权/建 Turn → 模型生成检索参数 → CueKB 检索 → 模型生成结构化答案 → 证据/权限复核与提交 → SSE → 渲染。通常含检索规划和最终回答两次串行模型请求（与 [SDK agent loop](https://developers.openai.com/api/docs/guides/agents/running-agents) 一致）；额外工具轮次、上游排队/网络、有限重试会增加耗时。语音还包含说话结束判定、最终 ASR、原生工具提取、结果注入/TTS 和播放缓冲；ACK 不计作最终答案。

原页面固定 300 ms 数据库轮询，并在 final 事件后再 GET messages 才显示聊天答案。现在事务提交后唤醒同进程 SSE，通知只作为加速，持久化 Event/server_seq 仍是事实来源；回滚不通知，跨进程或漏通知保留 300 ms 补查，重连按 cursor 补读。页面对已加载 Turn 直接应用鉴权过滤后的 final，未知 Turn 才补取；晚到的 running 快照不能覆盖已收到的 final，新 epoch/revision 不能被旧请求覆盖。

这消除了同进程 0–300 ms 的轮询等待和已有气泡的一次 HTTP 往返，但不承诺总耗时从 3 秒降到某个数。legacy、文字和复杂路径保留模型规划；Q07 直查使用最终输入和已确认条件，经 EvidenceGate 决定 Nano 续答或升级，不先请求外置分类模型。直查的质量、升级率及实际时延仍需同样本实测，不能因少了调用就宣称整体效果已优化。

每个 `conversation_id/turn_id` 关联以下无正文日志：

| 阶段 | 日志与字段 | 解释 |
| --- | --- | --- |
| 每次模型调用 | `agent_model_call_finished`：call_index/status/duration_ms | 模型网络往返和完整输出；失败/取消也结束计时，不代表 TTFT |
| CueKB | `cuekb_response_validated`：trace_id/duration_ms/service_total_ms | 本项目往返含重试与解析；服务 total 若缺失为 null，不视为 0 |
| 工具 | `tool_run_finished`：status/duration_ms | 权限、当前任务检查及 adapter；其范围包含 CueKB，不能重复相加 |
| Agent | `agent_pipeline_finished`：model_calls/duration_ms | SDK 循环及结果校验，含模型与工具 |
| 提交与总计 | `answer_delivery_finished`：commit_ms/total_ms/committed | total 从 Coordinator execute 开始，含业务链和提交；不含浏览器网络/渲染 |

先收集同模型/KB/问题集的冷、热请求 p50/p95，按 turn 对齐。若模型阶段主导，再实测降低回答长度、模型服务排队与缓存；若 CueKB 主导，依据其 trace/timings 优化检索路径；若只在浏览器等待，检查 Nginx SSE 缓冲与额外代理。不得把 30 秒超时预算当实际等待，或把减少模型调用当不影响检索质量的已验证优化。


## 3. 复测步骤


代码成本与字段定义见 [查询延迟](../integration.md#41-查询延迟与优化边界)。发布后使用同一组英文问题，分别记录文字“提交→完整答案”和语音“说完→最终答案开始口述”（不计 ACK），各自统计冷启动和热请求。用同一 turn 关联日志，不把不同请求的时间相加：

```sh
docker compose --env-file .env -f deploy/compose.production.yaml logs --no-color api \
  | rg 'agent_model_call_finished|cuekb_response_validated|tool_run_finished|agent_pipeline_finished|answer_delivery_finished'
```

时间包含关系：Agent 内含模型和工具，工具内含 CueKB，提交 total 内含 Agent；不能把所有 duration 相加。浏览器通过 Network 的 SSE/HTTP 时间线补足提交后的网络和渲染。此前 300 ms 轮询改为同进程提交通知，跨进程补查保留；final 已含答案时不再等待 messages GET。部署 Nginx 已关闭 SSE 缓冲，额外的 9002 代理仍需现场核对。没有真实分段日志前，不给各阶段虚构占比，不承诺优化后的实际总耗时。
