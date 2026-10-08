# CueKB M3 接口契约

2026-10-07 按应用基线 `f498fa4` 核对。职责：请求、证据映射、上游错误；不维护模型路由或部署参数。实现见 [CueKBAdapter](../../apps/api/app/tools/adapters.py) 和 [工具 schema](../../apps/api/app/tools/schemas.py)，回归见 [工具契约测试](../../tests/contract/test_tools.py)。真实目标服务仍须核对 OpenAPI。

## 1. 请求与边界

核对本地 CueKB revision：`1b9379de53c55dd41193a1529e46d48c0f219c14`（M3）；来源为其 `src/cuekb/schemas.py`、API routes/dependencies 与检索实现。部署前核对目标服务 OpenAPI，不把该 revision 当作所有部署版本。

```http
POST {server-configured-cuekb-base-url}/v1/search
Authorization: Bearer <server-side-scoped-key>
Content-Type: application/json
```

```json
{
  "query": "用户问题与已确认条件",
  "kb_ids": ["00000000-0000-4000-8000-000000000001"],
  "mode": "auto",
  "top_k": 5,
  "filters": {},
  "include_context": true
}
```

示例 UUID 仅说明类型。实际 KB UUID 来自服务端授权映射；旧 `kb_support` 不能直接传入。当前 CueKB query 上限 2000 字符，top_k 支持 1–20；默认选 5 是本应用建议。工具输入目前只开放 `product_model`、`software_version`，由 BusinessRuntime 从明确输入或已确认上下文传入；Adapter 不从自然语言猜测。CueKB 支持的 `document_ids` 暂不开放给模型。`relations` 是 CueKB M3 的可选请求字段；本项目可接收其返回的关系证据，但暂不让模型生成实体 UUID、关系类型或时间条件。

不向上游发送自造 request_id/locale/deadline 并假定生效。HTTP 超时与应用 deadline 自行执行，取消本地等待不证明 CueKB 后台已停止。

## 2. 结果映射

| CueKB | 内部证据与回答处理 |
| --- | --- |
| trace_id | 关联本项目 task/run，不能要求回显不存在的 request_id |
| retrieval_status | 保留 ok/degraded/not_found，与最终 answer status 分开 |
| evidence_status | 保留 unassessed 等原值，不能转换成“证据充分” |
| degraded_reasons、scope_limited | 审计并参与回答决策，必要时告知限制 |
| content_revisions | 保留知识内容版本线索，不宣称跨系统事务一致性 |
| hits.document_id / chunk_id / version_id | 引用真实身份与版本；本项目另生成 citation_id |
| source_text、context | 保留证据原文与上下文，内容相同不重复占预算 |
| context_parts、context_truncated | 保存逐块原文及锚点、CueKB 的上下文截断标记；门户可展开逐块来源 |
| relations | 保存关系类型、条件和 supports/refutes 立场；该立场不是事实真假结论 |
| title_path、anchor、metadata | 来源定位及适用条件；缺失标题用“来源片段”，不伪造 |
| rank、retrieval_sources | 检索排序/来源，不当作事实置信度 |
| timings_ms、retrieval_path、executed_stages、skipped_stages | 内部诊断，不要求普通客户理解 |

Citation 的 updated_at 已改为可选，未填当前时间冒充文档更新时间；不再生成 score。`version_id` 与 metadata 中受控的 `business_version` 分开，旧历史 JSON 在读取时仍按原数据兼容，新增任务字段由 Alembic 0004 迁移。

CueKB M3 已提供有界章节、相邻块及表头上下文，但预算耗尽时仍可能截断或返回空 context；本项目另以 6000 字符证据预算和小于 32 KiB 的工具结果预算裁剪，使用 `context_omitted` 和 `hits_omitted` 明示应用侧裁剪，不将其冒充 CueKB 状态。回答模块仍需检查证据充分性。原件查看需经本项目重新鉴权并固定检索版本；这是待实现入口，不向浏览器暴露服务 Key 或私有下载 URL。

## 3. 身份和错误

有效范围取客户授权、部署允许、CueKB Key 可读范围的交集。CueKB 当前鉴权主体是 API Key；自定义 X-Tenant-ID/X-User-ID 不代表它已执行客户级 ACL。不同隔离范围由服务端映射受限 Key/KB，不由工具参数指定。

not_found 表示本次未命中，不能推导事实不存在；degraded 有 hits 时保留原因并判断可用性；401/403 为授权或配置问题，422 为契约问题，429 为负载限制，5xx/超时为服务故障。禁止统一降为“查无资料”。返回答案、读历史证据和原件时都需覆盖撤权策略。

HTTP 3xx 不跟随，也不使用其正文中的证据，返回 `TOOL_BAD_RESPONSE`；损坏的压缩编码同样归为坏响应。上游连接中途关闭、超时或 5xx 最多重试一次，始终受工具和整轮预算限制；部分响应不能保留或与重试结果拼接。重试仍失败返回 `TOOL_UNAVAILABLE`，不回退模拟数据。

CueKB 上游 HTTP 响应上限为 256 KiB，内部工具输出上限为 32 KiB，外置模型证据正文加上下文预算为 6000 字符；Q07 D2 的 evidence-v1 包另限默认 6000 bytes / 3 项（包括问题、身份与条件字段）。这些是不同边界，超过内部预算时显式舍弃上下文或命中。知识工具预算仍为 5 秒，业务整轮预算默认 30 秒，均不能证明真实端到端时延已达标。
