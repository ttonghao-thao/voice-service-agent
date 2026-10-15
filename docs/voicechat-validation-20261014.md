# 2026-10-14 新部署验证：握手恢复后的三个问题

本记录对应用户提供的 15:54:34–15:54:50 通话日志。它确认了故障边界，不代表已取得现场录音、模型配置或 GPU 采集，也不把返回的产品介绍认定为 CueKB 答案。

2026-10-15 整理注：本页保留该次事件事实与可复现采集步骤；新 PAD 版本尚无现场结果。当前诊断分支见 [故障定位](voicechat-fault-recovery-design.md)，架构和实施顺序见 [方案](q1003-target-architecture.md)，不要把本页建议操作当作已执行结果。

## 1. 本次已确认什么

| 边界 | 现场证据 | 判定 |
| --- | --- | --- |
| 工具注册 | `voice_registration_confirmed tools_count=2 tools_echo_type=str` | 字符串 tools 回显修复已生效；不等于模型调用了工具 |
| 音频输入 | Received 202 块；16 kHz submitted/completed 均为 256000 样本，queue=0 | 持续送音、推理在运行；没有传输丢块证据 |
| ASR 发布 | step34 首次累计 17、emitted_before=0、delta=17；final/completed 均为 44 字符 | gate 发布全部累计长度，无发布层裁去前四字符的证据；结合用户转写报告，应继续定位累计形成及其上游，日志长度不提供每帧原始 token |
| 原生函数输出 | sequence.end：PAD=200、SOTC=0、EOTC=0、other=0，state=idle | 本轮没有原生工具调用；不能归为 CueKB 或外置 LLM 执行慢 |
| 回答开始 | 15:54:40.054 首次 response/audio/text | 普通回答已经开始 |
| 回答结束 | BOS 后未见 EOS、transcript.done、audio.done、response.done；16.24 秒连接时长内仍同一 response | 回答回合未正常结束 |
| Gateway 关闭 | 15:54:50.961 `VOICE_ANSWER_TIMEOUT`，约首次输出后 10.9 秒 | 默认 10 秒回答结束期限加看门狗轮询，与现场一致 |
| 输出队列 | queued_events=sent_events=167、output_queue=0、最终 Dropped=0 | 没有 WS 输出队列丢失结束事件的证据 |

关联标识：VoiceChat client `58bb88bc` / sequence `3887488407`；input item `81b26d76-2832-4127-a3db-f1253abf6b27`；response `80c60be3-6d6b-409b-ab36-81381536f057`；API voice session `bf930711-4def-4133-8afa-323dd88c4e16`；trace `d54f2f06-3bf9-4434-a099-43814379e463`。

`voice_ws_diagnostic.dropped_events` 增长至 42 是有界诊断历史的淘汰计数，与 `chunks_dropped` 分开，不能当作 42 个音频事件丢失。参见快照 [audio_server.py](voicechat/s2s/audio_server.py) 的 `_voice_diagnostic` / `_queue_output`。

## 2. 句首缺失：定位到 RNNT 输出或其上游，尚未定位到具体算子

快照 [model.py](voicechat/opt/tritonserver/backends/nemotron-voicechat/model.py) 的 `_rnnt_display_decode_frame` 每帧先累计 token，`_append_rnnt_display_output` 再按 gate 和发布游标输出。现场 step24 累计 1 字符，step30 累计 9，step34 累计 17，并一次发布 17；中间没有 reset。WS 的 raw20 → clean17 删除的是 `<s>` 三字符，与缺失四字符 `Hell` 无关。

`frame_idx=68` 是 70 帧滑动窗口中的局部索引，global step 仍在递增；不能据此认定重复旧音频。第一块 1920 个 24 kHz 样本只即时输出 979 个 16 kHz 样本，也不能单独证明丢音：soxr 是有状态滤波。最终输入 16.16 秒，Triton 处理 16 秒、尚缓冲 148 ms，剩余约 12 ms 可由滤波内部延迟解释。

VoiceChat 的辅助 RNNT 转写不作为最终文本输入给 LLM；共享 encoder 的音频表示直接参与 LLM 融合。因此不能把 `Hell` 缺失直接解释为函数漏调用的原因。两者可以有共同上游原因，也可以独立失败，需逐层采集。

修复依据：先比较 WS/重采样/Triton 的 PCM，再比较同帧手写 RNNT 与参考 greedy。PCM 首次缺失则修该传输阶段；PCM 完整、手写缺而参考完整则修解码状态/词表；等价解码都缺则继续检查 encoder 选帧、权重及模型能力。不要补写 Hello、降低显示 gate 或继续加开头静音来掩盖结果。

## 3. 无工具产品回答：策略与模型能力分开处理

现有 [gateway.py](../apps/api/app/voice/gateway.py) 在 `general_qa` 下，允许唯一最新用户输入绑定的普通回答。它没有产品语义分类器；要求公司/产品事实使用工具的约束由 prompt 提供。此次原生 function 全 PAD，与没有知识查询的普通回答相符。

模型说出 classic / Pro 并不证明检索成功；没有真实知识结果，亦不能仅凭日志断言这两个产品一定不存在。该回答应记为未验证的模型知识，不能通过企业知识验收。

CueKB + 外置 LLM 的企业部署可立即采用：

```dotenv
QA_EXECUTION_MODE=dual_tools
QA_ANSWER_POLICY=knowledge_required
```

重建 API 容器后，新建 conversation 再进行文件通话；旧 conversation 持久化了策略，不随 env 自动更新。读取 `/api/v1/capabilities` 核对 `qa_execution_mode`、`answer_policy`、`native_tools` 和 `native_tool_revision`。严格策略将未获工具授权的非空回答拒绝为 `VOICE_TOOL_REQUIRED`；这是保护生效，不是工具调用成功。被选中的 direct 知识工具会转外置知识执行。

`QA_TOOLSET_VERSION` 是应用版本标签，不能激活 function head、修复全 PAD 或强制调用。必须核对后台目录中的真实名称、description、schema、ack_messages，以及最终 Jinja prompt、tokenizer special IDs、转换产物的 function head/custom outputs 与实际融合权重。固定官方 checkpoint 声明 audio/text/function=1/1/2；backend 对缺失权重字段默认 1，不能只看论文后盲改现场配置。静态检查命令见 [部署](deployment.md#voicechat-诊断探针与部署产物核验)。

若运行 backend 与归档一致，`_vllm_llm_step` 直接索引 `custom_outputs["function_tokens"]`，缺 key 会抛错；此次 200 个真实 PAD 不能解释成该路径静默保留缺省 PAD。仍需核对加载的 head/权重、有效音频 embedding 和 PAD/SOTC score/rank，区分接线错误与模型漏选。契约正确后再做单变量工具描述对照，不添加自动 ASR 转工具调用。

## 4. 超时：已有静音渲染保护没有同步结束回合

快照 `audio_server.py::_dispatch_model_output` 在响应活跃时持续发送非空 codec 数组；普通回合由 agent text EOS `</s>` 结束。`_finish_spoken_response` 会先 flush 重采样及 PCM 尾部，再发字幕/音频/response 结束。TTS 是持续运行的流，不能用其推理请求 done 替代语音回合结束。

backend `vllm_tts_step` 的 PAD 尾部策略在超出 `S2S_TTS_PAD_TAIL_RATIO` 后替换静音 codec，却不插入 EOS。默认 PAD 渲染尾部 ratio=3；RNNT 的另一项回合上限 ratio=16，绝对时限 `RNNT_MAX_AGENT_RESPONSE_SEC` 默认 0（关闭）。两种预算约束不同，不能直接把 16 改为 3。前者可能已停止渲染，响应仍等模型 EOS；这是源码能证明的结束条件缺口，但日志尚未证明本次确实进入该替换分支。

文字停止也不等于语音结束。上游允许先输出文字、随后用 PAD 帧继续合成语音；42.452 后持续的音频是否静音必须用 PCM 或 codec 替换证据判断。本次真正确定的是缺结束边界，不能把 `QA_PROVIDER_ANSWER_TIMEOUT_MS` 调大当成修复。

独立补丁及应用说明见 [交付 README](../deploy/voicechat/README.md)。它默认关闭；开启后仅在当前 text 与最近一批 function 均 PAD、工具 idle、agent 回合活跃、已有内容 token、没有待处理用户语音，且**已经满足现有强制静音尾部条件**时生成 EOS，记录 `agent.eos source=render_budget`，让已有渲染预算与协议收尾一致。批次保护避免 SOTC 尚未解析时结束；语音保护保留短发声后仍待处理的 nonblank 累计。当前帧仍使用原策略同一 silence codec，不额外削去该帧语音。

这只修结束条件缺口，不保证模型预测 EOS、未来不会恢复内容或原渲染预算足够。尾部预算近似为内容 token 数 × ratio × 80 ms，尚需加内容生成、流水线和发送时间，可能仍超过 API 10 秒期限；必须实测 `agent.eos` 与完成事件，不能保证本次答案开启开关后必然不超时。真实听音必须区分原生 EOS 与预算 EOS；不能把强制截断的回答记为正常完整答案。后端补丁不会修复 RNNT 首缀或原生 function 全 PAD。

## 5. 同一次 sequence 的隔离采集

先用未开启收尾补丁的基线重现，再单独开启补丁对照；语音、目录、prompt、模型、采样及其它阈值保持一致。采集有真实 PCM、token 和文本，只用于授权测试录音，保存于忽略目录，不提交 Git。

在 VoiceChat WS 与 Triton backend **两个进程启动前**传入相同配置；若分容器，两侧路径挂载到同一个可写测试卷。预先创建绝对 root 目录，sequence 子目录必须不存在，只进行一次连接：

```dotenv
VOICECHAT_DIAGNOSTICS=1
VOICECHAT_CAPTURE_DIR=/capture/voicechat-validation
VOICECHAT_CAPTURE_SEQUENCE_ID=3887488408
VOICECHAT_CAPTURE_MAX_SECONDS=30
VOICECHAT_CAPTURE_MAX_STEPS=256
VOICECHAT_CAPTURE_MAX_BYTES=33554432
```

这是下一次测试的固定 ID，不是修改历史 sequence。WS 会采用该 ID，第二个连接会因独占目录而失败；不要同时打开原生 HTML 与门户。保持实时送音及后续静音驱动推理，单次通话限制在 30 秒内，完成后关闭 capture 配置并重建测试进程。默认 96 步只覆盖约 7.68 秒模型步长，对本轮 200 步的结束故障不足。

检查同目录的产物：

| 产物 | 检查 |
| --- | --- |
| `ws-summary.json` | sequence、采样率、样本数、hash、提交/完成计数 |
| `ws-decoded-24k.f32le` | 浏览器实际发给 VoiceChat 的 Hello 是否完整；mono float32 little-endian / 24 kHz |
| `soxr-output-16k.f32le` | 相邻阶段的首部语音是否仍完整 / 16 kHz |
| `triton-submitted-16k.f32le`、`triton-received-16k.f32le` | 相同提交区间的 PCM 内容/hash 是否一致；不要跨采样率直接比 hash |
| `model-events.jsonl` 的 `rnnt.compare` / `asr-selected` | 首个语音帧 manual/reference token、文本、初态与选帧；`rnnt.reference_error` 表示参考对照未成立 |
| `encoded-selected`、`combined-embeds` | 有声区间 shape/finite/norm 与帧来源；tensor shape 正确不证明融合权重正确 |
| `llm.output` | 原始 text token / function token、custom output keys、PAD/SOTC/EOTC score/rank（若可用）；按实际 tokenizer 判定 EOS |

参考 greedy 与手写解码要在相同初态、同帧、同权重下比较。现有手写路径有标点 bias，标准 greedy 没有；另外进行一轮 `RNNT_PUNCT_BIAS_ENABLED=0` 的等价对照，作为单变量测试记录，不能把不同算法的差异当成解码 bug。优先比较首个 agent BOS 前的 Hello 区间，注意 predictor reset；不要跨不同状态直接比较 token。

要观察原生 EOS，现有 capture 已记录 text token；若仍缺 EOS，下一步采集相同帧的 EOS logits/rank 与采样选择。音频输出可由 [probe](../scripts/probe_voicechat.py) 的 `--output-wav` 保存供实际听测；其合成工具回填不能用于证明真实 CueKB 成功。

## 6. 验收应分别记录

1. 首缀：最终 ASR 保留原录音 Hello；逐层 PCM 和同帧 RNNT 对照解释此前缺失。
2. 工具：原生 SOTC/EOTC → Gateway `voice_tool_call_received` → 实际 CueKB/外置执行 → 相同 call_id 回填；答案有可核对知识证据。
3. 收尾：自然 EOS 或明确记录的预算 EOS → PCM 尾部 → transcript.done/audio.done/response.done → 无超时；预算结束不得掩盖内容截断。
4. 门户：错误有明确代码、保留最终用户字幕、允许重试；严格策略拒绝模型内存产品回答视为受控失败。

本轮已确认注册与送音恢复、门户显示可恢复超时；ASR、知识执行与真实回答完整收尾均未达到放行条件。
