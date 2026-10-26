# Voice Service Agent · 英文全双工知识客服

本系统面向英文实时全双工知识问答与连接内多轮对话。独立 NVIDIA VoiceChat 负责语音和原生工具选择，独立 CueKB 提供检索；本项目处理 Web 门户、会话、权限、有限检索及有来源答案交付。当前部署为 `dual_tools/general_qa`，单工具 `search_knowledge_base`，不需要外置答案模型。具体需求、已实现设计及未解决问题统一见 [交付说明](docs/delivery.md)。

产品语音、门户交互和新会话语言均为英文（`en-US`）。中文开发文档、历史测试文本及 Unicode 数据兼容不代表产品支持中文语音；实现约束见 [架构的产品边界](docs/architecture.md#1-产品与部署边界)。

后续开发统一在 `main` 演进，基线来自 `codex/q1003`。

Codex 从 [AGENTS](AGENTS.md) 开始，按任务查 [文档索引](docs/README.md)；跨模块关系见 [架构](docs/architecture.md)。当前任务归 [任务板](docs/TASK_BOARD.md)，已执行测试及真实服务边界归 [验收](docs/validation.md)。

## 验证

当前工具等待、Golden Yolk 首问及回答完整性按 [最新验证手册](docs/validation.md) 执行两次 Web 通话；历史方法已归档。操作者按顺序执行即可；采集自动关联会话并输出精简证据，不需要选取全量日志或判断故障。

## 云端部署

API/Web 独立镜像；仅一套验证 env/Compose。按 [部署](docs/deployment.md) 准备镜像、TLS、真实服务配置和数据库备份后运行：

```sh
./scripts/deploy-cloud.sh .env
```

脚本先运行数据库 migrate 再启动应用；推送 Git 代码不升级部署数据库。端口、当前迁移版本、模式切换、drain/回滚只在部署文档维护。真实供应商/GPU/听音放行仍须执行验收；模拟验证不替代它们。
