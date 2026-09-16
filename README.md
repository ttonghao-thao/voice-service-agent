# Voice Service Agent · 语音客服 Agent

客户通过简单 HTML 门户发起语音服务。本项目管理会话、业务推理、工具和回答，后台连接独立的 **NVIDIA VoiceChat** 与 **CueKB**；第三方查询按实际接入启用。

```text
HTML 语音门户 ⇄ HTTPS / SSE / WSS ⇄ Voice Service Agent
                                   ├─ VoiceChatAdapter ⇄ 独立 VoiceChat
                                   ├─ 后台客服 Agent ⇄ 文本推理模型
                                   │                 └─ ToolRegistry → CueKB / 可选第三方系统
                                   └─ PostgreSQL / Redis
```

**当前状态：最终设计已整理，D01–D05 代码已完成本地验证。** 门户事件契约已冻结为严格联合类型，VoiceChat 能力声明绑定 API 版本/镜像 digest/真实探测模式；OIDC 已支持受限 `customer` 角色、会话隔离、KB 交集及历史撤权脱敏。CueKB 原生适配、简单客户门户及独立任务 revision/停播/取消语义已经实现；真实 VoiceChat、SSO、CueKB、PostgreSQL/Redis/Docker 和生产验收未完成。默认开发 mock 有明确标识，不能作为真实语音或知识查询结果。

## 按需阅读

- 产品和架构：[最终设计](docs/architecture.md)。
- 接入 HTML 客户端：[门户消息/语音契约](docs/portal-protocol.md)。
- 对接 CueKB、VoiceChat、身份和第三方：[接入说明](docs/integration.md)。
- 下一步和现状差距：[任务板](docs/TASK_BOARD.md)。
- 其余主题：[文档索引](docs/README.md)。Codex 从 [AGENTS.md](AGENTS.md) 按任务读取，无需全量加载。

## 本机启动（当前实现）

Python 3.12、uv、Node.js 22；仓库根目录执行：

```sh
cp .env.example .env
uv sync --locked
uv run alembic -c apps/api/alembic.ini upgrade head
PYTHONPATH=apps/api uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

另开终端：

```sh
npm ci --prefix apps/web
npm run dev --prefix apps/web -- --port 5173
```

访问 [本地门户](http://localhost:5173)。`PUBLIC_ORIGIN` 必须与浏览器地址一致；不要混用 localhost 与 127.0.0.1。当前页面是精简客户入口，工具管理只保留受权限保护的后端运维 API，不进入客户页面。

“联调示例”仅验证合成文字、引用和历史；mock 语音只验证传输，不识别或合成业务语音。开发身份默认不是管理员，需要本地管理功能时才设置 `DEV_ADMIN=true`。不将开发身份用于生产。

## 验证命令

```sh
uv run --locked pytest -q
uv run --locked ruff check apps/api tests scripts
npm test --prefix apps/web
npm run build --prefix apps/web
```

启动本地后端和前端后，按需要执行浏览器测试：

```sh
cd apps/web
npx playwright install chromium
npm run test:e2e
```

测试产物写入被忽略的 artifacts/test-results 等目录。真实语音探测：

```sh
PYTHONPATH=apps/api uv run python scripts/probe_voicechat.py \
  --wav /path/to/authorized-first-question.wav \
  --barge-in-wav /path/to/authorized-distinct-second-question.wav \
  --output-wav artifacts/authorized-review-output.wav
```

探测前必须配置目标 `VOICECHAT_API_VERSION` 和 `VOICECHAT_IMAGE_DIGEST`。脚本默认延迟工具结果 5 秒，第二段录音用于观察等待阶段插话；报告只给出事件时序和候选证据，实际音频必须人工复核。脚本不会自动设置 `VOICECHAT_CAPABILITY_MODE` 或生产开关。未配置地址时输出 blocked；合成工具返回不能证明真实 CueKB/第三方业务通过。已执行范围见 [验收记录](docs/acceptance-report.md)。

## 云端部署

云端只使用 Docker Compose 构建、迁移和运行，不直接启动宿主 Python/Node。配置完成后使用：

```sh
./scripts/deploy-cloud.sh .env.production
```

配置准备、现有生产天气硬依赖、TLS、迁移、粘性路由和回滚见 [部署文档](docs/deployment.md)。当前不能只配置 CueKB 地址就宣称符合最终方案。

## 代码入口

| 位置 | 责任 |
| --- | --- |
| `apps/api/app/api/` | HTTP/SSE/WS、认证、管理 |
| `apps/api/app/voice/` | VoiceChat 适配、音频与工具桥接 |
| `apps/api/app/sessions/` | 会话、任务、epoch、租约与取消 |
| `apps/api/app/agent_runtime/` | 后台客服 Agent、SDK 循环、答案校验 |
| `apps/api/app/tools/` | 工具注册、权限、契约与服务适配 |
| `apps/api/app/storage/`、`apps/api/migrations/` | 持久化和 Alembic |
| `apps/web/src/`、`apps/web/public/` | 门户与浏览器音频 |
| `contracts/` | 当前代码生成的接口，非未来设计已实现证明 |
