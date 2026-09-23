# 部署与运行

> 2026-09-23：本文描述当前代码的运行方式，不代表真实服务已经验收。生产依 `ENABLED_TOOLS` 仅校验启用工具；默认模板为 CueKB-only，不需要天气代理配置。真实验收仍须按 [任务板](TASK_BOARD.md) D07 执行。最终系统边界见 [架构](architecture.md)。

## 部署方式边界

本阶段只有一套生产部署配置：`.env.example` 是唯一模板，实际 `.env` 不提交；`deploy/compose.production.yaml` 是唯一 Compose 拓扑。测试是对这套部署的受限访问和授权样本，不另设 integration/test 运行模式。应用代码保留显式注入的 fixture 设置供自动化测试使用；正常 API 启动会执行严格部署校验，拒绝 fixture 身份、mock、自动建表、缺少真实文本模型/CueKB 与未验证的 VoiceChat 声明。

门户只用于测试，可从公网直接访问 `https://<证书覆盖的域名或公网 IP>:8087`。Nginx 已打包在 Web 镜像中，负责提供静态门户和终止 HTTPS，无需独立部署 Nginx 或外层反向代理。它在容器内监听 `0.0.0.0:8087`，Compose 同端口公开映射；填写证书和私钥在宿主机上的绝对路径，容器只读挂载。`PUBLIC_ORIGIN` 必须与浏览器实际使用的 HTTPS origin 完全一致，证书须受测试设备信任；若使用公网 IP，证书须包含该 IP 的 SAN。浏览器麦克风和 Secure 登录 Cookie 依赖可信 HTTPS，不能用 HTTP 地址验收语音。

API 容器内部继续监听 `0.0.0.0:8000`，宿主机仅在 `API_BIND_ADDRESS:8088` 映射；`API_BIND_ADDRESS` 必须填写部署机实际拥有的 RFC 1918 私网 IPv4。私网客户端可通过 `http://<主机私网 IP>:8088/api/v1/...` 调用会话、文字问答和授权管理接口，使用本地测试账号登录返回的短期 Bearer token，并受该账号角色/KB 范围约束。当前没有专门的系统间凭据，也没有公网 API 入口；这不等于正式第三方集成鉴权已经完成。浏览器从 Web 同源 `/api/` 调用，Web 容器内的 Nginx 仅将这一路径转到 Compose `app` 网络的 `api:8000`，不需要浏览器连接私网地址。PostgreSQL、Redis 只在容器网络中。防火墙只允许授权来源访问私网 `8088`；公网只开放 Web 的 `8087`。宿主机私网映射不等于容器内部监听端口。

编码机没有 Docker 或真实 CueKB/VoiceChat 接口；本地只运行契约、静态与夹具测试。镜像/容器、PostgreSQL/Redis、真实供应商和浏览器验收归 D07，不能以本地测试或 `/health/ready` 冒充通过。

## 配置与受限测试身份

复制 `.env.example` 为 `.env`，填写镜像标签、数据库/Redis 凭据、文本模型、CueKB、租户、KB UUID、HTTPS 测试 origin、宿主私网 IP、TLS 证书/私钥路径和测试账号。`AUTH_MODE=local`；不需要 OIDC、`APP_ENV` 或第二份 env 文件。`LOCAL_USERS_JSON` 是单行 JSON 数组，每项包含 `id`、`password_hash`、`role`（`customer|operator|admin`）、`knowledge_base_ids`（UUID 数组）。至少配置两个 customer 测试身份以验收会话隔离；若验收 KB 范围隔离，再配置两个授权范围和对应 KB。仅在测试管理 API 时配置单独 admin。所有角色和 KB 范围均由服务端账号配置确定，不接受浏览器或模型覆盖。

在已安装项目依赖的构建机生成账号密码哈希，命令交互读取密码，不回显原文：

```sh
PYTHONPATH=apps/api uv run python scripts/hash_test_password.py
```

例如账号结构（哈希和 UUID 均须替换为真实值）：

```json
[{"id":"alice","password_hash":"pbkdf2_sha256:...","role":"customer","knowledge_base_ids":["00000000-0000-4000-8000-000000000001"]}]
```

将压缩后的 JSON 写入 `.env` 的 `LOCAL_USERS_JSON='[...]'`（单引号包住整段 JSON）；密码哈希使用冒号分隔，避免 Compose 将 `$` 当作变量插值。门户调用 `POST /api/v1/auth/login` 后使用 Secure、HttpOnly、SameSite=Strict 的 1 小时 Cookie；后台 API 可取同一登录响应的 `access_token` 作为 Bearer token。每次请求按当前服务端账号配置重新确定角色和 KB；修改账号密码哈希并重启 API 后，旧 token 失效。退出门户删除浏览器 Cookie，已签发的 Bearer token 到期或凭据轮换后失效。测试入口必须限制来源和登录尝试速率，不对真实客户开放。

## 镜像构建与部署

API/Web 镜像在构建机独立构建，同一发布使用唯一标签；云端 Compose 只消费预构建镜像，迁移复用 API 镜像：

```sh
release_tag=$(git rev-parse --short=12 HEAD)
docker build -f deploy/Dockerfile.api -t "voice-service-agent-api:$release_tag" .
docker build -f deploy/Dockerfile.web -t "voice-service-agent-web:$release_tag" .
```

把相同标签的镜像和仓库发布目录送到部署机，填写 `.env` 的 `API_IMAGE`/`WEB_IMAGE`，设置仅部署账号可读，然后运行：

```sh
cp .env.example .env
# 填写 .env 中所有 REPLACE_ 值及 LOCAL_USERS_JSON
chmod 600 .env
./scripts/deploy-cloud.sh .env
```

脚本检查必需值、私网地址、TLS 文件、本地镜像和 Compose 配置，等待 PostgreSQL/Redis 健康，用 API 镜像执行 Alembic，再启动 API、核验容器内 `/health/ready`，最后启动 Web。配置和账号错误会在 API 启动时失败；镜像不会由部署脚本构建或自动拉取。readiness 只证明容器和启用工具的配置就绪，不能证明 CueKB、VoiceChat、文字答案或英语口述质量。当前依赖镜像固定为 `postgres:17.6-alpine` 和 `redis:7-alpine` 对应 digest；已有 PostgreSQL 数据卷在更换镜像前须备份并验证目标版本兼容，不把切换标签视为无风险降级。

```sh
docker compose --env-file .env -f deploy/compose.production.yaml ps
docker compose --env-file .env -f deploy/compose.production.yaml logs --tail=200 api web
```

`CUEKB_BASE_URL` 是独立 CueKB HTTPS 基地址，应用附加 `/v1/search`；`AGENT_BASE_URL` 仅用于兼容文本模型端点；`VOICECHAT_WS_URL`/`VOICECHAT_HEALTH_URL` 是独立语音服务地址，不由门户域名或服务器 IP 推断。CueKB-only 使用 `ENABLED_TOOLS=search_knowledge`，无需天气配置。数据库/Redis 凭据使用 URL 安全字符，容器内连接串由 Compose 构造。

真实语音可先保持 `VOICE_PROVIDER=disabled`，先验真实文字。独立完成固定版本的协议探针和人工听音后，再填 `VOICECHAT_API_VERSION`、镜像 digest、`basic|enhanced` 能力及验证声明，切换为 `nvidia`；真实失败不回退 mock。Web Nginx 直接终止 HTTPS，并处理 SSE buffering、WS upgrade、请求大小和安全头；不得记录 token、Cookie 或语音票据。

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

## D07 分阶段执行设计

状态：待执行。以下工作在后续具备真实服务的云端 Docker 验收环境执行，不是本次文档整理或编码阶段的运行指令。沿用上述构建、Compose 与回滚流程，不新增部署系统。各场景的唯一验收定义见 [V01–V12](acceptance-report.md#4-最终方案验收清单)。

| 阶段 | 执行方案 | 退出条件与证据 |
| --- | --- | --- |
| D07-A 基线与环境 | 固定应用 commit、API/Web 镜像、VoiceChat API/digest、CueKB revision、文本模型与脱敏配置摘要；准备受限测试账号、KB、授权英文样本。按唯一生产流程独立构建镜像，执行迁移、健康、备份恢复预检，验证公网 HTTPS `8087` 的证书/登录/麦克风和私网 `8088` 的网络隔离 | 记录版本、配置与迁移结果；`verify_deployment.py` 确认非 mock 与启用工具一致；readiness 只作为入口条件 |
| D07-B 真实文字与授权 | 先启用真实文字链路，语音保持 disabled；验证简单认证登录/过期、两个测试身份隔离、KB 交集/撤权，再跑真实文本模型 → CueKB M3 的支持/澄清/冲突/故障场景 | V02–V04、V10 的文字部分具备 trace、引用版本、状态和权限证据；失败不得归类为空命中 |
| D07-C 英文基础语音 | 在隔离的云端验收部署固定供应商版本，用授权录音执行协议探针并人工听音，再验证门户 → VoiceChat → 本项目 → 真实 CueKB → 实际口述 | V01/V03/V07/V09 有录音授权、事件、实际回答和人工判定；探针的合成工具结果不充当知识闭环证据 |
| D07-D 竞态与恢复 | 工具等待 5 秒时分别附和、新问、改问、取消、停止播报；在结果写回及播报边界断网；测试超过两分钟及多次轮换 | V05/V06/V08 留下旧 revision 拒绝、pending call 结清或关闭、新连接无旧音频的证据；增强能力不通过则只评估 basic |
| D07-E 故障与容量 | 从单副本开始，测真实 PG 事务/迁移、Redis 租约丢失、进程退出、drain 和备份恢复；逐档增加会话与任务并发。多副本仅在验证粘性路由后测试 | V11/V12 与延迟分解、错误率、资源峰值；先测基线再冻结阈值，以独立样本复测，不能把副本预算当全局预算 |
| D07-F 放行 | 汇总版本与所有适用 V 项，核对缺测/失败/不适用；按已验证版本设置能力声明，保留回滚版本与操作记录 | 基础语音必需项均有证据，增强声明另有 V05 门槛；失败修复后复测，剩余边界明确记录 |

**避免验证开关循环。** 使用同一生产部署和受限访问控制，先以 `VOICE_PROVIDER=disabled` 验证真实文字链路，同时独立运行 VoiceChat 协议探针并人工复核授权音频。只有记录版本、能力和音频证据后，才将该部署切换为 `nvidia` 并执行完整语音闭环；不要预先设置已验证声明来绕过启动检查。无需 integration Compose override。

每阶段保存：执行时间、操作者、应用/供应商版本、case/V ID、输入来源、预期/实际结果、失败原因、脱敏 trace 和受控证据位置。录音/票据/真实配置不提交仓库；仓库验收记录只写结论与受控证据引用。未执行标未执行；不适用须按本期范围解释，不能用来跳过基础语音的安全与恢复项。

若仅 enhanced 交互门槛失败，保留 basic 明确打断/重连能力；若权限、旧结果泄漏或实际口述事实错误等核心项失败，不放行语音。可继续提供已验收的文字服务，但不能把文字放行写成 D07 语音完成。
