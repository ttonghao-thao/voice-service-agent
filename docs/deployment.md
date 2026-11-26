# 云端容器部署手册

默认部署 Sophon Live。用 `ssh deploy@203.0.113.10` 登录实际云服务器，后续命令在服务器终端逐条执行，遇到报错先停在当前步骤。服务器需有仓库读取权限、Docker Engine、Compose 2.20 或更新版本以及 Buildx。缺少 Docker 时按 [Ubuntu 安装命令](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository) 安装。

## 第一步：获取仓库代码

首次部署运行：

```sh
git clone --branch main git@github.com:ttonghao-thao/voice-service-agent.git
cd voice-service-agent
```

升级时进入已有仓库，确认在 `main` 分支且 `git status --short` 无输出。先保存当前配置，再更新代码。记下备份目录，第三步会用到。

```sh
vsa_backup="$(mktemp -d "$HOME/voice-service-backup.XXXXXXXX")"
install -m 600 .env "$vsa_backup/.env"
cp deploy/compose.production.yaml "$vsa_backup/compose.yaml"
printf '%s\n' "$vsa_backup"
git pull --ff-only origin main
```

## 第二步：准备配置并制作镜像

**首次使用域名时**，在 DNS 管理页将域名 A 记录指向服务器公网 IPv4，在云安全组开放 `8087/TCP`、`7880/TCP`、`7881/TCP` 和 `7882/UDP`。门户和 LiveKit 共用证书。已有有效证书可保留。需要申请证书时开放公网 `80/TCP` 并确保未被占用，把下面的示例域名换成实际域名。

```sh
getent ahostsv4 voice.example.com
sudo apt-get update
sudo apt-get install -y certbot
sudo certbot certonly --standalone --cert-name voice.example.com -d voice.example.com
```

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
| `PUBLIC_ORIGIN`、`LIVEKIT_URL` | 实际域名，例如 `https://voice.example.com:8087` 和 `wss://voice.example.com:7880` |
| `WEB_TLS_CERT_FILE`、`WEB_TLS_KEY_FILE` | 主机绝对路径。Certbot 默认为 `/etc/letsencrypt/live/voice.example.com/fullchain.pem` 和同目录的 `privkey.pem` |
| `LIVEKIT_NODE_IP` | 浏览器和 API 容器都能访问的服务器公网 IPv4 |
| `SOPHON_LIVE_WS_URL`、`SOPHON_LIVE_API_KEY` | Sophon 的完整 WSS 地址，含 `/v1/live/sessions`。上游允许匿名时 Key 可留空 |
| `DELEGATION_ROUTER_BASE_URL`、`DELEGATION_ROUTER_API_KEY` | Qwen 工作空间的 OpenAI 兼容 base URL（通常以 `/compatible-mode/v1` 结尾）及 Key |
| `CUEKB_BASE_URL`、`CUEKB_API_KEY`、`KNOWLEDGE_BASE_IDS` | CueKB 根地址、授权查询 Key 和实际知识库 UUID |
| `WEATHER_BASE_URL`、`WEATHER_API_KEY`、`WEB_SEARCH_API_KEY` | 和风天气专属 HTTPS API Host、和风 Key、Tavily Search Key |

其他默认值见 [配置模板](../.env.example)。Sophon 使用 `VOICE_PROVIDER=sophon_live`，启用天气、通用知识和专业知识库三类查询。检查模板占位值已换成实际配置。

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

换成实际域名执行：

```sh
curl -fsS --max-time 15 https://voice.example.com:8087/health/ready
curl -fsS --max-time 15 https://voice.example.com:7880/
```

第一条应返回 `"status":"ready"`，第二条应返回 HTTP 2xx。浏览器打开 **`https://voice.example.com:8087/`**，点 **Start call**，允许使用麦克风，依次核对天气、通用知识和专业知识库。问句和预期结果见 [浏览器检查](validation.md#上线时的浏览器检查)。

## 需要时执行

**查看启动日志**

```sh
sudo docker compose --env-file .env --profile sophon -f deploy/compose.production.yaml logs --tail=100 web api livekit postgres redis
```

**续期证书**。Certbot 成功续期后重建 Web，再执行第四步。

```sh
sudo certbot renew --cert-name voice.example.com &&
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

需要单独使用 NVIDIA 时，在 `.env` 选择 `VOICE_PROVIDER=nvidia` 并填写 `VOICECHAT_WS_URL` 和 `VOICECHAT_API_KEY`，使用相同启动命令。Sophon 的 LiveKit 检查适用于本文默认配置。
