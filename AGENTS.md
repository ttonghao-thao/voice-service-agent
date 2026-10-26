# Voice Service Agent — Codex 工作入口

仅支持英文的全双工知识客服，支持连接内多轮对话。Python 3.12/FastAPI/Agents SDK；React/TypeScript/AudioWorklet；PostgreSQL/Redis。开发协作和文档可以使用中文；产品的语音输入、回答及门户交互仅支持英文。

后续开发主线为 `main`，基线来自 `codex/q1003`；该分支保留用于追溯。

产品语言以 `en-US` 契约和英文提示词为准；不得从中文开发文档、历史记录或 Unicode 测试夹具推导出中文产品要求。修改用户可见文案或模型工具描述时使用英文。

## 按需读取

1. 先 `git status --short`。范围明确就用 `rg` 找代码/调用方和对应主题；不明确再查 [文档索引](docs/README.md)。
2. 长文先 `rg -n '^##|关键词' 文件`，再 `sed -n '起始,结束p' 文件`。链接是入口，不是递归加载指令。
3. 不默认整读架构、任务板、历史、生成 JSON、锁文件或产物；同任务中仍有效的信息不重复读。
4. 跨模块时按实际调用/数据依赖逐步扩读。文档不足或冲突先核代码，正确性优先，不以节省上下文为由跳过必要检查。
5. 需求与交付范围查 `docs/delivery.md`，实际架构查 `docs/architecture.md`，工作状态查 `docs/TASK_BOARD.md`；按任务选择，不默认全部读。所有验证与当前证据只维护在 `docs/validation.md`。旧设计与排障流水由 Git 追溯，不重新复制进交付文档。

## 核心边界

- VoiceChat、CueKB 独立部署/API 对接；保留 Gateway → Coordinator → Runtime → Registry。供应商语音结构归 `app/voice`，Agents SDK 归 `app/agent_runtime`。
- VoiceChat 用 tools/instructions 原生选工具；不加 WS tool_choice、分类模型、独立 Nano 服务或子 Agent 调度。一般问答不是企业证据；真实 Provider 能力不能由模拟推定。
- 原生工具数量/描述由全局后台目录配置，客户可见目录一致；执行权限另行复核。修改注册/刷新时查 [工具目录](docs/interfaces/voice-tools.md)，不把初始两工具当作固定上限。
- Coordinator 唯一提交；epoch/revision/Turn/租约与权限在提交和交付时复核。普通发声不取消查询，停止播放与业务取消分开；旧调用结清或关连接，unknown 不重发。
- 身份/KB/凭据由服务端控制；模型/浏览器不能扩大权限。real 失败不回退 mock；模拟、转写检查、网络发送和实际听音分开记录。

## 修改与验证

以英文全双工、多轮对话及有据回答的原始验收为准，局部测试通过不等于真实验收。每轮定位写明假设、方法和预期；同一思路连续两次无新增证据即停止，三种假设仍未解决则暂停深入调试并评估设计、环境和技术路径。每三轮回顾目标、进展和更简单方案。非阻塞问题记录后退出，不降低验收标准；区分已知事实、合理推断和设计建议。

依赖沿用固定 requirements/package-lock；数据库用 Alembic。契约变更运行 `scripts/export_contracts.py`，同步对应客户端并做相关回归；命令见 [统一验证手册](docs/validation.md)。不提交密钥、票据、录音、数据库或产物。

只更新受影响的主题；接口字段以代码/生成契约为准。纯文档任务只改 Markdown，检查链接/锚点和差异，无需重跑应用测试。任务状态变化才更新任务板，实际新验证才记验收，不要求每次生成全仓总结。文档职责及维护例子见 [维护规则](docs/development/documentation.md)。

## VoiceChat 外部项目同步与启动

- 外部源码以 [NVIDIA-NeMo/Speech 的 nemotron-labs-voicechat 分支](https://github.com/NVIDIA-NeMo/Speech/tree/nemotron-labs-voicechat) 为来源；实际修改基线记录在 manifest，不自动跟随分支 HEAD 升级。
- 修改独立 VoiceChat/NVIDIA Speech 项目的源码或测试时，必须在同一提交中把修改后的完整文件按 Speech 根目录相对路径同步到 `docs/voicechat/`；不能只提交补丁文件或部署说明。同步更新 `docs/voicechat/snapshot-manifest.json` 的日期、文件 SHA-256、修改来源和已应用补丁；未修改的外部文件不得为凑快照而重写。
- 重建 VoiceChat 容器时，受维护的运行文件直接从本仓库 `docs/voicechat/` 的绝对路径只读 bind mount 到容器实际加载路径，不依赖另一个宿主目录中的手工副本。新增运行文件必须同时补齐挂载与部署身份检查；测试文件只收录，不作为推理文件挂载。
- `docs/voicechat/docker-run.md` 是本项目维护的 VoiceChat 验证容器启动命令。涉及 VoiceChat 运行文件、环境变量、GPU、卷挂载、端口、镜像或 entrypoint 的修改，必须在同一提交中更新该命令，并核对宿主文件与容器实际加载路径。新增环境变量或挂载必须通过重新创建容器生效，不能用重启旧容器代替。
- 推送前核对外部修改文件与 `docs/voicechat/` 完整文件一致，运行受影响的编译/契约测试并检查 manifest hash。GPU、真实音频、原生工具或 CueKB 未实际验证时必须如实标注，不能用 CPU 夹具或容器成功启动替代现场验收。

当前验证执行入口只有 `docs/validation.md` 一份，不并行维护专题验证手册；该文档必须自包含，不能要求操作者转读其他文档。按用户明确要求归档的历史方法存放在 `docs/archive/`，标明停用及当前入口，不作为最新版本通过证据。生产验证在容器执行，宿主机入口自动提取会话/窗口并输出有界摘要，操作者只执行和回传，不承担诊断选择。
