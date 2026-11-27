# 云端容器部署手册

默认部署 Sophon Live。以下命令在部署机项目目录执行。服务器需有仓库读取权限、Docker Engine、Compose 2.20 或更新版本及 Buildx。[Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository)

## 第一步：获取仓库代码

首次部署运行：

```sh
git clone --branch main git@github.com:ttonghao-thao/voice-service-agent.git
cd voice-service-agent
```

升级时进入已有仓库，在 `main` 上用 `git status --short` 检查本地改动。先保存现有配置并记下目录。曾直接修改 Compose 的 API CA 挂载时，按[旧部署 CA 迁移](#旧部署-ca-迁移)处理后再执行 `git pull --ff-only origin main`。

```sh
vsa_backup="$(mktemp -d "$HOME/voice-service-backup.XXXXXXXX")"
install -m 600 .env "$vsa_backup/.env"
cp deploy/compose.production.yaml "$vsa_backup/compose.yaml"
if [ -f deploy/compose.api-ca.yaml ]; then
  cp deploy/compose.api-ca.yaml "$vsa_backup/compose.api-ca.yaml"
fi
printf '%s\n' "$vsa_backup"
```

## 第二步：准备配置并制作镜像

开放 `8087/TCP`（门户）、`7880/TCP`（LiveKit 信令）、`7881/TCP` 和 `7882/UDP`（媒体）。公开部署采用 [Certbot webroot](#公网证书与续期)，域名和公网 IPv4 共用 `80/TCP` 的 HTTP-01 流程。已有覆盖访问地址的公信证书可直接填路径。门户和 LiveKit 信令共用证书，浏览器麦克风使用 HTTPS。[麦克风安全上下文](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)

### 填写 `.env`

**首次创建配置**时，复制模板。用下方随机值命令生成四个不同的值，填入 `POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`。升级保留现有密码。

```sh
cp -n .env.example .env
chmod 600 .env
openssl rand -hex 32
```

按下表和 [配置模板](../.env.example) 填写实际配置。

```sh
nano .env
```

| 修改位置 | 填写内容 |
| --- | --- |
| `IMAGE_TAG` | API 与 Web 镜像共同使用的标签，下文制作镜像时写入 |
| `PUBLIC_ORIGIN` | 门户的 HTTPS origin。填实际域名或公网 IPv4 和 `8087`，省略路径和末尾 `/` |
| `WEB_TLS_CERT_FILE`、`WEB_TLS_KEY_FILE` | 主机上的证书链和服务端私钥绝对路径。Certbot 默认为 `/etc/letsencrypt/live/voice.example.com/fullchain.pem` 和同目录的 `privkey.pem` |
| `API_CA_BUNDLE_FILE` | 默认留空使用系统信任。API 连接私有 CA 签发的服务时填[合并 CA bundle](#私有-ca-与-api-信任)的主机绝对路径 |
| `POSTGRES_PASSWORD`、`REDIS_PASSWORD` | 各服务的 URL-safe 密码。已有数据卷继续使用原部署值 |
| `VOICE_PROVIDER` | 模板为 `sophon_live`。切换 `nvidia` 见[兼容后端](#nvidia-兼容后端) |
| `LIVEKIT_URL` | LiveKit 的 WSS origin。填证书覆盖的域名或 IPv4 和 `7880`，浏览器和 API 容器均须可达 |
| `LIVEKIT_NODE_IP` | 客户端可达的服务器 IPv4，公网部署填实际公网地址 |
| `LIVEKIT_RTC_INTERFACE` | 可选。填 LiveKit 容器处理 `7881/TCP` 和 `7882/UDP` 入站流量的网卡名，留空使用默认候选收集。按[选择 LiveKit 媒体网卡](#选择-livekit-媒体网卡)确认 |
| `LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` | LiveKit 服务端和 API 共用的密钥对。分别用 URL-safe 随机值，Secret 至少 32 字符 |
| `SOPHON_LIVE_WS_URL`、`SOPHON_LIVE_API_KEY` | Sophon 的完整 WSS 地址，含 `/v1/live/sessions`。上游允许匿名时 Key 可留空 |
| `SOPHON_LIVE_LOCALE` | 本部署的语音语言，按上游选择 `zh-CN` 或 `en-US` |
| `DELEGATION_ROUTER_BASE_URL`、`DELEGATION_ROUTER_MODEL`、`DELEGATION_ROUTER_API_KEY` | Qwen 工作空间的 OpenAI 兼容 base URL（通常以 `/compatible-mode/v1` 结尾）、已批准的模型名和对应 Key。应用自动追加 `/chat/completions` |
| `WEB_SEARCH_API_KEY` | Tavily Search Key，供公开查询和受控兜底 |
| `WEATHER_BASE_URL`、`WEATHER_API_KEY` | 启用天气时填写和风天气专属 HTTPS API Host 和 Key |

### 查询服务的选择

`.env.example` 已写入 `ENABLED_TOOLS=search_knowledge,weather,web_search`。从旧版本保留下来的 `.env` 若没有该行，可直接新增。Sophon 部署保留 `web_search` 和有效的 `WEB_SEARCH_API_KEY`。省略可选服务时，修改工具清单并移除该服务的连接配置。

| 接入方式 | `ENABLED_TOOLS` | 可省略的配置 |
| --- | --- | --- |
| 三类查询 | `search_knowledge,weather,web_search` | 填齐三类服务的连接配置 |
| 省略 CueKB | `weather,web_search` | `CUEKB_BASE_URL`、`CUEKB_API_KEY`、`KNOWLEDGE_BASE_IDS` |
| 省略 QWeather | `search_knowledge,web_search` | `WEATHER_BASE_URL`、`WEATHER_API_KEY` |
| 只接 Tavily | `web_search` | 上述 CueKB 和 QWeather 配置 |

启用了某个查询时，启动脚本会校验其所需连接配置。CueKB 地址和 Key 都为空时，只有明确公开且获授权的独立问题可转 Tavily。停用天气时，专门的天气委派会按工具不可用处理。公网兜底细节见 [知识执行](architecture.md#3-知识执行)。

### CueKB 填写

| 配置 | 填写内容 |
| --- | --- |
| `CUEKB_BASE_URL` | API 容器可达的 CueKB API 根地址，例如 `https://cuekb.example.com`。受控内网可用 `http://cuekb.internal.example.com:8085`。应用会追加接口路径，填写时省略 `/portal/`、`/v2` 和具体接口名。 |
| `CUEKB_API_KEY` | CueKB 签发的业务 `api_key` 原值。为该 Key 关联的 `principal_id` 授予所填库的 `read` 权限。应用自动加 `Authorization: Bearer`。 |
| `KNOWLEDGE_BASE_IDS` | 允许语音客户检索的库 UUID。从 CueKB 门户「知识库管理」复制，或用该业务 Key 调用 `GET /v2/knowledge-bases` 读取对应 `id`。支持 1–50 个不同 UUID，用英文逗号分隔。 |

在目标 CueKB 实例确认业务 Key 拥有所填库的 `read` 权限，且所需资料已发布。语音检索要求 `POST /v2/evidence/query` 和 `POST /v2/evidence/validate` 支持 `cuekb.evidence/2.1`。接口细节见 [CueKB 契约](interfaces/cuekb.md)。

在 **CueKB 服务侧** 成对配置 `CUEKB_EVIDENCE_VOICE_TOKENIZER_PATH` 和 `CUEKB_EVIDENCE_VOICE_TOKENIZER_ID`。前者指向容器内可读的 `tokenizer.json`，后者标识该 tokenizer 的固定版本。语音服务发出 `delivery_profile=voice` 的请求，两项留空时 CueKB 返回 `409 VOICE_TOKENIZER_NOT_CONFIGURED`。

### 生产默认值

生产 Compose 生成 `DATABASE_URL` 和 `REDIS_URL`，设置 `AUTH_MODE=validation`、`AUTO_CREATE_SCHEMA=false`、`CUEKB_MODE=real`、`WEATHER_PROVIDER=qweather` 和 `WEATHER_MODE=real`。`AGENT_PROVIDER=disabled` 关闭独立文本问答模型。`DELEGATION_ROUTER_PROVIDER=openai_compatible` 独立启用 Qwen 解析 Sophon 已发出的原生 `task`。语音回答由 Sophon 生成。`QA_*`、预算与超时由 [生产 Compose](../deploy/compose.production.yaml) 和 [应用设置](../apps/api/app/config.py) 提供默认值。已有 `.env` 中与默认值相同的项可省略。

需要后台工具管理 API 时，在 `.env` 单独设置 `TOOLS_ADMIN_API_KEY`。该接口默认关闭。[后台配置契约](interfaces/voice-tools.md#2-后台配置与刷新)

### 制作镜像

两张镜像共用写入 `.env` 的 `IMAGE_TAG`。下载失败时按 [API 镜像的依赖下载](#api-镜像的依赖下载)处理。

```sh
vsa_image_tag="$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$vsa_image_tag/" .env
sudo docker buildx build --load -f deploy/Dockerfile.api -t "voice-service-agent-api:$vsa_image_tag" .
sudo docker buildx build --load -f deploy/Dockerfile.web -t "voice-service-agent-web:$vsa_image_tag" .
```

## 第三步：启动服务

发布新版本的已有部署先[备份数据库](#升级前备份数据库)。首次部署直接启动。

```sh
sudo ./scripts/deploy-cloud.sh .env
```

脚本检查配置、启动 PostgreSQL/Redis/LiveKit、执行 Alembic 迁移，再检查 API 和 Web。默认 Sophon 部署预期五个常驻服务为 healthy。仅修改运行配置时沿用 `IMAGE_TAG`。证书续期按[自动续期](#自动续期)加载证书。

## 第四步：访问 URL 验证

将以下示例换成实际域名或公网 IPv4，从使用默认系统信任的设备执行。

```sh
curl -fsS --max-time 15 https://voice.example.com:8087/health/ready
curl -fsS --max-time 15 https://voice.example.com:7880/
```

门户应返回 `"status":"ready"` 及已启用的查询，LiveKit 根路径应返回 `OK`。按 [Sophon 容器内检查](validation.md#sophon-live-验证)核对 API TLS 和凭据，再在门户点击 **Start call** 并允许使用麦克风，确认 **Voice ready**、双向音频和查询。[浏览器检查](validation.md#上线时的浏览器检查)

## 公网证书与续期

域名证书覆盖 DNS 名称，公网 IPv4 证书覆盖 IP SAN。两者的公信链由客户端系统验证。域名解析须指向提供验证文件的公网入口，公网 IPv4 直接验证该地址。HTTP-01 使用公网 `80/TCP`，门户和 LiveKit 使用各自的 HTTPS/WSS 端口。[HTTP-01 验证规则](https://letsencrypt.org/docs/challenge-types/#http-01-challenge)

### 配置 HTTP-01

在实际接收公网 `80` 流量的 Nginx 上配置。已有站点时，将下面的 `location` 加入匹配访问地址的 `server`。Ubuntu 空闲 `80` 端口可用 `sudo apt-get install -y nginx`，将完整示例保存到 `/etc/nginx/conf.d/voice-acme.conf`。用实际域名或公网 IPv4 替换 `server_name` 和 `vsa_tls_host`。HTTPS 跳转放在 `location /`，验证路径直接提供文件。容器化 Nginx 需挂载同一挑战目录。

```nginx
server {
    listen 80;
    server_name voice.example.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type text/plain;
        try_files $uri =404;
    }
}
```

```sh
vsa_tls_host=voice.example.com
sudo install -d -m 0755 /var/www/letsencrypt/.well-known/acme-challenge
printf '%s\n' acme-ready | sudo tee /var/www/letsencrypt/.well-known/acme-challenge/voice-check >/dev/null
sudo nginx -t && sudo nginx -s reload
```

从外网运行 `curl -fsS http://voice.example.com/.well-known/acme-challenge/voice-check`，将地址换成实际域名或公网 IPv4，预期返回 `acme-ready`。

### 签发证书

统一使用 Certbot **5.4 或更新版本**以支持公网 IPv4 的 `webroot`。Ubuntu 可用 `sudo snap install --classic certbot`，通过 `/snap/bin/certbot --version` 核对版本。将下方 `vsa_certbot` 设为实际可执行文件路径。[Certbot 安装](https://certbot.eff.org/instructions?os=snap&ws=other) · [IP 签发说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)

```sh
vsa_certbot=/snap/bin/certbot
```

访问地址为域名时执行下面的命令。

```sh
sudo "$vsa_certbot" certonly --webroot -w /var/www/letsencrypt \
  --cert-name "$vsa_tls_host" -d "$vsa_tls_host"
```

访问地址为公网 IPv4 时执行下面的命令。

```sh
sudo "$vsa_certbot" certonly --webroot -w /var/www/letsencrypt \
  --preferred-profile shortlived --cert-name "$vsa_tls_host" --ip-address "$vsa_tls_host"
```

将 `.env` 的 `PUBLIC_ORIGIN` 和 `LIVEKIT_URL` 设为证书覆盖的地址。证书链使用 `/etc/letsencrypt/live/<cert-name>/fullchain.pem`，私钥使用同目录的 `privkey.pem`。现有 `server.crt`、`server.key` 可通过指向这两个文件的链接跟随续期。

### 自动续期

公信 IP 证书有效期为 160 小时。在项目根目录使用上一步的变量配置续期钩子。证书按文件挂载，续期成功后重建 Web 以加载新证书，进行中的通话会断开。[IP 证书有效期](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) · [Certbot 续期](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)

```sh
vsa_project_dir=$(pwd -P)
sudo install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/voice-service-web >/dev/null <<EOF
#!/bin/sh
set -eu
[ "\${RENEWED_LINEAGE:-}" = '/etc/letsencrypt/live/$vsa_tls_host' ] || exit 0
cd '$vsa_project_dir'
exec docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml up -d --no-deps --force-recreate --wait web
EOF
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/voice-service-web
sudo "$vsa_certbot" renew --cert-name "$vsa_tls_host" --dry-run
systemctl list-timers --all | grep certbot
```

确认续期模拟通过且 Certbot 定时任务已启用。首次部署继续[启动服务](#第三步启动服务)。运行中的站点切换证书时，更新 `.env` 后运行同一钩子，再[验证入口](#第四步访问-url-验证)。

```sh
sudo env RENEWED_LINEAGE="/etc/letsencrypt/live/$vsa_tls_host" \
  /etc/letsencrypt/renewal-hooks/deploy/voice-service-web
```

## 需要时执行

### 私有 CA 与 API 信任

当 API 要访问由私有 CA 签发的服务时，准备同时包含系统公信根和所需私有根的 CA bundle，并确保 API 的非 root 用户可读。将其主机绝对路径填入 `.env`。

```dotenv
API_CA_BUNDLE_FILE=/etc/voice-tls/ca-bundle.pem
```

用[标准启动命令](#第三步启动服务)应用配置。脚本选择 [Compose CA 配置](../deploy/compose.api-ca.yaml)，将文件只读挂载给 API 并设置 `SSL_CERT_FILE`。为空时使用镜像内的系统信任。受控门户使用私有 CA 时，客户端需信任该私有根。

按原路径替换 CA bundle 后，用以下命令重建读该文件的 API。

```sh
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml \
  -f deploy/compose.api-ca.yaml up -d --no-deps --force-recreate --wait api
```

### 旧部署 CA 迁移

旧部署若直接修改过 `api` 的 CA 配置，备份后用 `git diff -- deploy/compose.production.yaml` 定位 `api.environment.SSL_CERT_FILE` 和对应文件挂载。仍依赖私有 CA 时，将原挂载的宿主路径填入 `API_CA_BUNDLE_FILE`。采用系统公信根时留空。移除旧的两项手工改动，保留其余现场配置，再更新仓库并用标准启动命令应用。

### 选择 LiveKit 媒体网卡

Sophon 部署使用固定 `LIVEKIT_NODE_IP` 时，若浏览器已通过信令却无法建立媒体连接，可只收集实际接收媒体流量的容器网卡上的 ICE 候选。将抓包中 Docker 转发的目标地址与容器网卡地址对照。以下命令显示现有 LiveKit 容器的 Compose 网络和网卡地址。[LiveKit 网卡过滤配置](https://github.com/livekit/livekit/blob/v1.13.11/config-sample.yaml#L174-L181)

```sh
vsa_livekit_id=$(sudo docker compose --env-file .env --profile sophon \
  -f deploy/compose.production.yaml ps -q livekit)
sudo docker inspect -f '{{range $name, $network := .NetworkSettings.Networks}}{{println $name $network.IPAddress}}{{end}}' "$vsa_livekit_id"
vsa_livekit_pid=$(sudo docker inspect -f '{{.State.Pid}}' "$vsa_livekit_id")
sudo nsenter -t "$vsa_livekit_pid" -n ip -o -4 addr show
```

在部署机 `.env` 中填写匹配目标地址的容器网卡名。若 `ip` 显示 `eth2@if47`，填 `eth2`。安排维护窗口重建 `livekit`，正在进行的通话会断开。

```dotenv
LIVEKIT_RTC_INTERFACE=eth2
```

```sh
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml \
  up -d --no-deps --force-recreate --wait livekit
```

预期 `livekit` 为 healthy。如果重建返回 unhealthy，用下列命令查看健康检查输出。网卡不存在时检查新容器的接口名。需要恢复默认候选收集时，将 `LIVEKIT_RTC_INTERFACE` 留空并再次重建 `livekit`。

```sh
vsa_livekit_id=$(sudo docker compose --env-file .env --profile sophon \
  -f deploy/compose.production.yaml ps -q livekit)
sudo docker inspect -f '{{range .State.Health.Log}}{{println .Output}}{{end}}' "$vsa_livekit_id"
```

重建成功后核对网卡地址与媒体流量的转发目标一致，再按[浏览器媒体检查](validation.md#start-call-媒体未建连)复测。

### API 镜像的依赖下载

API 镜像按 `requirements.txt` 安装固定版本。未提供构建配置时，pip 的网络读取超时为 60 秒。依赖下载复用当前 BuildKit builder 的缓存。如果出现下载超时或已发布的固定版本提示 `from versions: none`，先用同一 builder 和 `--progress=plain` 重试上面的 API 构建命令。保留缓存可复用先前下载的包。[pip 超时与重试](https://pip.pypa.io/en/stable/cli/pip/#general-options)

如果部署网络需要指定可达的 PyPI 兼容源，在项目目录外准备 pip 配置。以下以官方 PyPI 为例，使用内部源时填部署方提供的 HTTPS `index-url`。[pip 配置格式](https://pip.pypa.io/en/stable/topics/configuration/#pip-config-file)

```ini
[global]
index-url = https://pypi.org/simple
timeout = 60
retries = 5
```

将路径换成部署机上的实际配置。构建过程通过 `pip_config` 读取该文件，使用代理 CA 时可以同时传入 `--secret id=proxy_ca,src=/实际路径/ca-bundle.crt`。[BuildKit 构建 Secret](https://docs.docker.com/build/building/secrets/)

```sh
sudo docker buildx build --progress=plain --load \
  --secret id=pip_config,src=/实际路径/pip.conf \
  -f deploy/Dockerfile.api -t "voice-service-agent-api:$vsa_image_tag" .
```

### 升级前备份数据库

通知用户结束通话后暂停 API/Web，备份当前数据库及迁移版本。重新登录后将 `vsa_backup` 设为第一步打印的目录。

```sh
test -s "$vsa_backup/.env" &&
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml stop -t 30 web api &&
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U voice_service -d voice_service -Atc 'SELECT version_num FROM alembic_version ORDER BY 1' \
  > "$vsa_backup/alembic.txt" &&
test -s "$vsa_backup/alembic.txt" &&
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml exec -T postgres \
  pg_dump -U voice_service -d voice_service -Fc > "$vsa_backup/postgres.dump" &&
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml exec -T postgres \
  pg_restore --list < "$vsa_backup/postgres.dump"
```

### 日志与 Start call

`LIVEKIT_SIGNAL_FAILED` 时，从故障设备打开 `LIVEKIT_URL` 对应的 HTTPS 根路径，确认返回 `OK` 并通过证书校验。连接超时核对 `7880/TCP` 和 API 容器的回环访问。`LIVEKIT_JOIN_REJECTED` 时核对 API 和 LiveKit 密钥。`LIVEKIT_MEDIA_FAILED` 时核对 `LIVEKIT_NODE_IP`、`7881/TCP`、`7882/UDP` 和 NAT 映射，必要时[选择媒体网卡](#选择-livekit-媒体网卡)。

API 用 `voice_session_id` 关联 `livekit_session_issued`、`livekit_stream_accepted` 和 `livekit_session_closed_before_stream`。TLS 握手早于 HTTP 请求，握手失败时 HTTP 日志可能为空。

```sh
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml \
  logs --since=3m --tail=80 web api livekit
```

### 回滚应用

使用[备份的配置](#第一步获取仓库代码)和本机旧镜像恢复 API/Web。命令先核对数据库迁移版本。如果版本不同，由维护人员根据备份处理。重新登录后将 `vsa_backup` 设为实际备份目录。

```sh
(
set -eu
test -s "$vsa_backup/alembic.txt"
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U voice_service -d voice_service -Atc 'SELECT version_num FROM alembic_version ORDER BY 1' \
  > "$vsa_backup/alembic.current"
cmp "$vsa_backup/alembic.txt" "$vsa_backup/alembic.current"
set -- -f "$vsa_backup/compose.yaml"
case "$(sed -n 's/^API_CA_BUNDLE_FILE=//p' "$vsa_backup/.env" | tail -n 1)" in
  ''|'""'|"''") ;;
  *) set -- "$@" -f "$vsa_backup/compose.api-ca.yaml" ;;
esac
sudo env DEPLOY_ENV_FILE="$vsa_backup/.env" docker compose --env-file "$vsa_backup/.env" --profile sophon \
  "$@" up -d --no-deps --pull never --wait api
sudo env DEPLOY_ENV_FILE="$vsa_backup/.env" docker compose --env-file "$vsa_backup/.env" --profile sophon \
  "$@" up -d --no-deps --pull never --force-recreate --wait web
install -m 600 "$vsa_backup/.env" .env
)
```

### NVIDIA 兼容后端

在 `.env` 选择 `VOICE_PROVIDER=nvidia` 时，新增含 `/v1/realtime` 的完整 `VOICECHAT_WS_URL` 和对应的服务端 Bearer Key `VOICECHAT_API_KEY`。启动预检要求保留 `search_knowledge`、`web_search` 及其凭据。LiveKit 和 Sophon 原生 task 的 Qwen 配置适用于 `sophon_live`。使用同一启动命令。
