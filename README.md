# Voice Service Agent · 实时语音问答

英文实时语音问答系统，客服是首个应用场景。独立 NVIDIA VoiceChat-11B 负责语音理解、原生工具选择与语音生成；本项目负责会话、权限、知识执行和交付，独立 CueKB 提供检索。

```text
浏览器 → HTTPS :8087 → Web 容器内 Nginx → 同源 /api/ → API :8000
API → VoiceChatAdapter → 独立 VoiceChat
    → Coordinator → BusinessRuntime → ToolRegistry → CueKB
                                  └→ 复杂执行 / 升级 → 外置 LLM
    → PostgreSQL / Redis
```

| 模式 / 策略 | 原生工具与行为 |
| --- | --- |
| legacy（默认） | consult_service_agent；完整输入进入既有外置知识链 |
| dual_tools + general_qa | lookup_knowledge / reason_over_knowledge；一般问题可直接答，企业知识直查或推理 |
| dual_tools + knowledge_required | 同上两个工具；拒绝无工具实质性回答，lookup 也走外置执行 |

本期双工具模式仅注册两个工具，支持可信代码扩展。后台 ENABLED_TOOLS=search_knowledge 是另一层；VoiceChat 用 tools/instructions 选路，不发送 WS tool_choice、不增加分类模型。D2 直查交 Nano 基于证据续答，一般回复不带企业引用。

本轮 P1/P2 增加实际口述检查、结束/重连审计、门户进度与受 Provider 能力限制的等待问答/更正。NVIDIA 的等待期能力仍关闭；独立模拟扩展不能证明真实支持。短答与实际转写的检查也不能证明音频准确或用户听到。部署当前代码须迁移 **Alembic 0009**；真实服务放行仍待 D07/Q07-E。

状态见 [任务板 §1–3](docs/TASK_BOARD.md#1-当前结论与审计基线)，设计见 [Live 改进](docs/live-agent-implementation.md)，最新结果见 [验收 §0](docs/acceptance-report.md#0-当前审计与证据索引)。Codex 默认从 [AGENTS](AGENTS.md) 按需读取一份主题；其他入口见 [文档索引](docs/README.md)，不用全量加载。

## 本机验证

Python 3.12；夹具由测试显式注入，正常部署使用独立服务，不构成第二套部署配置。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-dev.txt
npm ci --prefix apps/web
.venv/bin/python -m pytest -q
.venv/bin/ruff check apps/api tests scripts
npm test --prefix apps/web
npm run build --prefix apps/web
PYTHONPATH=apps/api .venv/bin/python scripts/export_contracts.py
```

本次三项网络回归与固定连续报告：

```sh
.venv/bin/python -m pytest -q tests/integration/test_live_agent_interactions.py \
  tests/integration/test_delivery_lifecycle.py
PYTHONPATH=apps/api:. .venv/bin/python -m scripts.evaluate_continuous_dialogue \
  --output artifacts/continuous-dialogue
```

夹具监听独立 CueKB HTTP、VoiceChat WS、兼容模型 HTTP，本项目使用实际网络适配器和 Alembic。ASR、工具决策、口述与 PCM 由脚本生成，不是模型正确率或实际听音。浏览器启动/命令见 [模拟 §3](docs/simulated-full-flow.md#3-复现步骤)；真实探针及授权录音步骤见 [接入 §3.4](docs/integration.md#34-语言版本和能力验证)。产物只留忽略目录。

## 云端部署

仅一套验证 .env/Compose；API/Web 通过独立 docker build 构建，Compose 迁移/运行，不直接启动宿主 Python/Node。准备镜像、TLS 和配置后运行：

```sh
./scripts/deploy-cloud.sh .env
```

公网 HTTPS 8087 由 Web 镜像内 Nginx 提供，无独立 Nginx 服务；/api/ 转容器 api:8000，API 不映射宿主端口。每个标签页开始通话创建独立 conversation 和内存中的短期 call token；KB 范围由服务端固定，不是正式客户认证。

镜像、URL、迁移/drain/回滚与真实 D07 见 [部署](docs/deployment.md)。严格逐轮 response 归属依赖匹配的独立 VoiceChat 版本，见 [补丁交付](deploy/voicechat/README.md)。上一版 Docker/Compose 结果保留在验收日期记录，不能替代当前 0009 或真实服务检查。

## 代码入口

| 位置 | 职责 |
| --- | --- |
| apps/api/app/api/、voice/ | 认证/HTTP/SSE/WSS；Provider 能力、音频/工具桥接 |
| apps/api/app/sessions/ | 任务、期限、epoch/revision、租约与控制 |
| apps/api/app/agent_runtime/、tools/ | 原生注册/执行、证据/口述约束；后台工具、权限与 CueKB 适配 |
| apps/api/app/storage/、migrations/ | 上下文、交付台账与 Alembic |
| apps/web/src/、public/ | 门户、实际字幕与播放排空估计 |
| contracts/ | 当前代码生成契约；不证明供应商未来能力 |
