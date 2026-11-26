# 云端容器部署手册

在云端服务器上从仓库更新代码、制作 API 和 Web 镜像、准备 TLS、备份、迁移启动，再从浏览器检查服务。**每次发布按第 1 至 8 步顺序执行**。首次安装时，命令会识别没有旧库的情况。已有环境保留原工作目录、数据库、凭据和同一个 Compose 项目。第 9 步用于故障和续期。

默认使用 Ubuntu 22.04/24.04、Docker Engine、Sophon Live 中文语音、同机 PostgreSQL/Redis/LiveKit。Sophon 与 CueKB 是已经准备好的独立服务。Compose 项目名为 `voice-service-agent`，使用单机、一个 API worker。本文的门户入口示例是 **`https://voice.example.com:8087/`**。实际操作时换成自己的域名。

## 1. 登录服务器，检查工具

在自己的电脑终端输入下面一行。将 `deploy` 和 `203.0.113.10` 换成云端 SSH 用户和公网 IP。该账号须能执行 `sudo`，并已获 GitHub 仓库的读取授权。

```bash
ssh deploy@203.0.113.10
```

后面的命令在服务器上执行。先单独输入 `bash`，此后留在同一个 Bash 会话。每次复制**一块**命令，等执行结束并核对预期再继续。以下设置使检查失败时退出本次操作的 Bash。发生失败时保留报错和第 3 步打印的记录目录，查第 9 步再重试。

```bash
set -euo pipefail
umask 077
sudo -v
uname -sm
```

已有 Docker 的服务器直接执行本步末尾的检查。首次使用一台没有 Docker 的 Ubuntu 服务器时，先执行以下安装块。服务器已经运行其他容器但检查报错时，交给主机管理员处理现有 Docker，避免重装影响其他业务。

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl nano
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod 0644 /etc/apt/keyrings/docker.asc
vsa_ubuntu_codename=$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")
sudo tee /etc/apt/sources.list.d/voice-service-docker.sources >/dev/null <<APT
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $vsa_ubuntu_codename
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
APT
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

```bash
git --version
curl --version
openssl version
sudo docker info --format 'Docker Engine {{.ServerVersion}}'
sudo docker compose version
sudo docker buildx version
df -h / "$(sudo docker info --format '{{.DockerRootDir}}')"
```

预期能看到各工具版本以及可用磁盘空间。Compose 插件至少支持 2.20 引入的可选依赖和 `up --wait`。已有 Docker 的主机若缺 Git、curl、OpenSSL、nano，可用上面的 Ubuntu 软件包安装命令补齐。构建应留出两张应用镜像、依赖、缓存和数据库备份所需空间。安装来源见 [Docker 的 Ubuntu 安装手册](https://docs.docker.com/engine/install/ubuntu/) 和 [Compose 服务配置参考](https://docs.docker.com/reference/compose-file/services/#depends_on)。

## 2. 准备域名、端口和接入资料

在域名管理页给实际域名添加 **A 记录**，指向服务器可达的公网 IPv4。默认直接解析到主机，使用该主机上的 HTTPS 终止。已有环境继续使用当前域名；没有正确 IPv6 路由时，删除该域名误配的 AAAA 记录。在云控制台的安全组或入站防火墙添加下表规则；私网用户按实际客户端网络配置。

| 端口 | 来源与用途 |
| --- | --- |
| 22/TCP | 管理员 IP，SSH。使用其他 SSH 端口时保留现有规则 |
| 80/TCP | 公网，默认证书申请及续期的 HTTP-01 域名验证。Certbot 临时监听，门户使用 8087 |
| 8087/TCP | 访问用户，门户 HTTPS、控制 WebSocket 和 API |
| 7880/TCP | 浏览器和 API 可达，LiveKit 的 TLS 信令及 API |
| 7881/TCP、7882/UDP | 浏览器和 API 可达，LiveKit 媒体。存在 NAT 时按同端口映射到该主机 |

PostgreSQL、Redis 和 API 的容器端口不需要公网入站。当前 Compose 没有 TURN，需保留媒体端口可达。API 容器也需要访问 Sophon、意图模型、CueKB、和风天气、Tavily 以及本机 LiveKit 的对外域名。使用公网 IP 映射的主机须验证从容器到该地址的回程。端口依据见 [LiveKit 自托管端口说明](https://docs.livekit.io/transport/self-hosting/ports-firewall/)。

按提示输入服务器运营方给出的真实值。第 4 步申请证书时使用联系邮箱注册 Certbot 账号。

```bash
read -r -p '仓库目录（首次安装直接回车使用 /opt/voice-service-agent，更新填原目录）: ' vsa_dir
vsa_dir=${vsa_dir:-/opt/voice-service-agent}
read -r -p '实际门户域名（不填 https://、端口或路径）: ' vsa_domain
read -r -p '实际公网 IPv4: ' vsa_node_ip
read -r -p '申请证书用的联系邮箱: ' vsa_acme_email
test "${vsa_dir#/}" != "$vsa_dir"
test -n "$vsa_domain"
test -n "$vsa_node_ip"
test -n "$vsa_acme_email"
getent ahostsv4 "$vsa_domain" | awk -v ip="$vsa_node_ip" '
  {print}
  $1 == ip {matches=1}
  END {exit !matches}
'
```

预期 DNS 输出含刚填的公网 IP。请同时从一台外部设备确认 DNS，避免服务器本地 hosts 覆盖或 DNS 缓存遮蔽公网解析问题。

部署前向服务维护人拿到第 5 步列出的连接地址和 Key，以及一条当前 CueKB 库中有确定答案和来源的非敏感验收问句。保留到第 8 步。真实凭据只写服务器 `.env`。

## 3. 更新代码并制作镜像

在已有服务器上，以下命令先保留更新前的 `.env`、Compose 和实际容器镜像引用，再以快进方式更新 `main`。本次操作记录放在当前账号的私有目录，打印路径后请记下。首次安装的 `git clone` 使用当前账号的 GitHub SSH 授权。若提示权限不足，补仓库只读授权后重试。

```bash
vsa_run="$(date -u +%Y%m%dT%H%M%SZ)-$$"
vsa_record="$HOME/.local/state/voice-service-agent/releases/$vsa_run"
vsa_next_env="$vsa_record/next.env"
mkdir -p "$vsa_record/previous"
declare -p vsa_dir vsa_domain vsa_node_ip vsa_acme_email vsa_run vsa_next_env \
  > "$vsa_record/operator-context.bash"
printf '本次发布记录: %s\n' "$vsa_record"

if [ ! -d "$vsa_dir/.git" ]; then
  if [ -e "$vsa_dir" ]; then
    if [ ! -d "$vsa_dir" ] || [ -n "$(ls -A "$vsa_dir")" ]; then
      printf '目录存在且不是空目录或项目仓库，请核对输入的仓库路径。\n' >&2
      exit 1
    fi
  else
    sudo install -d -m 0750 -o "$(id -un)" "$vsa_dir"
  fi
  git clone --branch main --single-branch \
    git@github.com:ttonghao-thao/voice-service-agent.git "$vsa_dir"
fi
cd "$vsa_dir"
test -r deploy/compose.production.yaml
test -x scripts/deploy-cloud.sh
test "$(git branch --show-current)" = main
test -z "$(git status --porcelain)" || { git status --short; exit 1; }

if [ -f .env ]; then
  install -m 0600 .env "$vsa_record/previous/.env"
  vsa_previous_compose="$vsa_dir/deploy/compose.production.yaml"
  vsa_previous_web=$(sudo docker ps -aq \
    --filter label=com.docker.compose.project=voice-service-agent \
    --filter label=com.docker.compose.service=web)
  if [ -n "$vsa_previous_web" ]; then
    vsa_compose_files=$(sudo docker container inspect \
      --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$vsa_previous_web")
    if [ -n "$vsa_compose_files" ]; then
      vsa_previous_compose=${vsa_compose_files%%,*}
    fi
  fi
  cp "$vsa_previous_compose" "$vsa_record/previous/compose.yaml"
fi
git rev-parse HEAD > "$vsa_record/previous/checkout.txt"

# 记录已存在容器的镜像 ID，并用本机回滚标签保留镜像。
printf 'services:\n' > "$vsa_record/previous/images.yaml"
for vsa_service in postgres redis livekit api web; do
  vsa_container=$(sudo docker ps -aq \
    --filter label=com.docker.compose.project=voice-service-agent \
    --filter "label=com.docker.compose.service=$vsa_service" \
    --filter label=com.docker.compose.oneoff=False)
  if [ -n "$vsa_container" ]; then
    vsa_previous_id=$(sudo docker container inspect --format '{{.Image}}' "$vsa_container")
    vsa_previous_image="voice-service-agent-rollback-$vsa_service:$vsa_run"
    sudo docker image tag "$vsa_previous_id" "$vsa_previous_image"
    printf '%s %s %s\n' "$vsa_service" "$vsa_previous_id" "$vsa_previous_image" >> "$vsa_record/previous/images.txt"
    printf '  %s:\n    image: %s\n    pull_policy: never\n' \
      "$vsa_service" "$vsa_previous_image" >> "$vsa_record/previous/images.yaml"
  fi
done

git pull --ff-only origin main
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
test -z "$(git status --porcelain)"
vsa_revision=$(git rev-parse HEAD)
vsa_tag="$(git rev-parse --short=12 HEAD)-$vsa_run"
declare -p vsa_revision vsa_tag >> "$vsa_record/operator-context.bash"
printf 'commit=%s\nimage_tag=%s\n' "$vsa_revision" "$vsa_tag" | tee "$vsa_record/release.txt"
```

若 Git 有本地改动、分叉或上述检查中断，保留现有工作和容器，交给仓库维护人处理。再次构建前确认 `release.txt` 中的 commit 是目标版本。

下面两条构建命令在云端服务器本机执行，以部署机 CPU 架构构建并加载到本机 Docker。每次运行使用同一组新标签。依赖沿用仓库的 `requirements.txt` 和 `package-lock.json`，Dockerfile 基础镜像按 [构建镜像锁文件](../deploy/images.lock.json) 固定。

```bash
case "$(uname -m)" in
  x86_64) vsa_platform=linux/amd64 ;;
  aarch64|arm64) vsa_platform=linux/arm64 ;;
  *) printf '此手册未给出该 CPU 架构的构建配置。\n' >&2; exit 1 ;;
esac
sudo docker buildx build --platform "$vsa_platform" --load \
  --label "org.opencontainers.image.revision=$vsa_revision" \
  --tag "voice-service-agent-api:$vsa_tag" -f deploy/Dockerfile.api .
sudo docker buildx build --platform "$vsa_platform" --load \
  --label "org.opencontainers.image.revision=$vsa_revision" \
  --tag "voice-service-agent-web:$vsa_tag" -f deploy/Dockerfile.web .
for vsa_service in api web; do
  vsa_image="voice-service-agent-$vsa_service:$vsa_tag"
  test "$(sudo docker image inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$vsa_image")" = "$vsa_revision"
  sudo docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}} {{json .RepoTags}}' "$vsa_image"
done | tee "$vsa_record/application-images.txt"
```

预期看到 API、Web 各一条本地镜像 ID，架构和提交版本与本次相符。API 镜像同时用于迁移。构建失败先检查磁盘、镜像仓库、pip/npm 网络。受管 HTTPS 代理需要额外 CA 时，由管理员提供公用 CA 证书路径，在两条构建命令中加入 `--secret id=proxy_ca,src=/绝对路径/ca-bundle.crt`，保持 TLS 验证。

## 4. 申请并检查 TLS 证书

默认用 Certbot 为**同一个域名**申请 Let's Encrypt 证书。门户在 8087 上用 HTTPS，LiveKit 在 7880 上用 WSS，两个入口共用证书。升级时复用当前有效证书。证书文件保持 root 管理，通过 Compose 只读挂载。

使用 Let's Encrypt 时执行以下签发块。已有单位签发的证书时，把变量 `vsa_cert`、`vsa_key` 设为实际证书链和私钥的绝对路径，直接执行本小节后面的核对块。

```bash
vsa_cert="/etc/letsencrypt/live/$vsa_domain/fullchain.pem"
vsa_key="/etc/letsencrypt/live/$vsa_domain/privkey.pem"

if ! sudo openssl x509 -in "$vsa_cert" -checkend 604800 -noout 2>/dev/null; then
  if ! command -v certbot >/dev/null; then
    sudo apt-get update
    sudo apt-get install -y certbot
  fi
  sudo ss -ltnp 'sport = :80'
  sudo certbot certonly --standalone --non-interactive --agree-tos --keep-until-expiring \
    --email "$vsa_acme_email" --cert-name "$vsa_domain" -d "$vsa_domain"
fi
```

首次申请时 `ss` 预期只显示表头，域名的公网 80 端口须能到达服务器。如果已有服务占用 80，先交给负责该服务的管理员安排签发，已有业务继续运行。签发操作与文件定义见 [Certbot 操作手册](https://eff-certbot.readthedocs.io/en/stable/using.html#standalone)。

无论证书由哪方签发，都执行下面的核对块。证书文件须包含站点证书和中间证书。私钥为与证书匹配的 PEM。

```bash
sudo openssl verify -purpose sslserver -verify_hostname "$vsa_domain" \
  -untrusted "$vsa_cert" "$vsa_cert"
sudo openssl x509 -in "$vsa_cert" -noout -subject -issuer -dates -checkend 604800
vsa_certificate_pubkey=$(sudo openssl x509 -in "$vsa_cert" -noout -pubkey)
vsa_tls_pubkey=$(sudo openssl pkey -in "$vsa_key" -passin pass: -pubout)
test -n "$vsa_certificate_pubkey"
test "$vsa_certificate_pubkey" = "$vsa_tls_pubkey"
declare -p vsa_cert vsa_key >> "$vsa_record/operator-context.bash"
```

证书检查应显示 `OK`、有效日期和未临近过期。使用组织内部 CA 时，请组织管理员确认服务器、API 镜像和访问浏览器都信任该 CA。证书更新后的生效步骤见第 9 步。

## 5. 填写配置，拉取依赖镜像

下面的命令编辑本次预备配置 `next.env`，第 7 步才安装为正式 `.env`。首次部署生成本机 PostgreSQL、Redis、LiveKit 的独立随机凭据。升级从已有 `.env` 复制数据库和 LiveKit 凭据，再自动更新本次镜像标签、Web 和 LiveKit 入口、证书路径与媒体 IP。

```bash
vsa_env_set() {
  local vsa_env_tmp
  vsa_env_tmp=$(mktemp "$vsa_record/next.env.XXXXXX")
  awk -v key="$1" 'index($0, key "=") != 1' "$vsa_next_env" > "$vsa_env_tmp"
  printf '%s=%s\n' "$1" "$2" >> "$vsa_env_tmp"
  mv "$vsa_env_tmp" "$vsa_next_env"
}

if [ ! -e "$vsa_next_env" ]; then
  if [ -f .env ]; then
    install -m 0600 .env "$vsa_next_env"
  else
    if sudo docker volume inspect voice-service-agent_postgres-data >/dev/null 2>&1; then
      printf '已有 PostgreSQL 数据卷，请先恢复当前部署的原 .env。\n' >&2
      exit 1
    fi
    install -m 0600 .env.example "$vsa_next_env"
    for vsa_key_name in POSTGRES_PASSWORD REDIS_PASSWORD LIVEKIT_API_KEY LIVEKIT_API_SECRET; do
      vsa_password=$(openssl rand -hex 32)
      vsa_env_set "$vsa_key_name" "$vsa_password"
    done
    unset vsa_password
  fi
fi
vsa_env_set IMAGE_TAG "$vsa_tag"
vsa_env_set PUBLIC_ORIGIN "https://$vsa_domain:8087"
vsa_env_set LIVEKIT_URL "wss://$vsa_domain:7880"
vsa_env_set LIVEKIT_NODE_IP "$vsa_node_ip"
vsa_env_set WEB_TLS_CERT_FILE "$vsa_cert"
vsa_env_set WEB_TLS_KEY_FILE "$vsa_key"
nano "$vsa_next_env"
```

在 nano 用 `Ctrl+W` 查找字段，修改等号右侧，缺少的字段加在文件末尾。每个字段只留一行 `KEY=值`，值两侧不加引号或空格。已有服务配置按下表核对后保留。按 `Ctrl+O`、回车保存，按 `Ctrl+X` 退出。

| 要填的字段 | 在等号后填写 |
| --- | --- |
| `CUEKB_BASE_URL` | 集成方给出的 CueKB 根地址，例如 `https://kb.example.com`。路径由程序追加 |
| `CUEKB_API_KEY`、`KNOWLEDGE_BASE_IDS` | 同一授权范围的检索 Key 和真实 KB UUID。多个 UUID 用英文逗号分隔。替换模板里的示例 UUID |
| `WEATHER_BASE_URL`、`WEATHER_API_KEY` | 和风天气控制台的专属 HTTPS API Host 和 Key，Host 后不用加 API 路径 |
| `WEB_SEARCH_API_KEY` | Tavily Search Key |
| `SOPHON_LIVE_WS_URL`、`SOPHON_LIVE_API_KEY` | Sophon 运维提供的完整 `wss://实际域名/v1/live/sessions`，有实际端口时一并填写。上游允许匿名时 Key 可留空 |
| `DELEGATION_ROUTER_BASE_URL`、`DELEGATION_ROUTER_API_KEY` | 百炼工作空间的 HTTPS OpenAI compatible base URL 和 Key。通常以 `/compatible-mode/v1` 结尾，程序追加 `/chat/completions` |
| `SOPHON_LIVE_LOCALE`、`DELEGATION_ROUTER_MODEL` | 默认中文是 `zh-CN`，已接入模型是 `qwen3.7-flash` |
| `VOICE_PROVIDER`、`DELEGATION_ROUTER_PROVIDER` | 默认分别为 `sophon_live`、`openai_compatible` |
| `ENABLED_TOOLS` | `search_knowledge,weather,web_search` |
| `AGENT_PROVIDER`、`QA_EXECUTION_MODE`、`QA_ANSWER_POLICY` | 分别为 `disabled`、`dual_tools`、`general_qa` |
| `WEATHER_PROVIDER`、`WEATHER_MODE` | 分别为 `qweather`、`real` |

其余预算和兼容字段沿用 [.env.example](../.env.example)。这条发布路径使用三类真实服务。展示用身份由 Compose 设为 `validation`，通话按临时身份隔离。管理员已有运维 Key 时可保留 `TOOLS_ADMIN_API_KEY`，不要发给门户用户。

执行下面一块。它先检查预备配置并拉取依赖，再用刚构建的 API 镜像在临时容器里校验配置。旧容器继续运行。

```bash
awk -F= '
  /^[A-Z0-9_]+=/ {
    if (seen[$1]++) { print "字段重复: " $1 > "/dev/stderr"; bad=1 }
    if ($0 ~ /REPLACE_|example[.]com/ ||
        $0 == "KNOWLEDGE_BASE_IDS=11111111-1111-4111-8111-111111111111") {
      print "还需替换: " $1 > "/dev/stderr"
      bad=1
    }
  }
  END {exit bad}
' "$vsa_next_env"
test "$(sed -n 's/^VOICE_PROVIDER=//p' "$vsa_next_env")" = sophon_live
test "$(sed -n 's/^IMAGE_TAG=//p' "$vsa_next_env")" = "$vsa_tag"

vsa_compose() {
  local vsa_env_file=$1 vsa_compose_file=$2
  shift 2
  sudo env DEPLOY_ENV_FILE="$vsa_env_file" \
    docker compose --project-name voice-service-agent --profile sophon \
    --env-file "$vsa_env_file" -f "$vsa_compose_file" "$@"
}
dc() {
  vsa_compose "$vsa_dir/.env" "$vsa_dir/deploy/compose.production.yaml" "$@"
}
vsa_next_dc() {
  vsa_compose "$vsa_next_env" "$vsa_dir/deploy/compose.production.yaml" "$@"
}
vsa_next_dc config --quiet
vsa_next_dc pull postgres redis livekit
vsa_next_dc config --images | sort -u | while IFS= read -r vsa_image; do
  sudo docker image inspect --format '{{.Id}} {{json .RepoTags}} {{json .RepoDigests}}' "$vsa_image"
done | tee "$vsa_record/resolved-images.txt"
vsa_next_dc run --rm --no-deps --pull never -T api python - <<'PYTHON'
from app.config import Settings
from pydantic import ValidationError

try:
    settings = Settings()
    settings.validate_deployment()
    checks = {
        "LIVEKIT_URL": settings.livekit_enabled,
        "DELEGATION_ROUTER_PROVIDER": settings.delegation_router_provider == "openai_compatible",
        "ENABLED_TOOLS": settings.enabled_tool_names == {"search_knowledge", "weather", "web_search"},
    }
    for field, ready in checks.items():
        if not ready:
            raise SystemExit("请检查预备配置的 " + field)
except ValidationError as error:
    for item in error.errors(include_input=False):
        location = ".".join(map(str, item["loc"])) or "配置"
        print(f"{location}: {item['msg']}")
    raise SystemExit(1)
except ValueError as error:
    raise SystemExit(str(error))
print("API 配置检查通过")
PYTHON
```

预期 PostgreSQL `postgres:17.6-alpine`、Redis `redis:7-alpine`、LiveKit `livekit/livekit-server:v1.13.11` 拉取成功，镜像记录包含两张应用镜像，末行显示“API 配置检查通过”。三项 Compose 依赖按版本标签拉取。标签可能对应更新后的内容，以本次记录中的实际镜像 ID 和仓库摘要追溯。LiveKit 和 Redis 同一 Compose，共用 Redis，应用用 DB0，LiveKit 用 DB1。

## 6. 停止接新通话，备份旧库

提前通知用户维护窗口，让用户点 **End call** 结束现有通话。本步起已有站点会暂时停止访问。首次安装仍执行本块，看到“首次部署”即可继续。已有数据库时，原 PostgreSQL 容器应在运行。旧 API 配有管理员 Key 时，命令会停止接收新通话并等待活动通话结清，然后停旧 API/Web，保持 PostgreSQL 可用于备份。

```bash
vsa_previous_dc() {
  vsa_compose "$vsa_record/previous/.env" "$vsa_record/previous/compose.yaml" "$@"
}

if sudo docker volume inspect voice-service-agent_postgres-data >/dev/null 2>&1; then
  test -s "$vsa_record/previous/.env"
  test -s "$vsa_record/previous/compose.yaml"
  test -n "$(vsa_previous_dc ps --status running -q postgres)"
  if [ -n "$(vsa_previous_dc ps --status running -q api)" ]; then
    vsa_previous_dc exec -T api python - <<'PYTHON'
import json
import os
import time
from urllib.request import Request, urlopen

key = os.environ.get("TOOLS_ADMIN_API_KEY")
if not key:
    print("当前未配置管理员 Key，按已公告的维护窗口关闭旧服务。")
    raise SystemExit(0)

base = "http://127.0.0.1:8000/api/v1/admin/"
headers = {"Authorization": "Bearer " + key}
with urlopen(Request(base + "drain", headers=headers, method="POST"), timeout=5) as result:
    assert json.load(result)["status"] == "draining"
print("旧版已停止接收新通话，等待活动通话与查询结清。", flush=True)
for _ in range(36):
    with urlopen(Request(base + "services", headers=headers), timeout=5) as result:
        status = json.load(result)
    if status["active_voice_sessions"] == 0 and status["active_business_runs"] == 0:
        break
    time.sleep(5)
else:
    print("已到等待上限，按维护窗口关闭旧服务。")
PYTHON
  fi
  vsa_previous_dc stop -t 30 web api
  vsa_previous_dc exec -T postgres psql -X -v ON_ERROR_STOP=1 -U voice_service \
    -d voice_service -Atc 'SELECT version_num FROM alembic_version ORDER BY 1' \
    > "$vsa_record/previous/alembic.txt"
  test -s "$vsa_record/previous/alembic.txt"
  vsa_previous_dc exec -T postgres pg_dump -U voice_service -d voice_service -Fc \
    > "$vsa_record/previous/postgres.dump"
  test -s "$vsa_record/previous/postgres.dump"
  vsa_previous_dc exec -T postgres pg_restore --list \
    < "$vsa_record/previous/postgres.dump" > "$vsa_record/previous/postgres-contents.txt"
  ls -lh "$vsa_record/previous/postgres.dump"
else
  printf '首次部署，没有旧 PostgreSQL 数据卷。\n'
fi
```

预期看到非空备份文件，且 `pg_restore --list` 成功。它检查备份目录可读，实际恢复另由管理员验证。如果停机或备份失败，先按第 9 步恢复可用服务，未完成备份时停止迁移。保留数据库卷和发布记录，备份及其中的 `.env` 由管理员私密保管。

## 7. 迁移数据库并启动新版本

在当前服务器仓库目录执行现有启动入口。`sudo` 使其可以读取 Certbot 的私钥路径并连接 Docker。

```bash
cd "$vsa_dir"
install -m 0600 "$vsa_next_env" "$vsa_dir/.env"
sudo ./scripts/deploy-cloud.sh "$vsa_dir/.env" 2>&1 | tee "$vsa_record/deploy.log"
dc ps -a
dc exec -T api alembic -c apps/api/alembic.ini current
```

脚本依次核对配置、等待 PostgreSQL/Redis/LiveKit 健康、用新 API 镜像执行一次 `alembic upgrade head`、启动 API、检查就绪和三类配置，再重建 Web。迁移是一条执行完退出的命令，其容器会清理掉。预期其余五个服务均为 healthy，数据库显示迁移 `head`，当前仓库为 `0011`。出现迁移失败或返回非零时按第 9 步处理。

## 8. 打开 Web 并核对三个查询

先在服务器终端检查实际域名。三个命令都需要 TLS 验证通过并返回 HTTP 2xx，第二条预期有 `"status":"ready"`。

```bash
curl -fsS --max-time 15 "https://$vsa_domain:8087/health/live"
curl -fsS --max-time 15 "https://$vsa_domain:8087/health/ready"
curl -fsS --max-time 15 "https://$vsa_domain:7880/"
printf '\n请在客户端浏览器打开 https://%s:8087/\n' "$vsa_domain"
```

在平时访问服务的电脑上打开 Chrome/Edge，输入上面最后一行给出的 URL，例如 **`https://voice.example.com:8087/`**。浏览器应直接打开门户，无需处理证书警告。`wss://voice.example.com:7880` 是浏览器内部使用的信令地址。

进入页面后选择 **Microphone**，点 **Start call**，弹出权限提示时点允许，等到 **Voice ready**。按 [上线时的浏览器检查](validation.md#上线时的浏览器检查) 中的固定顺序测试普通问候、天气、通用知识及专业知识库。完成后点 **End call**。如果任一项失败，按下面的故障表记录是哪一步失败并排查。发布执行记录只回传提交、实际入口、服务状态和每项结果，不附凭据、私钥或内部资料。

## 9. 失败处理、回滚和证书续期

### 9.1 常见故障

| 看到的现象 | 下一步操作 |
| --- | --- |
| pull/build 报镜像下载失败、空间不足 | 在停机前检查仓库/代理访问和 `df -h /var/lib/docker`，修正后重跑第 3 或第 5 步。保留旧容器、卷和回滚镜像 |
| 启动脚本报缺少 KEY 或配置错误 | 重新登录后按本节命令恢复变量，编辑本次 `next.env`，运行 `vsa_next_dc config --quiet` 后重跑第 7 步 |
| 迁移失败 | 保存第 7 步的迁移报错和第 6 步的备份路径，停在当前步，交管理员核对数据库版本和权限 |
| 8087/7880 报证书错误 | 对照第 4 步检查域名和文件可读性，确认 `PUBLIC_ORIGIN` 和 `LIVEKIT_URL` 使用该域名，修正后重新执行第 7 步 |
| 门户打不开，健康失败 | 先执行下方日志块，再从客户端检查 DNS、8087/TCP 安全组和证书链 |
| 门户可以打开，但连不上或没有双向音频 | 检查系统麦克风权限和音频输出，再核对 7880/TCP、7881/TCP、7882/UDP、对外 IP 和 NAT 原端口映射 |
| 可以通话，但某类查询没有结果或来源 | 记下发生时间、测试种类、问句和页面状态。检查第 5 步对应的服务配置、Key 范围和 API 日志，再按验证手册回传 |
| 首次启动失败，没有旧版本 | 保留新建卷和本次记录，修正报错后从第 7 步重试 |

```bash
dc ps -a
dc logs --tail=100 web api livekit postgres redis
```

日志可能有业务内容，仅在运维范围内查看和脱敏。Bash 退出或 SSH 断开时，重新进入服务器，输入 `bash`，再执行下列恢复命令。输入第 3 步打印的实际记录目录。然后复制第 5 步的 `vsa_compose()`、`dc()`、`vsa_next_dc()` 三个定义，从失败的小节继续。`operator-context.bash` 只存本次操作变量，由第 3、4 步写入。

```bash
set -euo pipefail
umask 077
read -r -p '本次发布记录的绝对路径: ' vsa_record
source "$vsa_record/operator-context.bash"
cd "$vsa_dir"
```

第 3 步若在生成 `release.txt` 之前中断，先处理 Git 或备份报错，再重做第 3 步。已生成版本号后，使用同一记录和镜像标签继续本次发布。

### 9.2 回滚到更新前的应用

仅在有同拓扑的上一版镜像与配置时执行。若已经执行第 7 步，必须有已备份的旧迁移版本，且当前版本仍与它相同。版本不同时，由管理员用备份在隔离库核验恢复，再决定回滚。改配置或换镜像不会撤销已执行的迁移。

重新登录时先按 9.1 恢复本次操作变量及函数。下面命令恢复第 3 步记录的实际镜像，并将服务配置还原。此回滚保持同一个 PostgreSQL/Redis 卷。

```bash
test -s "$vsa_record/previous/compose.yaml"
test -s "$vsa_record/previous/.env"
test "$(wc -l < "$vsa_record/previous/images.txt" | tr -d ' ')" = 5
vsa_rollback_dc() {
  vsa_compose "$vsa_record/previous/.env" "$vsa_record/previous/compose.yaml" \
    -f "$vsa_record/previous/images.yaml" "$@"
}
vsa_rollback_dc config --quiet
if [ -e "$vsa_record/deploy.log" ]; then
  test -s "$vsa_record/previous/postgres.dump"
  vsa_rollback_dc exec -T postgres psql -X -v ON_ERROR_STOP=1 -U voice_service \
    -d voice_service -Atc 'SELECT version_num FROM alembic_version ORDER BY 1' \
    > "$vsa_record/alembic-before-rollback.txt"
  cmp "$vsa_record/previous/alembic.txt" "$vsa_record/alembic-before-rollback.txt"
fi
vsa_rollback_dc stop -t 30 web api
vsa_rollback_dc up -d --no-build --pull never --wait --wait-timeout 180 postgres redis livekit
vsa_rollback_dc up -d --no-build --pull never --no-deps --wait --wait-timeout 180 api
vsa_rollback_dc exec -T api python /app/scripts/verify_deployment.py \
  --base-url http://127.0.0.1:8000 --expect-autonomous-voice
vsa_rollback_dc up -d --no-build --pull never --no-deps --force-recreate --wait --wait-timeout 60 web
install -m 0600 "$vsa_record/previous/.env" "$vsa_dir/.env"
vsa_rollback_dc ps -a
```

旧版恢复后按第 8 步用**原域名**重查，日常容器命令使用回滚时的 `vsa_rollback_dc`，直到按第 3 至 8 步发布成功。发布和回滚均应保留数据卷。数据库恢复、密码轮换和依赖大版本升级安排单独的维护操作。

### 9.3 续期证书并让 Web 使用新证书

Certbot 用户按证书到期日安排续期，80/TCP 和域名仍须可达。用第 5 步的 `dc()` 操作已部署的服务。正常版本更新已在第 7 步重建 Web。单独续期时，在维护窗口执行下方命令。即使系统定时任务已更新文件，只要当前 Web 仍提供旧证书，也要执行重建。

```bash
sudo certbot renew --cert-name "$vsa_domain" --dry-run
sudo certbot renew --cert-name "$vsa_domain"
dc up -d --no-build --pull never --no-deps --force-recreate --wait --wait-timeout 60 web
curl -fsS --max-time 15 "https://$vsa_domain:8087/health/live"
curl -fsS --max-time 15 "https://$vsa_domain:7880/"
```

浏览器刷新后检查两个端口的证书有效期。组织证书由签发方续期，先按第 4 步核对新证书，再用相同重建命令让 Web 重挂载证书。续期流程参见 [Certbot Renewal](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)。

## 10. 发布包和后端选择

同机在线发布直接使用第 3、5 步已加载的镜像。需要交给离线服务器时，在完成第 5 步的联网构建机执行下列命令，并提供对应提交的仓库文件。发布包包含应用和三张依赖镜像。安全传输包和校验文件，环境配置和证书在目标机单独准备。

```bash
(
  cd "$vsa_record"
  sudo docker image save \
    "voice-service-agent-api:$vsa_tag" "voice-service-agent-web:$vsa_tag" \
    postgres:17.6-alpine redis:7-alpine livekit/livekit-server:v1.13.11 \
    > "voice-service-agent-$vsa_tag.tar"
  sha256sum "voice-service-agent-$vsa_tag.tar" > "voice-service-agent-$vsa_tag.tar.sha256"
)
```

目标机在包目录设置 `vsa_tag` 为包文件名里的实际标签，执行：

```bash
sha256sum -c "voice-service-agent-$vsa_tag.tar.sha256"
sudo docker image load -i "voice-service-agent-$vsa_tag.tar"
```

加载后用目标机的真实配置运行第 5 步配置检查，`vsa_tag` 采用包的标签。离线包替代第 3 步镜像制作和第 5 步在线拉取，仍依次准备证书、备份、迁移、启动和从浏览器核对。

`VOICE_PROVIDER` 是唯一后端选项。本手册默认 `sophon_live`。需选 NVIDIA 时使用 `nvidia` 并填写 `VOICECHAT_WS_URL`、`VOICECHAT_API_KEY`，沿用同一发布入口，它会停掉本项目的 LiveKit。现有兼容路径的能力见 [架构](architecture.md#2-数据流与代码入口)。当前生产容器验收运行时的交付缺口及已执行验证分别见 [实施方案](implementation-plan.md) 和 [验证记录](validation.md)。
