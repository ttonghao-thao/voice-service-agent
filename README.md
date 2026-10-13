# Voice Service Agent · 实时语音问答

英文实时语音问答，客服为首个场景。独立 NVIDIA VoiceChat 负责实时语音和原生工具选择，独立 CueKB 提供检索；本项目处理会话、权限、知识执行与交付。部署模板启用可配置多工具模式；未配置模式的旧部署仍兼容 legacy。工具数量、后台管理与动态刷新见 [工具目录](docs/interfaces/voice-tools.md)，执行行为见 [知识执行](docs/qa-routing-design.md)。

Codex 从 [AGENTS](AGENTS.md) 开始，按任务查 [文档索引](docs/README.md)；跨模块关系见 [架构](docs/architecture.md)。当前任务归 [任务板](docs/TASK_BOARD.md)，已执行测试及真实服务边界归 [验收](docs/acceptance-report.md)。

## 本机验证

Python 3.12；下面集中列出安装和完整回归命令，日常按改动选择相关测试。夹具由测试显式注入，正常部署使用独立服务。

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

预录语音入口的定向验证使用实际 Chromium Web Audio 和受控 HTTP/WS 夹具。先在一个终端启动门户：

```sh
npm run dev --prefix apps/web -- --port 5173
```

再在另一个终端执行：

```sh
cd apps/web
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium \
  npm run test:e2e -- voice-file.spec.ts
```

若 Playwright 已安装自己的 Chromium，可省略 `PLAYWRIGHT_CHROMIUM_EXECUTABLE`。

手工验证：打开门户后选择 `Audio file` → `Choose WAV`，使用不超过 30 秒/2 MiB 的 24 kHz 单声道 PCM16 WAV → `Start file call`。应先显示等待连接，再按实时速率推进至 `Recording sent. Waiting for reply`；文件模式不应请求麦克风。`Stop playback` 不停止文件上行，`End call` 结束本次输入。该入口在浏览器内解析并复用实时 WS，不把文件上传到本项目服务器。

夹具监听独立 CueKB HTTP、VoiceChat WS、兼容模型 HTTP，本项目使用实际网络适配器和 Alembic。ASR、工具决策、口述与 PCM 由脚本生成，不是模型正确率或实际听音。浏览器启动/命令见 [模拟 §3](docs/simulated-full-flow.md#3-复现步骤)；真实探针及授权录音步骤见 [接入 §3.4](docs/integration.md#34-语言版本和能力验证)。产物只留忽略目录。

## 云端部署

API/Web 独立镜像；仅一套验证 env/Compose。按 [部署](docs/deployment.md) 准备镜像、TLS、真实服务配置和数据库备份后运行：

```sh
./scripts/deploy-cloud.sh .env
```

脚本先运行数据库 migrate 再启动应用；推送 Git 代码不升级部署数据库。端口、当前迁移版本、模式切换、drain/回滚只在部署文档维护。真实供应商/GPU/听音放行仍须执行验收；模拟验证不替代它们。
