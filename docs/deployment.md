# 容器制作与部署手册

类型：当前可用操作与明确的发布限制。产品同时提供天气、通用知识、专业知识库，通用知识受控兜底；不存在仅启用 search_knowledge 的产品场景。需求见 [delivery.md](delivery.md)，未实施的容器验收/部署检查改造见 [implementation-plan.md](implementation-plan.md)，所有业务验证与结果只在 [validation.md](validation.md)。

## 1. 部署范围与当前门禁

| 组成 | 当前交付 | 数据 / 网络 |
| --- | --- | --- |
| Web | deploy/Dockerfile.web；React 构建 + Nginx TLS/CSP/WS/SSE | 对外 HTTPS 8087，证书只读挂载 |
| API | deploy/Dockerfile.api；FastAPI、Router、Runtime、内置 LiveKit AgentSession | 8000 仅容器网络；非 root、只读文件系统、临时 /tmp |
| migrate | 复用 API 镜像执行 Alembic | 当前 head=0011，新增 voice_profiles |
| PostgreSQL / Redis | deploy/compose.production.yaml，镜像 digest 见 deploy/images.lock.json | 独立持久卷，宿主不暴露数据库端口 |
| LiveKit / Sophon / CueKB | 独立部署，应用通过服务接口连接 | 真实凭据留服务端；GPU/ASR 归独立 Sophon |
| Sophon 验收容器 | 尚未交付；API 镜像没有浏览器验收运行时 | 设计在实施方案，不能用宿主开发测试冒充生产容器验收 |

当前 API/Web Dockerfile 和 Compose 可用于镜像制作与配置完整后的启动，Compose 不构建或自动拉取应用镜像。默认 Provider、ENABLED_TOOLS、configure_native_cuekb 和 --expect-cuekb-native 检查仍有旧 NVIDIA 假设；这些代码事实不是产品模式，不能当三类查询放行门禁。已自定义全局目录时须先审查部署脚本的覆盖动作。Sophon 生产容器验收入口、真实 CueKB 和完整三类闭环未完成，缺项不得以裁剪工具或填假 KB/凭据放行。

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

从 .env.example 复制并改为三类交付配置；模板的旧默认不等于目标范围。IMAGE_TAG 与两镜像标签相同，PUBLIC_ORIGIN 必须与浏览器实际 HTTPS 域名/端口一致，证书/私钥使用部署机上的可读绝对路径。PG/Redis 密码用 URL-safe 字符，不把 @、冒号、空格等未经编码拼入连接 URL。

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
LIVEKIT_URL=wss://实际LiveKit域名
LIVEKIT_API_KEY=实际Key
LIVEKIT_API_SECRET=实际Secret
CUEKB_BASE_URL=https://实际CueKB主机
CUEKB_API_KEY=实际scopedKey
KNOWLEDGE_BASE_IDS=实际授权KB的UUID
WEATHER_PROVIDER=qweather
WEATHER_MODE=real
WEATHER_BASE_URL=https://控制台分配的专属API主机
WEATHER_API_KEY=实际QWeatherKey
WEB_SEARCH_API_KEY=实际TavilyKey
```

Sophon 使用完整 Live sessions endpoint；匿名服务允许空 Key，有 Key 必须 WSS。LIVEKIT_URL 是同一浏览器/API 都能访问的 WSS origin，不含 API 路径。LiveKit 需可信 TLS、正确公网 RTC 地址及 WebRTC 媒体端口；不能只开放信令 HTTPS。独立节点的端口/镜像按已审核配置固定，参考 [LiveKit 自托管](https://docs.livekit.io/transport/self-hosting/)。API/Web Compose 不会自动创建它。

QWeather 必须使用控制台专属 API Host，服务端通过 X-QW-Api-Key 调用 GeoAPI / Weather v1；Tavily 使用服务端 Key 调用固定 Search endpoint。CueKB 基地址不附加 evidence 路径，Key 只授予配置的 KB 范围；HTTP 仅用于受控隔离内网，公网必须 HTTPS/WSS。配置来源以 config.py / config/tools.yaml 为准，不能从 Key 或门户域名猜主机。

NVIDIA/旧 Sophon WS 的实际限制见 [架构](architecture.md#2-数据流与代码入口) / [接入](integration.md)，不另列仅知识库部署方案。不得用它们的局部能力替代三类查询的发布标准。

## 4. 迁移、目录与启动

部署机使用与镜像匹配的仓库发布目录和真实 .env；宿主只需 Docker Engine/Compose v2，应用依赖在镜像内。下列 release_tag 取制作包记录的同一值。

```bash
docker load -i "voice-service-agent-$release_tag.tar"
chmod 600 .env
export DEPLOY_ENV_FILE="$PWD/.env"
dc() { docker compose --env-file "$DEPLOY_ENV_FILE" -f deploy/compose.production.yaml "$@"; }
dc config --quiet
dc pull postgres redis
```

已有环境先按 §6 drain/备份。当前启动入口为：

```bash
./scripts/deploy-cloud.sh .env
```

它依次检查真实配置/HTTPS/TLS/本地镜像，等待 PG/Redis，复用新 API 镜像执行 migrate，应用默认目录，再启动 API、检查 readiness、启动 Web。API 拒绝自动建表、fixture 身份、mock 和缺失依赖；Compose 固定 AUTH_MODE=validation、CUEKB_MODE=real，数据库连接串由容器配置构造。

**目录动作须单独审查**：脚本目前调用 configure_native_cuekb.py --apply，把已有数据库目录切到 config/voice-tools.yaml 的默认知识工具；不等于只部署一个业务能力，也不应无审查覆盖自定义目录。可用同镜像/数据库先预览（省略 --apply 不写入、不运行应用恢复或模型）：

```bash
dc run --rm --no-deps api python /app/scripts/configure_native_cuekb.py
```

该目录已保存时以数据库 revision/CAS 为准，文件修改不自动覆盖。原生入口/后台能力/用户权限的差异见 [工具契约](interfaces/voice-tools.md)。当前脚本使用 --expect-cuekb-native 检查知识目录，完整三类业务检查与 Sophon 容器验收仍按实施方案补齐，不能仅因脚本退出 0 就放行。

正常启动预期：migrate exit=0、head=0011、API/Web healthy。角色配置存于 PostgreSQL，默认闹闹是服务端配置；仅新语音会话读取新快照，删除不破坏旧快照。生产不得依赖 auto_create_schema。

## 5. 运行检查与容量

```bash
dc ps
dc exec -T api alembic -c apps/api/alembic.ini current
dc exec -T api python /app/scripts/verify_deployment.py \
  --base-url http://127.0.0.1:8000 --expect-autonomous-voice
dc logs --tail=100 api web migrate
```

/health/live 仅表示应用存活，/health/ready 检查数据库、协调/drain、配置和容量；text_configured=false 在 AGENT_PROVIDER=disabled 时正常。上述检查不是 QWeather/Tavily/CueKB 实际检索、原生触发、GPU 或听音通过，业务操作和回传只在 [统一验证手册](validation.md)。

API 只加入 app/data/egress 网络，PG/Redis 无宿主映射，Web 暴露 HTTPS 8087。Web TLS 检查须用真实域名/证书链；容器本地 wget 的忽略 IP 证书校验只用于内部健康探针。配置、镜像、LIVEKIT_URL/CSP 变化须重建对应 API/Web 容器，不能仅 restart；角色数据库编辑不热改活动会话。

MAX_VOICE_SESSIONS / MAX_AGENT_RUNS 是每副本容量，须按供应商总配额分配。Redis conversation 租约默认 15 秒 TTL、4 秒续约，空闲 60 秒可释放；新 owner 取得租约后经数据库 epoch fence 隔离旧任务。真实分区/行锁/粘性切换尚未验收，不声明 HA 已通过。

## 6. 升级、备份与回滚

1. 保存旧 API/Web 与独立语音服务的匹配标签/digest、配置和挂载 hash。使用已有管理员 POST /api/v1/admin/drain，停止新业务并等待活动通话结束；管理员 Key 未配置时该接口不可用，须先维护入口并安排窗口。
2. 停止将升级的 API/Web 后备份数据库；不删除数据卷。当前 stop grace=30 秒、Uvicorn graceful=20 秒，到期连接关闭，客户端需显式重开，不自动重放未知结果。

```bash
dc stop web api
umask 077
mkdir -p backups
dc exec -T postgres pg_dump -U voice_service -d voice_service -Fc \
  > "backups/pre-$release_tag.dump"
```

3. 确认备份有效，再用新 API 镜像执行 Alembic upgrade head。0011 只新增 voice_profiles；0010 是已有全局工具目录，不能误认为当前 head。推送代码不会修改部署数据库。
4. 迁移失败停止发布，不启动新 API；启动失败恢复已核对兼容性的旧 API/Web 镜像组合。优先保留新增表做应用回滚；不要自动 downgrade 0011 删除角色，0010/0009 downgrade 也有配置/审计数据损失，需要时用已验证备份恢复。
5. 新版本重新创建容器并执行 §5 与 validation.md 的对应环境验收。NVIDIA 外部文件/镜像/GPU/挂载变更仍遵守 [VoiceChat 专用运行手册](voicechat/docker-run.md)，不因本次文档整理重建或升级它。
