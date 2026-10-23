# 知识工具与证据执行

本文只描述一期原生 CueKB 路径。当前配置为 `dual_tools/general_qa`、`AGENT_PROVIDER=disabled`；已保留但不启用的兼容路径不作为本期设计或放行要求。

## 1. 模式与工具边界

VoiceChat 自主调用 `search_knowledge_base(query)` 或直接回答。一般问候和通用知识可以直接回答，页面标记 Model general answer，不创建知识 Turn 或引用。工具描述要求企业产品事实查询 CueKB，但提示不能保证模型零漏调用；未调用要如实记录，不根据转写偷偷补查。

公开名称与可信 implementation 均为 `search_knowledge_base`。Registry 后台能力 `search_knowledge` 是内部权限/适配器键，实际请求 CueKB `/v2/search`。身份、KB、凭据和 endpoint 来自服务端，模型只能提供 query。该两层含义不能与工具拼写混淆。

## 2. 注册、输入与扩展

`ToolDispatcher` 按注册名称和 CueKBArguments 校验，最终 ASR 用于输入归属和审计，不替换模型 query。不做第二次模型分类。后台目录控制启用及描述，代码静态注册可信实现，执行时复核权限、目录/后端版本。详细字段见 [工具契约](interfaces/voice-tools.md)。

一个 input 可有多个不同 call_id；call_id 标识调用，不代表新的用户问题。同 ID 相同事件幂等，同 ID 冲突失败；不同参数不能按工具名合并。模型一次解码的多成员批次由显式 metadata 标识，不用时间猜测。

## 3. 查询预算和结果

每个 input 默认最多两次实际 CueKB HTTP 查询，原生路径关闭自动重试。相同解析参数复用已授权来源并结束检索；第二次结果即标记 search_state=finished。终态前已收到的同批次超额成员只结清，终态后收到的新批次明确失败。语音 ACK、回答或新 call_id 不重置预算，新用户 input 才重置。

API 按批次返回一次累计证据 JSON，包括 status、sources、partial、search_state、instructions；多成员另含 call_results 的执行/复用/未执行状态。partial 在供应商边界使用字符串，避免数词转换把布尔值误变为 zero/one。VoiceChat 后端负责现有 TOOL_RESPONSE 格式化，API 不重复封装。

## 4. 证据与提交

只保留预算内完整英文证据块和必要条件，不截半句、不推测缺失事实。context_truncated 不再无条件拒绝：只有完整命中块可由 context_parts 精确回显证明时才可准入，截断 context 字符串仍不得进入模型结果。具体映射和状态见 [CueKB 契约](interfaces/cuekb.md)。

同输入来源合并、去重并保留出处/版本/trace；空补充查询不覆盖先前事实，权限失败或冲突不伪装成成功缓存。累计证据一致用于模型回填、答案检查和引用落库，交付前复核权限、版本、epoch/revision 与当前任务。

VoiceChat 根据来源回答；有限检索无法确认的字段应明确说明。Coordinator 在非 ACK 回答文本/音频结束后进行有限证据检查并唯一提交；检查依据为实际交付的文字/条件，原始引用另行保存。它是播后检查，不是语义真实性证明，也不能撤销已播内容。

## 5. 修改与验收

执行入口：`agent_runtime/native_cuekb.py`、`native_input.py`、`voice/relay.py`；批次协议见 [工具契约 §4](interfaces/voice-tools.md#4-原生函数批次传输)。本地覆盖预算、重复、空检索、累计来源、授权撤销和新输入重置，真实结果按 [统一验证手册](validation.md) 检查。

仓库中 legacy/lookup/reason/SDK 等兼容实现仍在代码和测试中；本期不需要外置文本模型、不启用直查升级或多业务分类。删除旧设计文档不表示已经删除这些代码。
