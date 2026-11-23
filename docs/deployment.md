# 容器制作与部署手册

类型：与仓库 Compose/启动脚本同步的容器制作和部署方案（2026-11-23）。产品同时提供天气、通用知识、专业知识库，通用知识受控兜底。需求见 [delivery.md](delivery.md)，未实施的验收容器和现场放行见 [implementation-plan.md](implementation-plan.md)，验证与结果只在 [validation.md](validation.md)。

## 1. 部署范围与当前门禁

| 组成 | 当前交付 | 数据 / 网络 |
| --- | --- | --- |
| Web | deploy/Dockerfile.web；React + Nginx TLS/CSP/WS/SSE | 门户 HTTPS 8087、LiveKit WSS/API TLS 7880，共用只读证书 |
| API | deploy/Dockerfile.api；FastAPI、Router、Runtime、内置 LiveKit AgentSession | 8000 仅容器网络；非 root、只读文件系统、临时 /tmp |
| migrate | 复用 API 镜像执行 Alembic | 当前 head=0011，新增 voice_profiles |
| PostgreSQL / Redis | deploy/compose.production.yaml，镜像 digest 见 deploy/images.lock.json | 独立持久卷，宿主不暴露数据库端口 |
| LiveKit | 与 Redis 同一 deploy/compose.production.yaml，镜像 tag+digest 锁定 | 复用 redis:6379/DB1，API 用 DB0；媒体 7881/TCP、7882/UDP 直接发布 |
| Sophon / CueKB | 独立部署，应用通过服务接口连接 | 真实凭据留服务端；GPU/ASR 归独立 Sophon |
| Qwen 意图解析 | API 共享 HTTP client 连接用户配置的云端模型 | 只接收已委派的非敏感 task；不新增容器或 Agent Runtime |
| Sophon 验收容器 | 尚未交付；API 镜像没有浏览器验收运行时 | 设计在实施方案，不能用宿主开发测试冒充生产容器验收 |

六个服务由同一份 Compose 定义：postgres、redis、livekit、migrate、api、web。API/Web 只使用预构建镜像；LiveKit 与数据库一样按锁定 digest 拉取，不另起独立 Compose。VOICE_PROVIDER 是唯一选择：sophon_live（默认）或 nvidia，每次只启用一个后端。主部署默认只配置 Sophon 和三类工具，不再应用/覆盖 NVIDIA 工具目录。本期仅验证 Sophon。

仓库部署定义已更新，不等于部署机已升级。真实镜像构建、LiveKit 公网媒体/NAT、PostgreSQL 迁移、三类服务闭环和听音仍需现场验收；Sophon 浏览器验收容器入口尚未交付，不能用占位凭据或部分工具放行。

单机首版一个 API 容器、一个 Uvicorn worker；AgentSession 已在 API 内。多副本前必须提供 HTTPS/SSE/WSS（含 ticket 申请）的粘性路由，不能直接增加 workers。validation 身份仍按临时通话隔离，不等于正式账号服务。

## 2. 镜像制作与发布包

在仓库根目录、干净的目标提交上构建；API/Web 共用唯一版本标签。以下 linux/amd64 是目标示例，须与部署主机一致。Dockerfile 按固定 requirements/package-lock 安装，基础镜像使用已锁 digest，不自动升级上游 HEAD。

```bash
release_tag=$(git rev-parse --short=12 HEAD)
docker buildx build --platform linux/amd64 --load \
  -f deploy/Dockerfile.api -t "voice-service-agent-api:$release_tag" .
docker buildx build --platform linux/amd64 --load \
  -f deploy/Dockerfile.web -t "voice-service-agent-web:$release_tag" .
docker save -o "voice-service-agent-$release_tag.tar" \
  "voice-service-agent-api:$release_tag" "voice-service-agent-web:$release_tag"
```

当前 API Dockerfile 引用的 artifacts/native-cuekb/cases.json 和 WAV 是已跟踪固定测试夹具，普通 Git 克隆可取得；现场录音、报告、.env、密钥、数据库不加入镜像/仓库。发布包同时提供相同提交的 Compose/部署脚本/模板、commit、架构、镜像 ID/digest 与包 SHA256；不要只交一个没有版本对应关系的 tar。

构建使用 BuildKit，复用同一 builder 缓存；业务源码变化应复用依赖安装层。不要日常 --no-cache 或 prune。需要代理 CA 时，两次构建分别增加 `--secret id=proxy_ca,src=/absolute/path/to/ca-bundle.crt`，pip/npm 使用临时 secret，保持 TLS 校验；不要把 CA 密钥或代理凭据写进镜像。

## 3. 完整三类查询配置

从 .env.example 复制并填写三类交付配置。IMAGE_TAG 与两镜像标签相同，PUBLIC_ORIGIN 必须与浏览器实际 HTTPS 域名/端口一致，证书/私钥使用部署机上的可读绝对路径。PG/Redis 密码使用 URL-safe 随机值；LiveKit Key/Secret 仅用 A–Z、a–z、0–9、点、下划线、波浪线或连字符，Secret 至少 32 字符。不要在值外加 shell 引号。

### 启用 LiveKit 音频和三类工具

以下是完整发布目标；所有“实际”值必须来自已部署服务，未完成接入不能凭占位值启动或宣称交付：

```dotenv
VOICE_PROVIDER=sophon_live
AGENT_PROVIDER=disabled
QA_EXECUTION_MODE=dual_tools
QA_ANSWER_POLICY=general_qa
ENABLED_TOOLS=search_knowledge,weather,web_search
SOPHON_LIVE_WS_URL=wss://实际Sophon主机/v1/live/sessions
SOPHON_LIVE_API_KEY=
SOPHON_LIVE_LOCALE=zh-CN
SOPHON_LIVE_TOOL_TIMEOUT_SECONDS=12
SOPHON_LIVE_MAX_DELEGATIONS_PER_INPUT=1
DELEGATION_ROUTER_PROVIDER=openai_compatible
DELEGATION_ROUTER_BASE_URL=https://实际工作空间域名/compatible-mode/v1
DELEGATION_ROUTER_MODEL=qwen3.7-flash
DELEGATION_ROUTER_API_KEY=实际意图模型Key
DELEGATION_ROUTER_TIMEOUT_SECONDS=4
LIVEKIT_URL=wss://实际门户域名:7880
LIVEKIT_NODE_IP=实际部署机可达IPv4
LIVEKIT_API_KEY=实际Key
LIVEKIT_API_SECRET=实际至少32字符的随机Secret
CUEKB_BASE_URL=https://实际CueKB主机
CUEKB_API_KEY=实际scopedKey
KNOWLEDGE_BASE_IDS=实际授权KB的UUID
WEATHER_PROVIDER=qweather
WEATHER_MODE=real
WEATHER_BASE_URL=https://控制台分配的专属API主机
WEATHER_API_KEY=实际QWeatherKey
WEB_SEARCH_API_KEY=实际TavilyKey
```

Sophon 使用完整 Live sessions endpoint；匿名服务允许空 Key，有 Key 必须 WSS。LIVEKIT_URL 指向本 Compose 的 Web TLS 7880，不含 API 路径；API 与浏览器均须能解析并访问，包括同机回环/NAT。通常与门户使用相同域名；另用域名时证书必须覆盖两个名字。

LIVEKIT_NODE_IP 是浏览器与 API 可到达的部署机 IPv4，不是容器的 172.x 地址；跨公网填实际公网 IPv4。NAT 必须把 7881/TCP、7882/UDP 原端口映射到该主机。LiveKit 内部信令 7880 不直接发布，Web 负责 TLS；媒体不经过 Nginx HTTP 代理。防火墙/安全组开放 8087/TCP、7880/TCP、7881/TCP、7882/UDP。此版本未配置 TURN；仅放行 443 的网络需后续提供经验证的 TURN/TLS，不能宣称所有网络可用。字段与端口依据 [锁定版本配置](https://github.com/livekit/livekit/blob/v1.13.11/config-sample.yaml) 和 [官方端口说明](https://docs.livekit.io/transport/self-hosting/ports-firewall/)。

QWeather 必须使用控制台专属 API Host，服务端通过 X-QW-Api-Key 调用 GeoAPI / Weather v1；Tavily 使用服务端 Key 调用固定 Search endpoint。CueKB 基地址不附加 evidence 路径，Key 只授予配置的 KB 范围；HTTP 仅用于受控隔离内网，公网必须 HTTPS/WSS。配置来源以 config.py / config/tools.yaml 为准，不能从 Key 或门户域名猜主机。

DELEGATION_ROUTER_* 与 AGENT_* 分开：前者只解析原生 task，后者仍 disabled，不增加答案模型。使用受信 HTTPS Chat Completions base URL，代码追加 /chat/completions；本次百炼接口使用 JSON Schema、enable_thinking=false 和有界期限。依据 [百炼 Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。Key 只在服务端 .env，不能传浏览器；API 错误/超时不回退规则执行，不把接口连通当语音工具已成功。模型配置变化须重建 API 并新建通话。

### 单选后端与切换

.env.example 只启用 Sophon 配置，VOICECHAT_* 为注释。需要选择 NVIDIA 时，将 VOICE_PROVIDER 改为 nvidia，并填写 VOICECHAT_WS_URL / VOICECHAT_API_KEY；Sophon、LiveKit、DELEGATION_ROUTER_* 可以移除或保留为未启用字段，均不激活该后端。三类工具及其服务配置仍保留，不能把兼容能力限制改成单知识库产品。NVIDIA 现有路径未实现天气/Tavily 闭环，配置可启动不代表三类能力验收，详见 [架构](architecture.md#2-数据流与代码入口)。本期不运行其服务/GPU 验证。

统一执行 §4 启动脚本，脚本覆盖外部 COMPOSE_PROFILES：Sophon 派生 sophon profile 并等待 LiveKit 健康；NVIDIA 派生空 profile、停止本 Compose 的旧 LiveKit，仅启动 PostgreSQL/Redis，再迁移并重建应用。不要并行启动两套后端、另设独立后端开关或只 restart。切换前 drain，切换后新建通话。Web 在 NVIDIA 选择下拒绝 7880 信令请求；不会因未启动 LiveKit 的 DNS 阻止门户启动。

## 4. 迁移、目录与启动

部署机使用与镜像匹配的仓库发布目录和真实 .env；宿主只需 Docker Engine/Compose v2，应用依赖在镜像内。下列 release_tag 取制作包记录的同一值。

```bash
docker load -i "voice-service-agent-$release_tag.tar"
chmod 600 .env
export DEPLOY_ENV_FILE="$PWD/.env"
# 以下手工命令用于默认 Sophon；部署脚本会按 VOICE_PROVIDER 自动设置。
export COMPOSE_PROFILES=sophon
dc() { docker compose --env-file "$DEPLOY_ENV_FILE" -f deploy/compose.production.yaml "$@"; }
dc config --quiet
dc pull postgres redis livekit
```

已有环境先按 §6 drain/备份。当前启动入口为：

```bash
./scripts/deploy-cloud.sh .env
```

默认 Sophon 下，它依次检查三类配置、Sophon/LiveKit/意图模型配置、IPv4/凭据、HTTPS/TLS/本地应用镜像，等待 PG/Redis/LiveKit 健康，复用新 API 镜像执行 migrate，再启动 API、检查 readiness、重新创建 Web。另一选择仅改变 §3 说明的后端依赖，不启动 Sophon SFU。Compose 固定 AUTH_MODE=validation、CUEKB_MODE=real，数据库连接串由容器配置构造。离线主机须预先载入 images.lock.json 中选用的依赖镜像；拉取失败停止发布，不改用 latest。

Sophon client delegate 不使用 NVIDIA 原生函数清单作为三类能力门禁；部署脚本不再调用 configure_native_cuekb.py --apply 或 --expect-cuekb-native。既有全局目录仍以数据库 revision/CAS 为准，手工维护规则见 [工具契约](interfaces/voice-tools.md)。readiness 与实际业务验收分开，脚本退出 0 不表示三类检索和语音已经通过。

正常启动预期：postgres/redis/livekit/api/web healthy，migrate exit=0、head=0011。角色配置存于 PostgreSQL，默认闹闹是服务端配置；仅新语音会话读取新快照，删除不破坏旧快照。生产不得依赖 auto_create_schema。

## 5. 运行检查与容量

```bash
dc ps
dc exec -T api alembic -c apps/api/alembic.ini current
dc exec -T api python /app/scripts/verify_deployment.py \
  --base-url http://127.0.0.1:8000 --expect-autonomous-voice
dc logs --tail=100 api web livekit migrate
```

/health/live 仅表示应用存活，/health/ready 检查数据库、协调/drain、配置和容量；text_configured=false 在 AGENT_PROVIDER=disabled 时正常。上述检查不是 QWeather/Tavily/CueKB 实际检索、原生触发、GPU 或听音通过，业务操作和回传只在 [统一验证手册](validation.md)。

API 加入 app/data/egress 网络，PG/Redis 无宿主映射，LiveKit 加入 data/app/edge，Web 加入 app/edge。Web TLS 检查须用真实域名/证书链；容器本地 wget 的忽略 IP 证书校验只用于门户内部健康探针。配置、镜像、LiveKit IP/凭据/CSP 变化须重建对应容器并重建 Web，不能仅 restart；角色数据库编辑不热改活动会话。Redis DB0/DB1 区分状态键，不是两个独立 Redis 实例。

MAX_VOICE_SESSIONS / MAX_AGENT_RUNS 是每副本容量，须按供应商总配额分配。Redis conversation 租约默认 15 秒 TTL、4 秒续约，空闲 60 秒可释放；新 owner 取得租约后经数据库 epoch fence 隔离旧任务。真实分区/行锁/粘性切换尚未验收，不声明 HA 已通过。

## 6. 升级、备份与回滚

1. 保存旧 API/Web、LiveKit 与独立语音服务的匹配标签/digest、配置和挂载 hash。使用已有管理员 POST /api/v1/admin/drain，停止新业务并等待活动通话结束；管理员 Key 未配置时该接口不可用，须先维护入口并安排窗口。
2. 停止将升级的 API/Web 后备份数据库；不删除数据卷。当前 stop grace=30 秒、Uvicorn graceful=20 秒，到期连接关闭，客户端需显式重开，不自动重放未知结果。

```bash
dc stop web api
umask 077
mkdir -p backups
dc exec -T postgres pg_dump -U voice_service -d voice_service -Fc \
  > "backups/pre-$release_tag.dump"
```

3. 确认备份有效，再用新 API 镜像执行 Alembic upgrade head。0011 只新增 voice_profiles；0010 是已有全局工具目录，不能误认为当前 head。推送代码不会修改部署数据库。
4. 迁移失败停止发布，不启动新 API；启动失败恢复已核对兼容性的旧 API/Web/LiveKit 镜像与配置组合。升级到本拓扑前须备份旧 Compose/代理配置及 LiveKit 来源，不能只退 Web 镜像却保留不匹配的端口/凭据。优先保留新增表做应用回滚；不要自动 downgrade 0011 删除角色，0010/0009 downgrade 也有配置/审计数据损失，需要时用已验证备份恢复。
5. 新版本重新创建容器并执行 §5 与 validation.md 的对应环境验收。NVIDIA 外部文件/镜像/GPU/挂载变更仍遵守 [VoiceChat 专用运行手册](voicechat/docker-run.md)，本次不重建或升级该外部服务。
