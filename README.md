# Voice Service Agent · 语音客服 Agent

测试人员可通过公网 HTTPS 门户验证语音服务；后台 API 是本阶段重点。本项目管理会话、业务推理、工具和回答，后台连接独立的 **NVIDIA VoiceChat** 与 **CueKB**；第三方查询按实际接入启用。

```text
公网浏览器 → HTTPS :8087 → Web 容器（Nginx 直接提供门户）
                            └─ 同源 /api/ → API 容器 :8000
API → VoiceChatAdapter / 后台客服 Agent / ToolRegistry → 独立 VoiceChat、文本模型、CueKB
    └─ PostgreSQL / Redis
```

**本期：英文知识库客服，默认仅启用 `search_knowledge`。** 代码完成范围与待办只维护在 [任务板](docs/TASK_BOARD.md)，真实环境尚未验收；本地受控测试不代表实际语音和知识效果。已执行检查见 [验收记录](docs/acceptance-report.md)。

## 按需阅读

- 产品和架构：[最终设计](docs/architecture.md)。
- 接入 HTML 客户端：[门户消息/语音契约](docs/portal-protocol.md)。
- 对接 CueKB、VoiceChat、身份和第三方：[接入说明](docs/integration.md)。
- 下一步和现状差距：[任务板](docs/TASK_BOARD.md)。
- 其余主题：[文档索引](docs/README.md)。Codex 从 [AGENTS.md](AGENTS.md) 按任务读取，无需全量加载。

## 本机验证

项目只维护一套功能验证部署配置。编码机使用显式注入的夹具、契约和静态检查验证实现，不用第二套 env 或 Compose。实际启动只需在 `.env` 填写镜像、HTTPS、数据库、文本模型、CueKB、VoiceChat、租户和 KB 范围；固定模式、容量与超时使用 Compose/代码默认值。Web 镜像内置 Nginx，浏览器直接访问公网 HTTPS `8087`；API 只在容器网络中供同源 `/api/` 使用。验证门户采用服务端固定 customer 身份，不包含登录流程或正式客户鉴权。部署步骤见 [部署文档](docs/deployment.md)。

## 验证命令

使用 Python 3.12 创建本地虚拟环境并安装固定版本的开发依赖：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --disable-pip-version-check -r requirements-dev.txt
```

然后执行：

```sh
python -m pytest -q
ruff check apps/api tests scripts
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
PYTHONPATH=apps/api python scripts/probe_voicechat.py \
  --api-version <target-api-revision> \
  --image-digest sha256:<target-image-digest> \
  --wav /path/to/authorized-first-question.wav \
  --barge-in-wav /path/to/authorized-distinct-second-question.wav \
  --output-wav artifacts/authorized-review-output.wav
```

目标 API revision 和镜像 digest 作为探针参数写入报告，不进入 `.env`。脚本默认延迟工具结果 5 秒，第二段录音用于观察等待阶段插话；报告只给出事件时序和候选证据，实际音频必须人工复核。未配置 VoiceChat 地址时输出 blocked；合成工具返回不能证明真实 CueKB 业务通过。已执行范围见 [验收记录](docs/acceptance-report.md)。

## 云端部署

生产镜像通过独立 `docker build` 制作；云端使用 Docker Compose 迁移和运行，不直接启动宿主 Python/Node。构建、镜像标签和 URL 填写方式见 [部署文档](docs/deployment.md)。配置完成并准备好镜像后使用：

```sh
./scripts/deploy-cloud.sh .env
```

配置准备、`ENABLED_TOOLS`、TLS、迁移、粘性路由和回滚见 [部署文档](docs/deployment.md)。CueKB-only 可只启用知识工具，但仍必须完成真实服务验收后才能放行。

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
