# 一期实施计划与进度

依据 `VoiceChat_AgentsSDK_Codex_Design.md` v1.1；主语言 Python 3.12，浏览器辅助语言 TypeScript。仓库起始只有设计文档。2026-09-05 中断恢复后已核对现有代码，继续完成最终回归和文档，未重复创建已完成模块。

2026-09-06 确认运行边界：云端服务器统一使用 Docker Compose 部署和运行，本地 Python/Node.js 开发验证路径保持不变。已新增独立生产 Compose、生产配置模板和顺序化部署脚本；真实云端容器验收仍待目标服务器执行。

## 里程碑状态

| 阶段 | 已完成的代码/本地验证 | 外部验收状态 |
| --- | --- | --- |
| P0 | 固定 SDK 与上游协议、native adapter、探测脚本、能力报告 | **blocked**：未配置真实云端地址/凭据，原生工具往返、音频结果与旧连接隔离未真实通过 |
| P1 | FastAPI、OIDC/JWT 边界、Alembic、工具注册/契约、SDK Runtime、HTTP/SSE、中文文字门户、证据展示 | 本地测试通过；真实模型/RAG/天气/SSO 待联调 |
| P2 | AudioWorklet、抗混叠重采样、连续 PCM、环形播放缓冲、单写入器、有界队列、VoiceChat 桥接与双字幕 | 合成音频/协议测试通过；真实 VoiceChat 接口和音频验收待执行 |
| P3 | epoch/turn fence、幂等、取消、历史事务、已确认 slots、受控恢复、Redis 会话归属 | 本地竞态和租约命令测试通过；真实 Redis/PostgreSQL/网络故障切换待验证 |
| P4 | 管理健康/启停、限流/容量/drain、日志脱敏、依赖/镜像锁、部署/回滚文档、100 条合成评测文本、A01–A22 报告 | 应用检查通过；容器部署、真实性能/容量/口述质量报告待目标环境完成 |

## 已执行检查

- [x] 44 项 Python 测试通过，包含实际 Agents SDK run/run_streamed 的本地合成 HTTP 工具循环及生产 Docker 静态契约。
- [x] Ruff、TypeScript/Vite 生产构建通过。
- [x] 6 项音频单元测试通过。
- [x] 5 项 Chromium 浏览器测试通过，包含显式合成麦克风及管理员操作。
- [x] FastAPI 本地启动与健康检查；开发配置明确标注 mock。
- [x] SQLite Alembic 新库升级、回退后重升级、schema drift 检查；PostgreSQL 离线 SQL 生成。
- [x] 契约重新导出；README、接入、部署、能力和验收文档同步。
- [ ] 真实 VoiceChat 中文工具往返、音频输出、旧连接隔离（P0 必须门槛）。
- [ ] 真实文本模型/RAG/天气/SSO、PostgreSQL/Redis/Docker 联调。
- [ ] 100 条真实语音用例、停顿/打断专项、并发与性能报告。

## 保持的边界

中文模型能力由云端提供，本项目不训练模型，不引入 NeMo/GPU 依赖。无密钥/地址不伪造成功；production 拒绝开发身份和 mock。没有使用未验证的原生取消、动态 instructions 或任意文本 TTS。已确认业务历史和实际播放进度分开，不把停止播放器称为供应商推理取消。

## 后续按依赖执行

1. 在 `.env` 或部署密钥系统补齐 VoiceChat、文本模型、RAG、天气和 OIDC 配置；核对租户/知识库 ACL 与目标容量。
2. 运行 P0 真实探测及 integration 门户回路，记录 API/容器版本、事件证据与失败项。
3. 在 PostgreSQL/Redis/HTTPS/WSS 拓扑验证迁移、认证、粘性路由、租约丢失及 drain。
4. 录制/授权音频并完成业务与停顿/打断样本；只根据真实观察更新能力开关与验收结论。

完整状态见 `docs/acceptance-report.md` 和 `docs/capability-report.md`。应用代码交付不等于生产验收完成。
