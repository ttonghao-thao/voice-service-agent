# CueKB 问题级证据接口契约

更新：2026-10-30。当前语音接入基线为 CueKB `main@30970e9`，公开契约修订为 `cuekb.evidence/2.1`。本项目的原生语音路径使用 `POST /v2/evidence/query`，注入 VoiceChat 前使用 `POST /v2/evidence/validate` 校验证据包；`/v2/search` 仅保留给非语音兼容路径和检索诊断。Provider 方案与实现范围见 [CueKB 问题级证据方案](../CueKB.md)，操作与现场回传见[验证手册](../validation.md)。

## 1. 请求与可信边界

VoiceChat 仍只看到 `search_knowledge_base(query)`，不能选择 KB、凭据、实体 UUID 或扩大权限。API 使用最终稳定 ASR 作为 `question`；服务器解析后的确认上下文只进入 `search_query`、可信过滤字段或已确认的实体 binding。模型给出的泛化 query 仅用于工具选择和审计，不替换原问题。

```json
{
  "contract_revision": "cuekb.evidence/2.1",
  "question": "Can it be shipped? How long is its shelf life, and how should it be stored after opening?",
  "search_query": "Can it be shipped? How long is its shelf life, and how should it be stored after opening?",
  "kb_ids": ["00000000-0000-4000-8000-000000000001"],
  "filters": {"product_model": "Golden Yolk"},
  "context_bindings": [],
  "requested_facets": [
    {"id": "f1", "text": "Can it be shipped?", "question_span": [0, 18]},
    {"id": "f2", "text": "How long is its shelf life", "question_span": [19, 45]},
    {"id": "f3", "text": "how should it be stored after opening?", "question_span": [51, 89]}
  ],
  "delivery_profile": "voice",
  "max_groups": 5,
  "token_budget": 120,
  "request_id": "voice-input-id",
  "input_revision": 1
}
```

`kb_ids`、Bearer Key 和可信 filters 只由服务端设置。facet 必须是 `question` 的原始连续跨度；无法可靠拆分时整句作为一个 facet。`CUEKB_VOICE_TOKEN_BUDGET` 默认 120，由 CueKB 已部署的 Voice tokenizer 精确计数。Voice tokenizer 未配置或签名证据包不可用属于配置故障，不能伪装成无证据。

## 2. 响应与消费动作

响应必须严格符合 `cuekb.evidence/2.1`，并包含：

- `decision`：`answer | partial_answer | clarify | insufficient | conflict`；
- `facets`：逐项 `direct | entailed | absent | ambiguous | conflicting | unassessed` 及证据映射；
- `evidence`、`sources`、必要条件、缺项和冲突；
- `model_view`：已经过 provider 选择和 token 预算的唯一模型视图；
- `delivery`：voice profile、tokenizer ID、token 数和预算；
- `content_revisions`、`authorization_revisions` 与签名 `package`；
- `continuation`：当前接入要求 `allowed=false`，补召回在同一次 CueKB 请求内有界完成。

应用不重新排序、不重新拆问题，也不从完整审计响应自行拼另一份模型证据。它只把 `model_view.decision/evidence/missing/search=finished` 加终态指令后注入 VoiceChat；完整来源保存在 Citation、Turn 和门户来源面板中。条件与 required context 同时进入引用上下文和语音答案核验，不能为了缩短载荷删除。

## 3. 请求—响应绑定与校验

客户端在注入前逐项验证：

1. `request_id`、`input_revision` 与当前输入/Turn 一致；
2. 返回 facet 的 ID 和原文与请求一致；
3. delivery 为 `voice`，tokenizer 计数不超过请求预算；
4. 内容与授权 revision 的 KB 集合恰好等于当前授权范围；
5. `model_view` 与 decision、evidence、missing 完全一致；
6. evidence/facet/source 引用闭合，无未知字段或枚举；
7. `continuation.allowed=false`；
8. `/v2/evidence/validate` 返回相同 request ID、内容 revision、授权 revision 和 `status=valid`。

任一项不一致都以 `CUEKB_CONTRACT_ERROR`、`CUEKB_EVIDENCE_STALE` 或对应认证/配置错误结束当前工具结果，不把旧包、越权包或不完整包送给模型。

## 4. 单输入控制流程

一个稳定输入只创建一个知识 Turn，并执行一次 `/v2/evidence/query` 和一次 `/v2/evidence/validate`。CueKB 内部可以在同一请求内做一次有界扩大召回；模型不能为缺项自行拆分、泛化或再次查询。

第一个严格结果提交后，该输入立即进入终态。若模型仍生成新的 tool call，API 会：

- 解析并记录 call，返回 `not_executed/search_finished`；
- 不访问 CueKB，不创建或修订 Turn；
- 抑制该冗余调用的 checking ACK；
- 保持 WebSocket，等待模型基于已有 evidence 回答或等待下一次用户输入。

重复调用计数保留在诊断中，但安全结清本身不是 `VOICE_TOOL_LOOP`。显式未知输入、未知工具调用、伪造 tool call ID、权限或目录变化仍严格失败。

## 5. 决策、来源和产品隔离

`answer` 表示所有识别 facet 均有闭合支持；`partial_answer` 必须明确缺项；`clarify` 只提出 provider 给出的有界澄清；`insufficient` 说明当前授权来源无法确认；`conflict` 展示冲突而不任选其一。命中数量、相似度或 `degraded` 本身不能升级为充分答案。

Golden Yolk 与 Black Pig 必须通过 CueKB 的实体/局部来源归属隔离。密封 45 天、最佳 15 天、开封冷藏三天和鲜肉月饼两小时属于不同产品或条件，只有 provider 返回的支持映射和必要条件可以授权口述。模型视图不携带 trace、UUID、分数和未选候选；门户引用仍保留真实文档、版本、chunk、anchor 和 revision。

## 6. HTTP 与部署错误

| 返回 | 本项目处理 |
| --- | --- |
| 200 + 合法 evidence package | 严格解析，随后 validate；只有 valid 才注入 |
| 401/403 | 认证或授权故障，不扩大范围重试 |
| 409 | tokenizer、索引、内容修订或 evidence package 配置错误，映射明确错误码 |
| 422 | 请求或契约错误 |
| 429/503/504 | 容量、依赖或时限故障；不转成正常 `insufficient` |
| 未知字段/枚举、绑定不一致 | `CUEKB_CONTRACT_ERROR`，不回退旧接口 |

HTTP 3xx 不跟随，语音路径不做叠加 HTTP 重试。上游响应有界为 256 KiB，最终模型载荷还必须满足本项目的原生结果字节上限。真实 Recall、产品归属、P95、GPU 注入速度、听音和多轮连接存活仍需部署后验收；代码契约和 CPU fixture 不能替代现场结果。
