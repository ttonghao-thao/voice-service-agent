# Voice Service Agent — 工作入口

英文实时语音问答系统，客服为首个场景；Python 3.12 / TypeScript，默认中文沟通。当前任务状态只看 [任务板](docs/TASK_BOARD.md) §1–3，测试证据只看 [验收](docs/acceptance-report.md) §0；不要默认读历史记录。

## 最小读取路径

1. 先 `git status --short`，用 `rg` 查本次任务涉及的函数和调用方。
2. 本文件 + 一份主题文档的相关章节即可起步。先 `rg -n '^##' 文件`，再 `sed -n '起始,结束p' 文件`；接口查模型/路由，不整读生成 JSON。
3. 链接是查阅入口，不是递归读取指令；只有跨模块问题才扩大范围。已有上下文未变化不重读；不默认扫描全仓 Markdown、锁文件、产物或 `docs/archive/`。
4. 复杂改动在任务板维护里程碑；设计归主题文档、证据归验收记录，避免重复维护长篇状态。

| 任务 | 首选入口 |
| --- | --- |
| 答案口述、结束/重连、等待进度/更正 | [Live 改进](docs/live-agent-implementation.md) §1–4 |
| 双工具、证据续答、扩展注册 | [Q07](docs/qa-routing-design.md) §0、4、6–8 |
| 来源条件、任务上下文、连续评测 | [上下文与评测](docs/task-context-evaluation.md) |
| 门户、SSE/WSS、浏览器音频 | [门户](docs/portal-protocol.md) 对应章节 |
| CueKB、VoiceChat、文本模型、身份 | [接入](docs/integration.md) 对应服务章节 |
| 架构、租约、取消 | [架构](docs/architecture.md) §2、5–6 |
| 镜像、配置、迁移、D07 | [部署](docs/deployment.md) 对应标题 |
| 独立 API 模拟、运行命令 | [模拟测试](docs/simulated-full-flow.md) / [README](README.md) |
| 无法判断主题 | [文档索引](docs/README.md)，不用通读所有链接 |

## 已确认实现

- 默认 `legacy` 注册 `consult_service_agent`；可选 `dual_tools` 本期仅注册 `lookup_knowledge` / `reason_over_knowledge`，保留可信代码扩展能力。
- 双工具默认 `general_qa`；`knowledge_required` 拒绝无工具实质性回答，lookup 也走外置知识执行。模式/策略和 Provider 能力由服务端快照，模型/浏览器不可修改。
- VoiceChat-11B + nemotron-labs-voicechat + WS 原生选择 tools/instructions；不发送 WS `tool_choice`，不新增语义分类器、独立 Nano 服务或多 Agent 调度。
- 后台 `ENABLED_TOOLS=search_knowledge` 是 Registry/CueKB 能力。直查 sufficient 完整证据交 Nano D2；复杂问题走外置 LLM，内部升级一次，共享期限/检索预算。一般回复不建知识 Turn、不作企业证据。
- 口述约束与实际 Provider 转写关联；D2 检查在音频输出后，不能撤回已播内容或证明实际音频/完整语义正确。网络发送、响应结束、浏览器排空估计、听到是不同状态。
- NVIDIA 等待期能力默认关闭；P2 仅在可信 Adapter 验证进度交互、修订、明确 call 关联和旧调用结清后启用。独立模拟扩展不证明真实 VoiceChat 支持。D1/D3、自由交谈、GPT Live Provider 仍未实现。

## 工程边界

- 保留 Coordinator → BusinessRuntime → ToolRegistry；供应商语音结构仅在 `app/voice`，Agents SDK 仅在 `app/agent_runtime`。VoiceChat、CueKB 独立部署，通过 API 交互。
- 普通 `speech_started`、停顿和 ACK 不取消任务；停止只清播放，显式取消/替换使任务失效。ASR 关键词只提取条件，不能执行控制；自然更正须经模型的类型化工具操作及最终 ASR。
- 业务提交/语音写回检查 epoch/revision/turn/租约；单上游写入器。旧 pending call 必须结清或关闭，unknown 不重发；数据库和 WS 没有共同事务。
- 每次开始通话签发独立短期 customer call capability，只存在当前标签页内存；KB 范围由服务端固定，不能获得管理权限。正式客户认证、中文/天气/股票/交易不在本期。
- 编码阶段不调用真实 CueKB/VoiceChat；显式独立夹具通过实际 HTTP/WS 测试，real 失败不能回退 mock。未执行的供应商/GPU/听音/部署检查不写 passed。
- DB 用 Alembic（当前 0009）；自动建表仅隔离测试。仅一套验证 .env/Compose；API/Web 独立镜像，无训练、GPU 或新队列服务。
- Web 镜像 Nginx 提供 HTTPS 8087，同源 /api/ 转容器 api:8000；无独立 Nginx 服务/宿主 API 端口/正式客户认证入口。
- Docker 不可用时只静态检查；用户要求且已有 Docker 可隔离构建/Compose。代理 CA 仅通过 BuildKit secret，保留 TLS 校验。
- 依赖固定于 requirements*.txt / apps/web/package-lock.json；协议变化后运行 `PYTHONPATH=apps/api .venv/bin/python scripts/export_contracts.py`。不提交密钥、录音、票据、DB 或产物；纯文档任务只改 Markdown。
