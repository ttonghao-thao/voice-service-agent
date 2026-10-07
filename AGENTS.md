# Voice Service Agent — Codex 工作入口

英文实时语音问答，客服为首个场景。Python 3.12/FastAPI/Agents SDK；React/TypeScript/AudioWorklet；PostgreSQL/Redis。默认中文沟通。

## 按需读取

1. 先 `git status --short`。范围明确就用 `rg` 找代码/调用方和对应主题；不明确再查 [文档索引](docs/README.md)。
2. 长文先 `rg -n '^##|关键词' 文件`，再 `sed -n '起始,结束p' 文件`。链接是入口，不是递归加载指令。
3. 不默认整读架构、任务板、历史、生成 JSON、锁文件或产物；同任务中仍有效的信息不重复读。
4. 跨模块时按实际调用/数据依赖逐步扩读。文档不足或冲突先核代码，正确性优先，不以节省上下文为由跳过必要检查。

## 核心边界

- VoiceChat、CueKB 独立部署/API 对接；保留 Gateway → Coordinator → Runtime → Registry。供应商语音结构归 `app/voice`，Agents SDK 归 `app/agent_runtime`。
- VoiceChat 用 tools/instructions 原生选工具；不加 WS tool_choice、分类模型、独立 Nano 服务或子 Agent 调度。一般问答不是企业证据；真实 Provider 能力不能由模拟推定。
- Coordinator 唯一提交；epoch/revision/Turn/租约与权限在提交和交付时复核。普通发声不取消查询，停止播放与业务取消分开；旧调用结清或关连接，unknown 不重发。
- 身份/KB/凭据由服务端控制；模型/浏览器不能扩大权限。real 失败不回退 mock；模拟、转写检查、网络发送和实际听音分开记录。

## 修改与验证

依赖沿用固定 requirements/package-lock；数据库用 Alembic。契约变更运行 `scripts/export_contracts.py`，同步对应客户端并做相关回归；命令见 [README](README.md#本机验证)。不提交密钥、票据、录音、数据库或产物。

只更新受影响的主题；接口字段以代码/生成契约为准。纯文档任务只改 Markdown，检查链接/锚点和差异，无需重跑应用测试。任务状态变化才更新任务板，实际新验证才记验收，不要求每次生成全仓总结。文档职责及维护例子见 [维护规则](docs/development/documentation.md)。
