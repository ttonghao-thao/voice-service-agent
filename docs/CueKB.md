# CueKB 问题级证据优化方案

更新：2026-10-30。本文是提交给 CueKB 项目的实施方案。CueKB 已在 `main@30970e9` 实现 `cuekb.evidence/2.1`、`/v2/evidence/query`、`/v2/evidence/validate`、问题级 facet 决策、有界内部补召回、产品实体过滤、voice tokenizer 预算和签名证据包校验。本项目按该基线接入；当前消费边界见[接口契约](interfaces/cuekb.md)，实施状态见[任务板](TASK_BOARD.md)。

实现核对基于 CueKB `main@30970e9`：新接口使用独立严格 schema，返回 provider 判定的 facet 支持、全局 decision、最小 `model_view`、`continuation`、revision 和签名 package；语音 profile 要求实际部署 tokenizer。现有 `/v2/search` 继续用于搜索与诊断，不能用其相关性命中或 `unassessed` 替代问题级答案充分性。

## 1. 目标与边界

CueKB 对一个完整用户问题执行一次有界问题级证据请求，返回：

- 每个所问事项是否有适用且闭合的证据；
- 可用于回答的最小证据集合；
- 缺失、歧义、冲突和必要条件；
- 本次是否为检索终态；
- 是否存在一次明确、受约束的续查计划。

CueKB 不生成最终自然语言回答，不运行 Agent 工具循环，不控制 VoiceChat 会话，也不根据模型重复调用自动扩大检索。现有 `/v2/search` 继续用于搜索、调试和检索评测；问题级回答资格由新接口独立给出。

## 2. 新接口与契约版本

新增：

```http
POST /v2/evidence/query
```

公开契约修订：

```text
cuekb.evidence/2.1
```

新接口使用独立严格 schema。服务端响应必须携带 `contract_revision`；未知字段、未知枚举和不兼容修订明确失败。CueKB 与 voice-service-agent 共同维护一份可生成、可校验的 OpenAPI/JSON Schema 和真实序列化 fixture，不能通过客户端 `extra=ignore` 掩盖不兼容。

## 3. 请求契约

```json
{
  "question": "Can it be shipped? How long is its shelf life, and how should it be stored after opening?",
  "search_query": "Monkey Golden Yolk mooncake shipping sealed shelf life best consumption storage after opening",
  "kb_ids": ["11111111-1111-4111-8111-111111111111"],
  "context_bindings": [
    {
      "entity_id": "22222222-2222-4222-8222-222222222222",
      "source": "confirmed_conversation_context"
    }
  ],
  "requested_facets": [
    {"id": "shipping", "text": "Can it be shipped?", "question_span": [0, 18]},
    {"id": "sealed_shelf_life", "text": "How long is its shelf life?", "question_span": [19, 49]},
    {"id": "opened_storage", "text": "How should it be stored after opening?", "question_span": [50, 95]}
  ],
  "delivery_profile": "voice",
  "max_groups": 5,
  "token_budget": 120,
  "request_id": "voice-input-correlation-id",
  "input_revision": 1
}
```

字段要求：

| 字段 | 要求 |
| --- | --- |
| `question` | 完整、稳定的当前问题；保留产品、属性、条件、否定和用户限定，最长 2000 字符 |
| `search_query` | 可选检索表达；省略时使用 `question`，不得删除问题中的业务约束 |
| `kb_ids` | 由受信服务端授权提供，调用方不能扩大范围 |
| `context_bindings` | 最多 8 项，只接受已确认且当前主体有权限的实体/版本/条件绑定 |
| `requested_facets` | 最多 8 项；每项保留原问题跨度，无法可靠拆分时以整句作为一个 limited facet |
| `delivery_profile` | `standard` 或 `voice`；控制证据视图和注册 tokenizer，不改变事实 |
| `max_groups` | 1—5，是返回上限，不要求凑满 |
| `token_budget` | 对完整交付视图按注册 tokenizer 精确计数，不能用字符数冒充 |
| `request_id/input_revision` | 关联一次输入及修订，不用于授权或正文真实性判断 |

模型推测的实体只能作为候选，不能自动升级为可信 `context_bindings`。绑定必须核验实体存在、当前发布版本、KB 权限和应用信任级别。

## 4. 一次请求的固定处理流程

1. 核验调用主体、KB、内容修订、索引 generation、实体绑定和请求修订。
2. 保留完整问题，解析实体候选和 requested facets；不把查询缩减为产品名。
3. 使用 BM25、向量、现有 adaptive 选择并行召回，按 facet 保留候选配额。
4. 对明确未支持的 facet，在总时限和候选预算内执行最多一次服务内部扩大召回；复用原问题与已确认实体，不做递归 Agent 查询。
5. 通过 PG 权威快照剔除旧版、撤销、无权限、未发布和产品范围不适用的候选。
6. 以“主事实＋产品/版本归属＋必要条件/表头/例外”为证据组，必要闭包不完整时不得作为完整事实。
7. 逐 facet 判断是否直接支持、条件是否匹配、是否存在歧义或冲突。
8. 优先覆盖尚未支持的 facet，再按支持强度、适用性、来源策略和增量 token 成本选择最小证据集合。
9. 在最终 PG 快照重新核验正文、关系、发布版、ACL 和引用；失效后最多一次有界重选。
10. 输出决策、支持映射、缺失项、紧凑模型视图和完整审计视图。

内部补召回属于同一个 evidence 请求，不能要求语音模型再发一次工具调用。

## 5. Facet 支持状态与证据规则

每个 facet 的 `support` 只能是：

```text
direct | entailed | absent | ambiguous | conflicting | unassessed
```

- `direct`：原文直接陈述，主体、版本、包装状态和必要条件匹配。
- `entailed`：只能由经过评测和校准的支持评估器给出，并保留方法修订与引用。
- `absent`：当前授权来源和本次检索计划没有可采纳支持，不表示全世界不存在该事实。
- `ambiguous`：主体、版本、地区或条件存在多个有依据选项。
- `conflicting`：同主体、同属性、可比较单位和重叠条件下存在未解决冲突。
- `unassessed`：当前评估能力不能可靠判断；不得进入全局 `answer`。

字面出现 `storage`、`shelf life` 等词不等于支持。`sealed 45 days`、`best consumed within 15 days`、`after opening 3 days` 是不同属性/条件，不能互相覆盖；Golden Yolk 与 Black Pig 的期限不能跨产品支持。

## 6. 全局决策与续查

响应 `decision` 只能是：

| decision | 语义与消费动作 |
| --- | --- |
| `answer` | 所有可靠识别的 facet 有闭合证据；终态回答 |
| `partial_answer` | 支持部分 facet，明确列出缺项；通常仍为终态 |
| `clarify` | 主体、版本或条件需要用户确认；提出一个有界澄清问题 |
| `insufficient` | 无可采纳支持或必要依赖不完整；终态说明无法确认 |
| `conflict` | 当前问题存在未解决证据冲突；终态展示冲突，不任选其一 |

只有同时满足下列条件时，`continuation.allowed` 才可为 `true`：

- 缺失 facet 明确且与已支持项分离；
- 存在不同于第一次召回的具体、有限计划；
- 剩余总时限和证据预算足够；
- continuation token 绑定主体、input revision、实体、内容快照、缺失 facet 和已使用次数；
- 最多使用一次，不能扩大 KB 或产品权限。

模型不能构造 continuation。没有明确新计划时返回 `allowed=false`，即使 decision 是 `partial_answer`。

## 7. 响应示例

```json
{
  "contract_revision": "cuekb.evidence/2.1",
  "trace_id": "33333333-3333-4333-8333-333333333333",
  "decision": "partial_answer",
  "assessment": {
    "method_revision": "structured-support-1",
    "calibration_id": null,
    "scope": "authorized_sources_for_this_question",
    "limitations": []
  },
  "facets": [
    {"id": "shipping", "support": "absent", "evidence_ids": []},
    {"id": "sealed_shelf_life", "support": "direct", "evidence_ids": ["E1"]},
    {"id": "opened_storage", "support": "direct", "evidence_ids": ["E1"]}
  ],
  "evidence": [
    {
      "id": "E1",
      "supports": ["sealed_shelf_life", "opened_storage"],
      "subject": {
        "entity_id": "22222222-2222-4222-8222-222222222222",
        "name": "Monkey Golden Yolk Mooncake",
        "resolution": "known"
      },
      "statement": {
        "text": "Shelf life is 45 days sealed; best consumed within 15 days. After opening, refrigerate and consume within 3 days.",
        "source_refs": ["S1"]
      },
      "conditions": ["sealed packaging", "after opening"],
      "completeness": "complete",
      "match_basis": ["confirmed_subject", "explicit_attribute"]
    }
  ],
  "missing": ["shipping"],
  "conflicts": [],
  "clarification": null,
  "continuation": {
    "allowed": false,
    "reason": "no_distinct_retrieval_plan",
    "token": null
  },
  "sources": {
    "S1": {
      "document_id": "44444444-4444-4444-8444-444444444444",
      "version_id": "55555555-5555-4555-8555-555555555555",
      "node_id": "66666666-6666-4666-8666-666666666666"
    }
  },
  "delivery": {
    "profile": "voice",
    "tokenizer_id": "registered-voicechat-revision",
    "token_count": 92
  },
  "search": "finished"
}
```

任何被预算删除的证据组必须从对应 facet 的 `evidence_ids` 和支持决策中同步移除。响应不能保留“已支持”状态却省略其必要证据。

## 8. 产品隔离与内容治理

1. Golden Yolk 与 Black Pig 建立独立实体及审核别名。
2. 多产品文档按章节路径或 `source_unit_id` 绑定局部实体；不得用未经确认的文档级 Monkey Brand 归属覆盖全篇。
3. 人工审核常见 ASR 变体，例如 `Golden Yolk`、`Golden-Yolk`、`golden yoke`；别名只用于实体解析和召回，不回写原文事实。
4. 已确认实体通过 `product_entity_id` 或 context binding 参与 OpenSearch、PG 和最终证据复验。
5. 不匹配实体的主事实必须排除；必要上下文可以附带，但必须保留自己的 applicability，不能改变所属产品。
6. scope binding、解析规则或源树变化后必须重处理、审核、发布和升级相应词法投影，不能只重建向量 generation。

## 9. Voice 交付视图和预算

完整响应供应用审计、回答验证和门户来源面板使用；发送给 VoiceChat 的模型视图只保留决策、支持项、必要条件和缺项：

```json
{
  "decision": "partial_answer",
  "evidence": [
    {
      "for": "sealed shelf life and storage after opening",
      "text": "Sealed shelf life is 45 days; best within 15 days. After opening, refrigerate and consume within 3 days."
    }
  ],
  "missing": ["shipping"],
  "search": "finished"
}
```

模型视图不包含 trace、UUID、排序分数、重复标题、营销描述、调试诊断或不支持 requested facets 的候选。内部来源身份仍保存在完整响应中。

现场已测 VoiceChat 结果消费约为每 token 32.5 ms，证据预算必须由目标注入时长反推：

```text
可用注入 token 数 = 可接受注入时长 / 实测保守每 token 时长
```

CueKB 文档中的 200—350 token 只能作为调优起点；按当前现场速度仍约为 6.5—11.4 秒，不能直接视为达标。必要条件不能为缩短载荷而删除；超过预算时返回准确的 `partial_answer/insufficient` 或选择其他交付路径。

## 10. 错误与版本边界

- 401/403：认证或权限失败，不返回实体候选，不扩大范围重试。
- 409：契约、内容快照、索引或模型修订不兼容，返回明确错误码。
- 422：问题、facet、binding 或预算字段无效。
- 429/503/504：容量或依赖故障，与 `insufficient` 分开。
- `degraded` 可以与有用证据并存，但必须进入 assessment limitations。
- 技术错误不能伪装为“知识库没有资料”。

## 11. 诊断与验收

诊断至少记录：

```text
request_id/input_revision
question_origin
resolved_entity_ids
requested_facets
retrieval/selection/assessment durations
candidate and selected group counts
facet support states
decision
continuation decision
evidence/model-view bytes and tokens
content and authorization revisions
```

固定金标集至少覆盖：

1. Golden 完整组合问题；
2. shelf-life 与 storage 拆分表达；
3. `Golden Yolk / Golden-Yolk / golden yoke` ASR 变体；
4. 带 `it` 且有确认实体绑定的跟进问题；
5. Golden 不得返回 Black Pig 期限；
6. Black Pig 不得返回 Golden 期限；
7. shipping 无证据时为 `absent`，不能从 `sealed and giftable` 推断可运输；
8. 45 天密封期、15 天最佳食用期、开封冷藏三天分别保留条件；
9. 必要上下文缺失、预算不足、冲突、权限撤销和内容修订变化；
10. continuation token 的权限、修订、次数和重放防护。

通过条件：facet 状态和全局 decision 与金标一致；禁止跨产品证据为零；常规完整问题 `continuation.allowed=false`；返回组数不超过解决问题所需数量；voice 视图满足实际 tokenizer 预算；真实 PostgreSQL/OpenSearch、严格契约、降级和错误分支全部通过。代码合入、HTTP 200 或 top-k 命中不能替代生产语料和时延验收。

## 12. 两项目责任边界与交付顺序

CueKB 负责一次请求内的召回、内部补召回、产品范围过滤、facet 支持判断、终态决策和精简证据。voice-service-agent 负责传入完整 ASR、确认实体与 facets，执行终态，结清模型额外调用并维持语音会话。

交付顺序：

1. CueKB 实现并发布 `cuekb.evidence/2.1` schema、端点、fixture、迁移/索引要求及回退说明。
2. 两仓使用同一契约 fixture 完成 provider/consumer 严格测试。
3. CueKB 使用真实月饼语料完成产品隔离、问题覆盖和 voice token 验收。
4. voice-service-agent 再按任务板接入；契约不兼容时明确失败，不静默回退旧的模型驱动两次检索流程。

