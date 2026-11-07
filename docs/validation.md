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
