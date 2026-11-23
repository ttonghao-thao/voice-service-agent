# 验证方法与执行记录

类型：唯一验证入口。先查 §1；本轮 Qwen 接入与用户授权的本机验证见 §8，复跑按 Provider 读 §2 或 §3。通用放行见 §4，其他能力待验项见 §6，旧开发/部署检查见 §7。§5 是已解决历史，仅追溯时读取。部署操作只看 [deployment.md](deployment.md)，未实施的验收容器看 [方案](implementation-plan.md)。

## 1. 当前结论与证据边界

产品同时提供天气、通用知识、专业知识库，通用知识受控兜底。b60028d 的两类历史委派故障已关闭，详见 §5。2026-11-23 用户确认接入 qwen3.7-flash 解析原生 task，并授权本机验证、本轮不接真实 CueKB；补充要求单选后端、默认 Sophon，本期仅验 Sophon。本轮数据在 §8，不修改历史测试日期，也不据此放行完整生产三类/容器部署。

| 检查 | 最近已执行事实 | 尚不能证明 |
| --- | --- | --- |
| 当前 Qwen 原生任务链 | 接入、官方录音重复、角色/同 Room 两问及回归结果见 §8 | 不等于真实 CueKB、完整口述或生产容器放行 |
| 专业知识库 | 问题/权限/失败有受控回归；本轮按用户要求排除真实服务 | 不以契约/部分服务代表真实 query/validate 或完整三类交付 |
| 天气/通用知识 | 真实 QWeather/Tavily 执行和回执，公开来源保留 | Melbourne 同名地点仅澄清；§6 的语音型号保真缺口仍需独立验收 |
| 角色/ASR | 名字、快照、隔离、五种 ASR 状态的业务独立性有证据 | 完整 Persona 遵循、真实 ASR 开关对照、人耳听音 |
| 容器 | LiveKit/Redis 同 Compose、三类/模型模板和启动检查已更新 | 本轮本机例外不替代镜像构建、生产 WSS/媒体/NAT 或容器验收单入口 |

原生触发和 Router 解析分别验收，已关闭的录音故障不继续列作当前待修。Gateway 回执、音轨/字幕、播放排空、实际听到、问题覆盖和事实正确分别记录。既有 Sophon 记录的 final_answer_verified 均为 false；未知或观察不足的样本不算通过。

## 2. Sophon 执行入口

### Sophon Live 验证

生产在部署机项目根目录操作，先按 deployment.md 部署同一 Compose 和三类依赖。API 内单次检查以下 LiveKit WSS/TLS/API 凭据及 Sophon 会话控制，不发送录音、不验证媒体/NAT、原生任务或听音；禁止打印完整 compose config，其中包含凭据：

```bash
export DEPLOY_ENV_FILE="$PWD/.env"
export COMPOSE_PROFILES=sophon # 本节只验证已选择的 Sophon
dc() { docker compose --env-file "$DEPLOY_ENV_FILE" -f deploy/compose.production.yaml "$@"; }
dc ps
dc exec -T api python /app/scripts/verify_deployment.py \
  --base-url http://127.0.0.1:8000 --expect-autonomous-voice
dc exec -T api python - <<'PYTHON'
import asyncio
import json
from app.config import Settings
from app.voice.sophon_live import SophonLiveAdapter
from livekit import api

async def run():
    settings = Settings()
    assert settings.voice_provider == "sophon_live"
    async with api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key.get_secret_value(),
        settings.livekit_api_secret.get_secret_value(),
    ) as livekit:
        await livekit.room.list_rooms(api.ListRoomsRequest())
    provider = SophonLiveAdapter(settings)
    try:
        await provider.connect()
        started = bool(provider.session_id)
    finally:
        closed = await provider.close()
    print(json.dumps({"livekit_api_authenticated": True, "started": started, "close_confirmed": closed}))

asyncio.run(run())
PYTHON
```

需要浏览器/ffmpeg/固定官方材料的完整生产验收容器与宿主单入口**尚未交付**，不把下面开发命令冒充已有容器能力。新入口须在容器运行、宿主只编排/回传，自动按窗口关联 API 日志且输出有界摘要，见实施方案。独立 Sophon 的启动权重/引擎/ASR 配置需部署方核对；session.started 回显 gpt-live-1 不能确认私有 32B-4bit 的实际身份。

### LiveKit 门户与工具验收

以下是开发复现入口，需 Web 开发依赖、Chromium、ffmpeg，以及对应 locale 的真实 API/Web/LiveKit。默认生产验收仍在容器执行；本轮用户明确允许本机且排除真实 CueKB，范围见 §8。不得用纯音/替身引擎替代真实录音。固定官方源，不升级 HEAD：

```bash
SOPHON_OFFICIAL_DIR=$(mktemp -d /tmp/sophon-official-XXXXXX)
GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/adventists-ai/sophon-realtime.git "$SOPHON_OFFICIAL_DIR/source"
git -C "$SOPHON_OFFICIAL_DIR/source" fetch --depth 1 origin 6b1713518eb75f8495415662870f682357372d09
git -C "$SOPHON_OFFICIAL_DIR/source" checkout --detach FETCH_HEAD
git -C "$SOPHON_OFFICIAL_DIR/source" sparse-checkout set live
node scripts/validate_sophon_web.mjs \
  --cases "$SOPHON_OFFICIAL_DIR/source/live/cases/cases.json" \
  --source-revision 6b1713518eb75f8495415662870f682357372d09 \
  --language zh --base-url http://localhost:5173 \
  --server-log /absolute/path/to/current-api-zh.log \
  --output "$SOPHON_OFFICIAL_DIR/report-zh"
# 在 en-US 的真实实例上，以它自己的日志运行 --language en，输出至 report-en。
# 仅在 Gateway 的 ASR 配置获确认时增加 --asr-state enabled 或 disabled。
# --profile-json /absolute/path/to/profile.json 可通过真实门户创建/选择角色；
# JSON 只含 name、description、language_style，不包含固定业务规则或凭据。
# 仅 localhost/127.0.0.1 的本机测试遇到 TUN/ICE 问题时可加 --local-ice；
# 该选项允许枚举本地 ICE 接口并在独立浏览器 context 授予 microphone 权限，
# 文件路径仍只发送所选官方 WAV，不打开/采集实际麦克风。参数写入 summary.json。
```

每次整组运行保存应用/source/WAV hash、原 delegation ID/task、route/parse_method、工具次数/status、business_outcomes、回执、ASR/候选语音和时间。asr_enabled 不能确认时为 null，字幕是否可见单列；Registry status=ok 只是调用正常，business_answered_cases 才区分可用结果/澄清，但依然不证明最终口述。

固定录音重复分别比较 ASR 开/关原生率和执行率，不要求逐字相同。补测同一合法 task 的 ASR 正常/迟到/空/错/关闭；角色 CRUD/跨用户/删除回退/新会话/重连，以及改角色后三类工具、缺地点、同名城市、超日期、未知/重复 ID、超时、隐私外发、快速追问和打断。原查询在普通发声时继续结清，旧结果不能注入新问题。需至少三十条有实际音频/结果的样本、两个以上真实会话及分层指标；纯文本规则不替代它们。

### LiveKit Sophon Phase 0

历史 Phase 0 验证了 24 kHz、client delegation、回传校验码和打断，但结果包含受控 Gateway/替身引擎与固定数据回填，不证明真实三类工具。相关源码入口是 scripts/probe_livekit_sophon.py、tests/fixtures/sophon_phase0_gateway.py；早期依赖冲突/临时虚拟环境和旧探针流程不再作为当前部署步骤，历史由 Git 追溯。固定上游参考：Sophon 6b1713518eb75f8495415662870f682357372d09；实际部署身份仍需现场核对。

## 3. NVIDIA 兼容容器验证

只验证仍维护的 NVIDIA Provider，不是仅知识库产品方案，也不能用来给 Sophon/三类查询放行。外部完整文件、GPU、卷、镜像、entrypoint 和只读 bind mount 按 [VoiceChat 运行手册](voicechat/docker-run.md) / manifest 配套，本文不重复整段 docker run。镜像变更须重建容器，不只 restart。

每窗口仅一个用例，无其他测试用户混入。先等待独立 VoiceChat 就绪，执行宿主单入口（Python/输入/采集均在容器）：

```bash
git rev-parse --short HEAD
bash scripts/validate_deployment.sh check
bash scripts/validate_deployment.sh auto core
```

check 应输出 checkout、preparation=passed 和实际容器/启动身份；脚本核对应用、工具、WAV、六个 VoiceChat 文件及诊断开关，拒绝 loopback 作为真实推理。version=failed/collection=failed 时停止回传，不自行改阈值或覆盖文件，磁盘 hash 匹配不证明进程已重载。

浏览器开启 Console Preserve log、过滤 voice_file，保持前台且声音可播放。每通话从 Start 起最多等待 120 秒，收集全部分段答案/来源；不要用追问补上首问失败后声称独立首问通过。

| 用例 | 开始采集 | 门户操作 / 必须观察 |
| --- | --- | --- |
| A 独立首问 | web-start golden_yolk | 原始 golden_yolk.wav 新 Start file call，保留全部分段 |
| B 同连接追问 | web-start black_pig_crisp_heart | 新通话 Black，回复后同连接 Send recording 发 Golden；未发完第二段如实记录，不重建连接伪装 |
| C 麦克风打断 | web-start greeting | 欢迎语中说 Hello, I need help with Golden Yolk.，再问 Can Golden Yolk be shipped? 并继续独立下一问，旧音频不得恢复 |
| D 查询/注入中补充 | web-start greeting | 先问保质期，在 checking/inject 中说 I have another question. I need to know what happens after opening the package and how it should be stored.；未命中注入时序只记未命中，不认证完整消费/回答 |

Golden 的运输/密封保质期/最佳期限/开封储存，Black 的价格/重量/层数/最佳食用均逐项对照来源或明确缺失；不跨产品、不把比例改成百分比，不用最后一段代替整通话。注入期应核对 backend.live_asr 的 observed_frames 与 llm_audio_frames，领先只表示显示 ASR；注入须 started → returned_idle → done(state=idle)、failed=0，finally 的 done 本身不算成功。分段读取 backend.inject_profile 的 tokens/completed/append_wait_ms，不能把全部等待归为检索。

开始命令示例（其余用例取上表第二列的参数）：

```bash
bash scripts/validate_deployment.sh web-start golden_yolk
```

每次先门户 End call，再执行以下同一结束/有界时间线入口后才开始下一用例。web_capture_already_started 表示上窗口未结清，不删除状态或覆盖；采集失败保留当前输出，不能读取上次成功结果。

```bash
bash <<'SH'
set -uo pipefail
umask 077
END_SUMMARY=$(mktemp /tmp/voice-current-end-XXXXXX.txt)
bash scripts/validate_deployment.sh web-end >"$END_SUMMARY" 2>&1
END_STATUS=$?
cat "$END_SUMMARY"
ARTIFACT_DIR=$(sed -n 's/^local_artifacts=\(.*\) raw_logs=not_printed$/\1/p' "$END_SUMMARY")
CASE_IDS=$(sed -n 's/^session={"conversation_id":"\([a-f0-9-]\{36\}\)".*/\1/p' "$END_SUMMARY" | sort -u)
if [[ -z "$ARTIFACT_DIR" || -z "$CASE_IDS" || ! -f "$ARTIFACT_DIR/api.log" || ! -f "$ARTIFACT_DIR/voicechat.log" ]]; then
  printf 'timeline=unavailable reason=NO_CURRENT_WINDOW_OR_SESSION\n'
  exit 1
fi
API_CONTAINER=$(docker compose --env-file .env -f deploy/compose.production.yaml ps -q api 2>"${END_SUMMARY}.setup")
if [[ -z "$API_CONTAINER" || "$API_CONTAINER" == *$'\n'* ]]; then
  printf 'timeline=unavailable reason=API_CONTAINER\n'
  exit 1
fi
TIMELINE_STATUS=0
while IFS= read -r CASE_ID; do
  cat "$ARTIFACT_DIR/api.log" "$ARTIFACT_DIR/voicechat.log" | docker exec -i "$API_CONTAINER" \
    python /app/scripts/summarize_voice_timeline.py --conversation-id "$CASE_ID" || TIMELINE_STATUS=1
done <<< "$CASE_IDS"
if [[ "$END_STATUS" != 0 || "$TIMELINE_STATUS" != 0 ]]; then exit 1; fi
SH
```

## 4. 分层放行与回传

| 层次 | 必须核对 |
| --- | --- |
| 身份/版本 | commit、image/digest、挂载/源码 hash、进程启动身份与环境；配置健康不等于功能通过 |
| 输入/原生触发 | 音频输入窗口、原生 ID/task；没有事件就是漏委派，不能用 ASR/普通输出补造；NVIDIA 才另绑最终 ASR |
| Router | 原 task 的路由/参数、parse_method、澄清/不支持；不猜型号/地点，不默认 Tavily |
| 工具 | 真供应商、权限、query/validate、有效来源、次数；只读契约/HTTP ok 不等于正确问题有结果 |
| 回填 | 原 ID、发送/unknown/匹配 Gateway 回执分开；unknown 不重发，回执不证明结果消费 |
| 生命周期 | 普通发声不取消查询；旧结果/迟到帧不改绑新问题；补充输入未完整消费/覆盖不提前 completed |
| 知识/预算 | 产品、主体、属性、数值、极性、条件和缺项逐项有据；NVIDIA 检查完整 NeMo 包与实际 tokenizer，不能把省略证据用于认证 |
| 语音 | 首音频、候选结束、播放器排空、实际听音和语义覆盖独立；F1/F3/F7 等仍可能限制认证 |

回传只含对应窗口的有界摘要、门户全部转写/答案/来源和是否完整听到、旧音频恢复、漏问/重复、错误/结束原因。不要贴 Key、票据、HAR、原始整库或无关全量日志。truncated/omitted 或关联缺失记证据不足；collection=complete 只是采集完成。

时延分段记录 query/validate → 构包/预算/锁 → 供应商接收 → ACK/注入 → PCM 追赶 → 候选音频/实际完整回答；API/Docker/浏览器时钟不直接混算。首段等待语不能当最终答案时延，不创造未经确认的秒数达标线。普通一般回答不伪造企业来源。

## 5. 已解决：原生委派触发与 Router 解析

状态：两类原故障均在 b60028d 修复，固定官方 14 录音及 en_time 独立重复复测通过；不再作为当前待办。两张表分别是**同一组 14 项的修复前和修复后**，不是两个尚待执行的测试任务，也不是两轮修复后验收。

| 原问题 | 修复和对应证据 | 关闭范围 |
| --- | --- | --- |
| 模型没有触发原生委派 | 简化 task 格式负担、分离 Persona 与固定规则，明确同响应实际调用；预期委派从 1/7 到 7/7，无需工具的 7/7 均未误调 | 固定官方样本集的漏委派故障已解决；不认证所有语音表达的稳定性 |
| 已委派 task 无法被 Router 识别 | 保留结构化优先并加入自然语言规则；天气/新闻成功解析，三类业务范围内 3/3 路由正确，其余明确 UNSUPPORTED | 已观察到的原生 task 格式故障已解决；不代表已实现时间、关灯或个人记忆业务 |

下面保留原始失败、改动原因和完整对照，以便回归追溯；其他待验项在 §6 / TASK_BOARD。部署静态检查不刷新这些历史语音结果。

### 官方 14 录音的 Web 全流程验证（2026-11-22）

基线应用 06aafd9，官方材料 6b1713518eb75f8495415662870f682357372d09。当时音轨和输出字幕 14/14，无需工具 7/7 未误调；预期委派仅 **1/7**，唯一 zh_news 原生 task 未被 Router 识别，工具执行 **0**；其余六条没有原生事件。该次 ASR 关闭，0/14 输入转写不代表后续已启用环境。此事实不得被后续通过覆盖。

| 样例 | 官方预期 | 原生委派数 | 工具执行数 | 首段有效音频 ms |
| --- | --- | ---: | ---: | ---: |
| zh_time | 委派 | 0 | 0 | 839 |
| zh_weather | 委派 | 0 | 0 | 674 |
| zh_light | 委派 | 0 | 0 | 786 |
| zh_memory | 委派 | 0 | 0 | 684 |
| zh_news | 委派 | 1 | 0 | 1662 |
| zh_chat1 | 直接回答 | 0 | 0 | 707 |
| zh_chat2 | 直接回答 | 0 | 0 | 719 |
| zh_chat3 | 直接回答 | 0 | 0 | 726 |
| zh_know | 直接回答 | 0 | 0 | 694 |
| zh_joke | 直接回答 | 0 | 0 | 677 |
| en_time | 委派 | 0 | 0 | 1019 |
| en_weather | 委派 | 0 | 0 | 841 |
| en_chat | 直接回答 | 0 | 0 | 856 |
| en_know | 直接回答 | 0 | 0 | 751 |

官方清单包含时间、关灯、记忆等三类工具之外操作，保持原生触发分母，业务执行另计。原始目录 `/tmp/sophon-web-14.aENraE` 包含 run-manifest.json、final-14.json、results/final-zh、results/final-en、原始截图/WAV/JSON；会话结束时 Room/participant 均为 0。该轮 118 Python、13 浏览器、42 Web 通过，不代表真实语音工具通过。

同版本的直连/天气单次成功和两条额外合成产品输入没有替换正式失败；前者不稳定复现，后者虽委派仍未路由。早期超时/HTTP 502 后，2026-11-22 14:48 同一 WSS 的 TLS/Upgrade/session.start/close 成功，只证明该主机当时传输控制。不可据此推断真实引擎身份或所有时段可用。

### 原生委派与 Router 分项修复复测

基于 06aafd9 开发、发布为 b60028d 的实现；修改前本地/tracking/实际 main 已核对。此节保留当时源码 hash/环境与实际数据，不把后续纯文档编辑当作新应用回归。

**A：模型没有产生原生委派。** 核对 SDK session.start 和固定上游 Gateway：注册 client delegate(task)，task 接受一句自然语言；部署引擎进程/最终模板/权重身份未审计。简化格式负担、分离 Persona/固定规则后中文 5/5 触发；英文首次 en_time 仍只说 I'll check，整组当时 6/7。同一官方 en_time WAV 直连原指令仍漏，官方短 Persona 和明确函数/task/同响应实际调用的候选指令各触发一次；采用候选规则后 Web 整组及独立重复触发。native-current/native-official/native-candidate.json 保留，未用 ASR、等待语或客户端关键词补调用。

**B：已经委派但 Router 无法解析。** 结构化优先，新增自然语言规则。实测“明天上海会下雨吗”“今天有什么科技新闻”“把客厅的灯关掉”“用户上次说喜欢吃什么”分别映射 WEATHER、WEB_SEARCH、UNSUPPORTED、UNSUPPORTED。早期后两条曾 CLARIFY，按实际 task 补规则后复验。参数不明/多路匹配澄清，不支持操作不搜索伪装执行；task 始终来自原生事件，ASR 不进入解析。

#### 官方 14 项分项结果

最后整组 delivery-zh / delivery-en，汇总 delivered-14.json：预期原生 **7/7**，无需工具 **7/7** 未误调，已委派且在三类范围的路由 **3/3**，ASR 可见 **14/14**。三次真实工具调用和回执均收到，中文天气/新闻两例有可用结果；英文天气只返回地点澄清，不算第三个天气答案。14 条有音轨/候选输出，最终口述认证为 0；时间/灯光/记忆的不支持不算业务执行成功。

| 样例 | 原生委派数 | Router | 工具执行数 | 业务结果 | 匹配 Commentary 回执 | 首段候选音频 ms |
| --- | ---: | --- | ---: | --- | --- | ---: |
| zh_time | 1 | UNSUPPORTED | 0 | 不支持时间查询 | 是 | 2881 |
| zh_weather | 1 | WEATHER | 1 | answered，口述未认证 | 是 | 3061 |
| zh_light | 1 | UNSUPPORTED | 0 | 不支持设备动作 | 是 | 3108 |
| zh_memory | 1 | UNSUPPORTED | 0 | 不支持个人记忆 | 是 | 3223 |
| zh_news | 1 | WEB_SEARCH | 1 | answered，口述未认证 | 是 | 2720 |
| zh_chat1 | 0 | 直接回答 | 0 | 一般候选语音 | 不适用 | 2145 |
| zh_chat2 | 0 | 直接回答 | 0 | 一般候选语音 | 不适用 | 67 |
| zh_chat3 | 0 | 直接回答 | 0 | 一般候选语音 | 不适用 | 2188 |
| zh_know | 0 | 直接回答 | 0 | 一般候选语音 | 不适用 | 2075 |
| zh_joke | 0 | 直接回答 | 0 | 一般候选语音 | 不适用 | 1896 |
| en_time | 1 | UNSUPPORTED | 0 | 不支持时间查询 | 是 | 1828 |
| en_weather | 1 | WEATHER | 1 | needs_clarification，同名地点 | 是 | 2639 |
| en_chat | 0 | 直接回答 | 0 | 一般候选语音 | 不适用 | 4532 |
| en_know | 0 | 直接回答 | 0 | 一般候选语音 | 不适用 | 2062 |

首段候选可能是等待语/重叠输出，不是完整答案时延。Provider/Router/指令源码与交付一致；中文整组后仅 App.tsx 的角色设置准备增加取消/互斥，该路径不在官方清单中，纳入最终 15 项浏览器回归。每组实际 hash 均保存，不伪称同一全仓快照。initial-zh、official-zh、final-zh、final-en、final-en-v2、repeat-en-time 全部保留，包括中间失败，未挑同组成功替换失败。

## 6. 其余能力的证据与待验项

本节是独立的剩余范围，不改变 §5 两类故障在固定录音集上的关闭状态。

### 角色、ASR 与三类依赖

真实 WebSocket 的闹闹/小狗，以及 LiveKit 的闹闹/小狗均在候选自我介绍中出现所选名字；有输入字幕和 PCM、无协议错误。小狗下北京天气/公开语音模型检索 2/2 有原生、正确路由、真实执行和回执（profile-xiaogou/summary.json）。小狗额外自称 AI2 的语音伙伴，不在配置中，不能认证完整 Persona 或该归属。角色 CRUD、跨用户 401/404、拒绝修改固定规则、快照/删除/损坏回退、重连与文件新身份有受控及真实本地 API 浏览器回归。

真实新环境已收到中英文 ASR，不能继续用旧 0/14 描述当前配置。五种 ASR 状态固定同一合法 task 的本地回归保持相同选择；unbound 字幕不授权。真实关闭/开启对照尚未执行，本项目不控制独立 Gateway 的 ASR 模型启动。

补充七条 Tingting 合成输入不是官方录音：北京实况、开源语音模型、明确公开产品资料有真实工具执行/回执；裸产品参数与缺地点天气澄清，内部客户日志被模型直接拒绝且未外发。缺地点是在委派后澄清、内部日志未委派而拒绝，分别不符合补充清单最初 native 标签，边界行为和标签差异分别记录。

两次产品 task 把 **F7005 写成 F705**，包括一次公开检索；Router 保留了该错误，未用 ASR 猜改。Tavily status=ok 不证明搜了正确产品。当前受控公开兜底和配置分支测试不能证明真实 CueKB 成功；最新 Sophon 环境 CueKB 尚未接入，不能据此宣布完整三类交付。

### 证据位置与未验项

原始目录：`/var/folders/1x/568sjwqd64z3p8prx9d5338w0000gn/T/sophon-implementation-6oiitmvq`，定位文件 `/tmp/sophon-implementation-path`。包括 delivered-14.json、各整组 summary、原生直连对照、supplement-zh、profile-xiaogou、ws-role-闹闹/小狗、room-cleanup.json 和 publication.json；这些是当时本机临时路径，缺失时从保存的原始材料恢复或按 §2 复跑，不能声称 Git 保存了 WAV/报告。清理前 Room/participant 为 0；生产未部署。

SQLite 0006→head(0011)→0007/0006→head 与 Alembic check 保留旧历史；真实 PostgreSQL 迁移未执行。真实 CueKB、ASR 开关对照、完整多轮/打断、型号保真、Persona 完整遵循、最终口述关联和人耳听音仍未通过。Gateway 回执不证明引擎消费，所有 final_answer_verified=false。

## 7. 开发检查与文档执行记录

| 适用版本 / 范围 | 实际记录 | 限制 |
| --- | --- | --- |
| b60028d 实现交付 | 定向 Python 152、Web 单测 42、浏览器 15 通过；TypeScript/Web 构建、Ruff、契约、JS/Shell/迁移/diff 检查通过 | 未把它们当 GPU/容器/听音通过；LiveKit chunk >500 kB、Starlette 既有弃用警告保留 |
| 同轮全量与隔离基线 | 首次 853 passed、28 failed；迁移版本断言更新后复验通过，余 27 个节点在原始 main 的 git archive 中全部复现（27 failed、1 passed） | 包括 NVIDIA/旧 CueKB/followup，未修复；不宣称当前全仓全绿。pytest-all.log / baseline-failures.log / final-scoped.log 留原目录 |
| 2026-11-23 容器方案只读核查 | 12 项部署契约、两部署脚本语法通过 | 当时无 Docker CLI；未构建/部署，也未实现新验收镜像或修改代码 |
| f20b1c2 文档整理 | 16 个 Markdown；145 个本地链接/锚点（含入链）、12 段 Shell、1 段内嵌 Python 语法检查通过；原始/最新两张官方 14 项表逐字保留 | 当时只改文档，未运行应用、Docker、GPU、供应商、浏览器或听音回归 |
| 2026-11-23 LiveKit 同 Compose 与故障归档 | 部署/Router/LiveKit 定向 66 项通过，Ruff、Shell/diff 通过；官方 Compose v2.39.4 静态展开 6 服务，LiveKit YAML/Redis DB1/API 凭据一致；13 个 Markdown、116 个链接/锚点、11 段 Shell/1 段内嵌 Python 检查通过；保存的 delivered-14 JSON 重核 7/7、无需工具 7/7、路由 3/3，两张表逐字保留于已解决章节 | 未使用 Docker Engine，未构建/启动镜像、验证 Nginx 实际运行或媒体/NAT；未重跑供应商/浏览器/听音；保留 Starlette 既有弃用警告 |

本轮发现并修复部署配置阻塞：SOPHON_LIVE_MAX_DELEGATIONS_PER_INPUT 的环境字符串 1 被 Literal[1] 拒绝；改为整数且上下界均为 1，仍拒绝 0/2/非整数。新增测试从实际模板读取字符串配置并执行 Settings.validate_deployment，不连接服务。主部署脚本拒绝缺少任一查询能力、假值、非 WSS、非法 IPv4、过短/不安全 LiveKit 凭据及 mock 天气；这些检查不证明真实依赖可用。

本地代码变更时按范围选择以下开发复跑入口（需要项目开发环境，非生产容器验收；本轮实际选择见上表，不表示整段都已执行）：

```bash
.venv/bin/python -m pytest -q tests/contract/test_deployment.py tests/contract/test_delegation_router.py \
  tests/contract/test_public_tools.py tests/integration/test_voice_profiles.py \
  tests/integration/test_sophon_live.py tests/integration/test_livekit_delegation.py \
  tests/integration/test_auth.py tests/integration/test_native_tool_catalog.py \
  tests/contract/test_qa_migration.py
npm --prefix apps/web test
npm --prefix apps/web run build
(cd apps/web && npx playwright test voice-file.spec.ts voice-profiles.spec.ts)
# 仅公开契约变化时导出；本轮未改公开契约，未执行：
PYTHONPATH=apps/api .venv/bin/python scripts/export_contracts.py
```

2026-11-06 NVIDIA 审查与 11-07 F1/F2/F3/F4 限定修复的详细方法/流水由 Git 追溯，审查边界见 [review](reviews/duplex-tool-audit.md)，剩余事项见 TASK_BOARD。历史 70 项定向通过/未部署事实不认证 b60028d 的 GPU 或语义；删除重复步骤不代表旧兼容代码或未解决项已消失。


## 8. Qwen 原生 task 接入与本轮本机整体验证

### 范围、版本与实际实现

2026-11-23 用户确认：启用 qwen3.7-flash 解析 Sophon 已产生的原生 task，替代自然语言规则解析；允许本机验证，本次不接真实 CueKB。补充确认：Sophon/NVIDIA 互斥，默认只配置 Sophon，本期仅验 Sophon；NVIDIA 只检查必要的启动选择契约，不运行服务/GPU 回归。产品仍是三类能力，生产仍按容器发布；本轮不是关闭专业库的产品方案，也不是生产三类放行。

基于 6a3d198 实现。实际链路为门户文件音轨 → LiveKit 1.13.11 → 真实 Sophon → 原生 delegation ID/task → Qwen → 服务端参数/权限校验 → QWeather/Tavily → Commentary 回执。ASR 不进入意图请求。API 使用隔离 SQLite/内存协调、validation 身份，本机回环 HTTP/WS；未运行生产 Docker/PostgreSQL/Redis/CueKB。Qwen Key 存仓库外私密配置，未进入 Git、报告或浏览器。

复用 DelegationRouter、共享 HTTP client、Coordinator/Runtime/Registry。结构化任务不调用模型；自然语言使用 JSON Schema，默认总期限 4 秒、16 KiB 响应、无重试/重定向。query 必须保留完整 task，私有/敏感 task 不自动外发；模型不能把未确认公开范围的专业问题改成公网查询。错误/超时/越界不执行、不回退规则；等待模型后复核 epoch 与输入窗口。

### 本轮检查结果

| 层次 | 实际证据 | 边界 |
| --- | --- | --- |
| Qwen 接口 / JSON Schema | HTTP 200；独立合成 6/6、真实 DelegationRouter 6/6；正常解析约 0.7–1.1 秒 | 仅文本/接口不算语音已委派；未接入专业库时的澄清不是 CueKB 成功 |
| Sophon 核心回归 | 单选续办后 156 passed，覆盖结构化快速路径、三类参数、ASR 五态、超时/恶意/错误输出、不外发私有任务、公开范围、权限/角色、模型等待期间新/补充发声及启动选择 | 受控测试不代替真实服务；保留 Starlette 既有弃用警告；未运行 NVIDIA 服务/GPU |
| 单选启动契约 | 模板只启用 Sophon；必要配置测试确认两个枚举选择各用一个 factory，双选拒绝，NVIDIA 不要求/激活 Sophon 字段。官方 Compose v2.39.4 静态展开 Sophon 6 服务、另一选择 5 服务且无 LiveKit | 仅合成配置/脚本桩检查，未运行 Docker Engine/Nginx；可选 depends_on 在 config 中仍保留，required=false，不等于启动被排除的服务 |
| Web / 静态检查 | Web 42 passed，TypeScript/生产构建通过，文件输入 13 + 角色 2 个浏览器测试通过；Ruff、JS/Shell、契约导出核对通过，contracts 无差异；12 个 Markdown、116 个链接/锚点、12 段 Shell/1 段内嵌 Python 检查通过，历史两张 14 项表逐字保留 | 文件输入 13 项使用受控 WS；角色测试使用真实本机 API。保留 LiveKit chunk >500 kB 警告 |
| 真实 Adapter 与会话 | QWeather 今日实况 ok 且未过期；Tavily 返回 3 个来源；Sophon session.start/close 成功 | 单次探测不证明原生触发、最终听音或生产实例身份 |
| 官方固定集两轮 + 角色 | 30 次播放：原生 16/16，无需工具 14/14 未误调，LLM 解析 16 次、失败 0；范围内路由 8/8，真实调用/回执 8/8 | 只测试天气/公开搜索实服务；6 个 answered，2 个 Melbourne 同名地点澄清，不算天气答案 |
| 语音/字幕与归属 | 30/30 有候选音轨和输入字幕，控制 WS PCM 为 0，无观察窗口缺失 | Gateway 回执/字幕/网络音轨不认证完整口述；final_answer_verified 均为 false，未人耳听音或真实 ASR 开关对照 |
| 公网门禁阶段同 Room 两问 | 一次 conversation、一次 voice session，天气 → 新闻，均 parse_method=llm、真实执行/answered；Stop playback 1 次未取消查询，第二问音轨恢复，实际麦克风调用/控制 WS PCM 均为 0 | 只证明本组基本连续两问与停止播放；不代表真实重叠发声/所有快速打断或口述语义均通过 |
| 单选续办最新源码实测 | 官方 zh_weather 再播放 1 次，Sophon 原生触发 1/1、LLM 正确路由 1/1、真实 QWeather 执行/回执 1/1、answered 和候选音轨各 1；控制 WS PCM 0 | 独立于此前 30 次统计；仍未人耳听音，final_answer_verified=false |

单选续办的 Ruff、部署 Shell/验收 JS 语法、契约导出核对及 diff 检查通过，contracts 无变化；最终 12 个 Markdown、115 个链接/锚点、12 段 Shell/1 段内嵌 Python 检查通过，两张历史 14 项表逐字保留。提供的模型 Key 未出现在 27 个变更文件或 104 个报告/日志等文本文件中。

全量回归和 30 次正式播放后，额外加了“模型不得把未确认公开的专业问题改为 WEB_SEARCH”的服务端门禁；随后 187 项较广核心回归及同 Room 两问通过，没有复跑全量。后续单选配置修改 config/gateway/routes/部署定义，未改 Router/原生音频指令；此阶段仅执行上表的 156 项定向回归和最新源码天气语音复测。各阶段源码 hash 分开保存，不伪称所有记录来自同一文件快照。

| 实服务录音组 | 样例数 | 预期原生 / 实际 | 范围内正确路由 | 真实工具 / 回执 | 可用结果 / 澄清 |
| --- | ---: | --- | --- | --- | --- |
| official-zh | 10 | 5 / 5 | 2 / 2 | 2 / 2 | 2 / 0 |
| official-en | 4 | 2 / 2 | 1 / 1 | 1 / 1 | 0 / 1 |
| official-zh-repeat | 10 | 5 / 5 | 2 / 2 | 2 / 2 | 2 / 0 |
| official-en-repeat | 4 | 2 / 2 | 1 / 1 | 1 / 1 | 0 / 1 |
| profile-xiaogou-public | 2 | 2 / 2 | 2 / 2 | 2 / 2 | 2 / 0 |

两组官方 14 项均完整执行，源为 6b1713518eb75f8495415662870f682357372d09；全部原始 WAV 的 SHA256 与既有官方输入逐条一致，未替换难例。时间、灯光和个人记忆原生委派后明确 UNSUPPORTED，不算三类业务成功。角色两项重用官方天气/新闻，不是新增两种独立语料。

### 范围外历史旁证（默认跳过）

明确本期仅验 Sophon 前，曾误纳入全仓旧兼容回归：修改前 862 passed / 27 failed，接入后 893 passed / 26 failed，无新增失败节点；26 个均在修改前存在。一条旧 deadline 用例未失败，不声称已修复。失败按文件分组：test_continuous_dialogue 5、test_delivery_lifecycle 1、test_live_agent_interactions 4、test_native_cuekb 1、test_qa_routing 1、test_simulated_full_flow 14；属于旧 VoiceChat/兼容链路，保留失败日志但不作为本期 Sophon 阻塞，不再复跑。没有将新 Router 通过解释为这些失败已解决或全仓全绿。

### 证据、采集修正与复跑

本轮原始目录：`/var/folders/1x/568sjwqd64z3p8prx9d5338w0000gn/T/vsa-qwen-validation-3sql4n1q`，定位文件 `/tmp/vsa-qwen-validation-path`。formal-30-summary.json、official-run1-14.json、official-run2-14.json、五组 summary/逐条 JSON/WAV/截图、runtime-source-manifest.json、multi-turn-real.json、基线/最终 pytest 日志和模型探测均保留；凭据在独立私密目录，不在报告目录中。临时证据不在 Git，丢失时必须复跑，不能引用为现存原始材料。

publication-source-manifest.json 标记公网门禁阶段 hash，同 Room 复测的 Router hash 与交付文件一致。单选续办追加 selector-source-manifest.json、selector-compose-config.json、selector-sophon-smoke/summary.json；后者逐文件 hash 与此次交付运行源码一致。两阶段结束均检查专用 LiveKit 的 Room/participant 为 0，临时 API、Web、LiveKit 及端口关闭，见 cleanup.json / selector-cleanup.json。

预检遇到本机 TUN 只暴露默认 ICE 接口、UDP 回程不通，以及 API 重启未就绪；失败均保留在 smoke-*，未送到模型的音频不计原生漏调。--local-ice 限 localhost，允许枚举回环/LAN，独立浏览器 context 授予 microphone 权限但仍只播放选定 WAV；本轮没有实际麦克风采集，没有更改系统 VPN/路由或生产默认。浏览器参数写入各 summary。多轮首次采集保存了第二问 running 旧快照，服务端日志已确认两次成功；原始 multi-turn-real-initial 保留，等待终态后在新通话重新采集通过。

当前核心复跑命令如下；实服务录音使用 §2 的现有入口，按需要为 localhost 增加 --local-ice，并分别启动 zh-CN/en-US 的服务。不能用这里的本机例外替代正式容器验收。

```bash
.venv/bin/python -m pytest -q tests/contract/test_delegation_llm.py \
  tests/contract/test_delegation_router.py tests/contract/test_public_tools.py \
  tests/contract/test_deployment.py tests/integration/test_livekit_delegation.py \
  tests/integration/test_voice_profiles.py tests/integration/test_auth.py \
  tests/integration/test_sophon_live.py
```

真实 CueKB、生产迁移/公网媒体、更多表达/真实交错打断、型号语音保真、完整 Persona、最终口述关联及听音仍按任务板分别验收；本轮结果不重新打开 §5 已解决的原问题，也不覆盖这些未验范围。
