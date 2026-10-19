# 部署与运行

> 本页维护一期原生 Function Calling 的配置、构建、迁移及运行操作。VoiceChat 决定直接回答或调用唯一工具 search_knowledge_base，不配置额外答案模型；服务端仍通过 CueKB 执行检索。真实语音验收见 [单工具验证](native-cuekb-validation.md)。

## 部署方式边界

本阶段只有一套原生 Function Calling 验证配置：`.env.example` 是唯一模板，`deploy/compose.production.yaml` 是唯一 Compose 拓扑。VoiceChat、CueKB 独立部署；本项目提供门户、原生工具注册、鉴权、Knowledge Adapter 和结果交付。VoiceChat 自主直接回答或调用工具，不增加 ASR/TTS、意图分类器或额外答案模型。

## 问答模式与预算（Q07）

一期配置：

```dotenv
AGENT_PROVIDER=disabled
QA_EXECUTION_MODE=dual_tools
QA_ANSWER_POLICY=general_qa
QA_TOOLSET_VERSION=cuekb-native-v1
QA_EXTERNAL_FALLBACK_ENABLED=false
QA_MAX_RETRIEVAL_CALLS=1
AGENT_DEADLINE_MS=60000
QA_PROVIDER_ANSWER_TIMEOUT_MS=30000
```

不需要 AGENT_MODEL、OPENAI_API_KEY 或 AGENT_BASE_URL。disabled 表示关闭独立文本答案模型，不能用 mock 代替。`/health/ready` 允许 text_configured=false；页面关闭文字提交，但仍提供麦克风和 WAV 语音通话。

`dual_tools` 为兼容模式名称；一期实际只注册 `search_knowledge_base(query)`，目录和可信 implementation 使用同一个名称。后台仍通过 Registry 的内部 `search_knowledge` 调用 CueKB `/v1/search`，不要把原生名称填进 ENABLED_TOOLS。KB UUID、凭据、访问范围和可信过滤条件只在服务端设置。VoiceChat 原生调用决定是否执行业务，最终转写用于关联和审计，不自动触发查询。

| 配置 | 一期值 / 用途 |
| --- | --- |
| VOICECHAT_WS_URL / VOICECHAT_API_KEY | 现有 VoiceChat 完整 /v1/realtime 地址及服务凭据 |
| CUEKB_BASE_URL / CUEKB_API_KEY | 独立 CueKB 基地址和授权检索凭据 |
| KNOWLEDGE_BASE_IDS | 服务端允许的真实 KB UUID；不下放给模型 |
| CUEKB_TOP_K | 默认 5；原生语音检索数量及证据项上限，仍受字节预算限制 |
| QA_DIRECT_MAX_HITS | 默认 3；仅旧 direct 执行器使用，一期 native CueKB 不使用 |
| QA_DIRECT_EVIDENCE_MAX_BYTES | 默认 6000；完整 ASCII 证据包上限 |
| VOICE_MAX_RETRIEVALS_PER_INPUT | 默认 2，可设 1；一期 native relay 同一输入的检索上限，关闭该路径 HTTP 自动重试。相同参数不重复查询，终态后再次调用关闭异常连接；不是回答成功标志 |
| AGENT_DEADLINE_MS | 示例 60000；检索执行期限，一期 relay 的模型续答不共用该期限 |
| QA_PROVIDER_ANSWER_TIMEOUT_MS | 示例 30000；仅旧工作流使用，一期 relay 按连接生命周期观察回答 |

### 更新已有工具目录

工具目录首次保存后持久化在数据库，改 YAML 不会覆盖旧的 lookup/reason 配置。部署脚本在 Alembic 0010 之后、API 启动之前，运行：

```sh
python /app/scripts/configure_native_cuekb.py --apply
```

该命令使用既有 CAS/revision 和 AdminAudit，把当前原生目录替换为仓库的一期单工具目录；不删除历史 Turn/证据。重复执行不产生无意义 revision。省略 --apply 只预览名称和 revision，不写数据库，也不运行应用恢复或模型。正常部署无需管理员 API Key。

手工 docker 部署在迁移完成后，先用新 API 镜像的相同数据库环境运行这条命令，再启动 API。已有旧目录且 AGENT_PROVIDER=disabled 时，API 会明确提示先应用目录，而不是静默注册双工具。

### 启动及新会话检查

同步 [完整 VoiceChat 模板](voicechat/docker-run.md)，重新创建 VoiceChat 容器。新模板包含 search_knowledge_base(query) 单调用示例及会话策略优先/禁止重复并行/只按授权结果发声约束；不修改模型权重、融合参数或引入其他语音模型。

部署脚本会检查实际 capabilities。手工部署完成后直接运行统一入口；它同时检查 readiness、capabilities、镜像材料和固定四用例，不再分别执行多个验证脚本：

```sh
API_CONTAINER=$(docker compose --env-file .env \
  -f deploy/compose.production.yaml ps -q api)
docker exec "$API_CONTAINER" \
  python /app/scripts/run_native_cuekb_validation.py
```

必须看到 `preflight=passed provider=nvidia mode=dual_tools policy=general_qa tools=search_knowledge_base cases=4`，随后根据四条 case 和最终 `fault/focus` 判断。完整输出契约见 [原生单工具验证](native-cuekb-validation.md#2-从宿主机运行一次验证)。

**结束旧通话并新建 Conversation。** 模式/策略在创建时快照，旧 knowledge_required 会话不会因重连或刷新页面变成 general_qa。

现场录音、连续追问、失败归类和输出听音步骤见 [原生单工具验证](native-cuekb-validation.md)。固定脚本、case 配置和授权 WAV 已打入 API 镜像的只读 `/app`，宿主机只用 `docker exec` 启动验证；无需创建输入目录、复制测试文件或依赖宿主机 Python。JSON 报告和回答 WAV 才写入 `/tmp`，且默认控制台只输出总状态和每个 case 的核心字段。knowledge_required、双工具及外置 SDK 的兼容实现保留，但不属于本期部署或自动 fallback。

## 镜像构建与部署

本项目发布 API/Web。针对用户确认的 speech 基线，语音还需更新独立 VoiceChat 的 WebSocket 文件与 Jinja 启动配置；推理模型/权重、GPU 参数和离线转换流程不变。API/Web 同标签发布；VoiceChat 记录独立镜像 digest 和 `audio_server.py` SHA-256。只更新 API/Web 不能修复整场 response_id 或上游模板冲突；只更新 VoiceChat 也不会修复门户 Stop playback 后永久静音。按下列顺序联动发布：

1. 在独立 speech 源码应用并核对 [D19 补丁](../deploy/voicechat/README.md)，运行其 CPU 协议测试，以现场已验证的离线镜像为基础，按补丁说明中的 Dockerfile 只覆盖 `/s2s/audio_server.py` 和已有模板，并设置 `USE_JINJA_TEMPLATE_PROMPT=1`。不得把 speech 加入本项目 requirements 或 Compose。
2. 在有 GPU 的部署环境核对最终提示词、两步推理设置与工具等待预算，启动新 VoiceChat，记录镜像/source hash；执行两轮原生工具、音频 done 和短尾帧验证。
3. 独立构建本项目 API/Web，按下述流程迁移和启动；清理浏览器旧页面缓存后重新 Start call。实际页面若通过 `9002` 暴露，须核对 HTTPS origin、证书和同源 `/api/` 路由，不直接修改规范端口为 9002。
4. 按 D07-C/D 复测真实英文输入、知识结果和实际扬声器输出，以及快速/延迟工具和停止后下一轮。记录失败，不能凭包数或 ready 放行。

独立 VoiceChat 的补丁只修复 WebSocket/PCM 边界，提示词使用现有模板配置，不更新模型权重。多轮音频在同一模型批次中必须有可确定的 80 ms 帧边界；非默认大批次或修改后的 codec 需重新验协议，不能沿用测试结论。

API/Web 镜像在构建机独立构建，同一发布使用唯一标签；云端 Compose 只消费预构建镜像，迁移复用 API 镜像：

```sh
release_tag=$(git rev-parse --short=12 HEAD)
docker build -f deploy/Dockerfile.api -t "voice-service-agent-api:$release_tag" .
docker build -f deploy/Dockerfile.web -t "voice-service-agent-web:$release_tag" .
```

构建使用 BuildKit。在需要代理 CA 的云环境中，将含系统根证书和代理 CA 的可信 bundle 作为临时 secret 传入两次构建：

```sh
docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -f deploy/Dockerfile.api -t "voice-service-agent-api:$release_tag" .
docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -f deploy/Dockerfile.web -t "voice-service-agent-web:$release_tag" .
```

该 secret 只在依赖安装的 RUN 中可见；pip 使用 PIP_CERT，npm 使用 NODE_EXTRA_CA_CERTS 且保持 strict-ssl=true，不写入镜像。没有自定义代理 CA 时省略 --secret；不要关闭 TLS 校验。API 构建还会规范应用源码、配置和迁移文件的读取/目录访问权限，防止构建机的 600/700 权限导致镜像内 UID 10001 无法启动；这些文件保持 root 所有，服务用户没有写权限。

API 镜像只使用 Python 基础镜像自带的 `pip`，按 `requirements.txt` 中的精确版本安装生产依赖。仓库不使用额外的 Python 包管理器或独立锁文件；`requirements-dev.txt` 引用相同生产依赖并追加测试/Lint 工具。修改依赖时直接更新这两份 requirements，并在全新 Python 3.12 虚拟环境中完成安装和回归：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --disable-pip-version-check -r requirements-dev.txt
```

Docker 先复制 `requirements.txt` 再复制应用源码，因此在同一个保留缓存的 builder 上，仅修改业务代码、配置或镜像标签不会重新安装依赖；安装步骤应显示 `CACHED`。API 的 pip 下载/构建缓存另存于 BuildKit cache mount，不进入最终镜像。依赖清单变化而安装层重跑时，可以复用该 builder 已缓存的兼容包；仍需解析依赖并安装，新包和未缓存的索引响应仍可能访问网络。

排查每次下载时，用 `docker build --progress=plain -f deploy/Dockerfile.api -t "voice-service-agent-api:$release_tag" .` 查看依赖层是否命中。日常构建不使用 `--no-cache`，不在构建间执行 `docker builder prune`，并复用同一个 builder；换机器、重建临时 builder 或缓存被回收后仍是冷构建。`--no-cache` 会强制重跑安装层，但不会自行清空 cache mount，两者不能混为一谈。需要代理 CA 时继续传入前述 secret。

当前 slim 镜像没有安装编译工具链或 VoiceChat/GPU 模型。换成完整版 Python 系统镜像不会预装本项目锁定的 FastAPI、Agents SDK、数据库驱动等 Python 包，反而增加基础镜像拉取量。若构建机经常更换，应在构建基础设施中导入/导出 BuildKit 层缓存，或维护按 Python/平台/requirements 版本发布的依赖基础镜像；仅依赖本地 pip cache mount 不能跨机器复用，普通层缓存导出也不自动携带 cache mount 内容。

把相同标签的镜像和仓库发布目录送到部署机，填写 `.env` 的单个 `IMAGE_TAG`，然后部署：

```sh
cp .env.example .env
# 填写全部真实值
chmod 600 .env
./scripts/deploy-cloud.sh .env
```

脚本检查必需值、HTTPS 公网入口、TLS 文件、本地镜像和 Compose 配置，等待 PostgreSQL/Redis 健康，用 API 镜像执行 Alembic，再启动 API、核验容器内 `/health/ready`，最后启动 Web。配置错误会在 API 启动时失败；镜像不会由部署脚本构建或自动拉取。readiness 只证明容器和启用工具的配置就绪，不能证明 CueKB、VoiceChat、文字答案或英语口述质量。当前依赖镜像固定为 `postgres:17.6-alpine` 和 `redis:7-alpine` 对应 digest；已有 PostgreSQL 数据卷在更换镜像前须备份并验证目标版本兼容，不把切换标签视为无风险降级。

Web 的容器内健康探针用 wget -Y off 直接访问 127.0.0.1，避免继承代理导致误报不健康。探针对本机 IP 不核对证书；正式 HTTPS 客户端仍须验证证书链与域名，不能沿用该探针作为 TLS 验收证据。

```sh
docker compose --env-file .env -f deploy/compose.production.yaml ps
docker compose --env-file .env -f deploy/compose.production.yaml logs --tail=200 api web
```

`AGENT_MODEL` 是 BusinessRuntime 后台文本 Agent 调用的模型标识，用于理解任务、选择 `search_knowledge` 并组织有依据的答案；它不是 VoiceChat 的实时语音模型。`AGENT_PROVIDER=openai` 时使用官方端点，无需 `AGENT_BASE_URL`；只有接 OpenAI-compatible 服务时才改为 `compatible` 并填写该 HTTP/HTTPS 基地址，两者不需要同时独立部署。`CUEKB_BASE_URL` 是独立 CueKB HTTP/HTTPS 基地址，应用附加 `/v1/search`；HTTP 仅用于已隔离、受控的内部网络。`VOICECHAT_WS_URL` 是 API 到独立语音服务的 WS/WSS endpoint，不由门户域名推断；WS 仅用于同主机或受控隔离内网，且对应端口不得公网暴露。浏览器公网入口仍强制 HTTPS/WSS。本期固定只开放 `search_knowledge`，无需天气配置。数据库/Redis 凭据使用 URL 安全字符，容器内连接串由 Compose 构造。

本期部署直接开放已配置的英文全双工链路，不再用人工填写的“已验证”布尔值阻止启动。部署后按下节以固定 VoiceChat API 版本、镜像 digest 和实际会话快照运行探针，人工复核输出音频，再通过门户验证实际录音→CueKB→口述。探针报告负责记录证据，运行配置只负责连接服务；真实失败不回退 mock。Web Nginx 直接终止 HTTPS，并处理 SSE buffering、WS upgrade、请求大小和安全头；不得记录凭据或语音票据。

## VoiceChat 诊断探针与部署产物核验

源码依据及证据边界见 [NVIDIA 核对记录](voicechat-upstream-analysis.md)。先记录实际镜像 digest、挂载源码和模板 hash；以下脚本不会远程证明版本，不自动开启能力或部署服务。

本期真实 CueKB 验收使用 [API 容器单入口流程](native-cuekb-validation.md#2-从宿主机运行一次验证)。以下是额外的合成结果协议探针，同样从宿主机通过 `docker exec` 在 API 容器内运行，不要求宿主机 Python。脚本和固定 WAV 已包含在 API 镜像中；多工具 `session.update` 直接由容器内实际持久化目录生成，不再从宿主机复制脚本、管理员 JSON 或 WAV：

```sh
API_CONTAINER=$(docker compose --env-file .env \
  -f deploy/compose.production.yaml ps -q api)
test -n "$API_CONTAINER"

docker exec -i \
  -e PYTHONPATH=/app/apps/api:/app \
  "$API_CONTAINER" python - <<'PYTHON'
import asyncio
import json
from pathlib import Path

import httpx
from app.agent_runtime.runtime import BusinessRuntime
from app.config import Settings
from app.storage.store import Store
from app.tools.registry import ToolRegistry
from app.voice.provider import session_update

async def main():
    settings = Settings()
    store = Store(settings.database_url)
    try:
        async with httpx.AsyncClient() as client:
            runtime = BusinessRuntime(settings, ToolRegistry(settings, client, store))
            snapshot = await runtime.catalog.snapshot()
            update = session_update(
                tools=snapshot.definitions,
                answer_policy=settings.answer_policy,
            )
            target = Path("/tmp/voicechat-probe/session-update.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(update, ensure_ascii=True))
            print("snapshot=passed tools=" + ",".join(snapshot.tools))
    finally:
        await store.engine.dispose()

asyncio.run(main())
PYTHON

docker exec \
  -w /app \
  -e PYTHONPATH=/app/apps/api:/app \
  "$API_CONTAINER" \
  python scripts/probe_voicechat.py \
    --wav /app/validation/native-cuekb/black_pig_crisp_heart.wav \
    --session-update /tmp/voicechat-probe/session-update.json \
    --api-version ACTUAL_API_REVISION --image-digest ACTUAL_IMAGE_DIGEST \
    --tool-delay-seconds 5 --tool-wait-timeout 20 --post-result-seconds 20 \
    --output-wav /tmp/voicechat-probe/authorized-response.wav \
    --output /tmp/voicechat-probe/voicechat-probe.json
```

API 容器读取自身已有的 `VOICECHAT_WS_URL`/凭据，不把 Key 放入命令或报告。首条命令应只输出 `snapshot=passed tools=search_knowledge_base`；探针随后只回传以下三行，不提供完整报告：

```text
probe_status=... phase=... reason=... connected=... tools=...
checks={...}
audio_output_samples=... pending_calls=... timeline_dropped=... output_wav_truncated=...
```

完整 JSON 保存在 `/tmp/voicechat-probe/voicechat-probe.json`。只有这三行不足以定位时才加 `--print-report`。legacy 可不传快照；dual_tools 缺快照直接失败，避免误测 bridge。需要插话协议观察时使用镜像内另一条明确授权 WAV；报告只说明输入时序，不能证明新问题影响了模型回答。

探针在等待工具和回填时持续送静音，默认再观察 20 秒续答，时长参数有上限。合成工具结果不经过 CueKB/业务授权，不是端到端验收。报告 `partial` 表示只完成部分协议观察，不能改写为模型通过；检查 `reason`、`phase`、`pending_call_ids_at_close`、`timeline_dropped` 和 `output_wav_truncated`。输入样本 hash 包含最后一帧补零，静音单独计数；报告不保存转写或工具参数正文。原探针在这些等待阶段停止送音，其“无工具”结果需重测。

静态检查脚本已随 VoiceChat 派生镜像写入只读 `/opt/voice-validation`。直接用 `docker exec` 检查该容器实际挂载的模型产物；替换为容器内实际转换后 vLLM LLM 路径，每个分片重复传 `--checkpoint`：

```sh
VOICE_CONTAINER=nemotron-labs-voicechat
docker exec "$VOICE_CONTAINER" \
  python /opt/voice-validation/inspect_voicechat_artifacts.py \
  --config /actual/converted-llm/config.json \
  --tokenizer /actual/converted-llm/tokenizer.json \
  --checkpoint /actual/converted-llm/model.safetensors \
  --voicechat-config /actual/voicechat-checkpoint/config.json \
  --output /tmp/voicechat-artifacts.json
```

默认只回传以下三行，完整 JSON 保存在 `/tmp/voicechat-artifacts.json`：

```text
artifact_checks={...}
fusion_status=... paper_weight_alignment=...
checkpoint_count=... missing_required_outputs=[]
```

正常结果中 `artifact_checks` 的必需项应为 `passed`，且 `missing_required_outputs=[]`；只有三行互相矛盾时才加 `--print-report`。该脚本无需 GPU/NeMo，只读配置和 safetensors header。可选 `--voicechat-config` 必须是含 `model.stt.model` 的原始 VoiceChat 配置，不是转换后的 vLLM config；它将声明值/公开 wrapper 缺省值与论文 1/1/2 融合权重比较，返回 `matches/differs/unverified`，不会自动改配置。官方固定 HF 配置本轮为 matches，不能据此推断现场运行配置；见 [论文 v2 核对](voicechat-paper-v2-review.md#6-本轮落地验证与未完成项)。`missing/mismatch/unverified` 定位提供文件的契约差异；`passed` 也不证明权重正确加载、实际 tokenizer 编码、RNNT 或 function 推理正确。它不修改模型，头部 hash 不代表权重内容 hash。真实定位仍需同 sequence 的输入帧、RNNT raw token 和 runtime function key/token 证据。

`function_outputs_contract` 仅检查当前 backend 必需的 `text_logits`、`function_tokens`；`function_output_declarations` 保存实际声明及缺失的必需字段。可选的 `function_logits` 单独报告为 `function_scores_declared`，缺失只代表没有声明诊断分数，不能据此判定原生工具不可用。早期脚本将三者合并，只有前两项的现场配置会被误报为运行契约 mismatch；不要为消除该旧告警直接修改模型配置。即使声明了分数，仍需验证运行时实际返回。

## 约 3 秒查询的分段复测

分段日志与复测步骤统一见 [故障排查 §2–3](operations/troubleshooting.md#2-延迟分解与可观测边界)；超时配置不是已测时延。

## 副本、容量与故障恢复

默认每个应用容器一个 Uvicorn worker。多副本部署必须由负载均衡提供浏览器粘性路由，覆盖 HTTPS/SSE/WSS（包括 ticket 申请及后续握手）；不能简单使用无粘性的多 worker 参数。

Redis 持有每个 conversation 的独占租约，15 秒 TTL、4 秒续约。未触碰的空闲租约 60 秒后释放。新 owner 取得租约后，先在 PostgreSQL 行事务递增 epoch、取消旧轮次，才接受新输出；旧 worker 在接收、提交以及单写入器前检查租约与 epoch。其他 worker 上的请求返回 `SESSION_OWNED_BY_OTHER`，不迁移隐藏音频状态。

`MAX_VOICE_SESSIONS`、`MAX_AGENT_RUNS` 是**每副本**的容量边界，按总供应商配额在各副本之间划分预算；不能每副本都配置全量云端额度。票据预留占用语音容量，60 秒过期释放。取消中的本地业务 task 仍计入任务容量，防止反复替换绕过上限。

已测试租约命令契约与新 owner 的数据库 epoch fence；真实 Redis 租约失效、网络分区、PostgreSQL 行锁与负载均衡粘性切换仍待部署集成验证，不保证生产 HA 已通过。

## 升级、drain、回滚

1. 备份数据库及 API/Web/VoiceChat 镜像与配置版本，保存三者匹配关系和源码 hash，检查 Alembic 迁移。
2. 对将升级的副本调用管理员 `POST /api/v1/admin/drain`。readiness 返回不可用，拒绝新业务/语音；活跃业务在预算内结束，语音在应用会话上限内结束。
3. 停止进程前给活跃会话留出窗口。SIGTERM 的 Uvicorn graceful timeout 为 20 秒，容器 stop grace 30 秒；到期关闭连接，客户端需重新开始，不自动重放录音。
4. 单独执行 `alembic upgrade head`，再启动新副本。标准 `./scripts/deploy-cloud.sh .env` 在 PG/Redis 就绪后运行 Compose 的 migrate 服务，再启动 API/Web；仅推送 Git 代码不会修改部署数据库。0010 是本项目数据库表结构迁移版本，新增全局工具目录表并保留现有业务记录。
5. 回滚按已记录的 API/Web/VoiceChat 组合执行；恢复已知整场 response 缺陷的旧 VoiceChat 时语音仍不能放行。数据库回滚与镜像回滚分开；当前迁移到 **0010**：0005 为每通话 capability，0006 为输入关联，0007 为问答策略/执行字段、Utterance 和 DeliveryAttempt，0008 为来源上下文/任务快照，0009 为交付/口述/播放审计，0010 为全局工具目录。旧数据迁移默认 legacy/knowledge_required，nullable 字段不伪造已验证关联或已播放，旧 slots 不伪造确认来源。0010 downgrade 删除工具目录配置，0009 downgrade 删除新增审计字段，均不是无损回滚；需要破坏式回滚时用已验证备份恢复。

当前没有等待期语音的部署开关：NVIDIA ProviderCapabilities 全部保持 false。门户进度可用；未来只有可信 Adapter 对四项真实能力完成验证后才启用工具的 operation 扩展，见 [Live §3](live-agent-implementation.md#3-等待进度与自然修订)。正常上游关闭、发送完成和播放排空估计分开记录；真实 PG/Redis、长会话轮换/断线仍须 D07-D/E 验证。

健康接口：`/health/live` 为应用存活；`/health/ready` 检查数据库、归属协调和 drain，分别返回文字配置、语音配置和本地容量。管理页健康端点探测只说明可达，不冒充真实推理/工具调用成功。

## D07 分阶段执行设计

D07-A–F 顺序、V01–V12 与 Q07 验收矩阵统一见 [真实服务验收](development/validation.md)。本页只维护运行配置和部署操作；执行结果写验收报告，不把 readiness 当作真实知识/音频闭环通过。
