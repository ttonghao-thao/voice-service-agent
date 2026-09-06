# 部署与运行

## 部署方式边界

云端服务器只使用 Docker Compose 构建、迁移和运行，不直接在宿主机启动 Python、Node.js、PostgreSQL 或 Redis。生产入口为 `deploy/compose.production.yaml`，配置模板为 `.env.production.example`，统一命令为 `scripts/deploy-cloud.sh`。

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

在云端服务器安装 Docker Engine 与 Docker Compose v2，将代码放到固定发布目录。复制配置并仅授予部署账号读取真实配置的权限：

```sh
cp .env.production.example .env.production
# 编辑 .env.production，替换所有 REPLACE_* 和 example.com 值
chmod 600 .env.production
./scripts/deploy-cloud.sh .env.production
```

部署脚本会在发现示例值、非 production 模式或非 HTTPS `PUBLIC_ORIGIN` 时停止，然后校验 Compose、构建锁定依赖的镜像、等待 PostgreSQL/Redis 健康、单独执行 Alembic，最后启动 API 与 Web 并等待健康检查。真实 `.env.production` 被 Git 和 Docker build context 排除。

生产 Compose 默认将 Web 映射到云端宿主机 `127.0.0.1:8080`，供同机 HTTPS 反向代理使用。若使用云平台负载均衡访问主机端口，可按网络边界设置 `WEB_BIND_ADDRESS`；不得把此 HTTP 端口无 TLS 地直接暴露到公网。PostgreSQL 和 Redis 只在 Docker 内部网络可见，API 另接 egress 网络访问模型、VoiceChat、RAG、天气和 OIDC。

- 设置 `APP_ENV=production`、`AUTH_MODE=oidc`、组织/SSO、真实文本模型、真实 RAG/天气。应用启动会拒绝 mock 和缺失的主要认证/工具配置。
- 设置精确 HTTPS `PUBLIC_ORIGIN`。Nginx 配置是内网入口模板；在前置网关终止 TLS，将 HTTPS/WSS 和 Origin 原样转发。外层代理同样不得记录 ticket、OIDC code/state、Authorization 或 Cookie。
- 内网 DB/Redis 使用部署凭据/网络控制；跨不可信网络时为 DB/Redis 配置 TLS 连接串。Redis 故障会关闭活跃输出，不能退回内存继续假装持有租约。
- Nginx 关闭 SSE buffering、支持 WS upgrade、限制请求大小和请求速率，设置 CSP、同源麦克风权限与 frame 禁止策略。
- 真实语音可先降级关闭，保留真实文字；P0 验收通过并记录服务/API 版本后才允许开放生产语音。

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
5. 回滚优先恢复兼容旧应用镜像；本期迁移 0001–0003 都是新增结构。`downgrade` 会删除对应表/字段，不应作为无损回滚手段；需要破坏式数据库回滚时使用已验证备份恢复流程。

健康接口：`/health/live` 为应用存活；`/health/ready` 检查数据库、归属协调和 drain，分别返回文字配置、语音配置和本地容量。管理页健康端点探测只说明可达，不冒充真实推理/工具调用成功。
