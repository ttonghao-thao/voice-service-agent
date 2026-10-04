# Voice Service Agent · 实时语音问答

可配置知识约束的实时语音问答系统，客服是首个应用场景。当前使用英文，通过 HTTPS 门户交谈；独立的 **NVIDIA VoiceChat-11B** 负责语音理解、原生工具选择与语音生成，应用负责会话、工具执行、权限和证据，**CueKB** 提供知识检索。

```text
公网浏览器 → HTTPS :8087 → Web 容器（Nginx 终止 TLS 并直接提供门户）
                            └─ 同源 /api/ → API 容器 :8000
API → VoiceChatAdapter → 独立 VoiceChat（原生选择直接回答或工具）
    ├─ 知识执行 → BusinessRuntime → ToolRegistry → CueKB
    │                 └─ 复杂问题 / 内部升级 → 外置文本模型
    └─ PostgreSQL / Redis
```

**Q07 基础实现已完成并通过本地验证；默认仍为 `legacy`，真实服务验收待 D07/Q07-E。** 当前模式如下：

| 模式 / 策略 | VoiceChat 可见工具 | 回答路径 |
| --- | --- | --- |
| `legacy`（默认） | `consult_service_agent` | 所有完整输入进入既有外置知识链 |
| `dual_tools` + `general_qa`（新模式默认策略） | `lookup_knowledge`、`reason_over_knowledge` | 一般问答可直接回答；企业知识直查 CueKB 或走外置 LLM + CueKB |
| `dual_tools` + `knowledge_required` | 同上两个工具 | 无工具实质性回答被拒绝；lookup 也走外置知识执行 |

本期新模式仅注册两个工具，可信注册机制支持后续扩展。后台 `ENABLED_TOOLS=search_knowledge` 控制 CueKB 能力，不应替换成上述两个原生工具名。VoiceChat 通过 tools/instructions 选择，不使用 WS `tool_choice` 或额外分类模型。D2 直查让 Nano 根据证据生成口述，服务端随后做有限检查；一般回答不带企业引用。严格知识模式也不保证 VoiceChat 逐字读出已批准文本。

配置见 [部署：问答模式与预算](docs/deployment.md#问答模式与预算q07)，完整行为与后续能力见 [Q07 设计](docs/qa-routing-design.md#0-本轮编码范围与扩展契约)。任务状态只维护在 [任务板](docs/TASK_BOARD.md)，已执行检查见 [验收记录](docs/acceptance-report.md)。

约 3 秒查询的分段计时与交付优化见 [接入 §4.1](docs/integration.md#41-查询延迟与优化边界)；speech 运行文件、Jinja 配置与派生镜像步骤见 [更新清单](deploy/voicechat/README.md)。

## 按需阅读

- 产品和当前架构：[架构](docs/architecture.md)；双工具与扩展契约：[Q07](docs/qa-routing-design.md)。
- 接入 HTML 客户端：[门户消息/语音契约](docs/portal-protocol.md)。
- 对接 CueKB、VoiceChat、身份和第三方：[接入说明](docs/integration.md)。
- 下一步和现状差距：[任务板](docs/TASK_BOARD.md)。
- API/Web 与独立 VoiceChat 联动升级：[VoiceChat 修复补丁与测试](deploy/voicechat/README.md)。单独更新门户不能修复上游 response 生命周期。
- 其余主题：[文档索引](docs/README.md)。Codex 从 [AGENTS.md](AGENTS.md) 按任务读取，无需全量加载。

## 本机验证

项目只维护一套功能验证部署配置。编码机使用显式注入的夹具、契约和静态检查；正常启动要求真实依赖，测试夹具不构成另一套部署。`.env.example` 包含连接配置及可选 Q07 参数注释，模式/策略在新建会话时固定。部署当前代码前须迁移到 Alembic 0008，并在真实验收环境验证。每个标签页开始通话都创建独立 conversation 和临时 call token，部署步骤见 [部署文档](docs/deployment.md)。

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

独立服务 API 的全流程模拟测试：

```sh
python -m pytest tests/integration/test_simulated_full_flow.py -q \
  -o junit_family=xunit1 --junitxml=artifacts/simulation/network.xml
```

两个 P0 已加入带来源的统一任务上下文与连续对话评测：一般背景中的确认条件可用于后续知识查询，纠正保留来源并清除旧版本。复现固定十组连续序列并生成报告：

```sh
PYTHONPATH=apps/api:. python -m scripts.evaluate_continuous_dialogue \
  --output artifacts/continuous-dialogue
```

实现边界、Alembic 0008、用例清单和真实/模拟评分见 [上下文与评测](docs/task-context-evaluation.md)。模拟工具决策不代表真实 Nano 的选择质量。

该测试启动独立监听的 CueKB HTTP、VoiceChat 原生 WS 和兼容文本模型 API，本项目适配器通过实际网络连接它们。覆盖三条问答路径、故障、取消、鉴权、SSE 和落库；[完整复现步骤与边界](docs/simulated-full-flow.md)包含连接实际门户的浏览器测试。模拟 ASR/工具选择/口述由脚本生成，不代表真实模型验收。

测试产物写入被忽略的 artifacts/test-results 等目录。真实语音探测：

```sh
PYTHONPATH=apps/api python scripts/probe_voicechat.py \
  --api-version <target-api-revision> \
  --image-digest sha256:<target-image-digest> \
  --wav /path/to/authorized-first-question.wav \
  --barge-in-wav /path/to/authorized-distinct-second-question.wav \
  --output-wav artifacts/authorized-review-output.wav
```

此探针在具备真实服务及授权录音的验收环境运行。目标 API revision 和镜像 digest 写入报告，不进入 `.env`。脚本默认延迟工具结果 5 秒，第二段录音用于观察等待阶段插话；实际音频必须人工复核。未配置地址时输出 blocked；该合成工具探针不代替 CueKB 闭环或 Q07 双工具选路验收。已执行范围见 [验收记录](docs/acceptance-report.md)。

## 云端部署

生产镜像通过独立 `docker build` 制作；云端使用 Docker Compose 迁移和运行，不直接启动宿主 Python/Node。构建、镜像标签和 URL 填写方式见 [部署文档](docs/deployment.md)。配置完成并准备好镜像后使用：

```sh
./scripts/deploy-cloud.sh .env
```

2026-10-04 已实际构建 API/Web，并用临时证书、独立项目名和测试配置完成隔离 Compose 检查：PostgreSQL 迁移到 0008、Redis/API/Web 健康、HTTPS、同源 API、SSE 与通话 capability 均通过。云代理 CA 通过可选 BuildKit secret 提供；外部模型/CueKB/VoiceChat 未调用，真实端到端验收仍待 D07/Q07-E，详见 [验收记录](docs/acceptance-report.md)。

配置准备、`ENABLED_TOOLS`、TLS 证书、浏览器麦克风权限、迁移、粘性路由和回滚见 [部署文档](docs/deployment.md)。CueKB-only 可只启用知识工具，但仍必须完成真实服务验收后才能放行。

## 代码入口

| 位置 | 责任 |
| --- | --- |
| `apps/api/app/api/` | HTTPS/SSE/WSS、认证、管理 |
| `apps/api/app/voice/` | VoiceChat 适配、音频与工具桥接 |
| `apps/api/app/sessions/` | 会话、任务、epoch、租约与取消 |
| `apps/api/app/agent_runtime/` | 原生工具注册/分派、直查证据、外置 SDK 循环与答案校验 |
| `apps/api/app/tools/` | 后台 ToolRegistry、权限、契约与 CueKB 等服务适配 |
| `apps/api/app/storage/`、`apps/api/migrations/` | 持久化和 Alembic |
| `apps/web/src/`、`apps/web/public/` | 门户与浏览器音频 |
| `contracts/` | 当前代码生成的接口，非未来设计已实现证明 |
