# Codex 工作入口

仅支持英文（en-US）的全双工知识客服；开发沟通/文档可用中文，产品语音、UI、工具描述必须英文。Python 3.12/FastAPI，React/TypeScript/AudioWorklet，PostgreSQL/Redis。

交付目标是 `origin/main`，完成验证后直接推送，不以任务分支替代。当前状态查[任务板](docs/TASK_BOARD.md)，不要把旧提交的通过记录当作本版本验收。

## 按需读取

1. 先 `git status --short`，用 `rg` 定位代码/调用方。只有范围不清楚时读[短索引](docs/README.md)。
2. 长文先查标题/关键词，再读相关行；链接不是递归加载指令。同任务不重复读取有效信息。
3. 不默认整读 docs、任务板、Git 历史、生成 JSON、锁文件、测试夹具或 VoiceChat 快照。跨模块按实际依赖扩读；文档冲突以代码核对，不能为节省上下文跳过必要检查。
4. 需求归 delivery.md，实际行为归 architecture.md，待办归 TASK_BOARD.md；按任务选择。当前所有验证操作/证据只维护在 docs/validation.md，必须自包含。旧方案和验收方法已删除，历史仅由 Git 追溯，不重建归档副本。

## 不变边界

- VoiceChat/CueKB 独立部署；保持 Gateway → Coordinator → Runtime → Registry。供应商结构归 app/voice，Agents SDK 归 app/agent_runtime。
- tools/instructions 原生选工具，不加 WS tool_choice、分类模型、独立 Nano 或子 Agent 调度。一般回答不是企业证据；mock 不证明真实能力，real 失败不回退 mock。
- 全局后台目录决定原生工具数量/描述，不能把初始工具数当上限；执行另验权限。涉及注册/刷新时读[工具契约](docs/interfaces/voice-tools.md)。
- Coordinator 唯一提交；提交/交付复核 epoch/revision/Turn/租约/权限。普通发声和停止播放不取消查询；旧调用结清或关闭连接，unknown 不自动重发。
- 身份、KB、凭据由服务端控制；模型/浏览器不得扩大权限。依赖沿用固定 requirements/package-lock，数据库用 Alembic；不提交密钥、票据、录音、数据库或产物。

## 修改与验证

以英文全双工、多轮、有据回答为准。区分实现、模拟/CPU、真实 GPU、网络发送和听音。定位记录假设/方法/预期；同思路两次无新证据即停，三种假设仍失败则评估设计/环境，每三轮回顾目标与更简单方案。非阻塞项记待办，不降低验收标准。

只更新受影响主题；契约变更执行 scripts/export_contracts.py，同步客户端并回归。纯文档只改 Markdown，检查链接/锚点/命令语法/diff，无需跑应用测试。状态变化更新任务板，新验证才更新证据，勿追加全仓流水。维护规则按需读 [documentation.md](docs/development/documentation.md)。

## VoiceChat 修改必须配套交付

上游是 [NVIDIA-NeMo/Speech nemotron-labs-voicechat](https://github.com/NVIDIA-NeMo/Speech/tree/nemotron-labs-voicechat)，实际基线以 manifest 为准，不自动升级 HEAD。

修改外部源码/测试必须在同一提交按 Speech 相对路径同步**完整文件**到 docs/voicechat，更新 snapshot-manifest.json 的日期、hash、来源和补丁；不能只交补丁，不重写未改文件凑快照。推送前核对外部文件与快照、相关编译/契约及 hash。

受维护运行文件从本仓库绝对路径只读 bind mount 到容器实际路径。新增运行文件须补挂载与身份检查，测试文件不挂推理容器。运行文件、env、GPU、卷、端口、镜像或 entrypoint 变化，必须同提交更新 docs/voicechat/docker-run.md；新增 env/挂载需重建容器，不能只 restart。未实际验证 GPU/原生工具/CueKB/听音就明确标未验证。

生产验证在容器执行，宿主单入口自动提取窗口/会话并输出有界摘要；操作者只执行回传，不承担诊断选择。
