# 部署与运行

> 2026-09-16：本文描述当前代码的运行方式，不代表真实服务已经验收。生产依 `ENABLED_TOOLS` 仅校验启用工具；默认生产模板为 CueKB-only，不需要天气代理配置。不要为满足启动条件接入假天气；真实验收仍须按 [任务板](TASK_BOARD.md) D07 执行。最终系统边界见 [架构](architecture.md)。

## 部署方式边界

编码阶段没有 Docker 环境。必要时只静态检查 Dockerfile、Compose、镜像制作和启动脚本；不安装 Docker、不搭建替代容器环境、不拉取或构建镜像、不启动容器。本文中的容器构建和运行命令用于后续具备环境的部署阶段，不属于当前编码验证步骤。CueKB/VoiceChat 真实联调同样在后续部署阶段执行。

生产 API/Web 镜像先用 `docker build` 单独构建；云端服务器使用 Docker Compose 执行迁移和运行，不直接在宿主机启动 Python、Node.js、PostgreSQL 或 Redis。生产入口为 `deploy/compose.production.yaml`，配置模板为 `.env.production.example`，部署命令为 `scripts/deploy-cloud.sh`。

本地开发仍使用 README 中的 Python/Node.js 启动和验证命令；需要检查容器拓扑时也可继续使用 `deploy/compose.yaml`。两种路径互不替代，本地测试通过不代表云端容器验收通过。

## 本地容器拓扑

Docker/Compose 需要由运行环境提供，本次机器未安装 Docker，没有声称容器或 PostgreSQL/Redis 已实测。

1. 复制 `.env.example` 为 `.env`，设置环境及服务配置。
2. 设置 `POSTGRES_PASSWORD`（建议随机字母数字，URL 特殊字符需编码），运行：

```sh
POSTGRES_PASSWORD=YOUR_LOCAL_DB_PASSWORD docker compose --env-file .env -f deploy/compose.yaml up --build
```

Compose 先等 PostgreSQL/Redis 健康，再由单独 migrate job 执行 Alembic，最后启应用与 Nginx。默认门户为 `http://localhost:8080`，只绑定宿主 `127.0.0.1`。数据库和 Redis 不向宿主公开端口。不要用此开发 HTTP 入口直接对外服务。

API 无 GPU/CUDA 依赖，VoiceChat 为外部服务。Python/Node/Nginx/PostgreSQL/Redis 基础镜像的实际 registry manifest digest 已锁在 `deploy/images.lock.json` 和 Dockerfile/Compose；Python/Node 全量依赖各有 lockfile。尚未在 Docker 中拉取和构建这些镜像，锁定 digest 不等于镜像运行验收。

## 生产

在构建机的仓库根目录，给同一次发布的 API 和 Web 镜像打唯一版本标签。构建不读取 `.env.production`：

```sh
release_tag=$(git rev-parse --short=12 HEAD)
docker build -f deploy/Dockerfile.api -t "voice-service-agent-api:$release_tag" .
docker build -f deploy/Dockerfile.web -t "voice-service-agent-web:$release_tag" .
```

若在另一台服务器部署，先经镜像仓库或 `docker save`/`docker load` 将**同一版本**的两个镜像送到部署机，确认部署机本地有这两个标签。基础镜像 PostgreSQL/Redis 仍按 Compose 的固定 digest 获取。部署机安装 Docker Engine 与 Docker Compose v2，将代码和镜像放到固定发布目录。复制配置并仅授予部署账号读取真实配置的权限：

```sh
cp .env.production.example .env.production
# 编辑 .env.production：设置 API_IMAGE/WEB_IMAGE 为上述标签，替换其余示例值
chmod 600 .env.production
./scripts/deploy-cloud.sh .env.production
```

部署脚本会在发现示例值、非 production 模式、缺少浏览器 SSO 地址或非 HTTPS `PUBLIC_ORIGIN` 时停止；校验 Compose，并在启动服务前检查本地 `API_IMAGE`/`WEB_IMAGE`。生产 Compose 没有 `build`，也不会自动拉取这两个应用镜像。随后等待 PostgreSQL/Redis 健康、用与 API 相同的镜像单独执行 Alembic、启动 API，在容器内执行 `/health/ready` 的部署配置核验，最后启动 Web。该核验会拒绝 mock 或与 `ENABLED_TOOLS` 不一致的 API；它仅证明容器就绪，不能替代 CueKB、VoiceChat、SSO 或口述答案验收。真实 `.env.production` 被 Git 和 Docker build context 排除。

### URL 配置对照

| 配置 | 从哪里取得、填写什么 | 使用位置 |
| --- | --- | --- |
| `PUBLIC_ORIGIN` | 客户在浏览器中访问门户的**唯一 HTTPS origin**，本部署为 `https://th.ppy123.xyz`；不含路径和结尾 `/` | 同源校验、Cookie、登录回调 `PUBLIC_ORIGIN/api/v1/auth/callback`；与容器地址无关 |
| `WEB_BIND_ADDRESS` / `WEB_PORT` | Web 在部署机上的 HTTP 监听地址/端口；同机 TLS 反向代理时为 `127.0.0.1:8082` | 只影响宿主端口映射，不填 `https://...`；外部客户仍访问 `PUBLIC_ORIGIN` |
| `OIDC_ISSUER` | 身份提供方 OIDC metadata 的 `issuer` **原值**，包括路径及可能的结尾 `/` | 验证 ID token 的 `iss`，不由门户域名推算 |
| `OIDC_JWKS_URL` | 同一 metadata 的 `jwks_uri` | 服务端取公钥验证 JWT；必须能从 API 容器访问 |
| `OIDC_AUTHORIZATION_URL` | 同一 metadata 的 `authorization_endpoint` | 浏览器登录重定向目标 |
| `OIDC_TOKEN_URL` | 同一 metadata 的 `token_endpoint` | API 容器用 authorization code 换 ID token |
| `OIDC_AUDIENCE` | 此门户在身份提供方注册的 OAuth `client_id`；当前代码也要求 ID token `aud` 与它一致 | 登录请求与 token 验证；若 IdP 给 API 使用另一 audience，需先调整身份集成契约 |
| `OIDC_CLIENT_SECRET` | IdP 为该 client 签发的 secret；只有明确允许 public PKCE client 时才留空 | 服务端换 token，属于凭据而非 URL |
| `CUEKB_BASE_URL` | 独立 CueKB 服务实际提供的 HTTPS 基地址；不能用门户地址代替 | API 在其后请求 `/v1/search`；启用 `search_knowledge` 时必填 |
| `AGENT_BASE_URL` | 留空表示使用所选 SDK 的默认文本模型地址；仅在模型供应商提供兼容 API 基地址时填写 | 文本推理模型请求 |
| `WEATHER_BASE_URL` | 当前模板未启用天气，留空；启用 `weather` 后填真实天气适配服务的 HTTPS 基地址 | 天气工具请求 |
| `VOICECHAT_WS_URL` / `VOICECHAT_HEALTH_URL` | 当前 `VOICE_PROVIDER=disabled`，留空；通过真实语音验收后填独立 VoiceChat 提供的 WSS/HTTPS 地址 | 语音连接/健康探测 |

从身份提供方提供的 OIDC discovery 文档逐项复制 `issuer`、`jwks_uri`、`authorization_endpoint`、`token_endpoint`，不要依照门户域名猜路径；不同 IdP 的实际路由可能不同。身份提供方还须登记回调 `https://th.ppy123.xyz/api/v1/auth/callback`。当前服务不会从 `OIDC_ISSUER` 自动发现其余三个端点。应将 `th.ppy123.xyz` 的 DNS 与 HTTPS 入口指向公网 IP `122.51.233.77`；公网 IP 不填入 OIDC/CueKB URL。`TENANT_ID` 和 `KNOWLEDGE_BASE_IDS` 是服务端身份/知识范围，不是 URL；`POSTGRES_PASSWORD`/`REDIS_PASSWORD` 用 URL 安全字符，容器内部连接串由 Compose 生成。

生产 Compose 默认将 Web 映射到云端宿主机 `127.0.0.1:8082`，供同机 HTTPS 反向代理使用。若反向代理在另一台机器上，取得部署机的内网 IP 后设置 `WEB_BIND_ADDRESS`，并仅允许代理访问 8082；不得把此 HTTP 端口无 TLS 地直接暴露到公网。PostgreSQL 和 Redis 只在 Docker 内部网络可见，API 另接 egress 网络访问模型、VoiceChat、CueKB、天气和 OIDC。

- 设置 `APP_ENV=production`、`AUTH_MODE=oidc`、组织/SSO、真实文本模型和 `ENABLED_TOOLS`。CueKB-only 使用 `ENABLED_TOOLS=search_knowledge`；启用天气才加入 `weather` 并提供真实天气配置。CueKB 的 KB 列表必须是 UUID；应用启动会拒绝 mock 和缺失的启用认证/工具配置。
- 设置精确 HTTPS `PUBLIC_ORIGIN`。Nginx 配置是内网入口模板；在前置网关终止 TLS，将 HTTPS/WSS 和 Origin 原样转发。外层代理同样不得记录 ticket、OIDC code/state、Authorization 或 Cookie。
- 内网 DB/Redis 使用部署凭据/网络控制；跨不可信网络时为 DB/Redis 配置 TLS 连接串。Redis 故障会关闭活跃输出，不能退回内存继续假装持有租约。
- Nginx 关闭 SSE buffering、支持 WS upgrade、限制请求大小和请求速率，设置 CSP、同源麦克风权限与 frame 禁止策略。
- 真实语音可先用 `VOICE_PROVIDER=disabled` 关闭并保留真实文字；按验收报告通过基础语音门槛，并配置精确 API 版本、镜像 `sha256` digest、`basic|enhanced` 模式及验证声明后，才切换为 `nvidia`。程序会拒绝缺少这些固定证据的 production NVIDIA 配置，但不会替代人工音频验收。

常用只读运维命令均显式指定生产配置，避免误用本地 Compose：

```sh
docker compose --env-file .env.production -f deploy/compose.production.yaml ps
docker compose --env-file .env.production -f deploy/compose.production.yaml logs --tail=200 api web
```

## 副本、容量与故障恢复

默认每个应用容器一个 Uvicorn worker。多副本部署必须由负载均衡提供浏览器粘性路由，覆盖 HTTP/SSE/WS（包括 ticket 申请及后续握手）；不能简单使用无粘性的多 worker 参数。

Redis 持有每个 conversation 的独占租约，15 秒 TTL、4 秒续约。未触碰的空闲租约 60 秒后释放。新 owner 取得租约后，先在 PostgreSQL 行事务递增 epoch、取消旧轮次，才接受新输出；旧 worker 在接收、提交以及单写入器前检查租约与 epoch。其他 worker 上的请求返回 `SESSION_OWNED_BY_OTHER`，不迁移隐藏音频状态。

`MAX_VOICE_SESSIONS`、`MAX_AGENT_RUNS` 是**每副本**的容量边界，按总供应商配额在各副本之间划分预算；不能每副本都配置全量云端额度。票据预留占用语音容量，60 秒过期释放。取消中的本地业务 task 仍计入任务容量，防止反复替换绕过上限。

已测试租约命令契约与新 owner 的数据库 epoch fence；真实 Redis 租约失效、网络分区、PostgreSQL 行锁与负载均衡粘性切换仍待部署集成验证，不保证生产 HA 已通过。

## 升级、drain、回滚

1. 备份数据库和当前镜像/配置版本，检查 Alembic 迁移。
2. 对将升级的副本调用管理员 `POST /api/v1/admin/drain`。readiness 返回不可用，拒绝新业务/语音；活跃业务在预算内结束，语音在应用会话上限内结束。
3. 停止进程前给活跃会话留出窗口。SIGTERM 的 Uvicorn graceful timeout 为 20 秒，容器 stop grace 30 秒；到期关闭连接，客户端需重新开始，不自动重放录音。
4. 单独执行 `alembic upgrade head`，再启动新副本。启动不替代迁移。
5. 回滚优先恢复兼容旧应用镜像；迁移 0001–0004 都是新增结构或字段。`downgrade` 会删除对应表/字段，不应作为无损回滚手段；需要破坏式数据库回滚时使用已验证备份恢复流程。

健康接口：`/health/live` 为应用存活；`/health/ready` 检查数据库、归属协调和 drain，分别返回文字配置、语音配置和本地容量。管理页健康端点探测只说明可达，不冒充真实推理/工具调用成功。
