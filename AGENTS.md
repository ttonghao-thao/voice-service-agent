# Codex 工作入口

全双工知识客服，按容器部署。产品支持天气（QWeather）、通用知识（Tavily）和专业知识库（CueKB）查询。Sophon 部署保留 Tavily，可按需接入天气或专业知识库。门户 UI 和 NVIDIA VoiceChat 沿用英文（en-US），Sophon Live 按 `SOPHON_LIVE_LOCALE` 验证中文或英文语音。

交付目标是 `origin/main`，完成验证后直接推送，不以任务分支替代。当前状态查[任务板](docs/TASK_BOARD.md)，不要把旧提交的通过记录当作本版本验收。

## 按需读取

1. 先 `git status --short`，用 `rg` 定位代码/调用方。只有范围不清楚时读[短索引](docs/README.md)。
2. 长文先查标题/关键词，再读相关行；链接不是递归加载指令。同任务不重复读取有效信息。
3. 不默认整读 docs、任务板、Git 历史、生成 JSON、锁文件、测试夹具或 VoiceChat 快照。跨模块按实际依赖扩读；文档冲突以代码核对，不能为节省上下文跳过必要检查。
4. 需求归 [delivery.md](docs/delivery.md)，实际行为归 [architecture.md](docs/architecture.md)，未实施方案归 [implementation-plan.md](docs/implementation-plan.md)，操作归 [deployment.md](docs/deployment.md)，待办归 TASK_BOARD.md。验证方法/执行记录只在 [validation.md](docs/validation.md)，先查章节；旧流水由 Git 追溯，不重建归档副本。

## 不变边界

- VOICE_PROVIDER 单选 sophon_live 或 nvidia，部署模板默认只配置 Sophon；两后端不能同时启用。本期仅验证 Sophon，NVIDIA 只检查必要的启动选择契约，不运行其服务/GPU 回归。
- NVIDIA VoiceChat、Sophon Live、CueKB 独立部署；保持 Gateway → Coordinator → Runtime → Registry。供应商结构归 app/voice，Agents SDK 归 app/agent_runtime。
- LiveKit 与 Redis/PostgreSQL/API/Web 由同一份 deploy/compose.production.yaml 管理，复用 Redis；不再另行部署 LiveKit。已解决问题只在 validation.md 的已解决章节保留，不反复列为当前待办。
- Sophon tools/instructions 原生触发委派；已收到的 task 使用批准的 Qwen LLM Router 解析（结构化任务直接校验），不加 ASR 分类/补调、WS tool_choice、独立 Nano 或子 Agent 调度。一般回答不是企业证据；mock 不证明真实能力，real/LLM 失败不回退 mock/规则执行。
- Sophon ASR 仅供字幕展示。模型直接理解音频，原生委派 `task` 提供工具意图与参数，音频窗口校验委派归属。
- 查询依赖按 `ENABLED_TOOLS` 校验。Tavily 公开兜底只处理获准公开发送的独立问题，私有失败、未知意图、无原生委派不自动转公网。NVIDIA 兼容路径仍依赖 CueKB。
- 全局后台目录决定原生工具数量/描述，不能把初始工具数当上限；执行另验权限。涉及注册/刷新时读[工具契约](docs/interfaces/voice-tools.md)。
- Coordinator 唯一提交；提交/交付复核 epoch/revision/Turn/租约/权限。普通发声和停止播放不取消查询；旧调用结清或关闭连接，unknown 不自动重发。
- 身份、KB、凭据由服务端控制；模型/浏览器不得扩大权限。依赖沿用固定 requirements/package-lock，数据库用 Alembic；不提交密钥、票据、录音、数据库或产物。

## 修改与验证

按各 Provider 的目标语言验证全双工、多轮及有据回答。区分实现、模拟/CPU、真实 GPU、网络发送和听音。定位记录假设/方法/预期；同思路两次无新证据即停，三种假设仍失败则评估设计/环境，每三轮回顾目标与更简单方案。非阻塞项记待办，不降低验收标准。

只更新受影响主题；契约变更执行 scripts/export_contracts.py，同步客户端并回归。纯文档只改 Markdown，检查链接/锚点/命令语法/diff，无需跑应用测试。状态变化更新任务板，新验证才更新证据，勿追加全仓流水。维护规则按需读 [documentation.md](docs/development/documentation.md)。

## VoiceChat 修改必须配套交付

上游是 [NVIDIA-NeMo/Speech nemotron-labs-voicechat](https://github.com/NVIDIA-NeMo/Speech/tree/nemotron-labs-voicechat)，实际基线以 manifest 为准，不自动升级 HEAD。

修改外部源码/测试必须在同一提交按 Speech 相对路径同步**完整文件**到 docs/voicechat，更新 snapshot-manifest.json 的日期、hash、来源和补丁；不能只交补丁，不重写未改文件凑快照。推送前核对外部文件与快照、相关编译/契约及 hash。

受维护运行文件从本仓库绝对路径只读 bind mount 到容器实际路径。新增运行文件须补挂载与身份检查，测试文件不挂推理容器。运行文件、env、GPU、卷、端口、镜像或 entrypoint 变化，必须同提交更新 docs/voicechat/docker-run.md；新增 env/挂载需重建容器，不能只 restart。未实际验证 GPU/原生工具/CueKB/听音就明确标未验证。

生产验证在容器执行，宿主单入口自动提取窗口/会话并输出有界摘要；操作者只执行回传，不承担诊断选择。
