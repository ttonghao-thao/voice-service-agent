# Voice Service Agent

本仓库主语言 Python 3.12，浏览器辅助语言 TypeScript。按生产系统对待，沟通默认中文。

- 开始修改前阅读 `docs/VoiceChat_AgentsSDK_Codex_Design.md`、`docs/implementation-plan.md`、相关契约和实现。
- 核心链路：门户 → SessionCoordinator → BusinessRuntime → ToolRegistry；原生 VoiceChat 事件只进入 `app/voice`，SDK 仅限 `app/agent_runtime`。
- 中文语音由云端模型提供。本项目不训练模型，不引入 NeMo/CUDA，不过滤中文。
- 原生工具桥接保留 pending call_id；提交历史及写回前检查 epoch/turn/租约。播放停止不等于模型取消。
- API 身份、租户和知识库权限来自认证服务，不能从工具参数或浏览器正文覆盖。工具 endpoint 与凭据仅从服务端配置取得。
- 开发 mock 必须显式标记；禁止 real 服务失败后回退合成数据。真实联调与契约测试分别记录。
- 数据库变化使用 Alembic；不要在已部署库用 create_all 代替迁移。`AUTO_CREATE_SCHEMA` 只供隔离测试。
- 云端服务器只使用 Docker Compose 构建、迁移和运行；不要在云端直接启动宿主 Python/Node 进程。本地开发与验证方式保持不变。
- Python 依赖锁在 `uv.lock`，Node 锁在 `apps/web/package-lock.json`。协议/schema 更新后运行 `PYTHONPATH=apps/api uv run python scripts/export_contracts.py`。
- 验证命令见 README。保持 `docs/acceptance-report.md` 与实际测试同步；未执行 PostgreSQL、Redis、云端语音、SSO 或真实工具的测试不得写成 passed。
- 不提交 `.env`、本地数据库、录音、浏览器票据或生成测试产物。
