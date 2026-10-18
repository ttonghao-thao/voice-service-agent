# VoiceChat + CueKB 一期原生 Function Calling 验证

目标是验证 VoiceChat 自主区分普通对话/通用知识与产品/企业文档请求。实际路径只有直接语音回答，或 `search_knowledge_base(query) → Knowledge Adapter → CueKB /v1/search → 精简证据 → VoiceChat 语音回答`。公开工具名和服务端可信 implementation 统一为 `search_knowledge_base`；CueKB 只表示后端知识库系统。没有前置 ASR、分类器、额外答案模型或应用强制工具路由。

## 1. 部署配置与已有目录

使用 [部署配置](deployment.md#问答模式与预算q07) 和 `.env.example`。一期设置 AGENT_PROVIDER=disabled、dual_tools/general_qa、cuekb-native-v1。不需要 OpenAI key 或文本模型 endpoint。复制 [VoiceChat 模板](voicechat/docker-run.md) 后重新创建容器，不需要修改权重/融合参数。

部署脚本在数据库迁移后、API 启动前自动执行 `configure_native_cuekb.py --apply`，将已保存目录更新为唯一的 search_knowledge_base；保留 revision/CAS、审计和历史数据。手工部署需用新 API 镜像在同一数据库环境执行该命令；省略 --apply 只预览。不需要 TOOLS_ADMIN_API_KEY。

实际 `/api/v1/capabilities` 应包含：

```json
{
  "provider": "nvidia",
  "agent_provider": "disabled",
  "qa_execution_mode": "dual_tools",
  "answer_policy": "general_qa",
  "qa_toolset_version": "cuekb-native-v1",
  "native_tools": ["search_knowledge_base"],
  "text_configured": false,
  "voice_available": true,
  "is_mock": false
}
```

新建 Conversation 后开始通话。旧 knowledge_required 会话仍会拒绝无工具回答，不能复用。工具 description、query schema、ACK 在 session.update 中注册，ACK 为 `Let me check the documentation.`。模型只填写 query，不能填写 KB、key、scope 或结构化产品/版本过滤字段。

## 2. 真实英文语音样本

门户支持麦克风和不超过 30 秒/2 MiB 的 24 kHz 单声道 PCM16 WAV。使用真实英文录音，固定产品名/型号/版本应来自现有授权知识库；不要以机器音、脚本 ASR 文本或合成工具事件替代模型验收。

| 类别 | 示例 / 预期 |
| --- | --- |
| 问候 | Hello, how are you? → 直接回答，无工具 |
| 科学常识 | Why does water boil? → 直接回答，无工具 |
| 通用技术 | What is the difference between TCP and UDP? → 直接回答，无工具 |
| 产品文档 | What does the manual say about the connection limit for Product AX? → search_knowledge_base |
| 消费品规格 | What does the company documentation say about its price, weight, storage requirements and shelf life? → search_knowledge_base |
| 混合表达 | Hello, please explain the documented configuration steps for Product AX. → search_knowledge_base |
| 连续追问 | 同通话先说产品名，再问 How do I configure this product? → query 应保留已知产品 |
| 缺失必要产品 | 新通话问 How do I configure that device? → 自然澄清，不得编造型号 |
| 版本差异 | 使用知识库中真实存在的型号与两个版本，要求保留版本和限制 → search_knowledge_base |

工具未调用不能自动判为错误：普通问题应避免调用，缺少必要信息也可能先澄清。产品问题有调用后，分别检查 query 是否保留原意、CueKB 是否检到相关证据、VoiceChat 是否忠实口述。

## 3. 从宿主机在 API 容器内运行固定 WAV

`scripts/validate_native_cuekb.py` 必须在 API 容器内运行；宿主机只负责执行 `docker exec`、`docker cp` 和保存结果。脚本通过 API 容器的 loopback HTTP/WS 进入实际 Portal Gateway，工具仍由本项目调用真实 CueKB，不生成合成工具结果。它为一组顺序录音创建一条会话，可检查连续追问。

以下命令都从仓库根目录在**宿主机**执行。示例使用生产 Compose 中的 `api` 服务和独立的 `nemotron-labs-voicechat` 容器。不要使用 `c5f` 之类的短 ID；容器重建后短 ID 会改变。

### 3.1 准备宿主机输入

在忽略目录 `artifacts/native-cuekb/cases/` 放入 `cases.json` 和授权 WAV。WAV 路径相对 `cases.json`；例如：

```json
[
  {"case_id": "greeting", "wav": "hello.wav", "expected_tool": null},
  {"case_id": "general_tech", "wav": "tcp_udp.wav", "expected_tool": null},
  {"case_id": "product", "wav": "product.wav", "expected_tool": "search_knowledge_base", "query_terms": ["AX"]},
  {"case_id": "follow_up", "wav": "follow_up.wav", "expected_tool": "search_knowledge_base", "query_terms": ["AX"]}
]
```

将 AX 替换为录音和知识库中使用的真实标识。宿主机先检查必要文件和容器：

```sh
COMPOSE='docker compose --env-file .env -f deploy/compose.production.yaml'
API_CONTAINER=$($COMPOSE ps -q api)
VOICE_CONTAINER=nemotron-labs-voicechat
HOST_CASE_DIR=artifacts/native-cuekb/cases

test -n "$API_CONTAINER"
test -f "$HOST_CASE_DIR/cases.json"
test -f scripts/validate_native_cuekb.py
test -f scripts/probe_voicechat.py
docker inspect -f '{{.State.Running}} {{.Name}}' "$API_CONTAINER" "$VOICE_CONTAINER"
```

预期两行均为 `true /容器名`。若 API 容器为空，先完成部署；不要用另一个旧容器代替。

### 3.2 在 API 容器内核对本期配置

```sh
docker exec "$API_CONTAINER" \
  python /app/scripts/verify_deployment.py \
    --base-url http://127.0.0.1:8000 \
    --expect-cuekb-native
```

预期退出码为 0，JSON 至少满足：

```json
{
  "readiness": {
    "status": "ready",
    "text_configured": false,
    "voice_configured": true,
    "voice_policy": {
      "provider": "nvidia",
      "qa_execution_mode": "dual_tools",
      "answer_policy": "general_qa"
    }
  }
}
```

随后直接读取实际 capabilities，确认持久化目录已切换到完整名称：

```sh
docker exec -i "$API_CONTAINER" python - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8000/api/v1/capabilities", timeout=5) as response:
    value = json.load(response)
keys = ("provider", "agent_provider", "qa_execution_mode", "answer_policy",
        "qa_toolset_version", "native_tools", "voice_available", "is_mock")
print(json.dumps({key: value.get(key) for key in keys}, indent=2))
PY
```

预期 `native_tools` 严格等于 `["search_knowledge_base"]`，`agent_provider=disabled`、`voice_available=true`、`is_mock=false`。若仍是旧目录，从宿主机在当前 API 容器内执行：

```sh
docker exec "$API_CONTAINER" \
  python /app/scripts/configure_native_cuekb.py --apply
```

预期 JSON 的 `target_names` 为 `["search_knowledge_base"]`，`applied=true`；首次迁移时 `changed=true`，已经应用过时 `changed=false`。执行后重新运行本节 capabilities 命令，并为验证新建 Conversation；不要复用旧会话。

### 3.3 创建容器临时目录并复制验证材料

生产 API 使用只读 rootfs，`/app` 不能写；Compose 只将 `/tmp` 挂为可写 tmpfs。先在**当前完整容器 ID**中创建目标目录并验证可写，再复制文件：

```sh
docker exec "$API_CONTAINER" sh -c '
  rm -rf /tmp/native-cuekb &&
  mkdir -p /tmp/native-cuekb/scripts \
           /tmp/native-cuekb/cases \
           /tmp/native-cuekb/output/replies &&
  test -w /tmp/native-cuekb/output
'

docker cp scripts/validate_native_cuekb.py \
  "${API_CONTAINER}:/tmp/native-cuekb/scripts/validate_native_cuekb.py"
docker cp scripts/probe_voicechat.py \
  "${API_CONTAINER}:/tmp/native-cuekb/scripts/probe_voicechat.py"
docker cp "${HOST_CASE_DIR}/." \
  "${API_CONTAINER}:/tmp/native-cuekb/cases/"

docker exec "$API_CONTAINER" sh -c '
  test -r /tmp/native-cuekb/scripts/validate_native_cuekb.py &&
  test -r /tmp/native-cuekb/scripts/probe_voicechat.py &&
  test -r /tmp/native-cuekb/cases/cases.json &&
  find /tmp/native-cuekb -maxdepth 3 -type f -print
'
```

预期最后至少列出两个 Python 文件、`cases.json` 和其中引用的全部 WAV。必须先创建目录再 `docker cp`；直接复制到不存在的 `/tmp/native-cuekb`，Docker 可能尝试修改只读 rootfs 或返回 `no such directory`。

若现场 Docker 仍拒绝向 tmpfs 执行 `docker cp`，使用标准输入写入两个脚本，并对每个授权输入文件采用同样方式；这仍由宿主机通过 `docker exec` 写入容器 `/tmp`：

```sh
docker exec -i "$API_CONTAINER" sh -c \
  'cat > /tmp/native-cuekb/scripts/validate_native_cuekb.py' \
  < scripts/validate_native_cuekb.py
docker exec -i "$API_CONTAINER" sh -c \
  'cat > /tmp/native-cuekb/scripts/probe_voicechat.py' \
  < scripts/probe_voicechat.py
docker exec -i "$API_CONTAINER" sh -c \
  'cat > /tmp/native-cuekb/cases/cases.json' \
  < "$HOST_CASE_DIR/cases.json"

set -- "$HOST_CASE_DIR"/*.wav
test -e "$1"
for wav in "$HOST_CASE_DIR"/*.wav; do
  filename=$(basename "$wav")
  docker exec -i "$API_CONTAINER" \
    sh -c 'cat > "$1"' sh "/tmp/native-cuekb/cases/$filename" \
    < "$wav"
done
```

### 3.4 在 API 容器内执行端到端验证

API 容器的 8000 端口是 Uvicorn 明文 loopback，因此这里必须使用 `http://127.0.0.1:8000`，不能写成 HTTPS。公网来源校验继续使用容器中的 `PUBLIC_ORIGIN`：

```sh
VALIDATION_START=$(date -u +%Y-%m-%dT%H:%M:%SZ)

set +e
docker exec \
  -w /tmp/native-cuekb \
  -e PYTHONPATH=/app/apps/api:/app:/tmp/native-cuekb \
  "$API_CONTAINER" \
  sh -c '
    test -n "$PUBLIC_ORIGIN" &&
    python scripts/validate_native_cuekb.py \
      --base-url http://127.0.0.1:8000 \
      --origin "$PUBLIC_ORIGIN" \
      --cases /tmp/native-cuekb/cases/cases.json \
      --audio-dir /tmp/native-cuekb/output/replies \
      --output /tmp/native-cuekb/output/report.json
  '
VALIDATION_EXIT=$?
set -e
echo "exit_code=$VALIDATION_EXIT"
```

预期 `exit_code=0`。脚本保持 80 ms 送帧，WAV 结束后继续送静音推进推理；不会在音频前段返回时提前断开。单个 case 默认上限 90 秒，可加 `--case-timeout 180`，不能超过 180 秒。过长语料组应拆开；需要验证指代的录音必须保留在同一组。

无论退出码是否为 0，都在容器内打印紧凑判定，不能只看 shell 退出码：

```sh
docker exec -i "$API_CONTAINER" python - <<'PY'
import json
from pathlib import Path

report = json.loads(Path("/tmp/native-cuekb/output/report.json").read_text())
print("report_status=", report.get("status"), "error_type=", report.get("error_type"))
for case in report.get("cases", []):
    print(
        case.get("case_id"),
        "status=" + str(case.get("status")),
        "selection=" + str(case.get("selection")),
        "tools=" + str(case.get("actual_tools")),
        "arguments=" + str(case.get("arguments")),
        "retrieval=" + str(case.get("retrieval")),
        "speech=" + str(case.get("speech_transport")),
        "portal_error=" + str(case.get("portal_error")),
    )
PY
```

预期总状态为 `observed`；问候/通用问题的 `selection=passed`、`tools=[]`、`retrieval=not_applicable`、`speech=passed`；产品问题的 `selection=passed`、`tools=['search_knowledge_base']`、`arguments` 不为 `failed/unverified`、`retrieval` 不为 `failed/unverified`、`speech=passed`。任何 `VOICE_TOOL_UNKNOWN`、`ConnectError`、空 `cases` 或 `status=partial` 都是不通过，先按第 4、6 节定位。

### 3.5 把报告和回答录音复制回宿主机

```sh
HOST_RESULT_DIR=artifacts/native-cuekb/result-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$HOST_RESULT_DIR/replies"

docker cp "${API_CONTAINER}:/tmp/native-cuekb/output/report.json" \
  "$HOST_RESULT_DIR/report.json"
docker cp "${API_CONTAINER}:/tmp/native-cuekb/output/replies/." \
  "$HOST_RESULT_DIR/replies/"

test -s "$HOST_RESULT_DIR/report.json"
sha256sum "$HOST_RESULT_DIR/report.json" "$HOST_RESULT_DIR"/replies/*.wav
echo "$HOST_RESULT_DIR"
```

预期 `report.json` 可解析，每个产生语音的 case 都有非空 WAV 和 SHA-256。若没有任何 WAV，shell 通配符会报错，同时报告中的 `speech_transport` 应为 `failed`。这些文件含授权回答音频，保留在 Git 忽略目录，不提交仓库。

未指定 --audio-dir 不保存输出录音；指定后每个 case 最多保留 16 MiB。脚本默认报告参数 hash、来源 trace、事件结果，不写访问票据/凭据或原始 query/转写正文；参数正文可在该会话的服务端 native_tool_arguments 记录中按权限复核。源 PCM 和已发送音频计数都不能证明模型理解正确或扬声器已经播放。

## 4. 分开判定结果

| 阶段 | 证据与错误判定 |
| --- | --- |
| 输入 | 实际转写与录音是否一致；丢词、型号/版本错误单独记录，不直接归为选工具错误 |
| 工具选择 | 是否产生期望的原生工具；普通问题多调用、专业问题漏调用、未知工具分别记录 |
| 参数 | native_tool_arguments 保留 query、精确工具名和输入关联；schema 失败为 VOICE_TOOL_ARGUMENTS；query_terms 仅做字面检查，不证明语义等价 |
| 知识检索 | ToolRun/CueKB trace/来源；鉴权、超时、格式失败不等同于无命中，insufficient/conflicting 不得生成肯定事实 |
| 语音回答 | 同 call 回填、音频/结束事件、实际口述与证据、数字/单位/否定/版本条件及听音；与工具选择单独判定 |

报告 selection/arguments/retrieval/speech_transport 分开记录；retrieval_relevance、input_transcription、spoken_faithfulness 保持人工复核。退出码 0 仅表示已观察到预期路由、参数字面条件、非失败检索和音频结束，不能表示答案事实、语义、发音或实际播放全部正确。

精简证据只选择完整 ASCII 来源/context，不从句中截断或音译型号/技术事实；保留 document/chunk/version/trace。不可用来源返回受控不足说明，不调用额外模型翻译或回答。unassessed 保留原值而不宣称服务已验证充分；partial 限定回答范围，检索失败或局部结果不证明事实不存在。

## 5. 工具等待与打断

配置的 ACK 只说明正在查文档，不宣称检索成功。记录工具请求、CueKB 耗时、结果回填、首段回答和结束时间；功能通过后再优化时延。

当前 NVIDIA Adapter 的工具等待交互能力仍为 false，未新增自然语音修订/取消/进度操作。VoiceChat 普通对话的原生全双工行为保留；工具等待阶段应单独验证实际语音行为和限制。门户 Stop playback 与 Cancel search 仍是独立操作，不用普通发声推断业务取消。

自动化协议夹具和 Chromium Web Audio 测试验证应用接线、文件输入和队列管理，不能替代真实 VoiceChat/CueKB、真实英文语音和实际听音。

## 6. 区分注册、Prompt 与模型输出

当前 VoiceChat 运行文件为每个新会话输出三类不含 query/Prompt 正文的诊断：

```text
Prompt prepared: tools_count=1 tool_names=search_knowledge_base prompt_chars=... prompt_sha256=...
System prompt received for sequence ...: tool_names=search_knowledge_base chars=... sha256=...
Function call decoded from model for sequence ...: tool_names=search_knowledge_base
```

前两行的工具名、字符数和 SHA-256 必须一致。这证明 WebSocket 层用正确工具渲染的最终 Prompt 已逐字节到达 Triton backend；第三行来自 backend 对同一 sequence 的模型函数通道解码，发生在 WebSocket JSON 解析和转发之前。顺序执行验证时，用 `System prompt received` 和 `Function call decoded` 的 sequence 号锁定同一轮。

| 观察结果 | 故障位置 |
| --- | --- |
| `Prompt prepared` 已是错误名称 | API 注册内容、挂载模板或 WebSocket Prompt 构造 |
| `Prompt prepared` 正确，但 backend 名称或 hash 不同 | VoiceChat 到 Triton 的 Prompt 传输/转换 |
| 两层 Prompt 都是 `search_knowledge_base` 且 hash 相同，模型解码行是其他名称 | Nano 9B/function decoder 输出 |
| 模型解码行正确，但客户端 JSONL 或 API 审计名称不同 | VoiceChat WebSocket 事件封装/转发 |

### 6.1 从宿主机核对 VoiceChat 容器运行文件

沿用第 3.1 节的 `VOICE_CONTAINER`，从宿主机执行：

```sh
docker exec "$VOICE_CONTAINER" sha256sum \
  /opt/tritonserver/backends/nemotron-voicechat/model.py \
  /s2s/audio_server.py \
  /s2s/prompt_template.jinja

docker exec "$VOICE_CONTAINER" printenv \
  USE_JINJA_TEMPLATE_PROMPT \
  S2S_INFERENCE_CLOSE_ON_PAD_TAIL \
  S2S_INFERENCE_FORCE_SPEECH_SILENCE_ON_PAD \
  S2S_TTS_PAD_TAIL_RATIO \
  RNNT_MAX_AGENT_RESPONSE_SEC
```

预期 hash 和环境依次为：

```text
c91b3af5b41f515fc1dbfaf7fdbec4ae528a4989f90a144829d2491a9b805990  model.py
0579d3713e28193f6c329a69b90c7a6d3d4e2c82b82d9dd4cc3ec7bee92fe984  audio_server.py
a71230010295a363a0fde0e0d86c4786aeb4ef5f458cf4d75ddf0926895bcdb7  prompt_template.jinja
1
true
true
3
0
```

hash 不一致时停止业务验证，先同步 [VoiceChat 完整运行文件](voicechat/docker-run.md)并重建 VoiceChat 容器。

### 6.2 从宿主机提取同一验证窗口的 VoiceChat 日志

先停止其他验证流量，再执行第 3.4 节的一条产品录音。该节已经在宿主机记录 `VALIDATION_START`。VoiceChat 主进程日志写到容器 stdout，Docker 没有可在容器内读取 stdout 历史的标准文件，因此这里必须由宿主机使用 `docker logs`；其余检查和验证程序都通过 `docker exec` 在容器内执行。

```sh
mkdir -p "$HOST_RESULT_DIR"
docker logs --since "$VALIDATION_START" "$VOICE_CONTAINER" 2>&1 \
  > "$HOST_RESULT_DIR/voicechat.log"

grep -E \
  'Prompt prepared:|System prompt received for sequence|Function call decoded from model for sequence' \
  "$HOST_RESULT_DIR/voicechat.log"
```

预期同一顺序会话出现：

```text
Prompt prepared: tools_count=1 tool_names=search_knowledge_base prompt_chars=N prompt_sha256=H
System prompt received for sequence S: tool_names=search_knowledge_base chars=N sha256=H
Function call decoded from model for sequence S: tool_names=search_knowledge_base
```

第一、二行的 `N` 与 `H` 必须完全相同；第二、三行的 sequence `S` 必须相同。`Prompt prepared` 本身不带 sequence，存在并发会话时不能按相邻行猜测配对。

### 6.3 可选：在 VoiceChat 容器内保存原生 function JSONL

NVIDIA `NeMo-Speech.cpp` 的独立客户端 `clients/voicechat/nemotron-voicechat-client.py` 支持 `--function-text-output`。它把收到的 `response.function_call_arguments.done` 保存为 JSONL，可用于旁路确认 VoiceChat 服务对外发出的名称：

先在 API 容器内从**实际持久化目录**生成与业务会话相同的 tools/instructions；不要手写简化定义：

```sh
docker exec -i \
  -e PYTHONPATH=/app/apps/api:/app \
  "$API_CONTAINER" python - <<'PY'
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
            update = session_update(tools=snapshot.definitions, answer_policy=settings.answer_policy)
            target = Path("/tmp/native-cuekb/direct")
            target.mkdir(parents=True, exist_ok=True)
            (target / "tools.json").write_text(json.dumps(update["session"]["tools"]))
            (target / "instructions.txt").write_text(update["session"]["instructions"])
            print("tool_names=", [item["name"] for item in update["session"]["tools"]])
    finally:
        await store.engine.dispose()

asyncio.run(main())
PY
```

预期只打印 `tool_names= ['search_knowledge_base']`。在宿主机指定官方客户端和一条产品 WAV，然后复制到 VoiceChat 容器的 `/tmp`：

```sh
VOICECHAT_CLIENT=/actual/NeMo-Speech.cpp/clients/voicechat/nemotron-voicechat-client.py
DIRECT_WAV="$HOST_CASE_DIR/product.wav"

test -f "$VOICECHAT_CLIENT"
test -f "$DIRECT_WAV"

docker exec "$VOICE_CONTAINER" sh -c \
  'rm -rf /tmp/native-cuekb && mkdir -p /tmp/native-cuekb'
docker cp "$VOICECHAT_CLIENT" \
  "${VOICE_CONTAINER}:/tmp/native-cuekb/nemotron-voicechat-client.py"
docker cp "$DIRECT_WAV" \
  "${VOICE_CONTAINER}:/tmp/native-cuekb/product.wav"
docker cp "${API_CONTAINER}:/tmp/native-cuekb/direct/tools.json" \
  /tmp/native-cuekb-tools.json
docker cp "${API_CONTAINER}:/tmp/native-cuekb/direct/instructions.txt" \
  /tmp/native-cuekb-instructions.txt
docker cp /tmp/native-cuekb-tools.json \
  "${VOICE_CONTAINER}:/tmp/native-cuekb/tools.json"
docker cp /tmp/native-cuekb-instructions.txt \
  "${VOICE_CONTAINER}:/tmp/native-cuekb/instructions.txt"

docker exec -w /tmp/native-cuekb "$VOICE_CONTAINER" \
  python nemotron-voicechat-client.py \
  --server 127.0.0.1:9000 \
  --input-file /tmp/native-cuekb/product.wav \
  --no-playback \
  --instructions /tmp/native-cuekb/instructions.txt \
  --tools /tmp/native-cuekb/tools.json \
  --ignore-tool-calls \
  --trailing-silence 10 \
  --function-text-output /tmp/native-cuekb/function_calls.jsonl \
  --conversation-output /tmp/native-cuekb/conversation.jsonl
```

执行后在 VoiceChat 容器内检查并复制回宿主机：

```sh
docker exec -i "$VOICE_CONTAINER" python - <<'PY'
import json
from pathlib import Path

path = Path("/tmp/native-cuekb/function_calls.jsonl")
assert path.stat().st_size > 0, "function_calls.jsonl is empty"
events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
names = [event.get("name") for event in events]
print("function_call_names=", names)
assert names and all(name == "search_knowledge_base" for name in names), names
PY

docker cp "${VOICE_CONTAINER}:/tmp/native-cuekb/function_calls.jsonl" \
  "$HOST_RESULT_DIR/function_calls.jsonl"
docker cp "${VOICE_CONTAINER}:/tmp/native-cuekb/conversation.jsonl" \
  "$HOST_RESULT_DIR/conversation.jsonl"
rm -f /tmp/native-cuekb-tools.json /tmp/native-cuekb-instructions.txt
```

预期打印 `function_call_names= ['search_knowledge_base']`；若同一输入产生多个调用，每一项也必须是该完整名称。

`--ignore-tool-calls` 避免参考客户端用内置 dummy dispatcher 冒充 CueKB；因此这条命令只验证 VoiceChat 的选择和输出名称，完整 CueKB 执行仍以第 3.4 节为准。JSONL 是服务端对外事件，不能单独证明模型收到的 Prompt；必须与第 6.2 节的两层 Prompt hash 和 backend 同 sequence 模型解码日志联合判断。若 VoiceChat 镜像缺少客户端运行依赖，此项记为未执行，不要在生产容器内安装包；使用与该镜像匹配的专用诊断镜像执行相同客户端命令。

## 7. 复制证据后清理容器临时文件

确认第 3.5、6.2 节证据已落到宿主机忽略目录后执行：

```sh
docker exec "$API_CONTAINER" rm -rf /tmp/native-cuekb
docker exec "$VOICE_CONTAINER" rm -rf /tmp/native-cuekb
```

预期两个命令退出码均为 0。清理只删除验证时复制到容器 tmpfs 的脚本、输入和输出，不删除宿主机结果或应用数据。
