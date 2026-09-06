# 声桥 · 客服智能体门户

Python 3.12 / FastAPI 后端，OpenAI Agents SDK 业务推理，React / TypeScript 中文门户，独立 VoiceChat WebSocket 适配器。

已实现应用代码和本地验证路径。真实 VoiceChat、文本模型、RAG、天气、SSO 需要填写集成配置；**当前默认是有醒目标识的开发 mock，不能作为生产或真实语音验收结果。**

云端服务器统一使用 Docker Compose 构建、执行 Alembic 迁移并运行全部服务；不在云端宿主机直接启动 Python、Node.js 或数据库进程。本机启动和验证方式保持如下，不受云端部署方式影响。

## 本机启动

需要 Python 3.12、uv、Node.js 22。仓库根目录执行：

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

浏览器打开 [客服工作台](http://localhost:5173)。`PUBLIC_ORIGIN` 必须与访问地址一致，尤其不能把 `localhost` 与 `127.0.0.1` 混用；麦克风依赖安全上下文。输入“联调示例”可检查中文文字、合成引用和历史；其他未命中的公司问题明确返回依据不足。Mock 语音只验证采集/传输与状态，不识别、不合成业务语音。默认开发身份不是管理员；本地需要管理工具时在 `.env` 设置 `DEV_ADMIN=true` 后重启后端。

## 验证

```sh
uv run pytest -q
uv run ruff check apps/api tests scripts
npm test --prefix apps/web
npm run build --prefix apps/web
```

启动上述后端和前端后执行浏览器测试：

```sh
cd apps/web
npx playwright install chromium
npm run test:e2e
```

浏览器测试包含显式合成麦克风。截图和报告写入被 Git 忽略的 `artifacts/`、`apps/web/test-results/`。真实云端探测不使用 mock 回退：

```sh
PYTHONPATH=apps/api uv run python scripts/probe_voicechat.py --wav /path/to/authorized-24k-mono-pcm16.wav
```

未配置云端地址时脚本输出 `blocked`。工具回传内容是明确标记的合成联调结果，不是天气或政策事实；脚本不自动打开生产能力开关。

## 云端 Docker 部署

云端复制 `.env.production.example` 为 `.env.production`，替换所有示例地址和凭据后执行：

```sh
./scripts/deploy-cloud.sh .env.production
```

该脚本只使用 `deploy/compose.production.yaml`，依次构建镜像、等待 PostgreSQL/Redis、执行 Alembic，再启动并检查 API 与 Web。默认 Web 只绑定宿主机 `127.0.0.1:8080`，由云端 HTTPS 网关转发；完整配置、升级和回滚方式见部署文档。

## 代码入口

- `apps/api/app/api/`：HTTP、SSE、OIDC/JWT、管理及 WebSocket 入口。
- `apps/api/app/sessions/`：业务任务、取消、租约与 epoch。
- `apps/api/app/agent_runtime/`：真实 `Runner.run` / `Runner.run_streamed`、工具封装、最终答案校验。
- `apps/api/app/tools/`：YAML 注册、权限、输入/输出 schema、预算、HTTP RAG/天气适配、证据审计。
- `apps/api/app/voice/`：官方事件映射、单写入器、有界队列、原生工具桥接。
- `apps/api/app/storage/` 和 `apps/api/migrations/`：SQLAlchemy 表与 Alembic 迁移。
- `apps/web/src/`、`apps/web/public/`：中文工作台、AudioWorklet、64-tap 抗混叠重采样、连续播放环形缓冲。

## 文档

- [设计方案](docs/VoiceChat_AgentsSDK_Codex_Design.md)
- [实施记录](docs/implementation-plan.md)
- [能力核对及 P0 阻塞项](docs/capability-report.md)
- [API、工具与认证接入](docs/integration.md)
- [部署、扩容、升级与回滚](docs/deployment.md)
- [A01–A22 验收记录](docs/acceptance-report.md)

生产不得采用开发身份或 mock；所有真实事实和语音验收均以实际服务报告为准。
