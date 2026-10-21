# 文档维护规则

交付文档只描述本期需求、实际实现及明确未验证的问题；历史通过 Git 追溯，不另复制排障流水或旧方案。

| 信息 | 维护位置 |
| --- | --- |
| 开发边界与外部源码同步 | 根 AGENTS.md |
| 导航 | docs/README.md |
| 需求、目的、范围、交付结论 | delivery.md |
| 实际架构与关键取舍 | architecture.md、decisions/architecture-decisions.md |
| 未解决事项与优先级 | TASK_BOARD.md |
| 已执行证据 / 放行标准 | acceptance-report.md / development/validation.md |
| 配置默认值 / 操作 | config.py / deployment.md、专题操作手册 |
| 接口字段与协议 | 代码、生成 contracts、interfaces 与 portal-protocol.md |
| 外部 VoiceChat 完整文件/hash/启动 | docs/voicechat 源码、snapshot-manifest.json、docker-run.md |

先查代码和最新现场证据，再修正文档。已知事实、合理推断、设计建议、未验证结果分开写。实现状态变化更新任务板；实际新验证才更新证据，不能将旧测试换日期或将模拟声明为真实通过。

删除被替代正文，避免反复追加“以前/后来/本轮修正”。兼容代码若仍存在，明确其非本期范围，不把删除文档说成已删除代码。文档整理不能顺带改变业务、预算或验收标准。

修改后检查本地链接/锚点、代码路径、围栏与 shell 语法、diff。纯文档不重跑应用回归。外部源码修改必须同步完整文件、manifest hash/来源和启动命令，不能仅交补丁。
