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

## 3. 从宿主机用一个入口完成 API 项目验证

API 镜像内置 `run_native_cuekb_validation.py`、底层验证脚本、`cases.json` 和四段固定 WAV。单入口依次完成 readiness、capabilities、镜像材料和真实 Portal→VoiceChat→CueKB 四用例验证；不创建输入目录、不复制宿主机测试文件，也不依赖宿主机 Python。详细 JSON 与回答 WAV 写到容器 `/tmp/native-cuekb`，控制台只输出定位所需字段。

以下命令均在宿主机项目根目录执行。先定位当前 API 容器，然后只运行一次 `docker exec`：

```sh
COMPOSE='docker compose --env-file .env -f deploy/compose.production.yaml'
API_CONTAINER=$($COMPOSE ps -q api)
test -n "$API_CONTAINER"

set +e
docker exec "$API_CONTAINER" \
  python /app/scripts/run_native_cuekb_validation.py
VALIDATION_EXIT=$?
set -e
echo "host_exit_code=$VALIDATION_EXIT"
```

API 容器的 8000 端口是 Uvicorn 明文 loopback；脚本固定使用 `http://127.0.0.1:8000`，并从容器环境读取 `PUBLIC_ORIGIN`。正常情况下只输出七行：一行预检、一行时间窗口/会话 ID、四行 case 和一行最终分类；宿主机最后再输出 `host_exit_code`。输出格式固定为：

```text
preflight=passed provider=nvidia mode=dual_tools policy=general_qa tools=search_knowledge_base cases=4
window_start=... window_end=... conversation_id=... report=/tmp/native-cuekb/report.json
case=greeting expected=- actual=- selection=passed arguments=not_applicable retrieval=not_applicable cuekb_evidence=- voice_evidence=not_applicable evidence_filter=- speech=passed reason=- portal_error=- elapsed_ms=... input_ids=... trace_ids=-
case=general_tech expected=- actual=- selection=passed arguments=not_applicable retrieval=not_applicable cuekb_evidence=- voice_evidence=not_applicable evidence_filter=- speech=passed reason=- portal_error=- elapsed_ms=... input_ids=... trace_ids=-
case=black_pig_crisp_heart expected=search_knowledge_base actual=search_knowledge_base selection=passed arguments=passed retrieval=passed cuekb_evidence=sufficient voice_evidence=passed evidence_filter=candidates:5,selected:N,truncated_ok:T,truncated_rejected:R,omitted_ok:O,omitted_rejected:M,non_ascii:0,limit:L,budget:B speech=passed reason=- portal_error=- elapsed_ms=... input_ids=... trace_ids=...
case=golden_yolk expected=search_knowledge_base actual=search_knowledge_base selection=passed arguments=passed retrieval=passed cuekb_evidence=sufficient voice_evidence=passed evidence_filter=candidates:5,selected:N,truncated_ok:T,truncated_rejected:R,omitted_ok:O,omitted_rejected:M,non_ascii:0,limit:L,budget:B speech=passed reason=- portal_error=- elapsed_ms=... input_ids=... trace_ids=...
validation=passed fault=PASS focus=human_transcription_and_spoken_faithfulness error_type=- exit_code=0
host_exit_code=0
```

`arguments=manual_review_required` 代表已有 query、但固定词项不足以自动判定语义；这种状态本身允许通过。产品用例必须同时满足 `retrieval=passed` 和 `voice_evidence=passed`。`cuekb_evidence` 直接显示 CueKB 的 `evidence_status`；仅省略周边 context 时，`sufficient` 可证明保留的主命中足以进入语音证据，`unassessed` 仍需完整命中块证明。`truncated_ok` 只统计由 chunk ID 与正文精确回显证明完整、且已从语音包移除截断 context 的主命中；`truncated_rejected` 是缺少该证明的截断命中。`omitted_ok/rejected` 对应应用省略周边 context 的准入结果。示例中的 `N` 必须至少为 1；`L` 和 `B` 由命中长度及固定条数/字节预算决定。脚本不输出证据正文，保持 80 ms 送帧，单 case 上限 90 秒。

失败时只提供脚本实际输出，不先提供完整报告或全量日志。最终 `fault/focus` 的含义为：

| `fault` | 下一步只补充的证据 |
| --- | --- |
| `PREFLIGHT` | 两行失败输出；检查 API 配置或镜像版本 |
| `PORTAL_CONNECT` | 时间窗口和最终行；检查 API loopback/WS |
| `INPUT_NOT_OBSERVED` | 失败 case 行；检查该产品 WAV 是否产生 input ID，不进入工具选择归因 |
| `TOOL_NOT_CALLED` | Python 七行及 `host_exit_code`，再按 [VoiceChat 独立验证](voicechat/native-tool-selection-validation.md)提取三类 Prompt/模型证据 |
| `UNKNOWN_TOOL_NAME` | 失败 case 行，再查该 `input_ids` 对应的 `emitted_tool/reason_code/stage` |
| `TOOL_ARGUMENTS` | 失败 case 行和对应 input ID 的参数校验结果，不提供 query 正文 |
| `CUEKB_RETRIEVAL` | 失败 case 行及对应 `trace_ids` 的一行 `cuekb_response_validated` |
| `CUEKB_VOICE_EVIDENCE` | 失败 case 行中的 `cuekb_evidence` 与 `evidence_filter`；`truncated_rejected>0` 表示缺少精确完整块证明，`omitted_rejected>0` 表示应用省略后既无充分性也无完整块证明，其余按 non_ascii、limit、budget 的非零计数处理 |
| `VOICE_RESPONSE_COMPLETION` | 时间窗口、失败 case 行，以及同窗口 VoiceChat 的工具结果接收与 audio done/EOS 行 |
| `PASS` | Python 七行及 `host_exit_code=0`；需要口述复核时只导出相关 case WAV |

完整报告仅在上述核心字段互相矛盾时读取：

```sh
docker exec "$API_CONTAINER" \
  cat /tmp/native-cuekb/report.json
```

需要人工听音时才把指定回答复制回宿主机；固定输入始终留在镜像内：

```sh
HOST_RESULT_DIR=artifacts/native-cuekb/result-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$HOST_RESULT_DIR"
docker cp "${API_CONTAINER}:/tmp/native-cuekb/replies/case-03.wav" \
  "$HOST_RESULT_DIR/black-pig-answer.wav"
```

源 PCM、收到音频样本及退出码都不能证明模型理解或口述事实正确。`input_ids`、`trace_ids`、conversation ID 和时间窗口用于精确筛选数据库及日志，不包含转写、query、凭据或 Prompt 正文。

## 4. 分开判定结果

| 阶段 | 证据与错误判定 |
| --- | --- |
| 输入 | 实际转写与录音是否一致；丢词、型号/版本错误单独记录，不直接归为选工具错误 |
| 工具选择 | 是否产生期望的原生工具；普通问题多调用、专业问题漏调用、未知工具分别记录 |
| 参数 | native_tool_arguments 保留 query、精确工具名和输入关联；schema 失败为 VOICE_TOOL_ARGUMENTS；query_terms 仅做字面检查，不证明语义等价 |
| 知识检索 | ToolRun/CueKB trace/来源；鉴权、超时、格式失败不等同于无命中，insufficient/conflicting 不得生成肯定事实 |
| 语音回答 | 同 call 回填、音频/结束事件、实际口述与证据、数字/单位/否定/版本条件及听音；与工具选择单独判定 |

报告 selection/arguments/retrieval/speech_transport 分开记录；retrieval_relevance、input_transcription、spoken_faithfulness 保持人工复核。退出码 0 仅表示已观察到预期路由、参数字面条件、产品检索通过和音频结束，不能表示答案事实、语义、发音或实际播放全部正确。

精简证据只选择完整 ASCII 来源/context，不从句中截断或音译型号/技术事实；保留 document/chunk/version/trace。不可用来源返回受控不足说明，不调用额外模型翻译或回答。unassessed 保留原值而不宣称服务已验证充分；partial 限定回答范围，检索失败或局部结果不证明事实不存在。

## 5. 工具等待与打断

配置的 ACK 只说明正在查文档，不宣称检索成功。记录工具请求、CueKB 耗时、结果回填、首段回答和结束时间；功能通过后再优化时延。

当前 NVIDIA Adapter 的工具等待交互能力仍为 false，未新增自然语音修订/取消/进度操作。VoiceChat 普通对话的原生全双工行为保留；工具等待阶段应单独验证实际语音行为和限制。门户 Stop playback 与 Cancel search 仍是独立操作，不用普通发声推断业务取消。

自动化协议夹具和 Chromium Web Audio 测试验证应用接线、文件输入和队列管理，不能替代真实 VoiceChat/CueKB、真实英文语音和实际听音。

## 6. VoiceChat 修改与验证独立维护

VoiceChat 最终 Prompt、Triton 接收内容、模型 function 通道和 WebSocket 封装不属于 API 容器内部证据。多轮定位进展、当前完整文件、运行 hash、最小日志归纳命令、判定表及预期结果统一维护在 [VoiceChat 原生工具选择验证](voicechat/native-tool-selection-validation.md)。

API 单入口输出 `fault=TOOL_NOT_CALLED` 后，再执行该页的 VoiceChat 两步核对。两层 Prompt hash 和同 sequence 模型 function 结果没有齐全前，不把故障归因给 Nano 9B、模板、注册或 Gateway。

## 7. 清理运行时输出

确认需要的核心行或回答音频已保存后，从宿主机执行：

```sh
docker exec "$API_CONTAINER" rm -rf /tmp/native-cuekb
```

预期退出码为 0。镜像内脚本、case 配置和固定 WAV 是只读发布内容，不需要也不能现场删除。
