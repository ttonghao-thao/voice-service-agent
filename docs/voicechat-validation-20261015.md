# 2026-10-15 严格模式现场复测分析

分析代码基线：`codex/q1003` / `b7a04d6`。输入是用户提供的两份门户/API/VoiceChat 日志和一个 WAV；未连接现场容器或执行 GPU 推理。用户已确认 capabilities 为 `dual_tools / knowledge_required / qa-tools-v3 / is_mock=false`；现场镜像、模型、模板和挂载文件 hash 尚未取得。

## 1. 结论

两次失败都发生在未经工具授权的第一段回答音频：VoiceChat 注册两个工具后，function 通道直到连接关闭仍全为 PAD，API 报 `VOICE_TOOL_REQUIRED`。严格交付保护得到现场支持；原生工具调用和真实知识语音闭环仍未通过。不能把工具注册成功或此次受控拒绝算作知识问答成功。

原生调用缺失的发生位置已缩小到 parser 之前，但原因仍可能涉及实际工具描述/最终模板、prefill、function head/tokenizer/权重加载、音频融合或模型选择。日志不能在这些原因中直接定案；也不能证明连接若继续保持就永远不会产生较晚调用。

## 2. 两次测试的证据

下列行号对应用户附件原文；原始日志和录音不纳入 Git。

| 项目 | 测试 1：麦克风，sequence 2012943390 | 测试 2：WAV，sequence 2987580059 |
| --- | --- | --- |
| 注册 | tools_count=2；instructions_chars=2521，最终模型提示 chars=6396 | 相同计数；使用 Jinja 模板 |
| function 终态 | PAD=254、SOTC=0、EOTC=0、other=0，idle（附件 1 第 544 行） | PAD=64、SOTC=0、EOTC=0、other=0，idle（附件 2 第 155 行） |
| 用户转写 | 前八次 agent BOS 对应 final_chars=0；最后一次仅 2 字符 | final_chars=44，全部累计字符已发布 |
| API 行为 | 先抑制八个初始 response；14:28:29.981 拒绝未授权 audio.delta，pending_tools=0（第 9–18 行） | 14:31:14.685 拒绝未授权 audio.delta，pending_tools=0（第 8–10 行） |
| 传输终态 | WS 解码 491520 个 24 kHz 样本（20.48 秒），queued=sent=271 | WS 解码 126720 个 24 kHz 样本（5.28 秒），queued=sent=17 |
| PAD 结束保护 | 五次 agent.eos source=render_budget；已有八组 response.done | API 在首段回答音频后关闭，未观察完整回答生命周期 |

测试 1 的 `dropped_events=232` 是诊断历史环形缓存淘汰计数，代码位于 [audio_server.py](voicechat/s2s/audio_server.py) 的 `_voice_diagnostic`；不能解释为丢了 232 个音频包。VoiceChat 的 sent 只证明向 API 发送，不证明门户播放。

测试 2 在 14:31:14.661 发布最终转写并创建回答，14:31:14.663 向 API 发音频，随后 API 拒绝并关闭；Triton 的 abort 出现在关闭之后。该时序支持应用主动结束未授权会话，不能将最后的 abort 当作原始推理崩溃。当前失败也不是旧的 `VOICE_ANSWER_TIMEOUT`。

## 3. WAV 与 Hell 缺失

本地读取附件确认：标准 PCM16、单声道、24000 Hz、119952 样本、4.998 秒；首个非零样本约在 1.0139 秒，最后一个约在 4.5225 秒。文件 SHA-256：

```text
de9589b01126d70982373ba74b8c71e9ab4f91ec1fe3a98131a714d636b7b77d
```

格式符合门户和探针要求，已有约一秒前置静音。以上是格式和波形检查，未进行听音转写；不能仅凭非零幅度证明 Hello 发音完整。

测试 2 的 ASR 累计长度在 step24/30/34 分别为 1/9/17；step34 一次发布全部 17 字符，期间没有清空非空转写的 reset。WS 收到 raw20 → clean17，按代码删除的是 `<s>` 三字符；最后累计和已发布长度都为 44。这不支持“显示 gate 丢掉首四字符”或“清理标记删掉 Hell”的解释。附件未包含原始转写正文/token，首个缺失位置仍须通过同一次 sequence 的 PCM 和 RNNT 原始输出确定。

WS 收到 5.28 秒、Triton 完成 5.12 秒，均已覆盖文件时长，但只是样本计数覆盖；尚不能证明现场首部波形与源文件一致。RNNT 字幕不是 VoiceChat 主干 LLM 的直接文本输入，不能把缺 Hell 直接判为 function 全 PAD 的原因。

## 4. 麦克风测试的独立异常

持续送音不等于有完整有效的人声。前八次模型主动 BOS 都记录 `user_speaking_before=false`、`nonblank_total=0`；最终一次在 display gate=false 时因 agent BOS 强制发布累计的 2 字符。该行为与 [model.py](voicechat/opt/tritonserver/backends/nemotron-voicechat/model.py) 为保留短句而在 BOS 前发布转写的逻辑一致。

因此需要分别核对麦克风设备、输入电平及实际送入 VoiceChat 的 PCM，并用同一文件排除设备因素。现有证据不能确认这 2 字符来自真实短句、噪声还是误识别，也不能确认用户是否在前段发声。若采集证实无语音却生成输入项，再修正输入回合成立条件；不能直接按字符数丢弃所有短句。

五次预算 EOS 分别在 step83/110/137/164/191，均为 content_tokens=4、pad_tail_frames=13、ratio=3。这证明预算结束分支在现场触发；未证明自然 EOS、回答内容完整或听音无截断。反复主动 BOS 的原因仍需对照，不能直接归因于 PAD 补丁。

## 5. 解决顺序与修复判据

1. **先隔离原生工具选择。** 使用同一 WAV、实际新会话 `session.update`、相同目录/策略/模板和模型，运行 [部署中的连续送音探针](deployment.md#voicechat-诊断探针与部署产物核验)。探针直接连接 VoiceChat，不播放到客户，也不会因 API 首段拒绝提前结束观察。先核对最终渲染提示包含两个工具的真实名称/参数和严格指令，并检查模板是否另有主动问候/自由回答要求；6396 字符和 ACK tools=2 不能证明工具 schema 正确。再核对现场转换产物、function head、特殊 token、实际融合配置和加载路径。
2. **同轮定位 ASR 首差异。** 按 [同 sequence 采集说明](voicechat-validation-20261014.md#5-同一次-sequence-的隔离采集) 捕获 WS 24 kHz PCM → soxr 16 kHz → Triton 提交/接收 PCM，以及 encoder 选帧、RNNT 同帧参考对照和原始 function token。采集使用独占 sequence/目录，并限制时长与容量。采集配置须同时到达 WS 和 backend，变更后重建验证容器。先保持本轮 PAD 参数不变；需要比较 PAD 行为时另开单变量 A/B，不把两类结果混合。
3. **只在首个差异处修复。** PCM 首部丢失则修发送/缓存/重采样；相同 PCM 和状态下参考 RNNT 完整、当前解码缺失则修 RNNT 状态/解码；原始 token 完整而字幕缺失则修发布链路。function 全 PAD 继续查模板/head/融合；只有原始 SOTC/EOTC 存在但 WS 无工具事件时才修 parser。若在未授权音频之后才出现工具调用，属于输出顺序问题，仍不符合当前交付契约。
4. **回门户验证真实闭环。** 先用相同产品问题核对文字入口的真实知识结果，再验证语音 native call → `voice_tool_call_received` → 真实知识执行 → 同 call_id 回填 → 授权音频和完整结束。文字成功只隔离知识服务问题，不代替语音验收；探针合成回填也不作为 CueKB 成功证据。

建议第一轮探针将 `--tool-wait-timeout` 设为 10、`--tool-delay-seconds` 设为 0、`--post-result-seconds` 设为 5，使用当前约 5 秒样本，保持采集总时长低于配置上限。这是有限窗口诊断，不是产品超时调参或模型可靠性通过标准。随后按观察结果再安排等待/收尾测试。

需要取得的最小现场材料：探针 JSON；该 sequence 的 `ws-summary.json`、各阶段 PCM 和 `model-events.jsonl`/相关 tensor；实际 session.update、挂载的 prompt_template.jinja、运行文件 hash、镜像 digest 和静态产物检查 JSON。只在授权调试位置保存原始内容，不上传密钥或完整模型权重。

继续保持 `knowledge_required`。切回 general_qa、social_only 默认放行、自动把每条 ASR 包装成工具调用、给字幕补 Hello、调大回答超时，都不能修复本次已定位的失败边界。若运行接线正确后，重复样本和代表性问题仍无法可靠先调用工具，或口述无法满足播前准确性要求，按 [架构替代条件](q1003-target-architecture.md#5-架构替代的进入条件) 评审 ASR→业务→受控 TTS；本轮证据不足以宣布必须切换。

## 6. 本轮验证边界

已完成附件事件计数/时序解析、WAV 头/样本/幅度/hash 检查，以及 Gateway、ASR 发布、WS 诊断缓存、门户 WAV 输入和连续送音探针源码核对。未进行新的现场推理、听音、CueKB 调用或代码修复；本次结论是故障定位与可执行采集/修复路径。

## 7. 后续隔离探针结果（北京时间 23:38）

用户随后提供实际探针 JSON，`tested_at=2026-10-15T15:38:36.709480+00:00`，即北京时间 2026-10-15 23:38:36。以下是该报告的离线分析，不是分析环境新执行的 GPU 测试。`api_version` 和 `image_digest` 仍为 unverified；配置来自 explicit_snapshot，SHA-256 为 `f7b0b128e6fc63bbcdeb88171e8dd3f13d7982328c79ff9488bd34fda1b9e95d`，尚未取得该快照正文和最终模板。

| 检查 | 报告结果与判定 |
| --- | --- |
| 连接及注册 | real_service_connected=true；handshake_and_24khz_format=passed；实际名称为 lookup_knowledge、reason_over_knowledge |
| 原生调用 | native_calls_observed=0；native_results_sent=0；pending_call_ids_at_close=[] |
| 退出原因 | status=partial，phase=waiting_native_tool，reason=TIMEOUT_WAITING_NATIVE_TOOL；不是门户 VOICE_TOOL_REQUIRED，也不是 API 回答超时 |
| 输入 | 120960 个 24 kHz 样本（5.04 秒，含末帧补零）；随后发送 236160 个静音样本（9.84 秒）；最大送帧间隔 82.453 ms |
| 输入一致性 | 原附件按探针 read_wav 规则补齐 80 ms 帧后，SHA-256 为 865f07e89b77918f377a6f49c5348c10b0007ad4ebb3a55c733804c1415640d4，与报告完全一致；这不证明现场重采样/encoder 后的样本完整 |
| 转写 | 首次 delta 为 17 字符；最终 44 字符；正文不在报告中，不能仅凭长度确认具体缺词 |
| 回答 | 同一 response 下 123 个 audio.delta、236160 个输出样本（9.84 秒）；17 个 speech_text.delta，累计 168 字符 |
| 观察完整性 | timeline_dropped=0；未记录 audio.done 或 speech_text.done；未保存输出 WAV，不能判定尾部是否静音或听音完整 |

相对探针流开始：5.001 秒源 WAV 发送完毕，5.114 秒收到最终转写及第一段回答音频；7.850 秒收到最后一段回答文字；音频一直到 14.969 秒仍在发送。探针持续等待期间没有工具事件。因此在本次约 15 秒的观察窗口内，未复现“首段音频后较晚出现原生调用”；门户提前拒绝不是该窗口内无工具事件的充分解释。

`synthetic_tool_result=true` 是探针的运行方式标记；本轮回填计数为零，实际上没有发送合成结果。Adapter 不保留原始 response.done，所以不能根据该报告判定它是否出现。该报告也不包含原始 function token，不能把此前两轮全 PAD 的计数套用到本轮；需取得同轮 VoiceChat sequence.end/function.raw 日志，区分模型未输出和 parser 未交付。

下一步优先取得本次 session-update.json、实际挂载的 prompt_template.jinja 和同轮 VoiceChat 日志，再核对最终提示与原始 function 通道。未见结束事件还需对照同轮 PAD/EOS 日志及实际运行配置，不据此断定补丁未加载。暂不扩大等待时间或修改严格授权规则。

用户随后补充本轮 VoiceChat 日志：client=817dc231、sequence=1488427340，input_item_id 与探针报告相同。sequence.end 记录 PAD=184、SOTC=0、EOTC=0、other=0、state=idle，确认本轮原始 function 通道也未产生调用标记；queued=sent=155，sent_audio_chunks=123，与探针音频计数一致。日志未出现 agent.eos 诊断事件，仍需检查预算条件而不能宣称补丁无效。

同轮 Session update 为 instructions_chars=2401、tools=2；使用 Jinja，ACK 配置 tools=2，去除 ACK 配置段后送入模型的提示为 6276 字符。本地当前代码生成的 knowledge_required 空历史 instructions 同为 2401 字符，但该长度不能代替正文比对；此前门户 2521 字符的指令亦不能仅凭长度差判为版本不一致。下一步将原探针 session 快照与容器当前模板重渲染，核对 snapshot hash、严格指令、工具 schema 及剥离 ACK 后正文；磁盘重渲染不是对历史进程实际 prompt 的远程证明。

## 8. 2026-10-16 四份提示文件复核

用户提供 voicechat-prompt-template.jinja、voicechat-prompt-check.json、voicechat-llm-prompt.txt、voicechat-session-update.json。使用临时目录中的 Jinja2 3.1.6/MarkupSafe 3.0.4 按当前 WS 源码配置重新渲染，仅作本地附件验证，未修改运行依赖、外部源码或现场容器。

- session 对象按探针序列化规则计算的 SHA-256 与上一轮报告完全一致，确认提供的是该探针的配置快照。
- 渲染并按 backend 正则剥离 ACK 段后，6276 字符与所提供 LLM prompt 逐字一致，SHA-256 为 `33bd2aba0b42b89b88a041e5832fe292605e629dd3dd6fdd6dba957b1dc8ce79`。
- AVAILABLE_TOOLS 内 JSON 可解析，两个工具逐项深比较与 session.tools 一致，仅移除模板设计要求的 type/ack_messages；名称、description、完整 parameters 均未丢失。三个 required 字段 user_request/product_model/software_version 完整；产品/版本字段允许 null，additionalProperties=false。
- instructions 与当前 Provider 生成的 knowledge_required 空历史指令逐字一致（2401 字符），严格口述和未经授权不得问候的约束完整保留。
- ACK 段的工具名称和消息与 session.tools 一致，并按设计不进入 LLM-visible tool JSON。
- 模板文件 SHA-256 为 `e28694b99a2b087ce6b2b9d1abec10878fa54b8c719324c484b1d15a559f6c2b`。这是所提供文件的 hash，不是历史进程内存或真实 tokenizer/prefill 的证明。

发现的提示歧义是模板尾部允许 “call additional tools ... correct tool calls if any errors are found”，而应用指令禁止重试同一请求或同时调用多个工具。尾部通用授权缺少会话策略边界；工具调用示例又展示两个调用、字符串 arguments，虽现有 WS parser 兼容字符串参数，仍可能误导单调用及参数对象契约。工具定义本身的“无公司来源的一般概念可直答”带有 unless session policy requires evidence 限定，不能认定它覆盖了严格策略；模板的 if required/If you decide 也不能单独证明原生调用未触发的原因。

建议最小修正模板末尾，使额外调用/错误处理服从会话策略，并明确不重试、一次一个调用；参数示例调整若实施应单独做兼容回归。该文本冲突主要描述结果返回之后的行为，不足以解释当前尚未发生的首次原生调用，不能标记为全 PAD 根因或声称改模板即修复。

当前已可排除所提供文件中的工具 schema 丢失、严格指令未插入、ACK 清理误删工具定义等解释。下一主检查点为现场转换后 function head/custom_outputs/tokenizer 与实际加载配置，以及同 sequence 的 function 分数/选中 token 和音频融合；不因文件静态正确而宣布模型调用可靠。模板冲突仅记录建议，本轮未更改外部模板。

## 9. 2026-10-16 输出声明复核与检查器修正

用户报告两份 tokenizer JSON 内容一致，并贴出转换后 LLM 的 custom_outputs 为 `["text_logits", "function_tokens"]`，必需字段缺失列表为空，仅 function_logits 未声明。此前 function_outputs_contract=mismatch 是检查器将可选诊断字段也列为运行必需条件造成；该结果不能解释原生 function 全 PAD。

当前 backend 直接读取 text_logits/function_tokens，隔离采集对 function_logits 使用可选读取；没有它时仍可记录选中 token，但无法获得该路径的 PAD/SOTC/EOTC 分数与排名。只向配置添加字段不证明实现支持返回相应数据。

检查器已修正：function_outputs_contract 仅判断两个运行必需字段，新增 function_scores_declared 独立报告诊断分数声明，并保留实际声明和缺失字段。针对本轮声明，预期为 passed/missing。增加缺诊断分数、缺必需字段和声明格式异常的回归；21 项静态产物测试及定向 Ruff、diff 检查通过。本轮没有修改模型配置、权重或外部 backend。

下一步核对实际 load_utils.py/checkpoint_utils 入口、完整静态报告和启动加载信息，确认函数头训练权重、tokenizer 和融合配置的真实加载情况。两份 tokenizer 一致及函数头 shape 检查通过不证明进程采用了这些文件，也不证明权重数值正确。

## 10. 2026-10-16 loader 与完整静态报告复核

用户提供 checkpoint_utils_init.py、load_utils.py 和 voicechat-artisfacts.json（附件名称如此）。按 AST/源码读取核对，未执行上传的 loader 或加载 GPU 模型。load_utils.py SHA-256 为 `d77a93396f08aa24cbeb40d67a38d615f29a1b482a1bf26f7653004be13369cc`。

已确认的文件级事实：

- __init__ 直接重导出 load_utils.load_nemotron_voicechat。默认配置读取模型版本目录的 config.json，LLM 指向 nano-v2-vllm，TTS 指向 eartts_vllm。
- load_nemotron_voicechat 把 pretrained_llm 改为版本目录的 tokenizer，并向 vLLM 显式传入同一目录；backend 的 get_tokenizer 也使用它。按此前挂载推算为 /data/models/nemotron-voicechat/1/tokenizer，而不是优先使用 nano-v2-vllm/tokenizer.json。用户已报告两份 JSON 相同，完整 tokenizer_config/包装器行为仍未验证。
- 完整报告确认 stt_model.function_head.weight、lm_head.weight、embed_tokens.weight 都为 [131072,4480]、F32；SPECIAL_20/21/22 分别为 20/21/22。原始 VoiceChat 配置明确声明融合 1/1/2、use_function_head=true；仅 fuse_method 使用参考缺省。loader 未改写这三个权重，当前 backend 读取同一 model_cfg 并进行加权相加。
- AsyncEngineArgs 已显式设置 dtype=bfloat16、compilation_config.cudagraph_mode=PIECEWISE。静态 F32 权重与运行转换为 BF16 并不构成单独的格式错误；该源码设置也不证明实际运行时版本或执行图。
- LLM 函数头没有在这份 loader 中单独装载，而是把模型目录交给 AsyncLLM.from_vllm_config。真实函数头的权重映射和 forward/output 实现位于 vLLM 内部模型类，本次三个附件尚不包含该部分。

发现的加载校验缺口：perception（384 行）、codec（412 行）、RNNT decoder/joint（578/579 行）都调用 load_state_dict(strict=False)，且未检查返回的 missing_keys/unexpected_keys。RNNT 日志的 tensor 数量来自按前缀筛选的输入字典，不能证明全部必要参数匹配；文件不存在时确实会报错，但文件存在、关键参数缺失仍可能被忽略。这个风险可能涉及 ASR 或共享感知输入，目前没有缺失 key 的现场证据，不能直接标记为故障根因；也不能把这些 strict=False 调用归到 vLLM 函数头加载。

另一个未覆盖项是 loader 使用独立 embeddings.safetensors 的 embed_tokens.weight 进行 prompt/历史 token 融合，之前的检查只查看 nano-v2-vllm/model.safetensors。需要核对独立 embedding 与训练/转换产物的一致性；shape 一致不代表数值相同。

建议继续读取实际安装的 vLLM 模型类与 load_weights/forward/custom output 实现，核对函数头键映射；对非严格加载补充缺失/多余键审计和关键参数完整性检查。该建议尚未修改外部 loader，也未重建现场服务。完整静态报告仍来自旧检查器，function_outputs_contract=mismatch 按上一节的两个必需字段/可选诊断字段分离解释，不是新增故障。

## 11. vLLM 模型源码报告复核

用户提供 vllm-model-source-check.json，报告 SHA-256 为 `6b4d297143140c25d2c5cc37d93870a932405a80879b9aa76dc3733d8a7eeb04`。报告声明 vLLM `0.24.0+precompiled`，包含 registry、nemotron_h.py 等源码及模型配置。本次仅读取附件与源码，未导入上传模块、执行 GPU 推理或修改外部源码。

- registry 将 NemotronHForCausalLM 映射到 nemotron_h 中的同名类；此前 loader 未显式改用 transformers 实现。该静态路径匹配，仍不能代替 worker 实际类路径及加载参数记录。
- nemotron_h.py 的 hf_to_vllm_mapper 明确将 stt_model.function_head 映射为 asr_head；load_weights 使用 AutoWeightsLoader，并只显式跳过 mtp 前缀。因此已找到函数头的加载映射，不能再以“源码中没有 function_head 模块名”判定遗漏。这里的 asr_head 用于函数 token，不能与独立 RNNT ASR 混同。通用 AutoWeightsLoader 实现、实际已加载 key 集合及 GPU 参数数值不在报告中。
- forward 对 asr_head 的 logits 做 argmax，返回 hidden_states、text_logits、function_tokens；两个 custom_outputs 的名字、数量和顺序与配置相符。function_logits 没有被返回；仅在 config 增加该名字不会产生诊断分数。全 PAD 不是这段代码硬编码的结果，但仅凭源码不能证明现场参数或输入正确，也不能排除非有限分数。
- custom_input_specs 声明 combined_embeds 为 4480 维 BF16，与此前报告的 hidden size 相符。forward 会无条件用 kwargs.get("combined_embeds", None) 覆盖传入的 inputs_embeds；底层第一 PP rank 在结果为 None 时回退 embed_input_ids。这是可掩盖自定义输入传递失败的分支，并非已经证明现场触发的故障。参数名 acoustic_embeds/asr_token_ids 在该 forward 中没有独立参与融合；当前 backend 应把融合结果经 combined_embeds 传入。

下一步应把采集点从 backend 发送端推进到实际 vLLM forward 接收端：在单会话、固定 WAV 下，对真实请求（区分 warmup/profile/graph capture）关联 prefill/decode 步，记录 combined_embeds 是否存在、shape/dtype、有限值及经相同转换后的数值摘要；与 backend 发送数据对照。另在模型加载阶段审计 asr_head.weight 是否实际装载，并在同轮采集函数头 PAD/SOTC/EOTC 分数与 top-k。若接收端输入缺失、错位或异常固定，优先修 custom_inputs/append_request 到 runner 的传递；若输入一致，再依据加载审计与分数排查参数和推理。graph 模式下不能假定 Python forward 日志每步都会执行，观察方式需明确实际执行模式。

不建议为取得分数直接增加整个词表的每步输出：可优先在头部计算处做限定帧数的诊断统计，避免无必要的传输和同步；如确需新增 custom output，必须同时修改实现、配置及消费者契约。此处记录的是下一步方案，尚无新的现场采集结果。
