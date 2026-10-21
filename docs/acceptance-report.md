# 当前验收证据

更新：2026-10-21。**Black Pig 单 case 有现场成功证据；Golden Yolk 批次修复尚未完成真实验收，整个产品尚未放行。** 操作见 [批次验收](native-tool-batch-validation.md)，要求见 [验收清单](development/validation.md)。

## 1. 保留的现场证据

| 日期/范围 | 事实 | 不能推导的结论 |
| --- | --- | --- |
| 2026-10-20 15:20 UTC，Black Pig 自动单 case | conversation=6d9dfaa1-1f84-4df6-94dc-65a3f711bdf3；两次检索、10 条来源、终态 1、终态后调用 0；answer_complete=passed、answer_status=answered、validation=passed | 不代表所有产品问题或 Web/多轮均通过；听音/来源语义仍需复核 |
| 2026-10-20 04:48 UTC，四 case 顺序 | 第四 case Golden Yolk 在输入被观察前遇到 VOICE_SESSION_ROTATION_REQUIRED | 该轮不能证明 Golden Yolk 选择工具或续答能力 |
| 2026-10-21 Web/自动对照 | 用户确认 Web Black Pig 出现 Checking knowledge；两次工具与结果可见；Web 后续仍观察到多次回答响应 | 不再能概括为始终没调用；重复响应及内容准确性未确认解决 |
| 2026-10-21 Golden Yolk 第二段 | 第 3 调用 04:58:45.381995 到达；第 2 终态提交 04:58:45.576696；此前按出队状态报 post_terminal | 第 3 调用不是终态提交后才收到；不证明模型已消费终态后仍拒绝停止 |
| 对照的时延 | CueKB 请求约 103/104 ms；工具结果到下一调用/回答存在较长间隔；VoiceChat sequence 当时未关联 | 不能把全部等待算为知识检索、GPU 推理或浏览器缓冲 |

历史旧工具名、严格模式故障及逐轮修补流水已从交付目录删除，可从 Git 追溯，不作为当前状态。

## 2. 本次本地验证

以下为本次实际执行的独立测试组，有覆盖重叠，不累加为全量通过数：

- 原生批次/验证摘要/时间线契约：57 passed。
- VoiceChat CPU 生命周期与诊断：28 passed，使用固定外部测试依赖的隔离环境。
- VoiceChat backend 补丁及部署契约：60 passed；补齐批次测试夹具状态，部署断言与既有两次预算配置对齐。
- 原生工具目录及探针契约：58 passed。
- 原生预算/累计证据集成场景覆盖正常补充、重复、空结果、真正终态后调用、权限撤销、批次超额结清及新输入重置。真实 HTTP/WS 适配器配合确定性供应商夹具，批次成功场景只执行两次检索并一次结清第二批。
- Web 构建及 30 项 Node 音频测试通过；受影响 Python Ruff/编译、外部 manifest 校验通过。

测试环境没有执行真实 GPU、远端 CueKB 或用户听音。本次 CPU 结果不证明模型一定正确消费聚合结果或已消除等待时延。文档整理后的检查仅核对链接、命令语法、配置/代码一致性，不重新标记历史测试日期。

## 3. 尚缺的放行证据

Golden Yolk 第二段的真实有据回答和前端提交；Web 内容与重复响应判定；同连接多轮/指代与真实打断；工具等待交互；生产权限、恢复和容量。hello 起始词缺失按用户要求暂缓。最新优先级只在 [任务板](TASK_BOARD.md) 维护。
