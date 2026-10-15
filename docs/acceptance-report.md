# 当前验收证据

更新日期：2026-10-16；最新附件分析基于 `codex/q1003` 的 `b7a04d6`。**真实知识语音闭环尚未放行。**现场日志已验证严格拒绝保护及预算 EOS 分支触发；原生工具仍未调用，正常知识回答及完整听音未通过。新提示文件复核确认严格指令与完整工具 schema 保留，发现模板结果处理措辞冲突但尚不能归为全 PAD 根因。状态见 [任务板](TASK_BOARD.md)，要求见 [验证清单](development/validation.md)。

当前用户目标已改为 VoiceChat 自主决定直接回答或调用工具，部署采用 general_qa。qa-tools-v4 已修正模板冲突并补充部署策略检查；以下严格模式现场结果保留为历史证据，不能当作新模式的验收结果。

## 0. 当前审计与证据索引

以下是分别执行的验证，不累加成一个“当前全量通过数”，也不更新历史测试的执行日期。

| 日期 / 版本 | 实际结果 | 限制与证据位置 |
| --- | --- | --- |
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
