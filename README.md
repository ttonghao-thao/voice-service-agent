# Voice Service Agent · 全双工知识客服

本系统提供实时全双工语音客服与连接内多轮对话。NVIDIA VoiceChat 负责英文口述，Sophon Live 按配置提供中文或英文口述。Sophon 可使用 LiveKit WebRTC，并通过客户端委派选择授权 CueKB、天气及公开搜索。身份、会话、证据与业务提交由本项目维护。部署范围见 [交付说明](docs/delivery.md)。

门户界面沿用英文。Sophon LiveKit 的语言、Room 与工具配置见 [部署文档](docs/deployment.md#启用-livekit-音频和三类工具)。

后续开发统一在 `main` 演进，基线来自 `codex/q1003`。

Codex 从 [AGENTS](AGENTS.md) 开始，按任务查 [文档索引](docs/README.md)；跨模块关系见 [架构](docs/architecture.md)。当前任务归 [任务板](docs/TASK_BOARD.md)，已执行测试及真实服务边界归 [验收](docs/validation.md)。

## 验证

当前工具等待、Golden Yolk 首问及回答完整性按 [最新验证手册](docs/validation.md) 执行当前文件与麦克风用例；过期方法仅由 Git 追溯。操作者按顺序执行即可；采集自动关联会话并输出精简证据，不需要选取全量日志或判断故障。

## 云端部署

API/Web 独立镜像；仅一套验证 env/Compose。按 [部署](docs/deployment.md) 准备镜像、TLS、真实服务配置和数据库备份后运行：

```sh
./scripts/deploy-cloud.sh .env
```

脚本先运行数据库 migrate 再启动应用；推送 Git 代码不升级部署数据库。端口、当前迁移版本、模式切换、drain/回滚只在部署文档维护。真实供应商/GPU/听音放行仍须执行验收；模拟验证不替代它们。
