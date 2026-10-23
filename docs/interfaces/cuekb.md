# CueKB V2 接口契约

更新：2026-10-23。接入基线为 CueKB `dac382e282f12ae1c1b89366d5c058725d9f13c6`。职责是严格 HTTP 契约、授权证据映射与错误边界；操作及回传只维护在统一验证手册。实现以本项目 `CueKBAdapter`、工具 schema 和 CueKB V2 公开契约为准，不能仅凭路径中的版本号判断兼容。

## 1. 请求与部署边界

本项目显式使用 `POST {server-configured-cuekb-base-url}/v2/search`，Bearer Key 与 KB UUID 仅由服务端授权映射提供。原生工具仍只有 `search_knowledge_base(query)`，不让模型选择 KB、凭据、实体 UUID 或扩大权限。

```json
{
  "query": "What are the documented storage conditions for the product after opening?",
  "kb_ids": ["00000000-0000-4000-8000-000000000001"],
  "mode": "auto",
  "top_k": 5,
  "filters": {},
  "include_context": true,
  "include_diagnostics": false,
  "max_evidence_bytes": 24000
}
```

UUID 仅说明类型。`top_k` 是最多返回数量，不要求凑满，也不代表问题已完整覆盖。`CUEKB_MAX_EVIDENCE_BYTES` 控制上游 hits JSON 的字节预算，默认 24000；它不是模型 token 上限。保留 `CUEKB_TOP_K` 默认 5 和查询长度 2000。`product_model/software_version` 过滤只能来自明确、可信的用户条件，不从自然语言猜测。模型 query 负责保留多轮指代和所问属性；CueKB 不读取语音历史，不支持隐含的 `original_question` 字段。

CueKB 必须先具备 V2 数据条件：其 0004 迁移、可用 Embedding 配置、V2 搜索投影及已发布文档。切块或归属变化需要对原件按 `evidence-v2` 重处理、审核映射、发布；generation 只重编码现有块，不能替代重处理。Reranker 可选，未配置不因这一项判为降级。此项目不管理 CueKB 的导入、权限、发布或索引写入。

V2 与旧 M3/D23 响应不混用。未知版本、未知字段或损坏响应明确失败，不静默切回 `/v1/search`，不删除未知字段后冒充兼容。旧历史 Citation 默认字段仍可读取，不等于旧在线接口回退。

## 2. 证据与评分映射

| CueKB V2 | 本项目处理 |
| --- | --- |
| schema_version | 必须是字符串 `2`；保存在引用和有界诊断中 |
| trace_id、content_revisions | 关联本次检索与授权内容修订，提交/交付继续复核权限与版本 |
| retrieval_status、evidence_status | 保留原值；`unassessed` 不因有命中或高分升级为充分 |
| ranking_method、ranking_version、rank、score、score_type | 保存实际排序方法、配置版本及分数；按 rank 消费，不将分数作为事实正确率，不要求模型口述 |
| document_id、version_id、content_sha256、chunk_id | 保留真实来源身份；内容 hash 不等于模型答案已验证 |
| applicability | 保存 resolved/unknown/ambiguous、可读主体及条件、出处；文档 metadata 标签不能代替局部事实归属 |
| source_text、title_path、anchor、source_spans | 保留原文及可信章节/位置；不根据 query 添加产品事实，不伪造缺失标题或位置 |
| context_parts.required/role | 保留必要归属、表头、条件、例外或续块的完整组；可选邻居独立取舍 |
| context_state/context_omissions | 必要依赖缺失的 unresolved 不能靠“主块完整”变成可用证据；可选上下文省略不等于必要组缺失 |
| relations | 保留可追溯关系；supports/refutes 是证据立场，不是真伪裁决 |
| diagnostics、timings_ms | 用于候选/返回/省略、载荷与阶段诊断；不把所有内部元数据逐 token 注入 VoiceChat |

接口保持严格嵌套校验，证据原文不因全局 strip 或预算被截短。公开合成响应仅验证协议，不当作产品知识。评分版本 hash 标识排序配置，不能替代冻结索引、文档版本与外部模型权重来保证逐次浮点值完全相同。

## 3. 完整证据组与语音负载

原生语音选择单位是“主事实＋已确认产品/版本归属＋必要条件上下文”。按 CueKB 最终顺序考虑完整组，省略完全重复的已发送来源；放不下时省略整组并记录预算原因，不能剪掉否定或必要条件后强行保留事实。保留有界的 top_k、API 字节与原生语音证据预算；本轮不引入 MMR、候选重新 Embedding、额外答案模型或属性分类器。

V2 中 `context_state=unresolved` 或已识别必要依赖缺失时拒绝该组。不能沿用 M3 的“context_parts 精确回显主块即可删除全部截断 context”规则。可选上下文被截断时，只使用已证明完整的主块与 required parts，不使用截断 context 字符串。unknown/ambiguous 明确随来源交给模型，不能补成问题中的产品；这不保证模型自动正确使用，仍需内容验收。

VoiceChat 实际结果只包含精简正文、有效标题、必要上下文和可读 applicability；内部实体 ID、完整 spans、评分、trace 及诊断留在 API/引用审计。不能把内部 ID 填进旧 `metadata.product_model` 并要求逐字口述。最终答案检查只以实际交付的事实和条件为依据，不能引用被应用省略的上下文来证明回答。

语音结果的 `partial` 表示主事实组、检索范围或服务结果受限，不表示“完全没有任何省略”或“全部问题已回答”。主动省略可选邻居由独立计数记录；必要条件不能作为可选内容省略。首次选择也预留终态指令与批次结清开销，避免正文先装满预算、提交时才发现无法容纳完整结果。

同输入最多两次实际查询，原生 HTTP 自动重试保持关闭；相同参数只检索一次，终态后新调用明确失败。累计证据、精确来源去重及权限复核不跨输入共享，补充查询正常未命中不抹除先前有效事实。授权失败、冲突及依赖故障不能靠缓存伪装成功。

## 4. 身份、状态与故障

有效范围取客户授权、部署允许和 CueKB Key 的交集，API Key 不发送给浏览器或 VoiceChat。没有检索到某字段不表示全库不存在该字段。

| 返回 | 处理边界 |
| --- | --- |
| 200 ok + hits | 使用完整、适用的有据片段；不宣称已充分覆盖问题 |
| 200 not_found + [] | 本次正常检索未命中，不扩展为事实不存在 |
| 200 degraded + hits | 保留限制原因并核对仍可用的完整组 |
| 200 degraded + [] | 区分检索依赖、选择/预算受限，不一律称为“没有文档” |
| 401/403 | 认证/授权故障，不扩大范围重试 |
| 409 | V2 模型/索引/配置未就绪，明确配置错误，不自动调用旧接口 |
| 422 | 请求/接口契约错误 |
| 429、5xx、超时/断流 | 限流或服务故障，不转换为正常空结果；原生路径不重试 |

HTTP 3xx 不跟随，不接收跳转正文。上游响应总上限 256 KiB，内部工具结果保持小于 32 KiB，模型侧原生完整证据包默认 6000 bytes；这些预算层级不能互相替代。HTTP 结束或本地取消不能证明远端模型计算已停止。真实 Recall、误归属、P95、语音覆盖与听音必须独立验收，CPU fixture 不是生产达标证据。
