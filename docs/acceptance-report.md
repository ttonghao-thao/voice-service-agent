# 验收状态与真实服务门槛

更新：2026-09-17。目标架构已定稿，**D01–D06 与 D08 已完成代码和本地自动化验证，D07 的部署前检查代码已具备；真实服务与生产验收仍未完成**。缺口和实施顺序见 [任务板](TASK_BOARD.md)。

编码阶段约束（2026-09-16 确认）：CueKB/VoiceChat 无真实接口可调用，满足已确认接口规范和处理逻辑并通过相应契约/本地测试，即满足该阶段验收要求。Docker 环境不提供，仅在必要时静态检查镜像制作和启动代码，不搭建环境或执行镜像构建。以下真实服务与容器验收项留待后续部署阶段，不作为编码完成的阻塞项。

## 1. 本轮 D01–D06 与 D07 部署前检查验证

### 2026-09-17 D08 镜像/URL 配置检查

- 生产 Compose 的 migrate/api/web 只引用明确的应用镜像标签，`pull_policy: never`；部署脚本检查本地镜像并用 `--no-build` 启动，镜像构建命令独立写入部署文档。URL 对照表按门户、宿主端口及 IdP metadata 分别说明。
- 生产配置校验要求浏览器 OIDC 授权与换 token 地址，`PUBLIC_ORIGIN` 限定为无路径的 HTTPS origin；相应配置/Compose 契约纳入本地测试。
- 使用受控假 `docker` 命令验证部署前检查：缺少指定 API 镜像时脚本在启动任何服务前失败；未调用真实 Docker。
- 本次 `uv run --locked pytest -q`：65 passed、1 条 Starlette/AnyIO 上游弃用警告；`uv run --locked ruff check apps/api tests scripts`、`sh -n scripts/deploy-cloud.sh`、`git diff --check` 通过。未运行 Docker、真实 OIDC 或外部服务。
- 2026-09-17 配置跟进：生产模板 `PUBLIC_ORIGIN=https://th.ppy123.xyz`、`WEB_PORT=8082`，Compose 默认端口同步；未知的 OIDC/CueKB 地址改为明确占位符。相关契约/配置测试 26 passed，Ruff、脚本语法与差异检查通过。DNS、TLS、代理位置和真实外部端点尚未验收。

### 2026-09-16 既有验证记录

- Python `pytest`：60 passed；除 D01/D02 边界外，覆盖 CueKB `/v1/search` 请求、UUID KB 注入、trace/status/version/anchor 映射、降级和 HTTP 错误区分、旧 Citation 读取兼容、任务 supersede/cancel、重启恢复、晚到结果拒绝、停止播报不取消查询，以及仅有输入活动/附和时不取消。D06 另覆盖 CueKB-only 生产配置、部署白名单与管理员/身份权限的交集；D07 覆盖 readiness 报告拒绝 mock 或不匹配的工具集合。仍有一条 Starlette/AnyIO 上游弃用警告。
- Ruff：`apps/api`、`scripts`、`tests` 通过；生成 HTTP、上下行事件及答案 schema 后差异检查通过。
- 前端：6 项 Node 音频测试通过；TypeScript/Vite 生产构建通过。
- Playwright：启动隔离的开发 API/Vite 后 4 passed；覆盖中文文字/证据/刷新/窄屏、无依据、麦克风拒绝，以及合成麦克风连续帧/静音/停止播报/新 epoch 重建/释放。客户页面不含管理工作台；合成设备不证明真实语音质量。
- Alembic：临时空 SQLite 执行 0001→0004、downgrade base、再次 upgrade head 通过；0004 新增 request revision、parent/native call、取消原因、delivery status 和输出抑制字段。真实 PostgreSQL 迁移仍未执行。
- 发布脚本经 `sh -n` 静态检查；Compose 启动 API 后会在容器内核验 `/health/ready` 的真实非 mock 配置和 `ENABLED_TOOLS` 一致性。该检查没有执行 Docker，也不代表外部服务调用成功。
- 探测脚本 CLI 通过；当前未配置/调用真实 VoiceChat，没有生成事件报告或音频，不将脚本存在当作能力通过。
- `git diff --check` 通过。真实 PostgreSQL、Redis、Docker、OIDC、CueKB 和 VoiceChat 均未在本轮运行。

以上验证证明本地代码边界，不替代端到端协议、撤权传播速度和语音体验验收。

## 2. 已记录的应用验证（非本轮重跑）

| 日期 | 检查 | 当时结果与范围 |
| --- | --- | --- |
| 2026-09-15 | pytest | 44 passed；隔离 SQLite、SDK 合成 HTTP/provider、静态容器契约；一条上游弃用警告 |
| 2026-09-15 | Ruff、音频单测、前端构建 | Ruff 通过；6 项 Node 音频测试通过；TypeScript/Vite 构建通过 |
| 2026-09-05 | Playwright | 5 passed；Chromium/合成麦克风；不证明实际设备或模型语音效果 |
| 2026-09-05 | 启动与迁移 | 开发 FastAPI/SQLite 健康；SQLite 0001→0003、回退重升/schema check；PostgreSQL 仅离线 SQL |
| 2026-09-05 | VoiceChat 探测 | blocked，缺 VOICECHAT_WS_URL，真实连接次数为零 |

历史 A01–A22 详细表、当时能力报告和问题修复记录已保存在清理快照；不是当前验收通过的替代依据。运行时版本及锁文件仍以仓库当前文件为准。

## 3. 当前明确未验证

- 真实 VoiceChat 中文语音、工具往返、工具等待时的新问题回答、pending call 结清和实际口述准确性。
- CueKB 原生 `/v1/search` 的真实服务连接、受限 Key/ACL 和真实知识正确性；本地仅使用符合当前 CueKB schema 的受控响应与显式 mock。
- 客户 SSO/IdP 的实际 claim 与撤权传播、真实文本模型、任何实际天气/股票供应商。
- 真实 PostgreSQL/Redis/Docker、负载均衡故障、容量及端到端性能。
- 任务 revision、停止播报和安静边界轮换尚未在真实 VoiceChat 上验证；特别是工具阶段改问、pending call 安全结清和实际音频抑制仍受 D01 能力门槛约束。

这些是已记录验证范围，不表示本轮检查了当前机器是否安装了相应运行环境。

## 4. 最终方案验收清单

下表为待执行目标，均不能标记 passed。范围选择以部署启用能力为准。

| ID | 场景 | 完成依据 |
| --- | --- | --- |
| V01 | 简单 HTML 门户 + 标准消息 | 独立客户端创建会话、收发音频/字幕/答案，窄屏可用；不调用供应商私网 API |
| V02 | 客户身份与知识权限 | 两个客户并发互不读取历史/音频/证据；KB 交集与撤权生效；客户不能管理工具 |
| V03 | 真实 CueKB 知识闭环 | 中文语音→原生工具→授权检索→有依据回答→实际口述；保留版本、定位及 trace |
| V04 | 无依据、降级、冲突与故障 | 空命中、unassessed、degraded、版本冲突、401/403/422/429/5xx/超时分别正确处理；无假数据回退 |
| V05 | 工具等待时的交互 | 延迟工具 5 秒，等待中测试新问题、改问、取消、附和；记录是否在返回前回答新问题，ACK 不算 |
| V06 | 结果/改问/取消竞态 | 旧 revision 结果不进历史/播报；pending call 安全结束或连接关闭；新调用继续；不复用旧 call_id |
| V07 | 连续音频和播放 | 实际麦克风/扬声器，静音、背压、帧序号、慢网、后台/休眠、清音及晚到帧；转写先后不重复执行业务 |
| V08 | 长会话和恢复 | 超两分钟、多次轮换/断网，明确恢复损失；不重播旧录音；历史、字幕、播放估计分离 |
| V09 | 口述与事实一致 | 产品型号、版本、金额/数字、否定条件、引用；不能以正确文字卡片掩盖错误音频或绕过工具 |
| V10 | CueKB-only 与第三方扩展 | 无天气配置可启动知识服务；未启用工具不可调用；实际启用第三方时单独验收事实、时效和权限 |
| V11 | 数据库、租约、重启、部署 | 真实 PostgreSQL 迁移/行锁、Redis 失效、粘性路由、drain、备份/恢复和 Compose 健康 |
| V12 | 预算、隔离和安全 | 超时/有限重试、提示注入、输入输出上限、子任务预算、敏感数据脱敏；并发容量实测 |

## 5. 测量与放行

记录服务/API revision、镜像 digest、硬件和并发、样本来源/语言/数量、成功失败分母、事件时间线及授权音频证据。区分上行延迟、工具耗时、等待提示首音频、有效答案首音频和停止播放延迟；项目 SLA 阈值需在真实服务基线测量后确定。

当前 `tests/fixtures/voice-evaluation-cases.jsonl` 有 100 条合成文本（40 天气/30 知识/20 混合/10 澄清打断），尚未录音或真实执行。后续按知识客服和实际启用工具重新分配样本，不能因天气占比大就将天气设为必需能力。评分脚本只统计实际观测且标记 real_service 的项目。

基础语音客服放行要求核心知识、权限、恢复及部署验收通过。V05 的连续交谈门槛未通过时，保留明确的硬打断恢复模式，不能宣称完整工具阶段全双工。生产能力开关只根据真实报告设置。
