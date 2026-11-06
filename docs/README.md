# 按需文档索引

英文全双工知识客服；开发沟通可用中文。先读根 AGENTS.md，按任务只选下列一行；链接不是递归加载指令。

| 任务 | 首选入口 | 何时扩读 |
| --- | --- | --- |
| 当前故障与下一步 | [任务板](TASK_BOARD.md) | 需要原因和代码证据再读[本次审查](reviews/duplex-tool-audit.md) |
| 理解产品或跨模块改动 | [范围](delivery.md)、[架构](architecture.md) | 根据架构表直接定位代码 |
| CueKB 请求、证据、预算 | [CueKB 契约](interfaces/cuekb.md) | 仅查相关 schema/函数，勿整读生成 JSON |
| 原生工具注册与批次 | [工具契约](interfaces/voice-tools.md) | 再看对应 Provider/Registry |
| 浏览器音频和事件 | [门户协议](portal-protocol.md) | 再看 VoiceClient/Worklet |
| 接入、授权、配置 | [接入](integration.md)、[部署](deployment.md) | 按标题定位相关段落 |
| 本地或现场验证 | [唯一验证手册](validation.md) | 不使用 Git 历史中的旧流程 |
| VoiceChat 完整文件交付 | [快照](voicechat/README.md)、[启动](voicechat/docker-run.md) | 按 manifest 定位相关文件，勿整读快照 |
| 取舍或文档维护 | [决策](decisions/architecture-decisions.md)、[维护规则](development/documentation.md) | 仅此类任务读取 |

已删除被实现替代的 CueKB 提案、重复设计页及旧验收归档；历史由 Git 追溯。当前代码基线、验证边界和待优化状态集中维护，不按轮次追加同一故障的全过程。
