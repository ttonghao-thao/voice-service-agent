# Voice Service Agent — 工作入口

生产级英文知识库语音客服；Python 3.12 / TypeScript。默认中文沟通。本期仅启用 `search_knowledge`。

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
| 门户、HTTP/SSE/WS、音频 | [门户契约](docs/portal-protocol.md) 对应章节 |
| CueKB、VoiceChat、文本模型、身份 | [接入](docs/integration.md) 对应服务章节 |
| Web/API 入口、镜像、部署、迁移、D07 | [部署](docs/deployment.md) 对应章节 |
| 测试证据、放行 | [验收](docs/acceptance-report.md) §0、3–5；命令见 README |

## 工程边界

- 保留 SessionCoordinator → BusinessRuntime → ToolRegistry；供应商语音结构限于 `app/voice`，Agents SDK 限于 `app/agent_runtime`。VoiceChat、CueKB 独立部署。
- 身份、租户、KB 范围和 endpoint/凭据由服务端确定；浏览器/模型不可覆盖。客户不能获得管理权限。
- 播放停止、任务失效、上游取消分开；业务提交和语音写回检查 revision/epoch/turn/租约，旧 pending call 必须结清或关闭连接。
- 编码阶段无 CueKB/VoiceChat 真实接口，按规范与受控夹具交付，不尝试真实调用。无 Docker 环境：只静态检查，不安装、拉取、构建或启动容器。真实验证归 D07 部署阶段。
- real 失败不回退 mock；未执行的真实服务、浏览器、数据库及云端检查不得写 passed。中文/天气/股票不属本期，不引入训练、GPU 或新队列服务。
- DB 用 Alembic；`AUTO_CREATE_SCHEMA` 仅限隔离测试。只有一套生产 `.env`/Compose 部署配置，镜像独立构建；fixture 仅由自动化测试显式注入。
- Web 镜像内的 Nginx 直接提供公网 HTTPS `8087`，无独立 Nginx 服务；仅将同源 `/api/` 转至容器内 `api:8000`。API 宿主端口 `8088` 只绑定私网 IP，可供持 Bearer token 的私网客户端调用；当前没有专用系统间身份，真实客户暂不开放。
- 锁文件为 `uv.lock`、`apps/web/package-lock.json`。实际修改协议/schema 后执行 `PYTHONPATH=apps/api uv run python scripts/export_contracts.py`。
- 不提交密钥、录音、票据、本地 DB 或产物。文档任务仅改文档，不改代码、配置、生成契约和依赖锁。
- 复杂任务先在任务板维护里程碑，逐步实施验证；设计归主题文档、证据归验收记录。完成报告说明变更、验证和未验证项。
