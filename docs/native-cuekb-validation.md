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

## 3. 通过本项目运行固定 WAV

新脚本 `scripts/validate_native_cuekb.py` 连接实际门户 API/WS；所有工具执行均发生在本项目中，不使用探针合成结果。它为一组顺序录音创建一条会话，可检查连续追问。

在忽略目录 `artifacts/native-cuekb/` 放入授权录音，并创建 `cases.json`。wav 路径相对该文件；例如：

```json
[
  {"case_id": "greeting", "wav": "hello.wav", "expected_tool": null},
  {"case_id": "general_tech", "wav": "tcp_udp.wav", "expected_tool": null},
  {"case_id": "product", "wav": "product.wav", "expected_tool": "search_knowledge_base", "query_terms": ["AX"]},
  {"case_id": "follow_up", "wav": "follow_up.wav", "expected_tool": "search_knowledge_base", "query_terms": ["AX"]}
]
```

将 AX 替换为录音和知识库中使用的真实标识，再执行：

```sh
PYTHONPATH=apps/api:. .venv/bin/python scripts/validate_native_cuekb.py \
  --base-url https://voice.example.com:8087 \
  --cases artifacts/native-cuekb/cases.json \
  --audio-dir artifacts/native-cuekb/replies \
  --output artifacts/native-cuekb/report.json
```

base-url 使用实际门户 origin，沿用可信 TLS 校验；本机直连 API 时可用 --origin 指定部署的 PUBLIC_ORIGIN。脚本保持 80 ms 送帧，WAV 结束后继续送静音以推进推理；不会在音频前段返回时提前断开。单个 case 默认上限 90 秒，可用 --case-timeout 调整，最大 180 秒。会话仍受既有轮换时限约束：脚本不实际播放，不伪造播放完成 ACK；过长语料组应拆成多次运行，需要验证指代的语料保留在同一组。

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

容器重建后先清出一段可控日志窗口，再顺序执行一条产品录音：

```sh
docker logs --since 1m nemotron-labs-voicechat 2>&1 \
  | grep -E 'Prompt prepared:|System prompt received for sequence|Function call decoded from model for sequence'
```

若现场存在并发会话，先停止其他验证流量；`Prompt prepared` 行本身不带 sequence，不能跨并发会话用时间猜测配对。两层 hash 一致后，以 backend 两行的相同 sequence 判断模型收到的名称和模型实际输出的名称。

NVIDIA `NeMo-Speech.cpp` 的独立客户端 `clients/voicechat/nemotron-voicechat-client.py` 支持 `--function-text-output`。它把收到的 `response.function_call_arguments.done` 保存为 JSONL，可用于旁路确认 VoiceChat 服务对外发出的名称：

```sh
python clients/voicechat/nemotron-voicechat-client.py \
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

`tools.json` 必须只含与 API 相同的 `search_knowledge_base` 定义。`--ignore-tool-calls` 避免该参考客户端用内置 dummy dispatcher 冒充 CueKB；因此这条命令只验证 VoiceChat 的选择和输出名称，完整 CueKB 执行仍用本项目 `validate_native_cuekb.py`。JSONL 记录的是服务端事件，不单独证明模型收到的 Prompt；必须与上述两层 Prompt hash 和 backend 模型解码日志合并判断。
