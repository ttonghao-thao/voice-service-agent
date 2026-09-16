# Voice Service Agent — 按需工作指南

生产级语音客服 Agent；Python 3.12 为主，TypeScript 仅用于浏览器。默认中文沟通。

## 阅读规则

1. 先检查 `git status` 和相关实现；本文件是默认入口，不要求全量读取设计。
2. 根据下表选一个任务入口；不明确时仅看 `docs/README.md` 索引。
3. 用 `rg -n '^## ' 文件` 定位标题，再读取相关段落、契约及调用链；不递归展开所有链接。
4. 只在跨模块架构变更时通读 `docs/architecture.md`；实施前检查 `docs/TASK_BOARD.md` 对应条目。
5. `docs/archive/` 是恢复快照，非有效规范；只在用户要求追溯或当前资料不足时读取。禁止默认全仓拼接 Markdown、整读生成契约或历史快照。

| 任务 | 阅读入口 | 代码入口 |
| --- | --- | --- |
| 产品范围、架构、模块边界 | `docs/architecture.md` 对应章节 | `apps/api/app/` |
| HTML 门户、消息、音频 | `docs/portal-protocol.md` | `apps/web/src/`、`api/routes.py`、`voice/` |
| CueKB、VoiceChat、身份、第三方工具 | `docs/integration.md` 对应章节 | `voice/provider.py`、`tools/`、`api/auth.py` |
| 改问、取消、子任务、结果过期 | `docs/architecture.md` §5–6 | `sessions/`、`agent_runtime/`、`storage/` |
| 部署、配置、迁移、故障 | `docs/deployment.md` | `deploy/`、`config.py`、`apps/api/migrations/` |
| 未完成工作、验证 | `docs/TASK_BOARD.md`、`docs/acceptance-report.md` 对应条目 | `tests/`、`scripts/`；命令见 README |

上表 API 相对路径均以 `apps/api/app/` 为基准。产品目标以架构文档为准；现有行为以实现及契约为准；差距写入任务板，不能把目标当成已完成。

## 必须保持的边界

- 简单客户语音门户 → 本项目消息接口；VoiceChat、CueKB 独立部署。天气/股票按接入启用，不是所有部署的必需依赖。
- 保留 SessionCoordinator → BusinessRuntime → ToolRegistry；供应商语音事件限于 `app/voice`，Agents SDK 限于 `app/agent_runtime`。
- 中文能力由云端语音服务提供，本项目不训练模型、不引入 NeMo/CUDA、不过滤中文。
- 身份、租户、KB 权限由服务端认证与授权确定；浏览器/模型不能覆盖。endpoint、凭据仅来自服务端配置。
- 播放停止、任务失效、上游取消分开；提交和工具写回前检查 epoch/turn/租约。不得伪造 VoiceChat 事件或遗留 pending call。
- real 失败不回退假数据；mock 显式标记。未执行真实服务、浏览器、数据库或云端验证不得写 passed。
- DB 使用 Alembic；`AUTO_CREATE_SCHEMA` 仅供隔离测试。云端仅用 Docker Compose，本地开发不受影响。
- 锁文件为 `uv.lock`、`apps/web/package-lock.json`；实际修改协议/schema 后运行 `PYTHONPATH=apps/api uv run python scripts/export_contracts.py`。
- 不提交密钥、录音、票据、本地 DB 或测试产物。文档任务不修改代码、配置、生成契约或依赖锁。
- 复杂实现维护任务板与验收记录；新决策归并相应主题文档，不再追加另一份长期并行的总设计。
