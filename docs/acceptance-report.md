# 验收状态与真实服务门槛

更新：2026-09-25。**D01–D06、D08–D10、E01–E03 已完成历史编码和本地自动化验证，D13 已完成独立测试通话、实时字幕与 HTTP 门户改造；D07 的真实服务端到端验收仍未完成**。本期仅验收英文知识库客服；真实验证在云端 Docker 环境执行。缺口和实施顺序见 [任务板](TASK_BOARD.md)。

编码阶段约束（2026-09-16 确认）：CueKB/VoiceChat 无真实接口可调用，满足已确认接口规范和处理逻辑并通过相应契约/本地测试，即满足该阶段验收要求。Docker 环境不提供，仅在必要时静态检查镜像制作和启动代码，不搭建环境或执行镜像构建。以下真实服务与容器验收项留待后续部署阶段，不作为编码完成的阻塞项。

## 0. 当前审计与证据索引

2026-09-22 的历史文档审计基线为 `9b44859`；当时只审计和修改文档，没有重跑应用测试。此后 D09/D10 已在本地完成编码和验证；本节按日期区分证据，不将旧工作树状态当成当前状态。

| 要回答的问题 | 证据位置与边界 |
| --- | --- |
| 最近应用验证 | §1 的 2026-09-25 D13：72 项 pytest、6 项前端单测、4 项 Chromium E2E、构建、Ruff、契约与迁移循环；不代表真实服务或 Docker 验收 |
| M3、D08、D01–D06 何时验证 | §1 对应日期；不累加各次测试数作为当前总数 |
| 真实服务是否通过 | §3：仍待验证；测试文件存在、配置 verified 或本地测试通过都不能替代真实记录 |
| 下一阶段怎样执行 | [部署 D07-A–F](deployment.md#d07-分阶段执行设计)，场景标准见 §4，放行见 §5 |
| 新增优化是否已实现 | [任务板 Q01–Q04](TASK_BOARD.md#3-待完成与建议顺序)：建议待排期，不属于当前实现 |

历史文档审计（2026-09-22）：9 份活动 Markdown 的 63 处本地链接及章节锚点、代码块闭合、`git diff --check` 与当时的仅文档修改检查通过。AGENTS 从 3688 减为 2912 UTF-8 字节（约 21%），这是当时入口大小变化，不是实际 token 节省测量。历史快照未修改；当前 D11 的验证另记于 §1。

## 1. 按日期记录的编码与部署前验证

### 2026-09-25 D13 独立测试通话、实时字幕与 HTTP 门户

- 删除启动时 `TENANT_ID` 与固定 `validation-customer`。每次创建 conversation 时生成独立 owner 和高熵 call token；token 哈希入库，明文只返回一次并保存在当前标签页内存。HTTP/SSE 以 Bearer 认证，WS ticket 绑定同一 owner/conversation/epoch；交叉 token、缺 token 和结束后复用均被拒绝。Alembic 0005 将 conversation 的 tenant/user 所有权迁移为 owner/token hash，并移除 admin audit tenant。
- 门户加载时不再读取 conversation 列表；每次点击 “Start call” 都创建新 conversation，结束通话撤销 token。`portal.transcript.*` 和 `portal.speech_text.*` 分别实时打印用户输入与 VoiceChat 实际输出字幕；业务答案/证据继续与口述字幕分离。
- Web Nginx 改为公网 HTTP `8087`，移除证书挂载、TLS 启动参数和 `.env` TLS/tenant 项；内部 `/api/` 仍只转发到容器网络 API。公网 HTTP 麦克风是否被目标浏览器允许尚未实机验证，是 D07 阻断项。
- 本地验证：72 项 pytest 通过（1 条 Starlette/AnyIO 上游弃用警告）；6 项前端音频单测、4 项 Chromium 门户/音频 E2E 和 TypeScript/Vite 构建通过；Ruff、部署脚本语法、契约导出和 `git diff --check` 通过；Alembic 临时 SQLite `upgrade head → downgrade base → upgrade head` 通过。真实 PostgreSQL、Docker、公网 HTTP 浏览器麦克风、多人语音、CueKB、VoiceChat 和文本模型未运行。

### 2026-09-25 D08 Python 依赖工具链简化

- 删除 Python 依赖管理工具及其锁文件；`requirements.txt` 固定生产依赖，`requirements-dev.txt` 引用生产清单并追加测试/Lint 工具，`pyproject.toml` 只保留 pytest/Ruff 配置。README、工程入口、契约导出和 VoiceChat 探针命令统一为标准 Python 3.12 venv/pip。
- API 镜像直接使用基础镜像自带的 pip 安装 `requirements.txt`，不创建额外虚拟环境；Docker 在复制源码前单独安装依赖，保留原有分层缓存。运行命令仍直接使用 `uvicorn`，业务代码和服务边界未改变。
- 全新 Python 3.12 venv 仅通过 pip 安装固定依赖，`pip check` 无冲突；`python -m pytest -q` 73 passed（1 条 Starlette/AnyIO 上游弃用警告），Ruff、6 项前端音频测试、TypeScript/Vite 构建、契约导出、API import smoke、部署脚本语法及 `git diff --check` 通过。当前机器没有 Docker，未执行 Linux 镜像构建、容器启动或下载速度测量，这些仍归 D07。

### 2026-09-24 D12 功能验证配置收敛

- 审查 `.env`、Compose、启动校验、门户、认证、VoiceChat 探针及完整调用链；保留 VoiceGateway → SessionCoordinator → BusinessRuntime → ToolRegistry → CueKB、PostgreSQL/Redis、revision/epoch、单写入器和 pending call 结清语义。
- `.env.example` 收敛为 15 个实际连接/部署值；固定模式放入 Compose，API 不再映射宿主端口。删除多账号 JSON、密码哈希、Cookie/JWT 登录、语音 verified/capability 开关、VoiceChat health/revision/digest 和 CueKB revision 等运行配置。版本与镜像 digest 改由探针参数写入证据报告。
- 门户无需登录，服务端统一注入 customer 验证身份、tenant 和 KB 范围；管理 API 仍拒绝访问。正式多客户认证、独立 API 客户端与系统间鉴权明确留待核心端到端验证后设计。
- 本轮 `uv run --locked pytest -q` 73 passed（1 条 Starlette/AnyIO 上游弃用警告）；Ruff、6 项前端音频单测、TypeScript/Vite 构建、契约导出、部署脚本语法和 `git diff --check` 通过。未运行 Docker、真实 PostgreSQL/Redis、CueKB、VoiceChat、文本模型、HTTPS 浏览器或人工听音；D07 仍待执行。

### 2026-09-23 D11 当前文档与发布整理

- 核对现有 Web/Nginx、API 路由与 Bearer 鉴权、Compose 端口和部署模板；明确浏览器直连 Web 容器 HTTPS、同源 `/api/` 在 Web 容器内转发，以及私网客户端直连 API 的当前权限和系统间鉴权缺口。
- 将入口文档、主题设计、任务板和本验收记录与 D09/D10 对齐；旧日期的验证和配置保留为历史，不作为当前部署方式。
- 本轮 `uv run --locked pytest -q` 75 passed（1 条 Starlette/AnyIO 上游弃用警告）；Ruff、6 项前端音频单测、TypeScript/Vite 构建、部署脚本语法、`git diff --check` 通过。9 份活动 Markdown 的 72 处本地链接及章节锚点通过检查。真实 Docker、证书、网络、数据库和供应商验收仍属于 D07；GitHub `main` 推送状态以仓库记录为准。

### 2026-09-23 D10 镜像与公网 HTTPS / 私网 API 端口

- 生产 Compose 固定 PostgreSQL `17.6-alpine`、Redis `7-alpine` 的官方 digest；Web 容器直接在 `8087` 提供 HTTPS 并映射公网 `8087`，API 容器 `8000` 映射到部署机指定私网 IPv4 的 `8088`。数据库和 Redis 仍仅在容器网络中。
- Web TLS 证书和私钥以只读文件挂载，部署脚本校验路径和私网绑定地址；`PUBLIC_ORIGIN` 应为浏览器实际使用的 HTTPS origin。真实证书、网络隔离、镜像兼容和 Docker 健康仍待 D07 验收。
- 本轮 `uv run --locked pytest -q` 75 passed（1 条 Starlette/AnyIO 上游弃用警告）；Ruff、部署脚本语法和 `git diff --check` 通过。新增受控部署脚本测试验证公网 API 绑定与缺失 TLS 文件会在启动前被拒绝；未运行 Docker/Nginx 或真实网络检查。

### 2026-09-23 D09 单一配置与受限测试认证

- 移除运行环境分类和 OIDC 实现；只保留 `.env.example` 与生产 Compose。实际 API 启动执行严格配置校验，自动化测试通过显式注入 fixture 设置验证处理逻辑。API/Web 仅绑定部署机 `127.0.0.1`；远程测试仍需访问受限的 HTTPS 入口。
- 本地测试账号通过 PBKDF2 哈希校验登录，签发短期签名 Cookie/Bearer；身份、角色、租户和 KB 范围由服务端账号配置确定。受控 HTTP 测试覆盖错误密码、Cookie/Bearer、客户不可访问管理 API、两个账号会话隔离、过期和密码哈希变更失效。
- 本轮 `uv run --locked pytest -q` 74 passed（1 条 Starlette/AnyIO 上游弃用警告）；Ruff、6 项前端音频测试、TypeScript/Vite 构建、契约导出、部署脚本语法及 `git diff --check` 通过。未运行 Docker/Nginx、真实 PostgreSQL/Redis、CueKB、VoiceChat、文本模型、真实 HTTPS 门户或声学验收；D07 仍待执行。

### 2026-09-22 E03 英文知识客服适配

- 新会话和配置默认 `en-US`，新会话拒绝其它 locale；旧会话保留原值，非英文旧会话不能开启 VoiceChat。`/capabilities` 的英文语言声明只在既有真实集成验证标志成立时才列为已验证。
- VoiceChat 提示词、工具说明、ACK、业务口述及云端探针固定结果改为英文；适配器检查 session 配置和解码后的工具结果为 ASCII，非 ASCII 口述改为引导查看门户文字答案，避免只用 JSON 转义掩盖语义字符。门户、浏览器语音错误和服务端客户可见消息改为英文，默认仅启用知识工具；天气实现留存但本期不启用。
- 新增 6 条英文知识/澄清/寒暄文本样本，均为作者编写的合成文本，`audio_path`/`result` 为空；只验证桥接参数、配置和 ASCII 契约，不代表真实音频、工具质量或口述质量。
- 本地 `pytest` 71 passed、Ruff、6 项音频单测、TypeScript/Vite 构建、协议/schema 导出和差异检查通过。未运行云端 Docker、真实 PostgreSQL/Redis/OIDC/CueKB/VoiceChat 或实际浏览器/声学验收。

### 2026-09-22 E01–E02 CueKB M3 接入

- 核对本地 CueKB `1b9379d` 的 `SearchRequest`、`SearchHit`、关系与上下文实现；严格模型新增 `context_parts`、`context_truncated`、`relations`，保留逐块位置、关系条件及 supports/refutes 立场。原有模型会拒绝这些新增字段，已用受控夹具复现并修复。
- `search_knowledge` 支持明确给出的 `product_model`、`software_version`；KB UUID 仍由服务端身份注入。CueKB HTTP 响应限制 256 KiB，工具输出低于 32 KiB，证据正文及上下文限制 6000 字符；应用裁剪以 `context_omitted`、`hits_omitted` 显示。门户能展开逐块来源和截断提示；旧历史引用读取提供默认值。
- 本期默认仅启用 `search_knowledge`；此节记录的是 E01–E02 当时状态。英文语音提示词、门户文案、默认语言随后由 E03 完成代码适配，实际口述仍待 D07 云端 Docker 验收。
- 本次 `uv run --locked pytest -q`：67 passed；`uv run --locked ruff check apps/api tests scripts`、`npm test --prefix apps/web`（6 passed）、`npm run build --prefix apps/web`、契约导出及 `git diff --check` 通过。仅受控夹具和本地静态验证；未调用真实 CueKB、VoiceChat、文本模型、OIDC 或运行 Docker。

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

- 真实 VoiceChat 英文语音、工具往返、工具等待时的新问题回答、pending call 结清和实际口述准确性；E03 仅完成本地代码与契约验证。中文不属于本期验收范围。
- CueKB 原生 `/v1/search` 的真实服务连接、受限 Key/ACL 和真实知识正确性；本地仅使用符合当前 CueKB schema 的受控响应与显式 mock。
- 独立 call capability 的真实多人隔离、CueKB 范围、管理 API 拒绝和真实文本模型。正式多客户登录、账号恢复和撤权生命周期不在本期；天气/股票不在本期验收范围。
- 真实 PostgreSQL/Redis/Docker、负载均衡故障、容量及端到端性能。
- 任务 revision、停止播报和安静边界轮换尚未在真实 VoiceChat 上验证；特别是工具阶段改问、pending call 安全结清和实际音频抑制仍受 D01 能力门槛约束。

这些是已记录验证范围，不表示本轮检查了当前机器是否安装了相应运行环境。

## 4. 最终方案验收清单

下表为待执行目标，均不能标记 passed。范围选择以部署启用能力为准。

| ID | 场景 | 完成依据 |
| --- | --- | --- |
| V01 | 简单 HTML 门户 + 标准消息 | 独立客户端创建会话、收发音频/字幕/答案，窄屏可用；不调用供应商私网 API |
| V02 | 独立 call capability 与知识权限 | 两个标签页 token/conversation 交叉访问失败；结束后 token 失效；KB 只能来自服务端；call capability 不能管理工具 |
| V03 | 真实 CueKB 知识闭环 | 英文语音→原生工具→授权检索→有依据回答→实际口述；保留版本、定位及 trace |
| V04 | 无依据、降级、冲突与故障 | 空命中、unassessed、degraded、版本冲突、401/403/422/429/5xx/超时分别正确处理；无假数据回退 |
| V05 | 工具等待时的交互 | 延迟工具 5 秒，等待中测试新问题、改问、取消、附和；记录是否在返回前回答新问题，ACK 不算 |
| V06 | 结果/改问/取消竞态 | 旧 revision 结果不进历史/播报；pending call 安全结束或连接关闭；新调用继续；不复用旧 call_id |
| V07 | 连续音频和播放 | 实际麦克风/扬声器，静音、背压、帧序号、慢网、后台/休眠、清音及晚到帧；转写先后不重复执行业务 |
| V08 | 长会话和恢复 | 超两分钟、多次轮换/断网，明确恢复损失；不重播旧录音；历史、字幕、播放估计分离 |
| V09 | 口述与事实一致 | 产品型号、版本、金额/数字、否定条件、引用；不能以正确文字卡片掩盖错误音频或绕过工具 |
| V10 | CueKB-only 与第三方扩展 | 无天气配置可启动知识服务；未启用工具不可调用；实际启用第三方时单独验收事实、时效和权限 |
| V11 | 数据库、租约、重启、部署 | 真实 PostgreSQL 迁移/行锁、Redis 失效、粘性路由、drain、备份/恢复和 Compose 健康 |
| V12 | 预算、隔离和安全 | 超时/有限重试、提示注入、输入输出上限、整轮/工具预算、敏感数据脱敏；并发容量实测。子任务预算仅在未来引入子任务后验收 |

## 5. 测量与放行

记录服务/API revision、镜像 digest、硬件和并发、样本来源/语言/数量、成功失败分母、事件时间线及授权音频证据。区分上行延迟、工具耗时、等待提示首音频、有效答案首音频和停止播放延迟；项目 SLA 阈值需在真实服务基线测量后确定。

历史 `tests/fixtures/voice-evaluation-cases.jsonl` 有 100 条合成文本（40 天气/30 知识/20 混合/10 澄清打断），不再作为本期英文知识客服样本。E03 新增 `tests/fixtures/english-knowledge-cases.jsonl`，目前仅有 6 条合成英文文本且没有录音或真实执行。D07 须在云端 Docker 环境扩充授权英文录音、真实知识和故障样本；评分脚本只统计实际观测且标记 real_service 的项目。

基础语音客服放行要求核心知识、权限、恢复及部署验收通过。V05 的连续交谈门槛未通过时，保留明确的硬打断恢复模式，不能宣称完整工具阶段全双工。生产能力开关只根据真实报告设置。

### 放行记录填写规则

逐项记录 V ID、case ID、应用/服务版本、执行环境、日期、状态（通过/失败/未执行/不适用）、证据位置与复核人；不适用须注明范围依据。当前表是待执行清单，不包含真实通过记录。

基础模式须完成 V01–V04、V06–V09、V11–V12 及 V10 的 CueKB-only 部分；V05 必须执行并记录能力结论，其中工具等待期间自由连续交谈只作为 enhanced 放行门槛。天气扩展与未引入的子任务可记不适用；取消、旧结果隔离和连接恢复不能豁免。

权限越界、旧结果泄漏、伪造知识或关键口述事实错误属于阻断项。质量/时延的数量阈值在基线后按业务目标冻结，记录预期样本总量及每项已观测分母，再以独立样本复测；阈值未确定或关键场景缺测时不宣称生产验收完成。现有评分脚本只做局部指标汇总，完整性增强见架构 Q03。
