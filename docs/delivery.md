# 产品范围与交付边界

本文描述已实现的产品边界及现场验收要求。

产品是全双工知识客服。浏览器麦克风或 PCM WAV 持续输入。部署可选英文 NVIDIA VoiceChat，或配置为中文、英文的 Sophon Live 验证链路。语音服务负责语音理解、工具选择及口述，CueKB 提供授权证据，本项目负责身份、会话、执行、业务提交和来源展示。

当前原生路径为 `dual_tools/general_qa`、`AGENT_PROVIDER=disabled`。模型自主调用 `search_knowledge_base(query)`；API 使用最终 ASR 构造问题级请求，每 input 最多一次 CueKB query，成功包再 validate，不以额外模型、转写自动补查或强制 tool_choice 代替原生选工具。

已编码：持续收发、欢迎语及播放打断、逐输入调用归属、终态后调用结清、CueKB 2.1 严格消费、完整组预算、实际 VoiceChat tokenizer 预检、注入期间显示 ASR、答案/来源与权限检查。具体链路见[架构](architecture.md)。这些实现不等于当前生产已经验收。

交付目标：企业事实有来源；部分支持明确缺项；产品、数值及条件不串用；打断停止旧播放而不误取消查询；同连接下一问正常。音频发送完成、设备排空、实际听到、问题覆盖和事实正确分别判断。

当前阻塞和优化项在[任务板](TASK_BOARD.md)维护。真实语音、各工具服务与麦克风交互的放行流程见[验证手册](validation.md)。

后台持续播报暂缓；现有连接寿命和轮换不是无限连续通话承诺。legacy、外置 SDK/文本模型、天气等兼容实现仍存在，但不属于当前知识语音路径；删除旧说明不代表删除兼容代码。

## LiveKit 与 Sophon

升级沿用现有门户。浏览器用 WebRTC 加入 LiveKit Room，后端签发限定 Room、身份和时效的 Token，LiveKit AgentSession 通过 `/v1/live/sessions` 与独立部署的 Sophon 进行全双工对话。Sophon 自主决定何时委派。普通闲聊由 Sophon 直接回答。

Sophon 模型直接理解音频并生成委派 `task`。ASR 仅供字幕展示。委派处理只对存在可靠音频归属的任务运行一次 Router，按 `task` 校验 WEATHER、CUEKB、WEB_SEARCH 的参数或请求澄清。工具供应商确认为 QWeather（和风天气）、私有 CueKB 和 Tavily。天气支持实况及未来十日内单日预报，地点不明确时澄清，不猜测用户位置；企业检索复用现有 CueKB 证据 query/validate 与服务端授权。Router 采用原生 `task` 格式校验，不增加轻量 LLM 分类服务。发送前重新核对输入归属，按原委派 ID 回传 Commentary。单工具、短时有界的委派状态保存在会话内存，已存在的会话身份和知识权限继续由后端维护。

依次验证官方 GPT-Live 兼容性、迁移门户语音、接入三个工具、回归多轮与打断。放行须验证真实 Sophon、各工具的实际供应商与门户听音。委派按 `task`、音频归属与工具结果验收，ASR 按字幕准确性和呈现单独验收。阶段状态见[任务板](TASK_BOARD.md)，探针、结果及放行条件见[验收](validation.md#livekit-sophon-phase-0)。
