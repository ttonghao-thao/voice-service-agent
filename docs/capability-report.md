# P0 能力核对报告

核对日期：2026-09-05。结论：**应用实现与本地契约已建立，真实 VoiceChat 集成验收尚未完成。** Python 3.12 适合异步会话网关、Agents SDK 和 HTTP 工具；浏览器用 TypeScript/AudioWorklet，未更换已确认架构。

## 固定的来源与运行依赖

- NVIDIA Speech 仓库 commit：`097dfe9e2f55baf653b83035868bdc89849f1b47`。
- `voicechat_realtime_instructions/api-reference.md` git blob：`06252330444f0a81679fdeb1f25c8ee067ac8c90`。读取内容与方案中的 API blob 一致。
- 官方协议来源：[固定版本 API Reference](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/voicechat_realtime_instructions/api-reference.md)。本实现只发送文档建立契约的事件。
- SDK 调用根据[官方运行文档](https://developers.openai.com/api/docs/guides/agents/running-agents)及安装包签名核对。`openai-agents==0.22.0`、`openai==3.8.0`，已用本地 HTTP 合成响应测试实际 `Runner.run` 和 `Runner.run_streamed` 的工具循环及结构化结果。
- Python `3.12.13`；其余完整 Python 版本见 `uv.lock`，浏览器版本见 `apps/web/package-lock.json`；应用基础镜像 manifest digest 见 `deploy/images.lock.json`。VoiceChat 云端容器 digest/API 实际版本尚未提供，不能以本地应用镜像替代。

## 能力状态

| 能力 | 文档/设计依据 | 当前验证状态 |
| --- | --- | --- |
| session.created → session.update → session.updated | 官方基线 | Adapter 和严格 24kHz 回显检查已实现；未连接真实云端 |
| PCM16 LE、24 kHz mono、80 ms | 官方在线线格式 | 浏览器合成麦克风、1920 samples/3840 bytes 分块及重采样测试通过；云端双向音频未验证 |
| consult_service_agent → function_call_output | 原生函数事件及本项目工具定义 | 脚本化 provider 的相同 call_id 回传、重复去重和先工具后转写测试通过；真实原生往返未验证 |
| 中文提示词、参数、引用和结果 | 项目明确由云端提供中文模型能力 | 应用 UTF-8/中文传输和 SDK 合成响应测试通过；云端中文语音质量未测 |
| 停止播放、逻辑取消、epoch fence | 应用控制 | 单元/后端竞态/浏览器合成音频测试通过；不表示 GPU 推理或供应商计费已取消 |
| 工具阶段自然无缝打断 | 官方基线未支持 | Adapter 保守关闭；应用采用关闭旧连接并重建 |
| response.cancel、动态 instructions、任意文本 TTS | 公开基线未建立本方案所需支持证据 | 未发送这些事件，未开放这些能力 |
| 实际口述与工具事实一致 | 需要人工核对真实音频 | 未测，不用正确文字卡片替代口述验收 |
| 云端会话时长、1/2 并发、延迟 | 必须实测 | 未测；105 秒只是应用轮换初值，不是容器测得时限 |
| 中文桥接率、参数正确率、证据正确率 | 需要真实样本分母 | 已准备 100 条合成文字用例，未录音/未执行，不生成虚假百分比 |

## 本次实际探测

已执行 `scripts/probe_voicechat.py`，输出写入本机 `artifacts/voicechat-probe.json`：

- `status=blocked`
- `real_service_connected=false`
- 原因：`VOICECHAT_WS_URL not configured`
- `checks={}`，没有真实云端成功记录。

该脚本可接收用户授权的 24kHz mono PCM16 WAV，按实时节奏持续发送音频和静音，验证握手与工具回传。其固定结果显式标记 synthetic，不代表真实天气/政策；不保存音频或私有转写，不自动设置生产验证开关。真实 SDK/工具联调应在 `APP_ENV=integration` 的门户中进行，使用相同业务 Runtime。

## 外部依赖与下一步

在 `.env` 或部署密钥系统配置：VoiceChat WS/health/凭据/API 版本与部署声明；文本模型供应商、模型 ID/凭据；RAG 实际契约及 ACL；天气供应商/代理；OIDC/SSO 与业务保留要求。不要把凭据提交到 Git。

配置齐备后按设计第 18 节完成原生工具往返、中文业务结果音频输出、旧连接隔离及工具阶段恢复，再开展 100 条业务用例与停顿/打断专项评测。至少分别记录 0.3/0.8/1.5 秒停顿、附和、持续插话、关键数字口述和真实并发；`scripts/score_voice_evaluation.py` 只统计明确标记真实服务且人工填写的观察项，缺失项不进入分母。

中文模型训练/微调不属于本项目。原生事件未提供可靠的工具调用与用户输入 item 直接关联时，保留各自 ID 和记录，不按文字相似度或时间猜测绑定。
