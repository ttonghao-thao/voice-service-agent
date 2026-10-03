# 部署与运行

> 更新 2026-10-02：正文描述当前代码的运行方式，不代表真实服务已经验收；末节 Q07 是可选外置 LLM 的待编码部署规格。本期 Compose 使用每次通话独立的临时 capability、真实 CueKB、`search_knowledge` 和 NVIDIA VoiceChat，不需要天气代理配置。真实验收仍须按 [任务板](TASK_BOARD.md) D07 执行。最终系统边界见 [架构](architecture.md)。

## 部署方式边界

本阶段只有一套功能验证部署配置：`.env.example` 是唯一模板，实际 `.env` 不提交；`deploy/compose.production.yaml` 是唯一 Compose 拓扑，无需逐项修改 Compose。PostgreSQL、Redis、迁移、API、Web 是当前业务链路的必要容器；VoiceChat、CueKB 仍独立部署。应用代码保留显式注入的 fixture 设置供自动化测试使用；正常 API 启动会执行严格部署校验，拒绝 fixture 身份、mock、自动建表，以及缺少真实文本模型、CueKB 或 NVIDIA VoiceChat WS/WSS 配置的部署。

门户只用于受控测试，可从公网直接访问 `https://<域名>:8087`。Nginx 已打包在 Web 镜像中，直接终止 TLS 并提供静态门户，无需外层反向代理。它在容器内监听 `0.0.0.0:8087`，Compose 同端口公开映射；`PUBLIC_ORIGIN` 必须与浏览器实际使用的 HTTPS origin 完全一致。证书和私钥通过只读挂载提供，证书链、域名匹配、浏览器信任及麦克风授权必须在 D07 实机验证。

API 容器内部监听 `0.0.0.0:8000`，不映射宿主端口。浏览器只从 Web 同源 `/api/` 调用，Web 容器内的 Nginx 将请求转到 Compose `app` 网络的 `api:8000`。PostgreSQL、Redis 也只在容器网络中；公网仅开放 Web 的 `8087`。独立 API 客户端与正式系统间鉴权不属于本期核心验证。

编码机没有 Docker 或真实 CueKB/VoiceChat 接口；本地只运行契约、静态与夹具测试。镜像/容器、PostgreSQL/Redis、真实供应商和浏览器验收归 D07，不能以本地测试或 `/health/ready` 冒充通过。

## 配置与独立测试通话

复制 `.env.example` 为 `.env`；模板中的 `voice.example.com`、`cuekb.example.com`、证书路径、镜像标签和 KB UUID 仅演示填写格式。替换示例值和所有 `REPLACE_` 值，并将 `WEB_TLS_CERT_FILE`、`WEB_TLS_KEY_FILE` 指向部署机上的可读绝对路径。Compose 固定 `AUTH_MODE=validation`、`CUEKB_MODE=real`、`ENABLED_TOOLS=search_knowledge`、`VOICE_PROVIDER=nvidia`；容量、语言和检索条数沿用代码默认值；整轮业务预算由 `AGENT_DEADLINE_MS` 显式配置，默认 30000 毫秒。不需要 tenant、账号 JSON、密码哈希、Cookie 密钥、能力开关、服务 revision、健康地址、OIDC、`APP_ENV` 或第二份 env 文件。

门户首次加载不创建身份或读取历史。测试人员在当前标签页点击 “Start call” 时，API 创建新的 conversation、随机 owner 和高熵 `call_access_token`；token 只保存在该标签页 JavaScript 内存中，HTTPS/SSE 请求以 Bearer 发送，WSS 使用与该 owner/conversation/epoch 绑定的一次性 ticket。其它标签页不会得到该 token，不能读取或控制本次通话；结束通话撤销 token。`KNOWLEDGE_BASE_IDS` 仍由服务端配置并对所有测试通话统一生效，浏览器和模型不能扩大范围，管理 API 仍拒绝匿名 call capability。这不是正式客户认证方案。

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

API 镜像只使用 Python 基础镜像自带的 `pip`，按 `requirements.txt` 中的精确版本安装生产依赖。仓库不使用额外的 Python 包管理器或独立锁文件；`requirements-dev.txt` 引用相同生产依赖并追加测试/Lint 工具。修改依赖时直接更新这两份 requirements，并在全新 Python 3.12 虚拟环境中完成安装和回归：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --disable-pip-version-check -r requirements-dev.txt
```

Docker 先复制 `requirements.txt` 再复制应用源码，因此仅修改业务代码不会重新安装依赖。

把相同标签的镜像和仓库发布目录送到部署机，填写 `.env` 的单个 `IMAGE_TAG`，然后部署：

```sh
cp .env.example .env
# 填写全部真实值
chmod 600 .env
./scripts/deploy-cloud.sh .env
```

脚本检查必需值、HTTPS 公网入口、TLS 文件、本地镜像和 Compose 配置，等待 PostgreSQL/Redis 健康，用 API 镜像执行 Alembic，再启动 API、核验容器内 `/health/ready`，最后启动 Web。配置错误会在 API 启动时失败；镜像不会由部署脚本构建或自动拉取。readiness 只证明容器和启用工具的配置就绪，不能证明 CueKB、VoiceChat、文字答案或英语口述质量。当前依赖镜像固定为 `postgres:17.6-alpine` 和 `redis:7-alpine` 对应 digest；已有 PostgreSQL 数据卷在更换镜像前须备份并验证目标版本兼容，不把切换标签视为无风险降级。

```sh
docker compose --env-file .env -f deploy/compose.production.yaml ps
docker compose --env-file .env -f deploy/compose.production.yaml logs --tail=200 api web
```

`AGENT_MODEL` 是 BusinessRuntime 后台文本 Agent 调用的模型标识，用于理解任务、选择 `search_knowledge` 并组织有依据的答案；它不是 VoiceChat 的实时语音模型。`AGENT_PROVIDER=openai` 时使用官方端点，无需 `AGENT_BASE_URL`；只有接 OpenAI-compatible 服务时才改为 `compatible` 并填写该 HTTP/HTTPS 基地址，两者不需要同时独立部署。`CUEKB_BASE_URL` 是独立 CueKB HTTP/HTTPS 基地址，应用附加 `/v1/search`；HTTP 仅用于已隔离、受控的内部网络。`VOICECHAT_WS_URL` 是 API 到独立语音服务的 WS/WSS endpoint，不由门户域名推断；WS 仅用于同主机或受控隔离内网，且对应端口不得公网暴露。浏览器公网入口仍强制 HTTPS/WSS。本期固定只开放 `search_knowledge`，无需天气配置。数据库/Redis 凭据使用 URL 安全字符，容器内连接串由 Compose 构造。

本期部署直接开放已配置的英文全双工链路，不再用人工填写的“已验证”布尔值阻止启动。部署后用固定 VoiceChat API 版本和镜像 digest 运行 `scripts/probe_voicechat.py --api-version ... --image-digest ... --wav ...`，人工复核输出音频，再通过门户验证实际录音→CueKB→口述。探针报告负责记录证据，运行配置只负责连接服务；真实失败不回退 mock。Web Nginx 直接终止 HTTPS，并处理 SSE buffering、WS upgrade、请求大小和安全头；不得记录凭据或语音票据。

## 约 3 秒查询的分段复测

代码成本与字段定义见 [查询延迟](integration.md#41-查询延迟与优化边界)。发布后使用同一组英文问题，分别记录文字“提交→完整答案”和语音“说完→最终答案开始口述”（不计 ACK），各自统计冷启动和热请求。用同一 turn 关联日志，不把不同请求的时间相加：

```sh
docker compose --env-file .env -f deploy/compose.production.yaml logs --no-color api \
  | rg 'agent_model_call_finished|cuekb_response_validated|tool_run_finished|agent_pipeline_finished|answer_delivery_finished'
```

时间包含关系：Agent 内含模型和工具，工具内含 CueKB，提交 total 内含 Agent；不能把所有 duration 相加。浏览器通过 Network 的 SSE/HTTP 时间线补足提交后的网络和渲染。此前 300 ms 轮询改为同进程提交通知，跨进程补查保留；final 已含答案时不再等待 messages GET。部署 Nginx 已关闭 SSE 缓冲，额外的 9002 代理仍需现场核对。没有真实分段日志前，不给各阶段虚构占比，不承诺优化后的实际总耗时。

## 副本、容量与故障恢复

默认每个应用容器一个 Uvicorn worker。多副本部署必须由负载均衡提供浏览器粘性路由，覆盖 HTTPS/SSE/WSS（包括 ticket 申请及后续握手）；不能简单使用无粘性的多 worker 参数。

Redis 持有每个 conversation 的独占租约，15 秒 TTL、4 秒续约。未触碰的空闲租约 60 秒后释放。新 owner 取得租约后，先在 PostgreSQL 行事务递增 epoch、取消旧轮次，才接受新输出；旧 worker 在接收、提交以及单写入器前检查租约与 epoch。其他 worker 上的请求返回 `SESSION_OWNED_BY_OTHER`，不迁移隐藏音频状态。

`MAX_VOICE_SESSIONS`、`MAX_AGENT_RUNS` 是**每副本**的容量边界，按总供应商配额在各副本之间划分预算；不能每副本都配置全量云端额度。票据预留占用语音容量，60 秒过期释放。取消中的本地业务 task 仍计入任务容量，防止反复替换绕过上限。

已测试租约命令契约与新 owner 的数据库 epoch fence；真实 Redis 租约失效、网络分区、PostgreSQL 行锁与负载均衡粘性切换仍待部署集成验证，不保证生产 HA 已通过。

## 升级、drain、回滚

1. 备份数据库及 API/Web/VoiceChat 镜像与配置版本，保存三者匹配关系和源码 hash，检查 Alembic 迁移。
2. 对将升级的副本调用管理员 `POST /api/v1/admin/drain`。readiness 返回不可用，拒绝新业务/语音；活跃业务在预算内结束，语音在应用会话上限内结束。
3. 停止进程前给活跃会话留出窗口。SIGTERM 的 Uvicorn graceful timeout 为 20 秒，容器 stop grace 30 秒；到期关闭连接，客户端需重新开始，不自动重放录音。
4. 单独执行 `alembic upgrade head`，再启动新副本。启动不替代迁移。
5. 回滚按已记录的 API/Web/VoiceChat 组合执行；恢复已知整场 response 缺陷的旧 VoiceChat 时语音仍不能放行。数据库回滚与镜像回滚分开；当前迁移已包含 0005 每通话 capability；本次 D19 不新增数据库迁移。`downgrade` 会删除对应表/字段，不应作为无损回滚手段；需要破坏式数据库回滚时使用已验证备份恢复流程。

健康接口：`/health/live` 为应用存活；`/health/ready` 检查数据库、归属协调和 drain，分别返回文字配置、语音配置和本地容量。管理页健康端点探测只说明可达，不冒充真实推理/工具调用成功。

## D07 分阶段执行设计

状态：真实端到端仍待执行。现场日志/录像已确认存在收发流量和用户转写，但没有形成放行证据；D19 修复后必须复测。以下工作在后续具备真实服务的云端 Docker 验收环境执行，不是本次文档整理或编码阶段的运行指令。沿用上述构建、Compose 与回滚流程，不新增部署系统。各场景的唯一验收定义见 [V01–V12](acceptance-report.md#4-最终方案验收清单)。

| 阶段 | 执行方案 | 退出条件与证据 |
| --- | --- | --- |
| D07-A 基线与环境 | 固定应用 commit、API/Web 镜像、VoiceChat API/digest、CueKB 服务版本、文本模型与脱敏配置摘要；准备验证 KB 和授权英文样本。按唯一流程独立构建镜像，执行迁移、健康和恢复预检，验证公网 HTTPS `8087`、证书链及目标浏览器麦克风权限 | 记录 API/Web 与 VoiceChat 的匹配版本、配置和迁移结果；`verify_deployment.py` 确认文字、语音和知识工具均已配置；readiness 只作为入口条件 |
| D07-B 真实文字与范围 | 用多个独立 call capability 验证相互隔离、KB 范围不可由请求覆盖、管理 API 被拒绝，再跑真实文本模型 → CueKB M3 的支持/澄清/冲突/故障场景 | V02–V04、V10 的文字部分具备 trace、引用版本、状态和权限证据；失败不得归类为空命中 |
| D07-C 英文基础语音 | 在隔离的云端验收部署固定供应商版本，先确认静音不创建整场 response、每轮 done 与后续新 ID、ACK 不耗尽最终答案许可，再用授权录音执行协议探针并人工听音，随后验证门户 → VoiceChat → 本项目 → 真实 CueKB → 实际口述 | V01/V03/V07/V09 有录音授权、事件、实际回答和人工判定；探针的合成工具结果不充当知识闭环证据 |
| D07-D 竞态与恢复 | 工具等待 5 秒时分别附和、新问、改问、取消、停止播报；在结果写回及播报边界断网；测试超过两分钟及多次轮换 | V05/V06/V08 留下旧 revision 拒绝、pending call 结清或关闭、新连接无旧音频的证据；增强能力不通过则只评估 basic |
| D07-E 故障与容量 | 从单副本开始，测真实 PG 事务/迁移、Redis 租约丢失、进程退出、drain 和备份恢复；逐档增加会话与任务并发。多副本仅在验证粘性路由后测试 | V11/V12 与延迟分解、错误率、资源峰值；先测基线再冻结阈值，以独立样本复测，不能把副本预算当全局预算 |
| D07-F 放行 | 汇总版本与所有适用 V 项，核对缺测/失败/不适用；按已验证版本设置能力声明，保留回滚版本与操作记录 | 基础语音必需项均有证据，增强声明另有 V05 门槛；失败修复后复测，剩余边界明确记录 |

运行配置不承载验收结论。Compose 直接启用 NVIDIA VoiceChat，以便快速暴露真实协议与音频问题；版本、事件和听音结论写入探针报告及验收记录。`/health/ready` 只证明配置和容器就绪，不能代替协议探针或完整知识闭环。

每阶段保存：执行时间、操作者、应用/供应商版本、case/V ID、输入来源、预期/实际结果、失败原因、脱敏 trace 和受控证据位置。录音/票据/真实配置不提交仓库；仓库验收记录只写结论与受控证据引用。未执行标未执行；不适用须按本期范围解释，不能用来跳过基础语音的安全与恢复项。

若仅 enhanced 交互门槛失败，保留 basic 明确打断/重连能力；若权限、旧结果泄漏或实际口述事实错误等核心项失败，不放行语音。可继续提供已验收的文字服务，但不能把文字放行写成 D07 语音完成。

## Q07：可选外置 LLM 的启动与发布（设计，未实现）

当前脚本仍强制文本模型，本节是后续编码规格，不能据此直接删掉当前生产配置。唯一全局选择是 AGENT_PROVIDER，组合校验和装配见 [架构 §11.2](architecture.md#112-全局配置和启动装配)。不增加第二份 env/Compose、不热更新、不根据请求或健康探测切换。

实施后的 `.env.example` 第 3 节默认提供：

```dotenv
AGENT_PROVIDER=none
AGENT_MODEL=
OPENAI_API_KEY=
AGENT_BASE_URL=
AGENT_DEADLINE_MS=30000
```

缺省 provider 且其余模型连接项全部空也表示 direct。保留注释中的两份完整 external 示例：openai + 实际 MODEL/KEY + 空 BASE_URL；compatible + 实际 MODEL/KEY + HTTP/HTTPS `/v1` 基地址。provider=none 时残留 model/key/base URL 必须清空，否则启动报错，不静默忽略凭据。原显式 openai/compatible 配置继续是 external，不因升级被关闭。只有样例增加 AGENT_BASE_URL 的空实值行，因此同步 test_deployment.py 的键集合断言；其余部署项仍按原模板真实填写。

scripts/deploy-cloud.sh 必须在 Compose 启动前按同一矩阵校验：共同必需项不再含 AGENT_MODEL/OPENAI_API_KEY；provider 缺省/空按 none；none 要求三项均空；openai 要求 MODEL/KEY，BASE_URL 可空或为合法 HTTP/HTTPS 基地址（默认示例留空）；compatible 要求三项齐全；mock/其他值拒绝。所有值采用去首尾空白规则，不能出现脚本放行/API 拒绝的常见组合。脚本不联网检查模型可用性、不修改环境文件、不回退模式；保留 TLS、origin、本地镜像、PG/Redis、迁移和单 Compose 流程。shell env 读取仍不 source/执行用户文件；对不支持的带引号值明确报错，模板使用既有未加引号格式。

生产 Settings.validate_deployment 的文本门禁改为已解析 external 才检查必需模型配置；direct 要求 real CueKB、NVIDIA VoiceChat，数据库/Redis/TLS/无 mock 的门禁保持。Settings.mock 不因 provider=none 报 mock，显式 fixture mock 仍拒绝。direct 不创建或探测外置客户端；external 初始化失败则启动失败，网络运行失败按原错误终止本轮。

`/health/ready` 增 execution_mode、external_llm_enabled、text_available，同时保留 status/text_configured/voice_configured/is_mock/enabled_tools/capacity 字段。direct 正常为 ready/direct/false/false，text_configured=false、voice_configured=true；external real 正常为 ready/external/true/true，两个 configured=true。正常 ready 只表明当前模式所需配置与本地基础设施就绪，仍不证明 VoiceChat/CueKB 连通、Nano 口述或模型推理通过。draining 或 DB/Redis 故障保持 503；必需配置错误在监听前失败。

`verify_deployment.py` 从本次部署 Settings 启动解析得到预期 mode，传入 validate_ready(payload, expected_tools, expected_mode)：同时要求报告 mode 与预期一致、is_mock=false、voice_configured=true、工具集合匹配；direct 要求 external_llm_enabled=false/text_available=false/text_configured=false，external 三者为 true。禁止只要接口声称 ready 就接受；报告记录 mode 和未验证边界。测试显式覆盖 none 正常、残缺 external、误报 mode、缺字段、fixture 和两个 provider。

修改模式的操作流程：同一环境配置更新 → 构建/提供匹配 API/Web 镜像 → drain 全部旧副本并结束旧通话 → 执行 Alembic 0007 → 用同一配置重建全部 API/Web → 逐副本核对 readiness 的 mode → 新通话验证。env 改变不能只 restart 旧容器期待 env_file 重新加载；按既有 compose up 重建配置。speech 仍使用既有镜像/模型仓库及补丁，Q07 不需要重新转换权重或修改它的 GPU 参数。旧通话不跨模式续接。

D07-B/C/D 必须分别报告 direct 和 external。direct 接受“没有外置模型服务和 Key”的部署，并通过网络审计/计数证明本项目零外置生成请求；这不代表 CueKB 内部 embedding/rerank 无模型。external 按现有工具规划和有据答案验收。两个模式共同验证英文口述、授权、旧结果隔离、停止与恢复，工具期间自然插话保持单独限制说明。与用户约定阈值前不写延迟 SLA，报告相同样本的 p50/p95 与失败率。
