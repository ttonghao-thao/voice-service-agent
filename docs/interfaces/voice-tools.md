# 全局 VoiceChat 工具目录

2026-10-13 实现。入口：`agent_runtime/tool_catalog.py`、`dispatch.py`、`api/routes.py`、`voice/provider.py`/`gateway.py`。工具数量由后台配置决定，两个知识工具只是初始目录。`dual_tools` 保留为兼容模式名称，含义是原生可配置多工具；`legacy` 仍是显式单 bridge 兼容路径。配置与迁移命令见 [部署](../deployment.md)，执行规则见 [知识执行](../qa-routing-design.md)。

## 1. 信息归属与注册契约

可信代码注册 `NativeTool` 的参数模型、执行器、执行种类、后台依赖和权限 scope。后台全局目录决定哪些名称启用，以及 description、examples、output_description、ack_messages；可用不同名称引用同一个可信 implementation。配置不能指定 Python import、任意 URL/凭据或覆盖参数 schema。新业务 API 需要先接入可信执行器；仅添加名称不会凭空产生执行能力。

初始目录来自 `config/voice-tools.yaml`。首次管理员保存后，以数据库 `native_tool_config` 单行配置及 revision 为准，多副本共享同一数据库；文件不会自动覆盖已保存配置。目录对所有客户可见，不按客户 scope 隐藏。部署或管理员关闭后台依赖时，对应原生工具全局不可用；执行仍检查当前 scope、KB 范围、epoch/revision、租约及依赖版本，可见不授予额外数据权限。

注册顺序：`session.created → session.update → session.updated → portal.session.ready → 用户音频`。`session.update.session` 提供 audio、instructions、tools；每个工具提供 name、description、parameters、ack_messages。参数 schema 从可信模型生成，examples 先通过该模型校验，再附于 description；output_description 说明结果语义，不是协议新增的输出 schema。工具调用和回填使用供应商原始 call_id，不发送 tool_choice。

知识参数 `user_request` 保留完整请求；product_model/software_version 为必填可空字段，未知必须传 null，不能省略或猜测。字段级 description、类型、长度和 additionalProperties=false 均发送给模型。系统提示词根据会话策略分别生成，选择规则只引用当前注册目录，不固定要求两个名称必须存在。ACK 按响应绑定的工具识别，不能把某工具的 ACK 当成另一个业务答案。

Adapter 核对 session.updated 的音频格式、工具集合/名称/description/parameters/ack_messages 及 instructions；工具列表顺序可变、额外默认字段可存在，但必要字段必须一致。缺失/被修改时返回 VOICE_PROTOCOL_ERROR，握手不能进入 ready。该回显只证明传输注册一致，不证明最终 Jinja 模板、function head、权重/tokenizer/SOTC/EOTC 或模型真实选择正确。

## 2. 后台配置与刷新

接口前缀 `/api/v1`；使用已有 tools:admin 权限。validation 部署可设置独立 `TOOLS_ADMIN_API_KEY`，以 Bearer 提交；未设置时管理 API 关闭。凭据不得下发门户或写入文件样例。客户 call token 无管理权限，操作员凭据也不授权读取客户通话历史。

| 接口 | 行为 |
| --- | --- |
| GET `/admin/voice-tools` | 返回 revision、完整配置 items、active_tools、实际 definitions 和可用可信 implementations/参数 schema |
| PUT `/admin/voice-tools` | `{expected_revision,items}` 全量原子替换，校验整份候选后提交新 revision；并发旧版本返回 409 |
| POST `/admin/voice-tools/reload` | `{expected_revision}` 重新读取当前实例的 YAML，校验并发布到共享数据库，保留同样的 CAS 和审计 |
| GET `/capabilities` | 公开 native_tools 名称及 native_tool_revision；不是某个旧连接的注册快照 |

一个新增配置条目示例（实际 PUT 的 items 必须包含希望保留的全部条目）：

```json
{
  "name": "lookup_product_manual",
  "implementation": "lookup_knowledge",
  "description": "Look up a documented product manual procedure.",
  "enabled": true,
  "ack_messages": ["I will check the product manual."],
  "examples": [],
  "output_description": "Return authorized evidence or a clarification/error."
}
```

刷新不依赖更改进程内 settings：新 session 从数据库建立新快照，活动连接在版本检查/每秒 watchdog 发现变化时失败关闭；旧工具输出和未知写回不重发。麦克风门户重新取票并注册最新目录，恢复授权摘要，不恢复模型隐藏状态；文件输入停止并显示未发送区间，用户显式重跑才会创建新的文件通话。不声称未经验证的同连接热 session.update 安全。

修改任一后台目录条目会刷新全部原生多工具连接，包括正在等待结果的连接；不能保证跨连接无缝交谈。后台依赖启停亦由 watchdog 检测并刷新。legacy 不使用本目录，不因此改变其会话策略。

资源约束为最多 64 个配置条目、编译后定义不超过 64 KiB，HTTP 请求仍受现有 16 KiB 上限；超过官方约 5 工具建议只告警，不限为 2 或 5。工具多时的模型选择质量仍须实测，现有并行调用限制不因此解除。

## 3. 验证与故障分界

先执行 Alembic 0010，再按 GET → 修改 items → 带当前 expected_revision PUT → GET capabilities 确认名称/revision。使用两个独立 call 确认可见目录一致；活动麦克风应重新注册，文件应结束且不重播；旧 revision 保存必须为 409。重启 API 后目录仍应保留。

针对性回归：`tests/integration/test_native_tool_catalog.py` 验证三工具注册/执行、原始 call_id、全局可见与执行权限、持久化/跨目录实例、CAS、启停/reload、活动连接刷新、无效候选保留旧配置及注册回显失败；迁移回归见 `test_qa_migration.py`。浏览器刷新/文件不重播见 `voice-audio.spec.ts`/`voice-file.spec.ts`。

没有 function_call_arguments.done：检查实际注册、最终模板和模型 function channel；已有调用但失败：检查参数/权限/执行；结果已回填但没有回答：检查原始 call_id、结果内容、续答边界/超时。描述不全主要影响选择和参数，不能直接据此断定 CueKB 响应或 function head 有故障。真实 GPU、ASR/工具质量和听音仍归 D07。
