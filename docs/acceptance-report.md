# 当前验收证据

更新日期：2026-10-20。**真实知识语音闭环尚未放行。**最新 04:48 UTC 现场 Black Pig 检索两次、终态后调用零次，answer_complete=passed，但 VOICE_MISSING_CONDITION 导致业务校验失败。第四轮被轮换阻断。当前只推进新连接中的单个 Black Pig 完整问答；本地修正校验依据与实际回填不一致，并增加精确失败诊断。该修正是否解释现场错误仍需检查已有答案；不能将局部代码缺陷直接认定为现场唯一根因。Golden Yolk、多轮打断和 hello 缺词均后置。状态见 [任务板](TASK_BOARD.md)，操作见 [单轮验证](native-cuekb-validation.md)。

当前用户目标已改为 VoiceChat 自主决定直接回答或调用工具，部署采用 general_qa。qa-tools-v4 已修正模板冲突并补充部署策略检查；以下严格模式现场结果保留为历史证据，不能当作新模式的验收结果。

本期 `cuekb-native-v1` 限定为唯一公开工具 search_knowledge_base(query)，内部仍由 CueKB 执行检索，VoiceChat 直接消费证据，不调用额外答案模型；已有真实录音测试，完整业务验收仍未通过。步骤和判定见 [原生单工具验证](native-cuekb-validation.md)。

## 0. 当前审计与证据索引

以下是分别执行的验证，不累加成一个“当前全量通过数”，也不更新历史测试的执行日期。

本次静态检查：`ruff check apps/api scripts tests`、`git diff --check` 通过；更新的单轮验证手册五个 Shell 代码块语法检查通过。此前 `ruff check .` 包含原样维护的 VoiceChat 外部源码快照并报 95 项既有规范问题，本次未修改这些源码，不将其列为业务验收阻塞或声称全仓 Ruff 通过。

| 日期 / 版本 | 实际结果 | 限制与证据位置 |
| --- | --- | --- |
| 2026-10-20 / 单轮证据校验对齐（本地） | 核心原生链路及报告测试 82 项通过。扩展到 QA、交付、交互和模拟流程，共 182 项中 181 项通过；一项旧兼容路径一秒等待测试记录为 VOICE_ANSWER_TIMEOUT，单独复核通过。覆盖排除的 context 不误加条件、不为新数字背书，已交付 only/产品条件仍拒绝遗漏，原始引用保留，诊断按 KB 权限隔离，单 case 原 WAV、门户答案和来源提交检查 | CPU/受控 HTTP、WS、SQLite；未连接现场模型、未真实听音，也未执行生产镜像构建。保留一次超时记录，不声称整组一次全绿。只读历史诊断不改数据库，不访问 CueKB/VoiceChat |
| 2026-10-20 04:48 UTC / 用户现场报告 | Black Pig retrieval_executions=2、terminal_results_submitted=1、post_terminal_calls=0、累计来源 10，后端注入 2/2 完成，answer_complete=passed；reason=VOICE_MISSING_CONDITION。Golden Yolk 在 12ms 内收到 VOICE_SESSION_ROTATION_REQUIRED，未观察到输入 | 证明本轮查询收敛和回答传输完成，不证明内容/门户交付通过；两次注入均属于 Black Pig，不能作为 Golden Yolk 成功证据 |
| 2026-10-20 / 输入级检索预算与累计证据（本地） | 191 项相关 pytest 通过：原生检索、验证摘要、工具契约、会话/交付、QA 兼容及工具目录。覆盖补充事实合并后通过校验并保留两条引用、第二次空检索保留第一次事实、不同 call_id 的精确重复不再次检索、终态后再次调用明确失败、权限撤回阻止缓存交付、同连接下一输入预算重置；原生 HTTP 5xx/超时/网络/响应中断只发送一次，其他路径仍两次 | CPU/受控 HTTP、WS、SQLite；不是 NVIDIA 模型、真实 CueKB、麦克风或听音验证。终态要求是软性模型引导，硬性执行上限仅证明查询受控；真实完整回答待验。VoiceChat 源码未修改 |
| 2026-10-19 / 原生证据覆盖与类型适配（本地） | 61 项原生 CueKB/验证脚本及 44 项工具契约/QA 兼容测试通过，共 105 项；新增第 4、5 条事实保留及字节预算省略场景；内外 partial 类型分别核对 | 受控 HTTP/WS/SQLite 夹具；不是真实模型续答验收。VoiceChat 运行源码未改动，仅 API 适配文本标记并恢复配置检索数；不能保证多取两条即解决工具循环 |
| 2026-10-19 / 真实规范化与来源覆盖复核 | 用户提供 04:56:50 UTC 的实际 before/after：来源正文保留，partial 的 false 被转为 "zero"；第一组来源只有标题、背景、18 层及肉料比例，未含录音要求的价格、重量、最佳食用时间 | 确认布尔类型转换问题及这一组证据覆盖不足；未证明它们是工具循环的充分原因，不能推断其余九组结果相同。API 改用文本 partial 标记，撤回压低 top_k 的应用限制，真实验收待执行 |
| 2026-10-19 / 轮换与注入摘要修正（本地） | 先在真实 HTTP/WS 夹具复现 ACK 结束后提前轮换；修正后延迟知识回答正常完成。同 call_id 幂等、不同 ID 新查询、多轮、仅 ACK 不通过、断线结清及摘要按 sequence 隔离等 59 项定向测试通过；Ruff/diff 检查通过 | CPU/受控服务；不修改 VoiceChat 运行源码，不证明真实工具循环已修复。摘要新增 backend_injection，工具传输检查明确 scope=tool_transport |
| 2026-10-19 04:56 UTC / 简化 relay 现场 | 前两个普通 case answer_complete=passed；产品 input 10 次原生调用、CueKB/回填各 10，后端接收/注入启动各 10、返回 idle/注入结束各 9、取消 1、decoder_limit=0。结果 322–881 字符、93–235 token。产品 answer_complete=unverified，约 105 秒触发 VOICE_SESSION_ROTATION_REQUIRED | 是真实日志证据；队列注入完成不证明语义理解。第四 case 未执行。第十次取消与连接关闭的时序需确认；工具循环根因未定，不能据此扩大超时或重放结果 |
| 2026-10-19 / 简化原生 relay（本地） | 518 项 Python 全量回归通过；新增网络夹具在同一连接完成普通→知识→普通→知识指代追问，工具等待期间持续送音、重复 ACK 不消耗回答资格、回答延迟超过旧预算仍能完成；同 call_id 重复通知只执行一次，不同 call_id 的新查询实际执行。最终定向回归 105 项通过 | CPU/受控 HTTP、WS 与音频夹具；不是 Nano 推理、真实知识忠实性或麦克风全双工验收。load_utils.py 原样补收，LF/CRLF 校验等价；完整外部 GPU 依赖未执行 |
| 2026-10-19 01:46（北京时间）/ 真实 CueKB 三用例 | 问候和 TCP/UDP 通过；产品轮工具选择、3 项检索、截断证据准入、唯一 API 调用和缓存结果重放均通过。VoiceChat `results_replayed=1 replay_limit=0`，API `results_submitted=1 ack_grants_restored=0`，最终仍为 `VOICE_TOOL_REQUIRED/VOICE_ANSWER_TIMEOUT` | 证明结果已经回到模型，也证明 API 未观察到可用于授权恢复的重复 ACK 边界。当时尝试在 VoiceChat 内部隐藏重复 ACK；后续 23:32 UTC 现场仍失败，本版已撤回该补偿。第四用例未运行，真实知识口述与完整四用例仍未通过 |
| 2026-10-19 00:45（北京时间）/ 真实 CueKB 三用例 | 问候和 TCP/UDP 通过；产品轮 `selection/retrieval/voice_evidence/speech` 均 passed，`candidates=selected=3 limit=0`。VoiceChat 仅产生一个 API 新调用并接收一个结果，API `received=inputs=unique_calls=1 unbound=0`；工具名、Prompt、CueKB 和去重摘要均通过。模型仍解码两次 function call，当时怀疑重复 ACK 消耗最终回答许可，但摘要不能证实该因果；最终出现 `VOICE_TOOL_REQUIRED`，验证归类 `VOICE_RESPONSE_COMPLETION` | 用户现场真实 NVIDIA VoiceChat/CueKB。当时修复把同一输入限制为一次 API 调用、一次缓存结果重放和一次重复 ACK 授权归还；后续 01:46 轮次证明 ACK 授权归还没有触发，后续隐藏 ACK 仍未解决，本版已撤回上述补偿。第四用例未运行，真实知识口述与完整四用例仍未通过 |
| 2026-10-18（北京时间）/ 原生工具故障单入口 | 新增 API 镜像内 `/app/scripts/run_native_cuekb_validation.py`：一次 `docker exec` 完成 readiness、capabilities、镜像材料及真实四 WAV 验证，输出时间窗口、conversation/input/trace ID、四条 case 和稳定故障分类；同一文件可把限定窗口的 VoiceChat stdout 归纳为 Prompt、backend Prompt、model function、关键事件和最终判定五行，不回显原始日志。故障一多轮定位与 VoiceChat 修改/预期结果独立写入 `docs/voicechat`，故障二明确暂缓。API 镜像实际构建成功并在只读 rootfs 下核对入口及四段 WAV；135 项相关测试通过，全仓 Ruff/编译、文档 Shell 语法、链接及 diff 检查通过 | 本地 Docker/CPU 和受控报告/日志夹具；未连接现场 VoiceChat GPU/CueKB，不能证明当前故障已定位或修复。VoiceChat stdout 仍须由宿主机按脚本输出的时间窗口读取，再通过标准输入交给镜像内摘要模式，这是 Docker 日志边界，不要求宿主机 Python |
| 2026-10-18（北京时间）/ 真实三用例与英文排版符号修复 | 问候、TCP/UDP 均正确直答；Black Pig 用例 `selection=passed`、工具名为 `search_knowledge_base`、CueKB 返回 trace 和命中，证明名称注册与原生检索已经打通。现场返回 `CUEKB_VOICE_EVIDENCE_UNAVAILABLE` 并在 90 秒缺少结束边界；附件原文含 143 个非 ASCII 排版字符，产品标题自身含弯引号。API 现将弯引号、长短破折号、间隔点及 `>=`/`<=` 的 Unicode 等价符号规范为 ASCII，其他 Unicode 仍拒绝；验证报告不再用原始命中覆盖证据不可用状态。39 项定向及 482 项 Python 全量测试通过 | 用户现场真实 NVIDIA VoiceChat/CueKB 报告与用户提供的产品原文；修复尚未部署。第三条只产生 49,920 输出样本且无完成边界，第四条未运行，因此不能宣称知识回答或连续四用例通过。规范化只覆盖语义等价的英文排版符号，不翻译中文证据 |
| 2026-10-18（北京时间）/ 镜像内置验证材料与最小输出 | API 镜像内置验证/探针脚本、`cases.json` 和四段固定 WAV；VoiceChat 派生镜像内置官方客户端、静态检查器、同一 case 配置和 WAV。现场从宿主机直接 `docker exec`，不再创建输入目录或复制宿主机测试文件；只在 `/tmp` 写报告、回答音频和动态会话快照。三个验证脚本默认只输出判定字段，完整 JSON 改为显式 `--print-report`。API 与 VoiceChat 测试镜像实际构建成功，API 在只读 rootfs 下核对四用例通过；79 项定向测试、Ruff、脚本编译及 30 个文档 Shell 块语法通过 | 本地 Docker/CPU 与受控夹具；未访问现场 GPU/CueKB，不能记为真实语音闭环通过。两次 485 项全量运行各有一个既有时序/SQLite 并发用例瞬时失败，其余 484 项通过，两个失败用例单独重跑均通过；本次改动的定向范围无失败 |
| 2026-10-18（北京时间）/ 完整工具名与跨层诊断 | 工具公开名称和可信 implementation 全面统一为 `search_knowledge_base`；旧缩写及错误拼写严格拒绝。VoiceChat WebSocket 和 Triton backend 增加最终 Prompt 工具名/hash及同 sequence 模型函数通道名称日志，用于区分注册/转换错误与模型生成错误。191 项名称/CueKB/语音定向、478 项 Python 全量、66 项 VoiceChat 交付契约及独立 VoiceChat 环境 25 项完整文件测试通过；Ruff、编译、manifest 与 diff 检查通过 | SQLite、受控 WS/CueKB 与 CPU VoiceChat 夹具；尚未部署真实 GPU。诊断不记录 Prompt、query 或参数正文，且必须以实际容器文件 hash、两层 Prompt hash 和同 sequence 模型日志联合判断 |
| 2026-10-18（北京时间）/ cuekb-native-v1 名称兼容（已被完整名称迁移取代） | 部署消费品路由元数据后，问候及通用技术控制通过；产品问题进入原生工具选择，但数据库记录错误拼写 `search_cueb`、`reason_code=VOICE_TOOL_UNKNOWN`、`stage=selection`，尚未进入参数或检索。早期严格别名兼容方案已删除，当前不接受任何别名 | 用户现场真实 NVIDIA VoiceChat 与 API 记录；该早期方案不再代表当前代码。问候录音首词转写问题按当前安排另行处理 |
| 2026-10-17 / cuekb-native-v1 消费品路由修正 | 扩展当时的单工具目录及会话提示，覆盖价格、重量、成分、包装、运输、存储、保质期等企业产品事实；VoiceChat 单工具示例改为消费品规格问题，并要求已注册工具覆盖请求时不得直接声称无权访问。76 项目录/模板/原生 CueKB 定向回归及 477 项 Python 全量回归通过；Ruff、manifest 9 文件 hash 与 diff 检查通过 | 本地配置、受控协议夹具和模板渲染验证；未部署真实 VoiceChat/CueKB，不能据此宣称模型选择率、检索相关性或口述忠实性已通过。模板 SHA-256 为 `1bcab28d15d343297de76e5109786532fe602f3350d4e475ece1716bd67029f7` |
| 2026-10-17 / cuekb-native-v1 消费品路由基线 | 同一合成英文产品文档问题连续 20 轮均 `selection=failed`、直接口述无权访问文档、未调用 CueKB；数据库窗口内 18 条产品转写逐字稳定，另两条为问候/通用技术控制；此前单轮曾生成错误名称 `search_cueb`。VoiceChat 21/21 会话均 tools=1、Jinja 渲染及 Triton prompt prefill 成功，prefill 失败为 0；API 工具回显确认通过 | 用户现场真实 NVIDIA VoiceChat、dual_tools/general_qa、当时的单工具目录；输入为本仓库合成 PCM16 WAV，不是真人录音。结果证明旧目录/示例对消费品事实选择失败，不证明新元数据有效，也未进入 CueKB 参数、检索、来源或实际知识口述阶段；直接回答未编造产品数字但不满足工具选择要求 |
| 2026-10-16 / cuekb-native-v1 | 477 项 Python 全量回归通过；最后补充检索审计后 44 项定向回归通过；26 项 Node 音频单测、8 项 Chromium 文件通话测试及前端构建通过。API 实际镜像构建成功，依赖层 CACHED；只读容器/UID 10001 下配置目录及 query-only/client=None 检查通过；Ruff/编译/manifest/链接检查通过 | Python 3.12、SQLite、独立监听的本项目 HTTP/VoiceChat WS/CueKB HTTP 协议夹具，其他错误注入使用 MockTransport；artifacts/native-cuekb/python.xml。覆盖单工具注册、无额外模型、实际 query、服务端鉴权/过滤、错误分层、证据预算/ASCII、同 call 回填、音频结束及旧目录升级。容器检查使用临时 SQLite，不是生产 PostgreSQL 启动、GPU 或真实英文听音；不宣称模型选择率、实际口述忠实性或工具等待打断已验收 |
| 2026-10-16（北京时间）/ qa-tools-v4 自主回答与模板 | 218 项定向 pytest 通过；全目录 Ruff、Python 编译、shell 语法、模板 manifest hash、相关 Markdown 链接与 diff 检查通过 | Python 3.12、Jinja2 3.1.6；受控 VoiceChat 事件、SQLite/mock CueKB 和本地 WS。覆盖音频先于/晚于 final ASR、同会话直接回答→两种原生工具分别调用→同 call 回填→音频结束、重复调用幂等、严格策略拒绝及部署策略不匹配。未访问真实 VoiceChat/CueKB、GPU 或浏览器听音，不能声称全 PAD 或现场语音已修复 |
| 2026-10-16 / vLLM 模型源码报告 | registry 匹配 NemotronHForCausalLM；function_head→asr_head 映射存在；forward 实际返回两个自定义输出，与配置一致 | [源码复核](voicechat-validation-20261015.md#11-vllm-模型源码报告复核)。发现 combined_embeds 缺失时回退 token embedding 的分支，尚无现场触发证据；实际权重装载、forward 接收值及头部分数未验证。仅附件审阅，无 GPU 测试或外部源码改动 |
| 2026-10-16 / loader 与完整静态报告 | 核对 loader 路径：nano-v2-vllm、共享 tokenizer、独立 embeddings；报告函数头 [131072,4480] 与融合声明 1/1/2；源码已配置 PIECEWISE | [加载复核](voicechat-validation-20261015.md#10-2026-10-16-loader-与完整静态报告复核)。仅源码/AST/报告检查；perception/RNNT 等非严格加载未检查缺失键，vLLM 实际函数头映射和 embedding 数值一致性未验证；无新 GPU 测试或外部源码改动 |
| 2026-10-16 / 静态检查器输出契约修正 | 21 项 test_voicechat_artifacts 测试通过，定向 Ruff、diff 检查通过；运行必需输出与可选 function_logits 声明分开报告 | 用户现场声明仅 text_logits/function_tokens，旧检查器合并判断产生 mismatch；[复核记录](voicechat-validation-20261015.md#9-2026-10-16-输出声明复核与检查器修正)。本地 CPU 夹具，不证明现场 head 实际加载或全 PAD 根因修复 |
| 2026-10-16 / 用户四份提示文件 | session hash 与探针一致；本地重渲染 6276 字符与 LLM prompt 逐字一致；两工具完整 JSON schema、2401 字符严格指令及 ACK 对照通过 | [文件复核](voicechat-validation-20261015.md#8-2026-10-16-四份提示文件复核)。本地 Python/Jinja2 3.1.6，非 GPU/prefill/head 验证；模板尾部错误修正/额外调用措辞需与策略对齐，尚无模板改动或现场 A/B |
| 2026-10-15 23:38（北京时间）/ 用户隔离探针 | 真实连接/握手通过，确认 lookup_knowledge 与 reason_over_knowledge；完整 WAV 后继续送约 9.84 秒静音，原生调用和回填均为 0，TIMEOUT_WAITING_NATIVE_TOOL；123 个音频块，未见 audio.done/speech_text.done | [后续探针分析](voicechat-validation-20261015.md#7-后续隔离探针结果北京时间-2338)。本地验证补帧 PCM hash 与原附件一致；报告无原始 function token、转写/回答正文或输出 WAV；现场 API/镜像版本仍 unverified，不构成知识/听音验收 |
| 2026-10-15 / 用户现场麦克风与 WAV 日志 | 两轮 tools=2；function 分别 PAD=254/64、SOTC/EOTC=0，均在未授权 audio.delta 触发 VOICE_TOOL_REQUIRED；麦克风轮有五次 render_budget EOS；WAV final_chars=44、仍报告缺 Hell | [本轮判定](voicechat-validation-20261015.md)。用户确认 dual_tools/knowledge_required/qa-tools-v3/is_mock=false；本地核对附件事件与源 WAV 格式/样本/hash。未取得现场镜像/模型/模板 hash、逐层 PCM 或原始 token；未执行新的 GPU/知识/听音验收 |
| 2026-10-15 / API 构建缓存（基于 `aa5c558`） | 3 次实际 Docker 构建成功：冷构建；仅应用目录变更时依赖层 CACHED；requirements 仅追加注释时 56 个 wheel 全部 Using cached、零 wheel 下载。容器内 pip check、UID 10001/只读文件系统下 API 导入、镜像内无 pip 缓存内容检查通过；5 项部署契约测试通过 | 托管 Linux amd64、Docker 28.4.0/vfs、基础镜像 digest 不变；artifacts/api-build-cache/{cold-build,source-change,requirements-cache}.log。只验证本地构建缓存与导入，不是现场速度基准、服务启动或真实知识/语音验收 |
| 2026-10-15 / P1-A（`92da334` 上的提示修正，qa-tools-v3） | 221 项定向 pytest 通过；Ruff、Provider/Settings 编译、Markdown 本地链接与 diff 检查通过 | Python 3.12.14、Ruff 0.16.6；legacy/dual_tools、两种策略、0/1/3 工具、未授权问候/混合请求/无匹配工具/固定 ACK、非空 PCM 拒绝、重连恢复、空边界及 ACK 竞态。模拟网络/SQLite 夹具；报告 artifacts/p1-a/strict-voice-policy.xml；未运行真实 VoiceChat、CueKB、GPU、容器、浏览器或听音 |
| 2026-10-14 / c6bba4e | 应用完整回归：340 Python、26 Node、18 Chromium；Ruff、TypeScript、前端构建通过 | API/VoiceChat/CueKB/文本模型为受控网络夹具；artifacts/q1003-release/{python,browser}.xml。Python 3.12.14、Node 24.19、Chromium 154；非 GPU/真实知识/听音 |
| 2026-10-14 / 用户现场日志 | tools_count=2/type=str；202 输入块；function PAD=200；最终转写缺 Hell；无结束事件后 VOICE_ANSWER_TIMEOUT，门户显示错误 | [完整判定](voicechat-validation-20261014.md)。无本轮 CueKB 调用证据；未取得实际 PCM、权重和运行 hash |
| 2026-10-14 / b924c72 | PAD 补丁 46 CPU 边界 + 22 QA 回归通过 | artifacts/voice-recovery/pad-tail-completion.xml；受控 backend/WS 方法，不保证触发时刻或真实语音完整 |
| 2026-10-14 / 2af7566 | 完整 backend 文件交付验证 48 + 8 CPU 诊断通过；hash、补丁反向/正向、编译检查通过 | artifacts/voice-recovery/bundled-backend*.xml；NumPy 2.3.5，未安装整套 GPU 依赖；hash 以 snapshot-manifest 为准 |
| 2026-10-15 / 63e7059 | VoiceChat 相关 62 项定向测试通过；四个运行文件编译、启动命令 shell 语法、diff 检查通过 | 对应 pad_tail_completion、snapshot_handshake、artifacts 三模块；仅交付与规则文档，未执行 GPU docker run |
| 2026-10-13 / 工具目录发布验证 | API/Web 构建、Compose config、SQLite 0006→0010 及降升级/检查通过 | 仅本地；不证明生产 PG 迁移、TLS、Redis、并发或云端部署 |
| 2026-10-13 / 论文与静态产物审计 | 固定上游 097dfe9、HF config 443794e 的融合声明 1/1/2；相关诊断 30 项通过 | artifacts/voicechat-paper/；未验证现场权重实际加载。研究细节按需读 [论文](voicechat-paper-v2-review.md)、[源码](voicechat-upstream-analysis.md) |

工具注册兼容的早期 124 项等定向结果已被对应完整应用回归覆盖，过程不再重复列入当前页。只有输入/函数/回答/业务/播放关联到同一轮的证据，才能判断端到端成功。

## 1. 历史证据定位

需要追溯早期 P1/P2、CTX1、模拟与发布记录时查 [历史验收](archive/2026-10-07/acceptance-report.md)；此次精简前的详细流水可查 Git `63e7059:docs/acceptance-report.md`。历史中的“当前/本轮”只属于其记录日期，不能覆盖新证据。

## 2. 记录新验证

每条新证据必须带执行日期、代码/模型/模板/镜像版本、策略/目录、环境、命令/样本、结果及未覆盖范围。失败、缺测和不适用分别记录；探针合成工具结果不当作 CueKB 成功，预算 EOS 不当作自然完整回答。普通日志无正文，授权原始采集放忽略目录。

P1-A 的实际测试命令：

```sh
.venv/bin/python -m pytest -q tests/integration/test_native_tool_catalog.py tests/integration/test_voice.py tests/integration/test_qa_routing.py tests/integration/test_simulated_full_flow.py tests/contract/test_english_scope.py tests/contract/test_protocol.py tests/contract/test_deployment.py --junitxml=artifacts/p1-a/strict-voice-policy.xml
.venv/bin/ruff check apps/api tests scripts
.venv/bin/python -m compileall -q apps/api/app/voice/provider.py apps/api/app/config.py
git diff --check
```

测试保留既有 Starlette/AnyIO 弃用警告和 22 条 JUnit record_property 兼容警告；无失败。生成提示的契约回归不能证明模型会遵从提示或主动调用工具；无匹配工具保持沉默时不会自动生成文字澄清。本次未修改 Gateway 放行规则、外部 VoiceChat、PAD 配置或口述控制架构。

## 3. 当前明确未验证

- ASR 首差异、全 PAD 根因、麦克风有效输入、正常回答自然 EOS 与预算结束的完整听音；预算分支触发已有新日志。
- 严格策略下真实 CueKB/ACL/外置模型、同 call 回填及授权口述闭环；配置和未授权回答拒绝已有现场证据。
- 工具等待期语音修订、未知迟到 response 归属、转写与实际音频一致性；NVIDIA 四项等待能力仍为 false。
- 真实播放、长会话/轮换/断网、生产数据迁移/备份、Redis/多副本故障与容量。

完整 D07/V01–V12/Q07-T01–T27 要求保留在 [验证清单](development/validation.md)。本地 fixture、容器 ready、文本正确及收到音频包均不能替代真实验收。
