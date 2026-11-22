# 当前配套版本验证

基线：本项目 `2a8ba8b`、CueKB `30970e9`。唯一当前执行手册，替代旧轮次和旧归档；历史通过 Git 追溯。本次（2026-11-06）只进行了代码审查/只读函数探针，没有部署或重新做 GPU/业务回归。

## 1. 使用范围与门槛

生产操作在部署主机本项目根目录执行；每窗口只做一个用例，无其他测试用户混入。不要改 WAV 停顿/语速、识别门槛、工具目录、权重或缓冲/超时来取得通过。

当前配置：`dual_tools/general_qa`、`AGENT_PROVIDER=disabled`，真实 VoiceChat 和 CueKB 2.1。API/Web/VoiceChat 六个挂载需配套；`CUEKB_VOICE_TOKEN_BUDGET=120` 限制 provider model_view，`VOICE_TOOL_RESULT_MAX_TOKENS=256` 限制完整 NeMo 包，两者不同。旧 VoiceChat 没有 `nemo-tool-response-v1` 能力会失败，不可跳过预算检查。

已知 F1—F4 等尚未修复，详见第 7 节。当前允许采集基线，不能把整体验收写成通过。CueKB 已实现不等于生产内容/实体/节点/tokenizer 准备正确；生产身份与 API/协议须匹配，不能只看 health。

## 2. 更新本次代码并检查部署

本节在部署主机执行，不是开发环境已经部署的记录。先将本次完整提交检出到主机项目目录，保持已有 `.env`、已验证的双 GPU（0,1）配置、9000 端口及模型挂载；停止旧测试通话，本次窗口内不要有其他测试用户。CueKB 由其运行方发布新版，本仓库不替它迁移、修改内容或重建索引。

### 2.0 先同步执行目录

`version=failed` 的 expected 来自**执行脚本所在目录的宿主文件**，actual 来自容器磁盘；expected 不等于远程 main 的最新版本。先在当前本项目目录执行下段，再使用更新后的本页执行 2.1 和 2.2；不要从旧目录构建覆盖新 API。命令保留 `.env` 和数据卷，不丢弃本地修改；受跟踪文件有修改或本地 main 与远端不一致时明确停止，回传失败行。

```bash
bash <<'SH'
set -euo pipefail
if ! git diff --quiet || ! git diff --cached --quiet; then
  printf 'checkout=failed reason=LOCAL_TRACKED_CHANGES_PRESERVED\n'
  exit 1
fi
git fetch origin main
git switch main
git merge --ff-only origin/main
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
  printf 'checkout=failed reason=LOCAL_MAIN_DIFFERS_FROM_ORIGIN\n'
  exit 1
fi
printf 'checkout=updated root=%s commit=%s\n' "$(pwd)" "$(git rev-parse --short HEAD)"
SH
```

`git pull` 只更新文件，不会重建 API，也不会重新加载 VoiceChat Python 进程。后续重新创建 VoiceChat 时，第 2.1 节的六个绝对路径挂载统一指向这个目录。即便磁盘文件 hash 已匹配，也必须完成进程重载；不能以在旧容器里复制文件代替部署。

### 2.1 配套更新 API、Web 与 VoiceChat

下面命令从同一目录构建并重新创建 API 与 Web，再用原 VoiceChat 的已验证镜像 ID、模型目录、GPU/端口参数及六个只读挂载重新创建 VoiceChat。当前 model/data/audio_server 必须配套加载；Web 页面阶段、输入关联和文件播放完成状态有实际行为修改，必须重建 Web 后强制刷新浏览器。运行库、权重、工具目录和识别门槛不变；CueKB 的数据库/审核升级由其项目负责。

```bash
bash <<'SH'
set -uo pipefail
umask 077
RELEASE_LOG=$(mktemp /tmp/voice-consumption-release-XXXXXX.log)
if (
  set -e
  VOICECHAT_SNAPSHOT_ROOT=$(cd docs/voicechat && pwd)
  VOICECHAT_RELEASE_IMAGE=$(docker inspect --format '{{.Image}}' nemotron-labs-voicechat)
  VOICECHAT_MODEL_ROOT=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data/models"}}{{.Source}}{{end}}{{end}}' nemotron-labs-voicechat)
  test -n "$VOICECHAT_MODEL_ROOT" && test -d "$VOICECHAT_MODEL_ROOT"
  docker image inspect "$VOICECHAT_RELEASE_IMAGE" >/dev/null
  API_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-api:/ {p;q;}')
  test -n "$API_IMAGE"
  WEB_IMAGE=$(docker compose --env-file .env -f deploy/compose.production.yaml config --images | sed -n '/^voice-service-agent-web:/ {p;q;}')
  test -n "$WEB_IMAGE"
  docker build -f deploy/Dockerfile.api -t "$API_IMAGE" .
  docker build -f deploy/Dockerfile.web -t "$WEB_IMAGE" .
  docker compose --env-file .env -f deploy/compose.production.yaml up -d --no-deps --force-recreate --wait --wait-timeout 180 api
  docker compose --env-file .env -f deploy/compose.production.yaml exec -T api python /app/scripts/configure_native_cuekb.py --apply
  docker compose --env-file .env -f deploy/compose.production.yaml up -d --no-deps --force-recreate --wait --wait-timeout 180 web
  docker stop nemotron-labs-voicechat
  docker rm nemotron-labs-voicechat
# Paired model/data/template runtime; recreate the container to load all three.
docker run -d --name=nemotron-labs-voicechat \
  --runtime=nvidia \
  --gpus '"device=0,1"' \
  --shm-size=16GB \
  -e USE_JINJA_TEMPLATE_PROMPT=1 \
  -e S2S_INFERENCE_CLOSE_ON_PAD_TAIL=true \
  -e S2S_INFERENCE_FORCE_SPEECH_SILENCE_ON_PAD=true \
  -e S2S_TTS_PAD_TAIL_RATIO=3 \
  -e RNNT_MAX_AGENT_RESPONSE_SEC=0 \
  -e VOICECHAT_DIAGNOSTICS=1 \
  -e NIM_HTTP_API_PORT=9000 \
  -e TTS_CUDA_VISIBLE_DEVICES=1 \
  -e LLM_GPU_MEM_UTIL=0.65 \
  -p 9000:9000 \
  -v "${VOICECHAT_MODEL_ROOT:?Set VOICECHAT_MODEL_ROOT}:/data/models" \
  --mount "type=bind,source=${VOICECHAT_SNAPSHOT_ROOT:?Set VOICECHAT_SNAPSHOT_ROOT}/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py,target=/opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/s2s/audio_server.py,target=/s2s/audio_server.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/s2s/prompt_template.jinja,target=/s2s/prompt_template.jinja,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/data_types.py,target=/opt/tritonserver/backends/nemotron-voicechat/data_types.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/model.py,target=/opt/tritonserver/backends/nemotron-voicechat/model.py,readonly" \
  --mount "type=bind,source=$VOICECHAT_SNAPSHOT_ROOT/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py,target=/opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py,readonly" \
  --entrypoint /s2s/run_s2s_server.sh \
  "$VOICECHAT_RELEASE_IMAGE"
) >"$RELEASE_LOG" 2>&1; then
  printf 'release=created commit=%s build_log=local_only\n' "$(git rev-parse --short HEAD)"
else
  printf 'release=failed local_build_log=%s\n' "$RELEASE_LOG"
  exit 1
fi
SH
```

成功输出 release=created 只说明完成创建。若失败，停止并回传失败行，原始构建日志留在本机；不要继续测试、猜测镜像、修改绑定文件或反复重启。已有离线镜像必须在删除旧容器之前记录，本命令不会安装新的 GPU 依赖。

### 2.2 等待就绪并核对版本

VoiceChat 重载模型可能需要几分钟。创建完成后等待 **5 分钟**，然后在同一终端/目录执行：

```bash
git rev-parse --short HEAD
bash scripts/validate_deployment.sh check
```

正常得到 checkout 的目录/提交/分支、preparation=passed 和 API/VoiceChat 的 runtime 身份行。入口会核对应用、采集器、query schema、目录 YAML、原始 WAV、VoiceChat 六个运行文件及既有诊断配置，并检查 VoiceChat 为真实 Triton/model ready（拒绝 loopback）。哈希失败时额外标注 version_source expected=host_checkout actual=container_disk，并输出容器 image/start 时间。出现 version=failed / collection=failed 就停止并回传；不自行复制文件、降低检查或再次重建。文件匹配不证明已运行的 Python 加载了新代码，也不证明 GPU 提速、模型首问选工具正确或 CueKB 内容已通过。

浏览器强制刷新门户，打开 F12 → Console，开启 **Preserve log**，过滤 voice_file，清空旧 Console 内容。保持门户前台、声音可播放，不锁屏或切后台。固定使用仓库 artifacts/native-cuekb/golden_yolk.wav 与 artifacts/native-cuekb/black_pig_crisp_heart.wav，选择 **Audio file → Choose WAV**。Python 和采集在容器内执行，宿主机无需 Python，不手工导出 HAR 或运行 docker logs。

## 3. 文件通话 A/B

NVIDIA 配套部署完成第 2 节检查后，可用宿主单入口顺序执行两个核心文件用例及同连接追问；该命令不用于 Sophon LiveKit 放行：

```bash
bash scripts/validate_deployment.sh auto core
```

**A：Golden 独立首问。**

```bash
bash scripts/validate_deployment.sh web-start golden_yolk
```

门户 Audio file → Choose WAV，选择原始 `artifacts/native-cuekb/golden_yolk.wav`，新建 Start file call。完整发送并等待全部分段回复，保留全部转写/答案/来源；不要只保留最后一条。页面提前错误或从 Start 起达到 120 秒则结束。如未获得首问企业知识调用，记失败，不以同连接预先播放 Black 代替。

**先执行第 5 节结束 A 并采集，再开始 B：**

```bash
bash scripts/validate_deployment.sh web-start black_pig_crisp_heart
```

新通话先发原始 `artifacts/native-cuekb/black_pig_crisp_heart.wav`；回复结束后，同连接 Choose WAV → Send recording 发送 Golden，不重新 Start。总等待上限仍是从 B Start 起 120 秒；未能发送第二份时如实记录，不重建连接伪装成同连接测试。分别保存两个产品的全部来源和回答，然后执行第 5 节。

这两通话是可比较样本，不证明稳定性/P95。后续重复性验证需固定版本和语料，不能混用旧结果。

## 4. 麦克风与交错输入

C、D 使用独立新通话和采集窗口。每次先执行：

```bash
bash scripts/validate_deployment.sh web-start greeting
```

- C：Microphone → Start call，约三秒静默等待欢迎语；欢迎语中说 `Hello, I need help with Golden Yolk.`。确认旧播报停止且不恢复，然后明确问 `Can Golden Yolk be shipped?`，等待回答，再问一个独立问题。不得因第一条欢迎语结束再主动输出而断连。
- D：新通话明确问 Golden 的保质期；在 checking/结果注入期间开始一段较长补充：`I have another question. I need to know what happens after opening the package and how it should be stored.`。观察新转写是否在等待中出现；保留它的完整文本、回答和时间线。重点检查补充尚未说完时，旧答案是否已把它标为 Included/completed；若后续漏答或工具只结清不检索，记 F3 失败。

每次最多等待 120 秒，结束后都执行第 5 节。若 D 的发声没有落入 inject_started/inject_done 之间，只记录“时序未命中”，不声称注入期 ASR 已验证。不用提前缩短 WAV 或提高阈值制造通过；真正的听音打断与 ASR 可见分别记录。

## 5. 每次通话的统一结束与采集

**每个采集窗口各执行一次：**先在门户点击 **End call**，随后在宿主机执行下面整段。不要跨用例留下未关闭的采集窗口。

即使 A 提前报错，也必须先执行本节采集，再开始 B。如果 web-start 显示 `web_capture_already_started`，说明上个窗口尚未结束；先执行本节保存现有窗口，不删除状态、不覆盖窗口。当前窗口可能包含两次通话，采集器会按会话分别输出；这个采集保护错误不代表 Black Pig 的模型或 CueKB 调用失败。

该命令先调用现有 web-end，再自动使用本窗口的文件和会话输出阶段时间线与分轮元数据。只读已有记录，不发送录音、不发起模型或 CueKB 请求；原始日志仍留在本机。采集失败也保留本次输出，不读取上一次结果。

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

## 6. 回传与放行判据

回传准备检查、web-start、第 5 节全部有界输出，门户全部转写/答案/来源，以及 Console 当前 `voice_file` 行（麦克风记不适用）。不贴密钥、HAR、原始日志或私有整库数据。每窗口补充：case、声音是否完整、旧音频是否恢复、字幕是否边说边显示、是否漏问/重复、页面错误原文、结束原因；B 再写第二份文件是否完整发送。

| 维度 | 必须核对的事实 |
| --- | --- |
| 版本 | preparation、镜像/启动时间、挂载文件一致；磁盘匹配不证明进程已重载 |
| 查询 | 稳定 input 触发原生工具后最多一次 evidence query；合法包一次 validate；call 数不等于 query 数 |
| 终态 | 冗余 call 只结清，不新建/替代 Turn，不重复正式回答；继续问下一问可用。未触发冗余 call 的现场用例不能认证该分支 |
| 生命周期 | ACK 不作为正式答案；抑制 done 仍排空；普通发声不取消查询；补充输入未完整消费/回答时不提前 completed |
| 事实 | Golden 的运输/密封保质期/最佳期限/开封储存，Black 的价格/重量/层数/最佳食用逐项有据或明确缺失；不跨产品，不把比例改成百分比 |
| 状态 | answer/partial/clarify/insufficient/conflict 与实际准入和口述一致；answered/source_checked 不单独证明覆盖 |
| 完整预算 | wire_admission、wire_tokens/token_budget、tokenizer 身份；控制指令、缺项及 call_results 计入预算；省略组不得授权核验 |
| ASR/理解 | inject 区间出现 backend.live_asr，observed_frames 领先 llm_audio_frames；随后 PCM 消费无漏/重复；可见转写不等于新问题已理解 |
| 注入 | started → returned_idle → done(state=idle)，failed=0；finally 的 done 单独不足以认定成功 |
| 播放 | 服务端首音频、浏览器播放、实际听音分开；旧 response 清除后迟到帧不得恢复 |

时延分段：query/validate → API 构包/预检/锁 → VoiceChat 收到结果 → ACK/调度 → 注入 → 音频追赶 → 正式首音频/结束。读取 `backend.inject_profile` 的 tokens/completed/append_wait_ms；不把全部等待称为检索慢。API 时间、Docker 观测时间、浏览器时钟不直接混算。

摘要中 truncated/omitted 或关联缺失须标证据不足。`admitted_evidence` 是有界样本，不能替代 B 所有轮次来源。collection=complete 只表示采集结束；不代表功能验收通过。现阶段没有新批准的秒数达标线，禁止把精确 token 上限称为 GPU 提速实测。

## 7. 本次只读证据与针对性验证

2026-11-06 执行了下列函数探针，未写源码或测试文件。执行位置是具备本项目开发依赖的仓库；不适用于缺少 .venv 的生产宿主机。输出用于证明规则缺口，不冒充完整集成结果。

```bash
PYTHONPATH=apps/api .venv/bin/python - <<'PYTHON'
import ast
from pathlib import Path
from types import SimpleNamespace
from app.tools.entity_context import context_binding
from app.tools.adapters import CueKBAdapter
from app.agent_runtime.evidence import EvidenceGate, presentation_contract
from app.contracts import Citation
entity = {'id': '11111111-1111-4111-8111-111111111111', 'name': 'Golden Yolk', 'aliases': []}
print('F2', context_binding("I don't mean Golden Yolk. Can it be shipped?", [], [entity]))
c = Citation(citation_id='C1', document_id='d', chunk_id='c', version_id='v',
             trace_id='t', retrieval_id='t', rank=1, title='Golden Yolk',
             content='It cannot be shipped. Do not freeze it.')
print('F1 polarity', EvidenceGate.check_presentation('It can be shipped. Do not freeze it.',
      presentation_contract('grounded'), [c]))
c = c.model_copy(update={'content': 'The price is RMB 42. The weight is 65 grams.'})
print('F1 coverage', EvidenceGate.check_presentation('Please check with the manufacturer.',
      presentation_contract('grounded'), [c]))
print('F4', [f.text for f in CueKBAdapter._question_facets('What is its price and weight?')])
p = Path('docs/voicechat/s2s/audio_server.py')
f = next(n for n in ast.walk(ast.parse(p.read_text()))
         if isinstance(n, ast.FunctionDef) and n.name == '_native_consumed_input')
ns = {}
exec(compile(ast.Module(body=[f], type_ignores=[]), str(p), 'exec'), ns)
print('F3', ns['_native_consumed_input'](SimpleNamespace(_native_audio_frame_end=11,
      _input_audio_intervals=[(10, 'new-input')]), 'new-input'))
PYTHON
```

当前输出：F2 返回 Golden 的 confirmed binding；F1 两项均 None（未检出）；F4 一个合并 facet；F3 True。CueKB 部分另对 `30970e9` 的实际 `_assemble` 支持循环进行了只读抽取：固定同一已绑定产品、两条 complete/source_refs 有效的 candidate，正文分别为 price 42 与 weight 65、facet=`What is its price and weight?`，得到 `unassessed`。未调用检索、数据库或模型，未证明生产 Recall。

后续修复的定向场景（在隔离测试环境执行，不能在生产撤权/改原件做故障注入）：

| 项 | 固定输入/时序 | 必须观察到的结果 |
| --- | --- | --- |
| F1 | 以上两条口述反例；另加正确极性、部分答案、冲突、无证据控制样本 | 错误不被认证；正确部分答案保留缺项，不误拒绝 |
| F2 | don't/isn't 撤回、更正为 Black；属性否定 `Can it not be shipped?` | 撤回不绑旧实体；肯定更正才绑新实体；属性否定不撤主体 |
| F3 | D 的半段输入交错；以受控 Provider 保证旧答结束时新 input 尚未 final | 关联不自动等于完成；新问题之后仍被处理，旧 call 不被转移 |
| F4 | 同一产品价格/重量分别两组；比较“price and weight”与拆开两个 facet | 两种表达均正确覆盖或明确可解释降级；错产品不能参与补齐 |
| F5 | 新会话独立 Black/Golden 和同连接追问，固定目录/prompt/hash | 企业问题真实工具调用；无工具一般回答记失败 |
| F6 | 同包/同 tokenizer/同 GPU，对照注入有无并发输入 | 分别报告预检、注入、ASR 和首音频，无漏音/重复消费；未命中交错不判通过 |
| F7 | query 后 valid，暂停预检；在隔离 CueKB 改 content/ACL，再释放 | 最终交付拒绝旧包，正常不变控制组可交付 |
| F8 | 受控 Provider 连续发第二/第三及更多 call，再发独立下一问 | query 不增加、结清有界、无无限注入、原任务终态清楚、下一问可处理 |

现场 A/B/C/D 没覆盖的故障分支，必须用相应受控集成测试补齐；CPU 受控通过仍不代替 GPU/听音。针对 F7 的时序暂停、F8 的强制调用需要后续测试实现，本次未新增测试能力，不能要求操作者凭口述强迫模型产生这些事件。

## 8. 开发检查记录与范围

本次只运行只读探针和文档链接/锚点、差异、命令语法检查，不重跑应用测试。上一代码版本记录：最终定向 120 项、VoiceChat CPU 65 项、Web 41 项及构建通过；后端此前全量 767 中 2 项夹具失败经补齐后纳入定向回归，未声称重新跑完整 767 全绿。这些属于 `2a8ba8b` 的历史开发证据，不覆盖本次新发现 F1—F4，也不是生产放行。

修改业务代码时按影响范围选择现有契约/集成/快照测试；契约变化执行 `PYTHONPATH=apps/api .venv/bin/python scripts/export_contracts.py`。本手册覆盖替换旧版本，不再追加“下一轮第 N 节”。

## 当前修复复测（2026-11-07）

本轮修改 API 的规则与完成判定，VoiceChat/CueKB 运行文件未改。保留以上配套部署和 A/B/C/D 方法。F1 原低层探针仍显示旧检查的局限；实际 Coordinator 现在额外调用 check_facets，因此须用集成回归判断最终认证。F3 起始帧谓词仍不证明整段消费，API 已不据 related 标完成。F4 简单 price/weight 输入现在拆两项；复杂组合仍未解决。

开发回归命令：

```bash
.venv/bin/python -m pytest tests/contract/test_review_regressions.py tests/contract/test_cuekb_evidence_v21.py tests/contract/test_voice_wire_budget.py tests/integration/test_voice_lifecycle_regressions.py tests/integration/test_wire_budget_delivery.py -q
```

定向验证包含缩写撤回/属性否定、属性跨度/产品名 and、极性矛盾/漏项/预算省略，以及尚未 final 的 related 输入后续仍可查询。生产真实 GPU、听音、首问路由和复杂语义不由这些测试替代。

本轮上述定向回归实际结果：70 passed，1 条既有 Starlette 弃用警告；Ruff 与 git diff --check 通过。未部署，未执行真实 GPU/听音。

## Sophon Live 验证

Sophon 部署参数见 [部署与运行](deployment.md#部署方式边界)。目标上游由部署方确认为独立运行的 32B Flash 4-bit。协议审计对照 [Sophon Gateway ace091b](https://github.com/adventists-ai/sophon-realtime/blob/ace091b1841adb2b0588cd609ac06e3dfc261643/live/sophon_live/gateway.py)。独立上游的 `session.started` 证明会话创建，不能凭回显的 `gpt-live-1` 判定引擎版本。

先用浏览器麦克风在同一次通话内测试以下场景，记录时间和关联 ID。

1. 问“你好，你是谁？”，听到自然回复并看到字幕。确认没有 Delegation。
2. 自然停顿后问“帮我查询一下 FTTR 主网关经常离线是什么原因”。核对原始 Delegation 的 `task`、CueKB query/validate、Commentary、口述与页面来源。单独核对 ASR 字幕是否忠实于原话。
3. 口述中插话换题，核对旧播放清理和下一轮接话。另在 CueKB 查询期间插话，旧查询继续结清，证据不得注入新问题。复测同题重问使用新输入 ID，及天气请求等不支持的委托。
4. 切回 NVIDIA，用本文英文样本复测其原生工具、播报和打断。

录音用现有的 WAV 入口。清单每项包含 `case_id`、`wav` 和 `expected_tool`。WAV 为 24 kHz 单声道 PCM16，闲聊工具标为 `null`，知识问答标为 `"search_knowledge_base"`。实际录音和报告保存到仓库以外。

```bash
PYTHONPATH=apps/api:. python scripts/validate_native_cuekb.py \
  --base-url https://portal.example.com:8087 --provider sophon_live \
  --cases /tmp/sophon-cases.json --case-id chat_zh --followup-case-id kb_zh \
  --output /tmp/sophon-report.json
```

报告按原始 Delegation 计算 TP/FP/TN/FN。后端拒绝的误委托仍属于 FP。观察不全的样本单独标记。报告分别统计 Gateway 回执、候选媒体片段结束、候选片段数和延迟。多个候选片段可包含等待话术和后续语音，不能据此确认答复已结束。Sophon 报告保持 `answer_complete=unverified`；浏览器实际播出、自然接话及语义准确性需按麦克风流程确认。

Sophon WebSocket 仅执行明确、可归属的 `CUEKB:` 任务。空任务或未声明的路由请模型澄清。`offset_ms` 是发送时间，以它为上限回看 8 秒时有多段可能音频，或处理时已换题，要求重述。每输入最多一次查询。ASR 时间戳用于字幕归属，业务参数使用模型 `task`。固定版本 Gateway 在模型已生成工具调用后可能等待 ASR 再发委派事件，测时需区别模型委派和接收到协议通知。 [固定 Gateway 源码](https://github.com/adventists-ai/sophon-realtime/blob/ace091b1841adb2b0588cd609ac06e3dfc261643/live/sophon_live/gateway.py)

每次证据回传分别记录发送、匹配到的 Gateway 回执和附近的候选音频。回执匹配用本次 `event_id` 对应的 `client_event_id`。真实引擎是否消费无从回执判断。门户以候选字幕继续展示实时语音，资料来源可复核，业务状态保留语音未核实。换题时清理当前的旧播放，继续接收后续语音，不因猜测晚到语音而丢弃下一段；可能晚到的口述不写入可信历史。现有公开 Live 协议缺少原始输入 ID、引擎消费确认及由该结果生成的响应 ID 和终态。可靠认证需上游提供这些关联证据，首段时间戳问题需在上游修复并复测。

2026-11-21 本机通过 6 个 Sophon WebSocket/CueKB 协议夹具、41 个 Web 单元测试和 5 个浏览器语音夹具测试；Web 构建、API fixture 启动、契约导出和静态检查通过。全量 Python 回归仍有原生 NVIDIA 和部署文档等历史失败。

同日审计修复复测：192 项定向 Python 测试通过，包括 11 项 Sophon 集成场景。42 项 Web 单测、5 项浏览器语音夹具、Web 构建、契约导出和静态检查通过。在官方 Gateway 源码接替代推理引擎的环境里，确认延迟委托遇到两段可能输入时被拒绝。夹具确认进度话术和后续语音均保留为候选，两段播放完成后业务状态仍为语音未核实。

早期 WSS 探测曾出现 TLS 超时。当前的真实 24 kHz 握手、回传与打断结果记录在下节。既有门户仍需验证中文输入 ASR、真实 CueKB 和浏览器听音。

## LiveKit Sophon Phase 0

2026-11-21，开发分支基于 `58bdb45`。24 kHz WAV 探针使用 LiveKit Server 1.13.11、Agents 和 OpenAI 插件 1.8.10。通过 Python RTC SDK 加入同一私有 Room 的两个会话，使用官方 `AgentSession` 和 `GPTLiveModel(delegation="client")`。完整 Sophon WSS URL 可直接用作 `base_url`。官方 Sophon LiveKit 示例采用 RealtimeModel，另有 GPT-Live 路径的协议要求。 [GPT-Live 文档](https://docs.livekit.io/agents/models/realtime/gpt-live/) · [Sophon 官方示例](https://github.com/adventists-ai/sophon-realtime/blob/main/live/examples/livekit_agent.py)

### 受控结果

- 使用固定提交的官方 Gateway，替换 ASR 和推理引擎。已收到启用客户端委派及 `store=false` 的 `session.started`，输入到达替身引擎，回放端收到 24 kHz 音频并完成正常关闭。
- SDK 提供原委派 ID 和 `pending_transcript`。Sophon 的 `task` 可经 SDK 公开的 `openai_server_event_received` 回调按 ID 读取，SDK 委派对象只保留 ID 与转写。受控回传用该 ID 调用 `append_commentary`，按发送事件 ID 匹配 Gateway 回执，替身引擎也收到结果。真实场景仍需验证转写的完整性与输入归属。 [固定 SDK 源码](https://github.com/livekit/agents/blob/ff962a019b6500e08f8f1fcd7f55673361bd9e91/livekit-agents/livekit/agents/llm/_realtime/gpt_live.py) · [固定 Gateway 源码](https://github.com/adventists-ai/sophon-realtime/blob/ace091b1841adb2b0588cd609ac06e3dfc261643/live/sophon_live/gateway.py)
- 在纯音 WAV 测试里，默认 AEC 预热期间，Sophon 未收到可触发打断的输入。探针使用直接发布的 PCM，关闭其 AEC 预热后，Gateway 记录打断，Room 录到第一段 660 Hz 音频从约 3.55 秒缩短到约 0.35 秒，随后收到约 0.85 秒的 770 Hz 下一段。浏览器麦克风的回声消除与实际播放清理仍待验。
- 直接把候选 SDK 版本加入现有依赖的 dry run 失败。项目固定 `opentelemetry-api==1.44.0` 和 `openai==3.8.0`，候选 SDK 分别需要 `opentelemetry-api>=1.45,<1.46` 和 `openai>=3.28,<4`。探针使用隔离虚拟环境。生产依赖合并及既有 OpenAI Agents API 回归归 Phase 1。
- 现有 Sophon 集成和 CueKB 2.1 契约复跑共 51 项通过。首次运行的 `barge_after_ack` 曾在读取历史时得到尚未写入的 answer。重构前全量 Python 基线为 694 项通过、98 项失败。Phase 0 独立于生产入口和依赖清单。

### 真实 Sophon 结果

- 2026-11-21 复测，同一地址在无 Authorization 时完成 HTTPS Upgrade 和 24 kHz `session.started`，送入占位 Bearer 会得到 HTTP 401。`sophon_gpt_live` 在未配置密钥时，通过专用 aiohttp session 省略 SDK 的占位请求头。有密钥时由官方 SDK 发送。TLS 校验保持开启。 [aiohttp 客户端中间件](https://docs.aiohttp.org/en/stable/client_advanced.html#client-middleware)
- 真实 Room 的中文 WAV 触发了委派。原始 `task` 含“校验码”，匹配收到 Commentary 回执。官方音频轨接收到回应，离线转写辨识到校验码 6429。打断用例中，收到的房间音频由等待话术转到下一题的身份回答，连接正常关闭。校验码是探针返回的数据，真实工具和浏览器扬声器尚待验收。
- 无打断和打断两类通话均未收到 `session.input_transcript.delta`，SDK 的 `pending_transcript` 为空。探针用原始事件计数区分未发送和 SDK 解析问题。WebSocket Adapter 直连复测也收到音频、输出字幕、委派和回执，输入字幕为零。使用固定版 Gateway 的部署可核对 `--asr`／`SOPHON_LIVE_ASR` 的实际值及 ASR 初始化日志。 [Gateway ASR 启动逻辑](https://github.com/adventists-ai/sophon-realtime/blob/ace091b1841adb2b0588cd609ac06e3dfc261643/live/sophon_live/gateway.py)

### 复测

在仓库根目录创建独立 Python 3.12 环境。先在单独终端运行官方开发服务器 `livekit-server --dev --bind 127.0.0.1`。

```bash
probe_dir=$(mktemp -d /tmp/voice-livekit-phase0.XXXXXX)
python3.12 -m venv "$probe_dir/venv"
probe_python="$probe_dir/venv/bin/python"
"$probe_python" -m pip install livekit-agents==1.8.10 livekit-plugins-openai==1.8.10 \
  livekit-local-inference==0.2.7 websockets==15.0.1
export LIVEKIT_URL=ws://127.0.0.1:7880 LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=secret
export PYTHONPATH=apps/api
```

受控复测使用以下 Gateway 与纯音样本。在一个终端运行最后一行启动夹具。

```bash
curl -fsSL 'https://raw.githubusercontent.com/adventists-ai/sophon-realtime/ace091b1841adb2b0588cd609ac06e3dfc261643/live/sophon_live/gateway.py' \
  -o "$probe_dir/gateway.py"
printf '%s  %s\n' '32d3bba7d45be8b699cfd622e28db20eab9c2fc53a33b394f21e08f8b9dc393d' "$probe_dir/gateway.py" | shasum -a 256 -c
ffmpeg -hide_banner -loglevel error -f lavfi -i 'sine=frequency=523:duration=0.65' \
  -af 'apad=pad_dur=0.6' -ar 24000 -ac 1 -c:a pcm_s16le "$probe_dir/input.wav"
ffmpeg -hide_banner -loglevel error -f lavfi -i 'sine=frequency=392:duration=1.1' \
  -af 'volume=4,apad=pad_dur=0.6' -ar 24000 -ac 1 -c:a pcm_s16le "$probe_dir/barge.wav"
"$probe_python" tests/fixtures/sophon_phase0_gateway.py "$probe_dir/gateway.py"
```

在保留 `probe_dir`、`probe_python` 及 LiveKit 环境变量的终端运行探针。检查 JSON 中的 24 kHz、委派 ID、`delegation_passed=true`、原始 `task`、匹配回执及 `close_confirmed=true`。当设置 `--expected-transcript` 时，同时检查 `input_transcript_matches=true`。检查夹具的 `playback dropped` 日志和 WAV 中旧音频后的下一段音频。

```bash
SOPHON_LIVE_WS_URL=ws://127.0.0.1:18782/v1/live/sessions SOPHON_LIVE_API_KEY= \
  "$probe_python" scripts/probe_livekit_sophon.py --language en \
  --wav "$probe_dir/input.wav" --barge-wav "$probe_dir/barge.wav" \
  --expected-task "verification code" --expected-transcript "verification code" \
  --output "$probe_dir/room-fixture.wav"
```

真实复测须从可达的验证环境运行。准备两段 24 kHz 单声道 PCM16 WAV。第一段问“请查询本次验证使用的校验码”，第二段要求停止刚才的回答并换题。先完整听一次结果，再开新通话在回复开始后打断。上游启用认证时通过环境配置 `SOPHON_LIVE_API_KEY`。

```bash
export SOPHON_LIVE_WS_URL=wss://sx.ppy8.xyz:14080/v1/live/sessions
"$probe_python" scripts/probe_livekit_sophon.py \
  --wav "$probe_dir/request.wav" \
  --expected-task "校验码" --expected-transcript "校验码" --expected-reply "六四二九" \
  --output "$probe_dir/room-live.wav"
"$probe_python" scripts/probe_livekit_sophon.py \
  --wav "$probe_dir/request.wav" --barge-wav "$probe_dir/interrupt.wav" \
  --expected-task "校验码" --output "$probe_dir/room-live-barge.wav"
```

真实放行核对原始委派、中文输入字幕、回传校验码和同连接打断后的新回答。`delegation_passed` 单独报告委派结果；只有指定 `--expected-transcript` 才将 ASR 字幕匹配纳入探针退出状态。耳麦或设备扬声器体验在门户单独验收。

## LiveKit 门户与工具验收

2026-11-22 10:47（北京时间），当前执行机访问 `wss://sx.ppy8.xyz:14080/v1/live/sessions` 时在 WebSocket Upgrade 收到 Nginx HTTP 502。同日 14:48 使用 Python `websockets` 对同一地址复测，禁用客户端代理且保持 TLS 证书校验。DNS、TCP、TLS 1.3 和 WebSocket `101 Switching Protocols` 通过。发送 `session.start` 后收到 `session.started`，返回 `status=active`、24 kHz PCM、`delegation.type=client`、`store=false`；发送 `session.close` 后收到 `session.closed`。本次只发送会话控制消息。线上 ASR 字幕和任务委派仍需使用上节语音探针复测 `input_transcript_matches` 和 `delegation_passed`。

本地浏览器测试使用真实 LiveKit Server、固定提交的官方 Gateway 和替身推理引擎。测试开启两个 Room，检查委派来源显示、候选口述、WebRTC 音轨、控制 WebSocket 无 PCM、停止播放及断开。先启动上节的 LiveKit Server，并下载校验 gateway.py。用仓库的开发依赖运行以下夹具：

```bash
.venv/bin/python tests/fixtures/sophon_phase0_gateway.py "$probe_dir/gateway.py" \
  --language zh --task 'CUEKB: Find the integration sample.' \
  --expected-commentary '"search":"finished"' --no-input-transcript
```

应用与 Vite 分别启动。以下应用配置使用临时 SQLite 与 CueKB mock，只服务本机：

```bash
ROOM_TEST_DATABASE="$probe_dir/portal.db" PYTHONPATH=apps/api .venv/bin/python - <<'PYTHON'
import os
import uvicorn
from app.config import Settings
from app.main import create_app

settings = Settings(
    _env_file=None, auto_create_schema=True,
    database_url="sqlite+aiosqlite:///" + os.environ["ROOM_TEST_DATABASE"],
    qa_execution_mode="dual_tools", agent_provider="disabled", voice_provider="sophon_live",
    sophon_live_ws_url="ws://127.0.0.1:18782/v1/live/sessions",
    livekit_url="ws://127.0.0.1:7880", livekit_api_key="devkey", livekit_api_secret="secret",
)
uvicorn.run(create_app(settings), host="127.0.0.1", port=8000)
PYTHON
```

Vite 使用 npm --prefix apps/web run dev。回归从仓库根目录执行：

```bash
ffmpeg -hide_banner -loglevel error -f lavfi -i 'sine=frequency=523:duration=0.85' \
  -af 'apad=pad_dur=0.8' -ar 24000 -ac 1 -c:a pcm_s16le "$probe_dir/room.wav"
LIVEKIT_E2E_WAV="$probe_dir/room.wav" npm --prefix apps/web run test:e2e -- livekit-room.spec.ts
.venv/bin/python -m pytest -q tests/integration/test_livekit_delegation.py
```

复测字幕时，去掉夹具的 `--no-input-transcript` 并用 `LIVEKIT_E2E_TRANSCRIPT='What is the verification code?'` 重跑浏览器测试。页面应同时显示夹具字幕和独立委派文本 `Find the integration sample.`。

三类工具回归通过现有 Registry 和 HTTP 夹具。检查 CueKB query/validate、天气地点和日期、公开来源、重复任务、短停顿、失败和未经授权的外发。真实部署使用麦克风分别测试闲聊、授权企业资料、天气、公开搜索及自然追问，在旧回复中插话并核对下一轮。记录原始委派 ID、工具执行与回执。使用实际音频核对口述，回执与候选字幕均保留为传输证据。

2026-11-22 当前工具升级基于 `3c2dda8`，实现 QWeather/Tavily，保留私有 CueKB；修复 LiveKit 已结清输入阻塞快速追问，以及宿主 Bash 3.2 空数组导致的采集失败。供应商与多轮定向 Python 回归 211 项通过；最后一次 Router 调整后的供应商/LiveKit 定向复测 47 项通过。Web 单测 42 项、TypeScript/Web 构建、Ruff、契约导出、编译、shell 语法、依赖一致性及 diff 检查通过。构建仍提示 LiveKit chunk 大于 500 kB；Starlette 有既有弃用警告。本轮没有重跑浏览器 Room 夹具，也没有部署生产 Compose。

全量 Python 首次运行记录为 807 passed、27 failed；其中环境模板测试在运行期间完成适配，随后定向复测通过，未将第一次结果改写为全绿。其余 26 项在 `3c2dda8` 源码导出目录中逐项复现（26 failed），失败节点和各模块主要症状一致：continuous_dialogue 5、delivery_lifecycle 1、live_agent_interactions 4、native_cuekb 1、qa_routing 1、simulated_full_flow 14。表现包括等待答案超时、audio.done 前断连、追问缺失及 WebSocketDisconnect；这些既有失败本轮未修复，不能宣布全仓回归通过。

使用用户提供的真实凭据直接请求供应商 API：QWeather GeoAPI 返回北京及行政区候选，唯一精确名称确定北京；实况与明日预报均返回 `status=ok`、来源和非过期数据。Tavily 以公开 LiveKit 文档查询返回三条来源。密钥通过请求头发送，只保存在本地被忽略、权限 0600 的 `.env`，不进入仓库。这些是 Adapter/API 证据，不证明真实 CueKB、Sophon 消费结果、浏览器发声或口述正确。

现场验收仍按本节先启动 LiveKit、再部署应用的方式执行。分别测试闲聊、北京实况、东京明日预报、私有 CueKB、公开 Tavily 搜索；补充无地点后澄清、同名城市、超出未来十日、工具超时，以及一轮结清后立即追问/主动重复查询。每次记录 delegation ID、工具次数、输入窗口、Commentary 回执和实际听音。正在执行/回执未知时换题仍保守拒绝歧义；上游缺少稳定输入 ID 时，不能将延迟旧任务的新 ID 认证为新问题。至少三十条有实际音频与结果的语音样本、两个以上真实会话、路由准确率、误委派率及端到端时延仍待验收，现有纯文本用例不能作为通过记录。


## 官方 14 录音的 Web 全流程验证（2026-11-22）

本次通过项目现有 Web 的 Audio file → Choose WAV → Start file call 逐条输入，未截获/伪造 API、WebSocket 或工具响应。上游测试材料固定为 `6b1713518eb75f8495415662870f682357372d09` 的 `live/cases/cases.json` 和对应 WAV：[官方清单](https://github.com/adventists-ai/sophon-realtime/blob/6b1713518eb75f8495415662870f682357372d09/live/cases/cases.json)。原录音仅用 FFmpeg 重采样到 24 kHz mono PCM16，没有裁剪、变速或修改停顿；逐样例保存原文件/输入文件 SHA256。官方脚本自身使用 mock backend，本次业务链未使用 mock。

环境为本机真实 LiveKit Server 1.13.11、Agents/OpenAI 插件 1.8.10、Chrome/Playwright、真实 Sophon `wss://sx.ppy8.xyz:14080/v1/live/sessions` 与 QWeather/Tavily。身份是隔离的本地 fixture identity，会话存储为临时 SQLite；没有部署生产 Compose、验证生产身份或审计远端 GPU/模型版本。CueKB 未接入，按已确认的规则保留入口并转公开搜索；两字段部分配置或真实私有服务失败不外发。每个样例独立 Room，中文/英文分别使用 zh-CN/en-US 的应用实例，串行执行。每条在录音播完后继续观察 20 秒。窗口结束不代表回答终态。

初轮 14 条都有有效音频，但委派为 0，且无输入 ASR 时页面隐藏了已收到的候选字幕。该 UI 问题已修复：显示实际输出字幕和 “Voice input · Transcript unavailable”，不填造用户原话，不认证业务回答。浏览器回归覆盖空 ASR、候选输出及迟到真实 ASR 替换。另明确了原生 delegate 调用提示；单次天气补测通过，但后续正式复测未稳定复现，不能标为已修复原生选工具。

最终正式复测如下：所有 14 条都收到浏览器 MediaStream 中的有效音频及可见输出字幕；输入 ASR 的原始事件和页面字幕均为 0。控制 WebSocket 中 PCM 帧为 0，音频确经 WebRTC。首段有效音频以录音最后有声的 20 ms 窗口（RMS > 300/32768）结束，到浏览器检测到峰值 > 600/32768 计时，包含 RTC/浏览器缓冲；中位数 738.5 ms，范围 674–1662 ms。首段可能只是等待话术，不能当作最终工具答案时延或与官方 H100 推理延迟直接比较。

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

官方分类一致性为 8/14：不需工具的 7 条均未误委派；应委派的 7 条只有 zh_news 发出原生委派，但任务路由未被识别，客户端返回澄清，正式复测工具执行为 0。官方清单含时间、关灯和个人记忆等超出本项目三工具范围的用例，保留官方分母，同时明确不把这些请求的模拟完成算业务成功。天气和新闻等已在本项目范围内的请求也未稳定完成，因此工具闭环验收失败。

对照与补测没有替换正式结果：使用官方 persona 直连同一 Sophon，上海天气收到自然语言 task 的原生委派；明确 native delegate 参数的直连对照也收到 WEATHER task。Web 单次天气补测得到一次真实 QWeather 执行（325 ms）、匹配 Commentary 回执及候选口述，但正式重跑又漏调。两条额外的 macOS Tingting 合成输入（公开产品参数、F7005 参数，均非官方录音）各发出一次原生委派，仍因任务路由未识别而澄清，未执行 Tavily。不能将 API 冒烟或格式正确的受控集成当作这些语音请求已经通过。

该次正式测试阻塞为两类委派问题：模型可能只说等待话术/工具标签而不调用 native delegate；实际 native task 也可能不符合本项目严格前缀契约，导致 Router 澄清。当时还需核对部署的原生 delegate 描述/参数约束与真实调用输出，以及输入 ASR 的部署配置和初始化。没有启用 ASR 补调、分类模型、工具强制选择或伪造调用。现有协议仍不能认证某段口述属于某次结果的最终答案，所有候选保持 unverified。英文长回答可能在观察窗口结束时仍在输出，报告保存已观察部分，不宣称完整回答结束。

复跑入口是 `scripts/validate_sophon_web.mjs`，需要已启动的真实 API/Web/LiveKit、项目 Web 开发依赖、Chrome/Playwright 和 ffmpeg。按部署文档配置公网工具与未接入 CueKB 状态。server-log 为该次 API 实例的当前日志文件，用 conversation_id 关联，报告不保存请求票据或 Room Token。中文/英文运行时必须分别配套 SOPHON_LIVE_LOCALE，输出到不同目录：

```bash
OFFICIAL_DIR=$(mktemp -d /tmp/sophon-official-XXXXXX)
GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/adventists-ai/sophon-realtime.git "$OFFICIAL_DIR/source"
git -C "$OFFICIAL_DIR/source" fetch --depth 1 origin 6b1713518eb75f8495415662870f682357372d09
git -C "$OFFICIAL_DIR/source" checkout --detach FETCH_HEAD
git -C "$OFFICIAL_DIR/source" sparse-checkout set live
node scripts/validate_sophon_web.mjs \
  --cases "$OFFICIAL_DIR/source/live/cases/cases.json" \
  --source-revision 6b1713518eb75f8495415662870f682357372d09 \
  --base-url http://localhost:5173 --language zh \
  --server-log /tmp/current-api-zh.log --output "$OFFICIAL_DIR/report-zh"
# 使用 en-US 的真实应用实例，提供它自己的当前日志：
node scripts/validate_sophon_web.mjs \
  --cases "$OFFICIAL_DIR/source/live/cases/cases.json" \
  --source-revision 6b1713518eb75f8495415662870f682357372d09 \
  --base-url http://localhost:5173 --language en \
  --server-log /tmp/current-api-en.log --output "$OFFICIAL_DIR/report-en"
```

本次原始材料保存在仓库外 `/tmp/sophon-web-14.aENraE`：`run-manifest.json`、`final-14.json`、`results/final-zh`、`results/final-en`、`results/native-zh`、`results/supplement`；目录含每条截图、浏览器返回 PCM WAV 和 JSON。现场测试结束后，LiveKit 查询为 0 Room、0 participant。开发检查：118 项定向 Python、13 项浏览器文件入口回归、42 项 Web 单测、Web 构建、Ruff、契约导出及 shell/JS 语法通过；这些不改变真实语音工具验收失败的结论。

## 原生委派与 Router 分项修复复测

本轮基于 `06aafd936fca6f6f2fe90768ceaf59e9c0e6d386`，修改前 local、origin/main、GitHub 实际 main 一致。正式失败基线仍是上节的 1/7：只有 zh_news 原生委派且无法路由，其余六条无原生事件；不能覆盖该事实。所有新报告包含各源码 SHA256、录音 SHA256、原始原生 ID、task、解析方式、工具执行日志、回执和候选字幕；不是将期望值写成结果。

### A：模型没有产生原生委派

核对当前 SDK 的 `session.start` 构造和固定上游 `6b1713518eb75f8495415662870f682357372d09`：GPTLiveModel 使用 client delegation，Gateway 为引擎注册唯一 `delegate(task)`，引擎 Bridge 的 tool_call 经 Gateway 转成 session.delegation.created。Gateway 的 task schema 本来就是自然语言一句话，不要求本项目的三种前缀。[固定上游定义](https://github.com/adventists-ai/sophon-realtime/blob/6b1713518eb75f8495415662870f682357372d09/live/sophon_live/gateway.py)。这次未访问部署引擎进程、权重或最终工具模板，不能将源码核对说成部署身份审计。

缩减格式负担，将 Persona 与固定规则分开，明确等待话术后必须实际调用原生函数。中文先复测 5/5 触发；英文首轮仍是 en_time 只说 “I'll check” 而无事件，完整 14 项当时为 6/7。使用同一官方 en_time WAV 直连，原指令再次漏委派；官方短 persona 和明确函数名、task 字符串参数、同一响应实际调用要求的候选规则各产生一次原生事件。采用候选英文规则后，Web en_time 正式复跑和独立重复均产生原生事件。对照保留在 native-current.json、native-official.json、native-candidate.json；没有用 ASR、等待话术、客户端关键词或假事件补调用。

### B：已经委派但 Router 无法解析

保留结构化优先，新增原生 task 自然语言规则。实测 task 包括“明天上海会下雨吗”“今天有什么科技新闻”“把客厅的灯关掉”“用户上次说喜欢吃什么”，分别进入 WEATHER、WEB_SEARCH、UNSUPPORTED、UNSUPPORTED。天气地点/日期不明和多路匹配则 CLARIFY；CueKB 仍走原 query/validate 与权限路径。任务只有在原生事件存在且音频窗口可靠时执行，ASR 文本不进入解析接口。

早期关灯和记忆的两种真实表述曾落入 CLARIFY，已根据上述 task 补规则并在后续官方录音中复验为 UNSUPPORTED；未调用 Tavily 伪装执行。公网回退只允许 CueKB 地址与凭据都为空、task 明确公开且无敏感信息；内部信息、账号、客户、日志、SN、邮箱/IP/电话均阻止外发。CueKB 配置存在或服务失败不触发公网回退。

### 官方 14 项分项结果

最后一轮完整中文、英文均为整组重跑（delivery-zh、delivery-en），汇总 delivered-14.json：官方预期委派 **7/7**，无需委派 **7/7** 未误调，ASR 可见 **14/14**，已原生委派且属于三工具范围的路由 **3/3**。三次真实工具调用和工具结果回执均收到，天气/新闻中文两例有可用业务结果；英文天气返回地点澄清，不能算第三个天气答案。14 条均观察到音轨和候选输出，最终口述认证仍为 0。这些数据没有将时间、灯光、记忆的“不支持”结清计成业务执行成功。

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

首段候选音频可能是等待语或全双工重叠输出，不是最终答案端到端时延。中文、英文的 Provider/Router/指令源码与交付内容一致；中文整组后仅 App.tsx 的角色设置准备增加取消/互斥保护，该路径未在官方清单中使用，已单独纳入最终 15 项浏览器回归。每组实际源码 hash 均保留，不把它们伪称为同一全仓快照或生产部署通过。前面的 initial-zh、official-zh、final-zh、final-en、final-en-v2 及 repeat-en-time 仍保留，包括英文首次失败，未挑选同组内的成功案例替换失败。

### ASR、角色与验收边界

真实新测试已观察到中文与英文输入字幕，不能再用旧环境 0/14 描述当前状态。ASR 延迟导致无法唯一关联时仅产生 association=unbound 展示字幕，不生成音频输入或授权。正常、迟到、空、错误和关闭五种状态的同一合法 task 本地测试保持同一选择，ASR 本身不会将任务入队。未改动独立 Gateway 的 ASR 开关，真实开启/关闭对照仍需部署方提供两种环境；asr_enabled 不能确认时报告为 null，是否实际收到字幕单列。

角色 API 按 owner_id 持久化，validation 使用 `?conversation_id=<当前通话>` 加原 Bearer token。覆盖 CRUD、跨用户 401/404、禁止提交固定 instructions、损坏/删除角色回退默认、两条传输的快照不被修改。数据库 0006 → head(0011) → 0007/0006 → head 及 Alembic check 保留已有历史；PostgreSQL 现场迁移未执行。真实 WebSocket 接入已分别用闹闹和小狗回答“你叫什么名字”，均有对应输出字幕、PCM 音频、输入字幕且无错误；这些是网络与候选文本证据，未做人耳听音认证。

LiveKit 默认闹闹和自定义小狗的名字也在真实候选自我介绍中出现；小狗配置下北京天气、公开语音模型检索均有原生事件、正确路由、真实工具执行和匹配回执（profile-xiaogou/summary.json 的 2/2）。小狗介绍额外自称“AI2 的语音伙伴”，该归属不在配置中，不能认证完整 Persona 遵循或把这句话当事实；仅确认选定名字已生效。门户 CRUD 和语言风格修改经真实本地 API 的浏览器测试，活动会话快照不变；首次实现曾复用文件重跑的通话，已修复并以 14 项浏览器回归确认新 owner/token 和历史清理。

真实 CueKB 尚未接入，不能以受控 evidence query/validate 测试替代私有知识验收。Melbourne 同名地点仍要求国家/地区澄清：Registry 的 status=ok 表示供应商调用正常，不表示返回了可用天气答案。报告另列 business_outcomes/business_answered_cases，不能将工具 HTTP 成功、Commentary 回执或首段等待语音当作最终回答成功。所有 final_answer_verified 保持 false。完整多轮打断的真实模型与最终口述归属仍需验收。

补充的七条 Tingting 合成输入不是官方录音。北京实况、开源语音模型和明确公开产品查询均产生真实工具执行及回执；不明确的 F7005 参数请求返回 CLARIFY，未知地点天气返回 CLARIFY，内部客户日志被模型直接拒绝，均未据此外发敏感内容。缺地点样例发生了原生委派后再澄清，内部日志未委派而直接拒绝，分别不符合该补充清单最初的 native 标签；业务边界与 native 标签独立报告，没有将标签差异藏掉。F7005 在两次 task 中变成 F705，包括公开搜索 task：这是模型生成参数的保真失败，不能因 Tavily status=ok 就认为检索了正确产品。原 task 在 Router 中保留，ASR 未被用来修正工具参数；此项仍未解决，需模型侧核查和真实型号金标。

### 当前运行与回传入口

原始资料在仓库外 `/var/folders/1x/568sjwqd64z3p8prx9d5338w0000gn/T/sophon-implementation-6oiitmvq`，便捷路径记录在 `/tmp/sophon-implementation-path`。官方材料仍固定为 `6b1713518eb75f8495415662870f682357372d09/live/cases`。每次正式运行使用对应 locale 的真实 API/Web/LiveKit，server-log 必须是该实例日志；报告目录相互独立，不能用挑选成功样本替换同轮失败。

```bash
# 先按 deployment.md 启动本版本 API/Web/LiveKit 和真实 Sophon、公网工具。
# 角色表需先通过现有迁移流程升级到 0011。
SOPHON_CASES_DIR=/absolute/path/to/pinned-sophon/live/cases
SOPHON_REPORT_DIR=/absolute/path/to/new-report
node scripts/validate_sophon_web.mjs \
  --cases "$SOPHON_CASES_DIR/cases.json" \
  --source-revision 6b1713518eb75f8495415662870f682357372d09 \
  --language zh --base-url http://localhost:5173 \
  --server-log /absolute/path/to/current-api-zh.log \
  --output "$SOPHON_REPORT_DIR/zh"
# 使用 en-US 实例及它自己的日志，再运行 --language en，输出至 /en。
# 仅在独立 Gateway 的配置已获确认时增加 --asr-state enabled 或 disabled。
# 角色语音回归可加 --profile-json /absolute/path/to/profile.json；
# JSON 仅包含 name、description、language_style，通过真实门户创建/选择。
.venv/bin/python -m pytest -q tests/contract/test_delegation_router.py \
  tests/contract/test_public_tools.py tests/integration/test_voice_profiles.py \
  tests/integration/test_sophon_live.py tests/integration/test_livekit_delegation.py \
  tests/integration/test_auth.py tests/integration/test_native_tool_catalog.py \
  tests/contract/test_qa_migration.py
npm --prefix apps/web test
npm --prefix apps/web run build
(cd apps/web && npx playwright test voice-file.spec.ts voice-profiles.spec.ts)
PYTHONPATH=apps/api .venv/bin/python scripts/export_contracts.py
git diff --check
```

本轮定向 Python 152 项通过、Web 单测 42 项、浏览器文件与角色 15 项通过（包含设置准备与开始/结束通话的竞争取消），TypeScript/Web 构建、Ruff、契约导出、JS 语法、迁移和 diff 检查通过。构建保留 LiveKit chunk >500 kB 提示与 Starlette 既有弃用告警。全量首次 853 通过、28 失败，其中新增迁移版本断言已修正并复验；余下 27 个失败节点全部在原始 main 的隔离 git archive 中复现（27 失败、1 通过），含 NVIDIA/旧 CueKB 及 followup 路径，未顺带修改这些系统。全量日志 pytest-all.log、baseline-failures.log 与最终定向 final-scoped.log 均保留；不能将定向全绿说成全仓全绿。
