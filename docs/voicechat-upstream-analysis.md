# NVIDIA VoiceChat 源码核对与 q1003 修复依据

2026-10-13，应用基线 `codex/q1003@d0dea88`。实际克隆并核对 `NVIDIA-NeMo/Speech` 的 `nemotron-labs-voicechat` 分支，固定到 **`097dfe9e2f55baf653b83035868bdc89849f1b47`**。以下链接均固定该提交。后续论文 v2 与官方 HF 配置对照见 [原理及改进](voicechat-paper-v2-review.md)。本文记录源码事实和实现依据；现场故障判定、样本和真实退出条件仍由 [D21](voicechat-fault-recovery-design.md) 维护。

当前握手结论：Adapter 已兼容 session.updated.tools 的 JSON 字符串并保留严格校验；2026-10-14 15:54 现场确认 tools_count=2/type=str 且持续送音。早期 0 音频块握手关闭已被这次证据更新，不能继续记为当前阻塞；实际文件身份与模型首差异仍待核验，见 [现场证据](voicechat-validation-20261014.md)。

## 1. 已确认结论与不能越过的边界

1. **原探针存在确定性缺陷**：发送 WAV 后，在 `tool_seen.wait()` 和延迟回填期间停止音频输入。官方明确模型随输入推进，文件需追加约 20 秒静音。旧探针可能在模型还需要输入帧时报告“未观察到工具”。本轮已修复；这不能解释持续送音的门户现场为何丢失 `Hell`。
2. **原探针没有复现多工具会话**：它始终以 legacy bridge 连接，并覆盖为接口探针提示词。q1003 的全局工具目录、实际 instructions 与该探针不一致。本轮要求多工具模式提供实际注册快照，并按快照中的 JSON Schema 检查任意已注册工具，不硬编码两工具或 bridge。
3. **缺失 function 输出与模型实际输出 PAD 必须分开**：公开 vLLM wrapper 在 runtime 没返回 `function_tokens` 时不设置 `function_predicted_token`；推理 wrapper 预填 PAD，仅在该 key 存在时更新。因此“上层全 PAD”不能单独证明模型选择了 PAD。已对上游真实方法做 CPU 受控调用，确认 key 缺失、存在且为 0、存在且为 20 三种分支；数值是夹具值，不是现场 special token ID。
   2026-10-14 对照新增快照：实际 backend `model.py:581` 直接索引 `custom_outputs["function_tokens"]`，缺 key 会抛错；公开 wrapper 的静默保留 PAD 行为不能直接套到此 backend。真实定位必须先确认运行入口与 hash。
4. **ASR final → 文本分类器 → 工具选择不是该模型的数据流**：共享 encoder 特征进入融合层/LLM，RNNT 显示转写与 function channel 分别产生输出。RNNT blank 状态仍能参与轮次控制，不能说两者完全互不影响；但缺少 `Hell` 不足以推出无工具调用的根因。
5. **已修复本项目的失败恢复链**：空边界事件误判输出、错误通知晚于清理、final 转写排队时被取消、关闭期间 DELETE 再次取消审计，以及门户静默断线。仍拒绝非空未授权回答，不把 final ASR 自动转成工具请求。
6. **尚未确定现场 ASR/function 首个差异点**：原始测试 WAV、实际运行镜像/挂载 hash 与同 sequence 捕获仍缺；2026-10-14 已取得用户归档的 WS/backend 源码快照，尚不能证明部署文件相同。公开仓库没有当前交付补丁针对的 `s2s/audio_server.py`，也不能替代容器内 `/opt/tritonserver/backends/nemotron-voicechat/model.py`。公开 Triton 示例与现场 NIM backend 必须按 hash/配置核对，不能把修改示例写成修复生产。

## 2. 核读路径与系统含义

| 源码/文档（固定提交） | 核对结果 | q1003 设计与编码影响 |
| --- | --- | --- |
| [README](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/README.md)、[deploy.md](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/voicechat_realtime_instructions/deploy.md#L104) | 统一流式模型；输入驱动输出；CLI 示例读 16 kHz 文件并建议约 20 秒尾静音 | CLI 文件格式与 WS 线上格式不能混淆；等待工具和续答时继续发送静音，单一音频 writer 保序 |
| [API reference](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/voicechat_realtime_instructions/api-reference.md#L126) | WS 为 24 kHz mono PCM16，80 ms/3840 bytes；instructions 只应用于首个 inference | 必须完成 created/update/updated 才送音；目录刷新使用新连接；不发送未支持的 tool_choice |
| [function template](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/examples/speechlm2/function_calling/template.jinja) | tools 经过模板进入可用工具段；有工具调用/结果格式约束 | session.updated 回显只证明注册传输；还需核对服务端最终渲染模板和 prefill，不能靠回显证明模型接线正确 |
| [checkpoint conversion](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/vllm/scripts/convert_nemotronllm_checkpoint.py#L110) | 默认保留 `stt_model.function_head`；声明 `combined_embeds`、`text_logits/function_tokens/function_logits` | 核对转换后产物，而不只检查原 checkpoint。新增静态检查器读取配置、tokenizer 和 safetensors header |
| [loader/fusion/special IDs](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/model_wrappers/nemotron_voicechat_inference_wrapper.py#L596) | `strict=False` 加载，部分缺失只记录计数；融合权重取决于 head 是否存在；special token 取编码结果的第一个 ID | 启动日志“已加载”不足以证明 function head 权重正确；需关键 missing/unexpected key、实际 tokenizer 单 token 编码和回环证据 |
| [vLLM output mapping](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/model_wrappers/model_factory.py#L476)、[streaming engine](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/vllm/streaming_llm_engine.py#L307) | output mapping 只在 runtime key 存在时传出 function token；engine 按配置请求 custom outputs | 采集 `function_key_present` 与实际 token 分开计数；缺 key 是契约故障，PAD 是有定义的模型输出，两者不能合并 |
| [streaming inference/frame selection](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/model_wrappers/nemotron_voicechat_inference_wrapper.py#L2340) | cache 路径从局部第 0 帧取；非 cache 路径取倒数第二帧，注释解释 10 ms/80 ms 首帧边界；语音/上一帧各通道 embedding 融合 | 必须分别记录 sample/window/encoded-frame 范围和 cache 模式；不能把 frame index 当 PCM offset，也不能直接统一改成第 0 帧 |
| [perception cache](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/model_wrappers/perception_cache.py#L340) | first/subsequent chunk、mel shift/cache、drop-extra 和 CUDA graph 有独立分支 | 句首检查需覆盖首块与后续块；必须在同音频/权重/初态下比较，前置静音不是修复依据 |
| [RNNT step/decode](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/model_wrappers/nemotron_voicechat_inference_wrapper.py#L2849) | 已有每帧最多 10 symbol 循环和 predictor cache；display 用 joint vocabulary 拼接，替换 `▁` 并 strip | 没发现固定删除前四字符的实现；不能未经同帧对照就归因于“没有 multi-symbol loop”。需比对参考 greedy 和词表 |
| [reference RNNT path](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/models/duplex_stt_model.py#L3510) | RNNT(BPE)Decoding、greedy、previous hypotheses 和参考文本处理 | 对照必须用同一帧特征、joint/decoder、初始状态、vocabulary/tokenizer；不同输入/离线全句结果不是等价性证明 |
| [pipeline ASR state](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/pipelines/streaming_s2s_pipeline.py#L1354)、[prefill](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/nemo/collections/speechlm2/inference/pipelines/streaming_s2s_pipeline.py#L2194) | 首帧自动初始化 RNNT；连续 10 blank 帧清空累计文本和 predictor；零长度首帧可只做 prefill | 核对运行版本是否具备这些逻辑；不要消费真实首段语音做 warmup，需记录 reset 原因/世代 |
| [Triton 示例增量发布](https://github.com/NVIDIA-NeMo/Speech/blob/097dfe9e2f55baf653b83035868bdc89849f1b47/examples/speechlm2/nemo_inference_pipelines/triton/model_repo_s2s/voicechat/1/infer_streaming.py#L340) | ASR 用累计字符长度截取 delta，以长度缩短推测 utterance reset；位置表在 END 清除 | 如果 reset 后新文本长度未变短，或异常结束复用 ID，单凭长度不能证明边界正确。这是需核对的条件风险，未证明现场命中 |

上表 prefill、异步 FC、RNNT 和示例内置工具执行路径并不等价于生产 WS 服务。企业业务执行继续走 Gateway → Coordinator → Runtime → Registry；不调用上游示例的演示工具执行器替代本系统授权。

## 3. 本轮代码交付

### 3.1 输入驱动的协议探针

`scripts/probe_voicechat.py`：

- 音频只有一个 writer。主 WAV 后、工具选择期间、延迟回填期间、续答窗口内持续发送 80 ms 静音；第二 WAV 由同一 writer 切入，无交错发送或静音突发追赶。
- `--session-update` 使用实际快照，不添加测试提示词；多工具模式缺快照直接失败。完整参数按注册 schema 校验；保持原始 call ID；重复通知只回填一次，同 ID 内容冲突失败。
- 默认续答观察窗口改为 20 秒。回填仍是显式标记的合成结果，不执行实际业务工具；“收到音频”不等于回答有依据。
- 所有子任务纳入 TaskGroup，发送、接收、回填异常及时终止；无工具超时、提前 session.end、事件流结束分别报告。关闭时记录未回填 call ID，不能将取消的调用算作完成。
- 事件最多 10000 条，工具最多 32 个调用，单 WAV 最多 120 秒，可选输出音频最多 16 MiB。报告显式计数被截断事件/音频，分别记录主文件、插话、静音样本以及最大发送间隔。
- 默认只保存文本长度/hash，不保存正文或参数；输入 hash 覆盖 24 kHz PCM 和最后一帧补零，不是 WAV 文件 hash。API revision/image digest 仍是操作者提供的标签，未远程证明实际镜像。

这套持续静音修复针对原探针。门户 AudioWorklet 在 EOF 后继续产生输入，不能因此宣称原现场问题也是断供音频。

### 3.2 CPU 部署产物检查

`scripts/inspect_voicechat_artifacts.py` 只读取调用者指定的配置、tokenizer JSON 和 safetensors header，不导入 PyTorch/NeMo、不下载模型、不执行模型代码：

- 检查转换后 `combined_embeds` 维度、三个 custom outputs 的声明；
- 可选核对原始 VoiceChat 融合配置与论文的音频/文本/function 权重 1/1/2，区分声明值和公开 wrapper 缺省值，不自动修参数；
- 找出提供的文件中 function head 的 shape/dtype，核对 vocab/hidden 维度；
- 检查 SPECIAL_20/21/22 added-token ID 是否唯一且位于 head 范围内；
- 输出文件头 hash、配置/tokenizer hash。**header hash 不是权重 hash**，存在 tensor 也不证明参数训练正确、runtime 已加载或 tokenizer 实际编码一致。

需提供所有转换后权重分片；不能把“未在提供的文件里找到”扩大解释为整个部署没有。启动时真正的关键 key 审计、tokenizer encode/decode 和 live output 检查必须在实际 runtime 补齐。不要用增加 SOTC boost 来掩盖输出 key/权重缺失。

### 3.3 Gateway 与门户

空 PCM delta 忽略；空响应结束不消耗工具续答许可；有声/非空文本仍受原授权约束。失败先交付允许保留的 final ASR 和结构化错误，再关闭上游/持久化；关闭中的 session 不被门户 DELETE 再次取消。门户保留转写，显示错误码及显式重试入口，意外正常码断开也展示提示。详见 [D21 §7](voicechat-fault-recovery-design.md#7-门户失败恢复)。

## 4. 下一次真实定位的执行顺序

1. 固定 image digest、挂载文件 hash、模型转换目录、模板 hash、catalog revision；确认独立服务实际入口和配置。现有 response lifecycle 补丁的适用版本见 [交付说明](../deploy/voicechat/README.md)，不能直接套在公开示例上。
2. 按 [部署命令](deployment.md#voicechat-诊断探针与部署产物核验) 执行产物检查和修复后的探针。同一录音的原始/500 ms/1 s 版本各建新连接；不改模型参数来混合实验条件。
3. ASR 逐层比较 24 kHz PCM → soxr 16 kHz → Triton frame → encoder → RNNT raw token → display → WS final。sample/frame 区间、reset 世代与 sequence ID 一起记录。找出首个缺失 `Hell` 的层，再修改该层。
4. function 逐层记录实际加载 key、special IDs、prefill 摘要/hash、combined embedding 的 shape/finite/norm、`function_key_present`、PAD/SOTC/EOTC 计数和有界 score/rank，再比对 parser 和原始 call ID。shape 正确不证明融合权重正确，PAD 不等于 key 存在。
5. 若 runtime 有完整 SOTC…EOTC 但无 WS tool event，修 parser/JSON/队列；若 WS 有 tool 而 Gateway 无 pending，修 Adapter/绑定。若等价 RNNT 解码都丢词，或接线正确仍不选工具，记录模型能力失败，不补写转写或放行无证据口述。
6. 最后通过门户跑真实 CueKB 检索、结果回填、授权口述、人工听音及失败恢复。方案设计、探针通过和静态产物检查均不构成真实放行；实施依赖以 [当前方案](q1003-target-architecture.md) 为准。

## 5. 已执行证据

- 本项目新增协议/静态产物回归覆盖持续送音、延迟/插话、动态目录、去重、异常传播、超时分类、捕获预算、head/output/token metadata 不一致。完整结果以 [验收报告](acceptance-report.md) 为准。
- 上游真实 `_process_inputs_to_outputs` 方法从固定源码 AST 提取，用受控 embedding/runtime 返回值执行三分支。证据：忽略目录 `artifacts/voice-recovery/upstream-output-contract.json`，对应源码 SHA-256 `14d791a65e6885a444fe48e437750a8540b6ba2d01fcd77339cdb29da0b0dc42`。
- 该受控检查没有做 tensor 计算、模型推理或语音识别；它证明输出 key 映射行为，不能证明现场缺 key。未更改 NVIDIA 源码，未构建/部署 GPU 镜像。
