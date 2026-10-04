# Voice Service Agent — 工作入口

可配置知识约束的实时语音问答系统，客服为首个场景；Python 3.12 / TypeScript，默认中文沟通，当前产品交互为英文。Q07 基础实现已本地验证，真实服务验收待 D07/Q07-E。

## 当前实现口径

- `QA_EXECUTION_MODE` 默认 `legacy`：VoiceChat 注册 `consult_service_agent`，所有完整输入经知识业务链。可选 `dual_tools`：首版仅注册 `lookup_knowledge` / `reason_over_knowledge`，支持可信代码扩展工具注册。
- `dual_tools` 默认 `general_qa`，允许无工具一般回答；`knowledge_required` 拒绝无工具实质性回答，lookup 也改走外置知识执行。模式/策略由服务端创建会话时快照，不能由模型或浏览器修改。
- VoiceChat-11B + nemotron-labs-voicechat + WebSocket 作为完整栈：用 tools/instructions 原生选择工具，不向该 WS 发送 `tool_choice`，不新增语义分类器或独立 Nano 服务。后台 Agents SDK 的工具选择配置是另一层。
- `ENABLED_TOOLS=search_knowledge` 指后台 Registry/CueKB 能力，不是 VoiceChat 可见工具名。直查 sufficient 且完整的证据交 Nano 续答，复杂问题走外置 LLM + CueKB；一次内部升级共享期限与检索预算。
- D2 口述完成后才进行有限证据检查，不能撤回已播内容或证明逐字音频正确。一般回答不创建知识 Turn、不伪造引用。D1/D3、工具等待自由交谈及新增业务工具尚未实现。
- CTX1 / EVAL1：服务端统一带来源的用户条件与跨轮历史，内部任务快照由 Alembic 0008 保存；一般回复不是企业证据。连续评测固定清单并区分模拟/真实、缺测及版本，见对应主题文档。

## 按需读取

1. 先 `git status --short`，查相关实现和调用链；修改前查任务板对应 ID。
2. 默认只读本文件 + 一份主题文档的相关章节。不明确主题时查 [文档索引](docs/README.md)，无需先通读 README 或架构。
3. 用 `rg -n '^##' 文件` 定位，再用 `sed -n '起始,结束p' 文件` 取段；接口查对应模型/路由，不整读生成 JSON。
4. 只有跨模块设计才扩大阅读范围；链接是查阅入口，不是递归加载指令。已有上下文未变化时不重复读取。
5. `docs/archive/` 仅用于明确追溯；不默认扫描历史、全仓 Markdown、锁文件或测试产物。

| 任务 | 首选入口 |
| --- | --- |
| 完成情况、下一步 | [任务板](docs/TASK_BOARD.md) §1–3 |
| 架构、取消、交付、优化设计 | [架构](docs/architecture.md) §2、4–6、10 |
| 原生双工具、证据续答、工具扩展 | [Q07](docs/qa-routing-design.md) §0、4、6–8；部署配置见部署文档 |
| 门户、HTTPS/SSE/WSS、音频 | [门户契约](docs/portal-protocol.md) 对应章节 |
| CueKB、VoiceChat、文本模型、身份 | [接入](docs/integration.md) 对应服务章节 |
| Web/API 入口、镜像、部署、迁移、D07 | [部署](docs/deployment.md) 对应章节 |
| 测试证据、放行 | [验收](docs/acceptance-report.md) §0、3–5；命令见 README |
| 独立 API 全流程模拟 | [模拟测试](docs/simulated-full-flow.md)；显式夹具启动、网络/浏览器步骤及验证边界 |
| 跨轮条件、纠正、重连与连续评测 | [上下文与评测](docs/task-context-evaluation.md)；CTX1 / EVAL1、Alembic 0008、固定清单与评分 |

## 工程边界

- 知识执行保留 SessionCoordinator → BusinessRuntime → ToolRegistry；一般回答走 Gateway 的输入/响应许可及记录通道。供应商语音结构限于 `app/voice`，Agents SDK 限于 `app/agent_runtime`。VoiceChat、CueKB 独立部署。
- 验证阶段每次开始通话都签发独立、短期使用的 customer call capability；没有启动级 tenant 或共享 customer。KB 范围仍由服务端固定，浏览器/模型不可覆盖，且不能获得管理权限。正式客户认证留待验证通过后设计。
- 自然插话、停顿和让话由 VoiceChat 模型处理，不因普通 `speech_started` 自动取消业务任务；显式停止只清播放，明确取消/替换才使任务失效。
- 播放停止、任务失效、上游取消分开；业务提交和语音写回检查 revision/epoch/turn/租约，旧 pending call 必须结清或关闭连接。
- 编码阶段无 CueKB/VoiceChat 真实接口，按规范与受控夹具交付，不尝试真实调用。Docker 不可用时只静态检查，不安装或搭建容器环境；用户明确要求且已有 Docker 时可执行隔离构建/Compose 检查，结果与 D07 真实服务验收分开。云代理 CA 仅用 BuildKit secret 临时挂载，保留 TLS 校验。
- real 失败不回退 mock；未执行的真实服务、浏览器、数据库及云端检查不得写 passed。中文/天气/股票不属本期，不引入训练、GPU 或新队列服务。
- DB 用 Alembic；`AUTO_CREATE_SCHEMA` 仅限隔离测试。只有一套验证 `.env`/Compose 部署配置，镜像独立构建；fixture 仅由自动化测试显式注入。
- Web 镜像内的 Nginx 直接提供公网 HTTPS `8087`，无独立 Nginx 服务；仅将同源 `/api/` 转至容器内 `api:8000`。每次点击开始通话签发仅存于当前标签页内存的 call token，不共享 conversation/history；验证阶段不映射 API 宿主端口，不提供正式客户认证入口。
- Python 生产/开发依赖分别固定在 `requirements.txt`、`requirements-dev.txt`，Web 锁文件为 `apps/web/package-lock.json`。实际修改协议/schema 后执行 `PYTHONPATH=apps/api python scripts/export_contracts.py`。
- 不提交密钥、录音、票据、本地 DB 或产物。文档任务仅改文档，不改代码、配置、生成契约和依赖锁。
- 复杂任务先在任务板维护里程碑，逐步实施验证；设计归主题文档、证据归验收记录。完成报告说明变更、验证和未验证项。
