# 云端容器部署手册

默认部署 Sophon Live。用 `ssh deploy@203.0.113.10` 登录实际云服务器，后续命令在服务器终端逐条执行，遇到报错先停在当前步骤。服务器需有仓库读取权限、Docker Engine、Compose 2.20 或更新版本以及 Buildx。缺少 Docker 时按 [Ubuntu 安装命令](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository) 安装。

## 第一步：获取仓库代码

首次部署运行：

```sh
git clone --branch main git@github.com:ttonghao-thao/voice-service-agent.git
cd voice-service-agent
```

升级时进入已有仓库，确认在 `main` 分支。用 `git status --short` 检查本地改动，先保存当前配置。记下备份目录，第三步会用到。

```sh
vsa_backup="$(mktemp -d "$HOME/voice-service-backup.XXXXXXXX")"
install -m 600 .env "$vsa_backup/.env"
cp deploy/compose.production.yaml "$vsa_backup/compose.yaml"
printf '%s\n' "$vsa_backup"
```

工作区干净时运行 `git pull --ff-only origin main`。如果 `git diff -- deploy/compose.production.yaml` 显示该文件唯一的本地改动是[私有 CA 挂载](#公网-ipv4-与私有-ca)，可以暂存该文件、更新并恢复挂载。恢复有冲突时，从备份核对并补回 `api` 的 CA 配置后再继续。

```sh
git stash push -m 'local API CA trust' -- deploy/compose.production.yaml &&
git pull --ff-only origin main &&
git stash pop
```

## 第二步：准备配置并制作镜像

在云安全组开放 `8087/TCP`（门户）、`7880/TCP`（LiveKit 信令）、`7881/TCP` 和 `7882/UDP`（媒体）。门户和 LiveKit 共用一份证书。浏览器麦克风需要 HTTPS 安全上下文。[浏览器麦克风文档](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia)

### 选择证书

- 已有公信证书时，确认它覆盖实际访问的域名或 IPv4，填写证书和私钥路径。
- 用公网 IPv4 直接提供语音服务时，按[公网 IPv4 与公信证书](#公网-ipv4-与公信证书)签发，客户端使用系统证书信任。
- 使用公网 IPv4 加私有证书时，按[公网 IPv4 与私有 CA](#公网-ipv4-与私有-ca)在服务器上生成证书并配置两端信任。
- 需要签发域名公信证书时，先确认域名解析指向服务器，再选择下面的 Certbot 验证方式。

```sh
getent ahostsv4 voice.example.com
sudo apt-get update
sudo apt-get install -y certbot
```

把命令中的 `voice.example.com` 换成实际域名。选用与现场条件匹配的一条。

- 公网 `80/TCP` 可达且空闲时，运行 `sudo certbot certonly --standalone --cert-name voice.example.com -d voice.example.com`。
- 公网 80 已由 Nginx 或 Apache 监听时，让现有服务从 `/var/www/letsencrypt` 提供 `/.well-known/acme-challenge/` 文件。从外网确认测试文件返回 `200` 和原文后，运行 `sudo certbot certonly --webroot -w /var/www/letsencrypt --cert-name voice.example.com -d voice.example.com`。[Certbot Webroot 指南](https://eff-certbot.readthedocs.io/en/stable/using.html#webroot)
- 有 DNS 管理权限时，可运行 `sudo certbot certonly --manual --preferred-challenges dns --cert-name voice.example.com -d voice.example.com`。按提示添加 `_acme-challenge` TXT，等待公网查到当前值再继续。该方式无需开放公网 80。[Let’s Encrypt 验证方式](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge)

### 填写 `.env`

**首次创建配置**时，复制模板。用下方随机值命令生成四个不同的值，填入 `POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`。升级保留现有密码。

```sh
cp -n .env.example .env
chmod 600 .env
openssl rand -hex 32
```

首次部署或配置有变化时，编辑仓库根目录的 `.env`。修改等号右侧，缺字段时加到末尾。用 nano 的 `Ctrl+O`、回车保存，再按 `Ctrl+X` 退出。

```sh
nano .env
```

| 修改位置 | 填写内容 |
| --- | --- |
| `IMAGE_TAG` | API 与 Web 镜像共同使用的标签，下文制作镜像时写入 |
| `PUBLIC_ORIGIN` | 门户的 HTTPS origin。填实际域名或公网 IPv4 和 `8087`，省略路径和末尾 `/` |
| `WEB_TLS_CERT_FILE`、`WEB_TLS_KEY_FILE` | 主机上的证书链和服务端私钥绝对路径。Certbot 默认为 `/etc/letsencrypt/live/voice.example.com/fullchain.pem` 和同目录的 `privkey.pem` |
| `POSTGRES_PASSWORD`、`REDIS_PASSWORD` | 各服务的 URL-safe 密码。已有数据卷继续使用原部署值 |
| `VOICE_PROVIDER` | 模板为 `sophon_live`。切换 `nvidia` 见[兼容后端](#nvidia-兼容后端) |
| `LIVEKIT_URL` | LiveKit 的 WSS origin。填证书覆盖的域名或 IPv4 和 `7880`，浏览器和 API 容器均须可达 |
| `LIVEKIT_NODE_IP` | 客户端可达的服务器 IPv4，公网部署填实际公网地址 |
| `LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET` | LiveKit 服务端和 API 共用的密钥对。分别用 URL-safe 随机值，Secret 至少 32 字符 |
| `SOPHON_LIVE_WS_URL`、`SOPHON_LIVE_API_KEY` | Sophon 的完整 WSS 地址，含 `/v1/live/sessions`。上游允许匿名时 Key 可留空 |
| `SOPHON_LIVE_LOCALE` | 本部署的语音语言，按上游选择 `zh-CN` 或 `en-US` |
| `DELEGATION_ROUTER_BASE_URL`、`DELEGATION_ROUTER_MODEL`、`DELEGATION_ROUTER_API_KEY` | Qwen 工作空间的 OpenAI 兼容 base URL（通常以 `/compatible-mode/v1` 结尾）、已批准的模型名和对应 Key。应用自动追加 `/chat/completions` |
| `WEB_SEARCH_API_KEY` | Tavily Search Key，供公开查询和受控兜底 |
| `WEATHER_BASE_URL`、`WEATHER_API_KEY` | 启用天气时填写和风天气专属 HTTPS API Host 和 Key |

同一份 Compose 内，Web 的 Nginx 反代到 `livekit:7880`。API 按 `LIVEKIT_URL` 加入房间，并把同一地址随房间 Token 下发给浏览器。两端都需要访问该 WSS 入口。经 NAT 访问时，核对 API 容器的回环访问以及 `7881/TCP`、`7882/UDP` 原端口映射。

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

将 [配置模板](../.env.example) 中保留的占位值换成实际配置。

执行下列命令制作镜像。两张镜像共用写入 `.env` 的 `IMAGE_TAG`。

```sh
sudo docker compose version
sudo docker buildx version
vsa_image_tag="$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$vsa_image_tag/" .env
sudo docker buildx build --load -f deploy/Dockerfile.api -t "voice-service-agent-api:$vsa_image_tag" .
sudo docker buildx build --load -f deploy/Dockerfile.web -t "voice-service-agent-web:$vsa_image_tag" .
```

## 第三步：启动服务

检查配置并拉取本机缺少的依赖镜像。LiveKit 与 Redis、PostgreSQL 由同一份 Compose 管理。

```sh
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml config --quiet
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml pull --policy missing postgres redis livekit
```

**已有部署升级时**，通知用户结束通话，再执行下列备份命令。首次部署跳过此块。如果重新登录过，用 `vsa_backup="/实际备份目录"` 恢复第一步的路径。

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

备份成功后执行启动命令。首次部署直接执行。它启动依赖、迁移数据库、检查 API 和启动 Web。预期五个常驻服务显示 healthy。只改了地址、证书路径或 Key 时，编辑 `.env`，保留当前 `IMAGE_TAG` 并重跑启动命令。

```sh
sudo ./scripts/deploy-cloud.sh .env
```

## 第四步：访问 URL 验证

使用公信证书时，将以下示例换成 `.env` 的实际入口。

```sh
curl -fsS --max-time 15 https://voice.example.com:8087/health/ready
curl -fsS --max-time 15 https://voice.example.com:7880/
```

使用私有 CA 时，替换为实际公网 IPv4，并在部署机运行下列命令。

```sh
curl -fsS --max-time 15 --cacert /etc/voice-tls/root-ca.crt https://203.0.113.10:8087/health/ready
curl -fsS --max-time 15 --cacert /etc/voice-tls/root-ca.crt https://203.0.113.10:7880/
```

第一条应返回 `"status":"ready"`，`enabled_tools` 应与 `.env` 的工具清单一致。第二条应返回 HTTP 2xx。按[Sophon 容器内检查](validation.md#sophon-live-验证)确认 API 容器也能通过 LiveKit TLS 和凭据校验。然后从客户端打开 `.env` 中的 `PUBLIC_ORIGIN`，点 **Start call**，允许使用麦克风，核对入房、双向音频和已启用的查询。[浏览器检查](validation.md#上线时的浏览器检查)

## 公网 IPv4 与公信证书

Let’s Encrypt 可以为固定公网 IPv4 签发浏览器信任的证书。以下用 `203.0.113.10` 举例。IP 证书的有效期为 160 小时，部署时配置自动续期。Certbot 的 IP `webroot` 模式需要 5.4 或更高版本。用 `certbot --version` 核对，版本较低时按官方说明安装新版。[IP 证书签发](https://letsencrypt.org/2026/03/11/shorter-certs-certbot) · [证书有效期](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability) · [Certbot 安装](https://certbot.eff.org/instructions?os=snap&ws=other)

### 签发并加载证书

公网 `80/TCP` 已由 Nginx 提供服务时，在该 IP 对应的 `listen 80` 虚拟主机中加入以下配置。Certbot 与 Nginx 要能访问同一个挑战目录。Nginx 在容器里时，将宿主机的目录只读挂载给 Nginx。若虚拟主机需要跳转到 HTTPS，把跳转放在 `location /`，让挑战路径直接返回文件。[HTTP-01 验证规则](https://letsencrypt.org/docs/challenge-types/#http-01-challenge)

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /var/www/letsencrypt;
    default_type text/plain;
    try_files $uri =404;
}
```

创建测试文件，检查 Nginx 配置并重载，然后从外网用 `curl -fsS http://203.0.113.10/.well-known/acme-challenge/voice-check` 确认响应为 `acme-ready`。

```sh
sudo install -d -m 0755 /var/www/letsencrypt/.well-known/acme-challenge
printf '%s\n' acme-ready | sudo tee /var/www/letsencrypt/.well-known/acme-challenge/voice-check >/dev/null
```

验证路径可用后，在部署机签发证书。若 `80/TCP` 可达且没有进程监听，也可用 `--standalone` 替换下面的 `--webroot --webroot-path /var/www/letsencrypt`。续期同样要求 `80/TCP` 上的验证方式可用。[Certbot 签发方式](https://eff-certbot.readthedocs.io/en/stable/using.html#getting-certificates-and-choosing-plugins)

```sh
sudo certbot certonly --webroot --webroot-path /var/www/letsencrypt \
  --preferred-profile shortlived --cert-name 203.0.113.10 --ip-address 203.0.113.10
```

在 `.env` 填写以下证书路径。门户使用 `https://203.0.113.10:8087`，`LIVEKIT_URL` 使用 `wss://203.0.113.10:7880`。首次部署继续完成第二步。现有部署只更换证书时，用[续期证书的命令](#需要时执行)重建 `web`，再按[第四步](#第四步访问-url-验证)检查两个端口的证书信任。

```dotenv
WEB_TLS_CERT_FILE=/etc/letsencrypt/live/203.0.113.10/fullchain.pem
WEB_TLS_KEY_FILE=/etc/letsencrypt/live/203.0.113.10/privkey.pem
```

### 配置 IP 证书自动续期

用 `systemctl list-timers --all` 核对 Certbot 的续期任务。续期会更新 `live` 目录下的文件链接，Compose 按文件挂载证书，需要在更新后重建 `web`，届时正在进行的通话会断开。将下面的内容写入 `/etc/letsencrypt/renewal-hooks/deploy/voice-service-web`，把 IP 和项目绝对路径替换为现场值，再执行 `sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/voice-service-web`。[Certbot 自动续期与钩子](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)

```sh
#!/bin/sh
set -eu
[ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/203.0.113.10 ] || exit 0
cd /srv/voice-service-agent
exec docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml \
  up -d --no-deps --force-recreate --wait web
```

运行 `sudo certbot renew --cert-name 203.0.113.10 --dry-run` 验证续期挑战。期望 Certbot 报告模拟续期成功。安排可中断通话的时间时，加 `--run-deploy-hooks` 可同时验证钩子加载当前有效证书。

## 公网 IPv4 与私有 CA

固定公网 IPv4 可以直接用于门户 HTTPS 和 LiveKit WSS。以下使用 `203.0.113.10` 作为示例，统一替换成实际地址。服务端证书的 `subjectAltName` 必须以 `IP` 类型包含这个地址。私有 CA 可以在同一台服务器签发，无需 DNS 权限或开放 `80/TCP`。此方式适用于可以在访问设备上安装私有根证书的部署。[TLS IP 身份校验](https://www.rfc-editor.org/rfc/rfc9525.html#section-6.4)

### 在部署机签发证书

Ubuntu 上执行 `sudo apt-get install -y openssl ca-certificates`，再执行 `sudo -i` 后运行下列命令。首次生成根 CA 时输入根密钥密码，签发时再次输入。`/root/voice-ca/root-ca.key` 供续签使用，部署和客户端使用导出的证书文件。

```sh
(
set -eu
umask 077
vsa_public_ip=203.0.113.10

install -d -m 0700 /root/voice-ca
install -d -m 0755 /etc/voice-tls
cd /root/voice-ca

if [ ! -e root-ca.key ] && [ ! -e root-ca.crt ]; then
  openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 \
    -config /dev/null -subj '/CN=Voice Private Root CA' \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -addext 'subjectKeyIdentifier=hash' \
    -keyout root-ca.key -out root-ca.crt
fi

if [ ! -e /etc/voice-tls/privkey.pem ]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
    -out /etc/voice-tls/privkey.pem
fi

openssl req -new -config /dev/null -sha256 \
  -key /etc/voice-tls/privkey.pem -subj "/CN=$vsa_public_ip" -out server.csr

cat > server.ext <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:$vsa_public_ip
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF

openssl x509 -req -in server.csr -CA root-ca.crt -CAkey root-ca.key \
  -CAcreateserial -days 365 -sha256 -extfile server.ext -out server.pem
openssl verify -purpose sslserver -CAfile root-ca.crt -verify_ip "$vsa_public_ip" server.pem

install -m 0644 server.pem /etc/voice-tls/fullchain.pem
install -m 0644 root-ca.crt /etc/voice-tls/root-ca.crt
cat /etc/ssl/certs/ca-certificates.crt root-ca.crt > ca-bundle.pem
install -m 0644 ca-bundle.pem /etc/voice-tls/ca-bundle.pem
)
```

预期输出 `server.pem: OK`。看到该结果后退出 root shell，回到项目根目录继续配置。[OpenSSL IP 校验](https://docs.openssl.org/3.0/man1/openssl-verification-options/)

### 填写 IP 和证书文件

在 `.env` 中替换以下五项，更新为实际公网 IPv4 和证书路径。用同一张证书服务两个端口。

```dotenv
PUBLIC_ORIGIN=https://203.0.113.10:8087
LIVEKIT_URL=wss://203.0.113.10:7880
LIVEKIT_NODE_IP=203.0.113.10
WEB_TLS_CERT_FILE=/etc/voice-tls/fullchain.pem
WEB_TLS_KEY_FILE=/etc/voice-tls/privkey.pem
```

### 配置浏览器与 API 的证书信任

在使用门户的客户端设备上导入 `/etc/voice-tls/root-ca.crt` 并设为受信任的根证书。

iPhone 手动安装根证书后，进入「设置 → 通用 → 关于本机 → 证书信任设置」，对部署用的根证书开启完全信任。用同一台 iPhone 的 Safari 打开 `https://实际公网IPv4:7880/`，应无证书警告并直接显示 `OK`，再回到门户发起通话。[Apple 证书信任步骤](https://support.apple.com/zh-cn/102390)

API 容器也会用 `LIVEKIT_URL` 加入房间。在部署机的同一份 [Compose](../deploy/compose.production.yaml) 中，将以下字段合并到 `api` 服务。`SSL_CERT_FILE` 加入现有 `environment`，`volumes` 与 `env_file` 同级。

```yaml
    environment:
      <<: *application-environment
      PUBLIC_ORIGIN: ${PUBLIC_ORIGIN:?Set the test HTTPS origin in .env}
      SSL_CERT_FILE: /app/ca-bundle.pem
    volumes:
      - /etc/voice-tls/ca-bundle.pem:/app/ca-bundle.pem:ro
```

合并包包含系统公信 CA 和私有根 CA。它供 API 的 WSS 和其他 HTTPS 连接校验证书。接着完成[第二步](#第二步准备配置并制作镜像)的其余 `.env` 配置并按[第三步](#第三步启动服务)启动。如果现有部署只增加了 CA 挂载，重建 API 容器以加载新挂载和环境值，再执行[第四步](#第四步访问-url-验证)。

```sh
sudo docker compose --env-file .env --profile sophon \
  -f deploy/compose.production.yaml up -d --no-deps --force-recreate api
```

## 需要时执行

**查看启动日志**

```sh
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml logs --tail=100 web api livekit postgres redis
```

**排查 Start call**。`LIVEKIT_SIGNAL_FAILED` 时先在出错的设备检查 `LIVEKIT_URL` 所对应的 HTTPS 根路径，预期返回 `OK`。证书警告按客户端信任步骤处理，连接超时检查 `7880/TCP`。出现 `LIVEKIT_JOIN_REJECTED` 时核对 API 和 LiveKit 的密钥配置。出现 `LIVEKIT_MEDIA_FAILED` 时核对 `LIVEKIT_NODE_IP`、`7881/TCP`、`7882/UDP` 及 NAT 映射。

复现一次后在部署机采集最近的日志。API 用 `voice_session_id` 关联 `livekit_session_issued`、`livekit_stream_accepted` 和 `livekit_session_closed_before_stream`。签发后在控制连接建立前结束时，重点查看浏览器上的错误码。TLS 握手在 HTTP 请求前失败时，容器里可能没有对应的 HTTP 记录。

```sh
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml \
  logs --since=3m --tail=80 web api livekit
```

**续期证书**。手动 DNS-01 使用第二步的签发命令更新 TXT 并续期。自动续期的 Certbot 配置可运行 `sudo certbot renew --cert-name voice.example.com`。私有 CA 使用[同机签发命令](#在部署机签发证书)续签，它会复用已有根 CA 和服务端私钥。证书更新后重建 Web，再执行第四步。若同时更新了 CA 信任文件，重建 API 并将新的根 CA 加入客户端信任。[Certbot 续期文档](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)

```sh
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml up -d --no-deps --force-recreate --wait web
```

**回滚应用**。用第一步备份的配置和本机旧镜像恢复 API/Web。先确认基础服务健康且兼容。重新登录时把 `vsa_backup` 设为第一步打印的目录。如果数据库版本不同，命令会停止，由维护人员根据备份处理。

```sh
test -s "$vsa_backup/alembic.txt" &&
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U voice_service -d voice_service -Atc 'SELECT version_num FROM alembic_version ORDER BY 1' \
  > "$vsa_backup/alembic.current" &&
cmp "$vsa_backup/alembic.txt" "$vsa_backup/alembic.current" &&
sudo env DEPLOY_ENV_FILE="$vsa_backup/.env" docker compose --env-file "$vsa_backup/.env" --profile sophon \
  -f "$vsa_backup/compose.yaml" up -d --no-deps --pull never --wait api &&
sudo env DEPLOY_ENV_FILE="$vsa_backup/.env" docker compose --env-file "$vsa_backup/.env" --profile sophon \
  -f "$vsa_backup/compose.yaml" up -d --no-deps --pull never --force-recreate --wait web &&
install -m 600 "$vsa_backup/.env" .env
```

### NVIDIA 兼容后端

在 `.env` 选择 `VOICE_PROVIDER=nvidia` 时，新增含 `/v1/realtime` 的完整 `VOICECHAT_WS_URL` 和对应的服务端 Bearer Key `VOICECHAT_API_KEY`。启动预检要求保留 `search_knowledge`、`web_search` 及其凭据。LiveKit 和 Sophon 原生 task 的 Qwen 配置适用于 `sophon_live`。使用同一启动命令。
