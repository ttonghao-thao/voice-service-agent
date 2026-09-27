# 当前任务板

更新：2026-09-27。目标设计见 [architecture.md](architecture.md)，状态证据见 [acceptance-report.md](acceptance-report.md)。本期范围已确定为英文知识库客服，默认只启用 `search_knowledge`。

## 1. 当前结论与审计基线

当前代码以 D01–D06、D08–D10、D13–D18、E01–E03 的本地验证为基础；D18 已修复现场文字问答的总超时预算及“已有答案/引用却标记失败”的终态矛盾。D17 已将 VoiceChat 工具调用绑定到用户 input item 和最终 ASR，并按已桥接 response 放行口述输出。D16 已强制知识工具调用后置条件、拒绝 VoiceChat 绕过业务 bridge，并恢复语音 Turn 与收音诊断。D15 已把服务端到独立 VoiceChat 的传输从仅 WSS 调整为 WS/WSS 可配置。D13 已将 D12 的共享验证身份替换为每次通话独立的临时 capability，并最终确认 Web `8087` 使用 HTTPS。D09/D12 的账号与共享身份属于已被本期新要求替换的历史实现；D10 的 Web TLS 入口继续保留，私网 API 入口已由 D12 移除。D07 尚未完成真实环境验收。2026-09-22 的 `9b44859` 是历史文档审计基线，验证日期和范围见 [验收 §0–2](acceptance-report.md#0-当前审计与证据索引)。

当前主链：客户门户 → HTTPS/SSE/WSS → SessionCoordinator → BusinessRuntime → ToolRegistry → CueKB；原生语音由 VoiceGateway/VoiceChatAdapter 桥接。服务端 call capability 与 KB 授权、取消/revision、单写入器、安静边界轮换、M3 逐块证据、英文提示词/门户均已存在。PostgreSQL/Redis 接入代码存在，真实迁移、租约和恢复尚未验收。

2026-09-27 的真实门户截图确认 D16 现场缺陷：文字模型可在未完成知识检索时返回 `insufficient_evidence`；语音模型可绕过 `consult_service_agent` 直接生成通用回答；语音业务 Turn 被门户过滤，实时字幕持续聚合；实际英文转写质量与回声边界尚无证据。D16 已按“强制知识工具后置条件 → VoiceChat bridge 与脱敏阶段日志 → 语音 Turn/临时字幕分离 → 麦克风实际约束可见性”完成代码和本地回归；不把本地修复写成 D07 真实验收。

同日第二轮现场截图暴露 D17：未说话时 VoiceChat 工具调用仍可创建业务 Turn；完成态用户 transcript 会被门户立即删除，持久 Turn 又采用模型工具参数而非最终 ASR；合法工具结果后的语音还可能被后续 `speech_started` 错误判定为未桥接输出。D17 已按“用户输入 item 绑定 → 最终 ASR 作为业务请求 → 工具响应级输出授权 → 完成字幕交接到持久 Turn”完成代码和本地回归；真实静音、回声、英文识别与听音仍归 D07。

同日第三轮现场截图暴露 D18：文字问答默认 `12s` 总预算同时覆盖两次模型调用和一次最长 `5s` 的 CueKB 检索，容易被提前截断；模型输出的 `failed` 又可直接成为业务终态，即使已有正文和 citations 仍显示 `Search failed`。D18 已按“扩大并显式配置总预算 → `failed` 仅由服务端运行/工具异常产生 → 规范化历史矛盾记录 → 增加状态一致性回归”完成代码和本地验证；真实服务复测仍归 D07。

仍有明确功能边界：`parent_task_id` 记录前一轮任务，并非子 Agent 调度；`accepted` 为业务结果接受，不代表已听到；引用存在性校验不证明语义正确；原件查看代理尚未实现。后续设计见 Q01–Q04，不回写为旧里程碑未完成。

## 2. 已完成的编码里程碑

D01–D06、D08–D10、D13–D18、E01–E03 已完成代码和本地自动化验证。编码阶段没有 CueKB/VoiceChat 真实接口，也没有 Docker 环境；按接口规范、处理逻辑、契约及本地自动化测试验收，真实联调和容器运行不作为编码交付前提。D07 的部署前代码与静态验证已具备，真实部署环境生产验收仍待执行。

| ID | 任务与影响模块 | 完成条件 | 状态 |
| --- | --- | --- | --- |
| D01 | 冻结门户契约；记录 API/源码规范基线，提供部署版本固定和能力探测机制。`voice/`、`contracts/`、探测脚本 | 原生 call/result、音频格式及异常处理符合规范；契约测试通过；基础/增强模式声明受控。真实部署 digest、事件/音频证据归 D07 | 编码阶段完成；真实探测在 D07 执行 |
| D02 | 身份与知识权限。`api/auth.py`、API、历史/引用读取 | call capability 不具备管理权限；KB 由服务端确定，正文不能覆盖 | D13 改为每次通话独立 token 与 owner；真实 CueKB ACL 待验收 |
| D03 | CueKBAdapter、证据契约与引用。`tools/`、`contracts/`、Runtime/存储 | `/v1/search`、UUID KB、trace/status/version/anchor 按规范正确映射；空命中/降级/错误和权限的受控测试通过；真实 CueKB 闭环归 D07 | 编码阶段完成；真实 CueKB/ACL 在 D07 验收 |
| D04 | 简单 HTML 客户门户。`apps/web/`、门户消息接口 | 开始/结束、字幕、语音播放、状态、答案/引用、窄屏及可选文字；不含管理工作台；复用现有音频模块 | 编码阶段完成；真实设备体验在 D07 验收 |
| D05 | 任务 revision、停止播报/取消/改问分离、pending call 恢复、长会话。`sessions/`、`voice/`、`storage/` | 晚到旧结果不提交、不播报；旧 call 结清或关闭；新会话无旧音频；附和不误取消；迁移与恢复测试通过 | 编码阶段完成；连续交谈仍受 D01 真实能力门槛限制 |
| D06 | 按部署及用户选择启用工具。`config.py`、registry、capabilities/health、部署模板 | CueKB-only 模式不要求天气；仅暴露部署启用、管理员启用且授权工具；新增第三方不改语音主流程 | 编码及本地测试完成 |
| D07 | 云端 Docker 真实端到端、故障、性能与上线。部署及测试模块 | 多个独立 call capability 下英文语音→CueKB→实际口述正确且互不串话；PostgreSQL/Redis/容器与长会话通过；记录版本、样本和阈值 | 部署前检查、Compose 与英文探针代码完成；云端真实验证待执行 |
| D08 | 将生产镜像构建与 Compose 部署分离。`deploy/`、部署脚本、模板及文档 | 独立 `docker build`；部署仅消费预置镜像；静态契约通过 | 编码与本地静态/自动化验证完成；Python 依赖统一为 pip + 固定版本 requirements，已删除 uv 及其锁文件；Docker 运行待 D07 |
| E01 | 接入 CueKB M3 命中契约与预算。`tools/`、`contracts/`、证据存储 | `context_parts`、`context_truncated`、`relations` 严格校验并保存；超预算明示本地裁剪；新版受控响应通过 | 编码及本地契约测试完成；真实 CueKB 待 D07 |
| E02 | 检索条件、逐块引用与文档。Runtime 工具输入、门户、集成文档 | 明确型号/版本经工具输入传递；引用显示来源块、截断及关系证据；历史读取兼容 | 编码、本地测试及前端构建完成；真实资料验收待 D07 |
| E03 | 英文语音与门户适配。`voice/`、Runtime、门户、消息契约 | 英文提示词/文案/默认语言、VoiceChat ASCII 工具结果及英文样本契约验证 | 编码和本地契约验证完成；实际英文语音与口述正确性待 D07 云端 Docker 验收 |
| D13 | 独立测试通话、实时字幕与 HTTPS 门户。`api/auth.py`、存储/迁移、Web、部署 | 点击开始即创建独立 conversation/token；标签页不共享历史；实时打印输入转写和实际口述字幕；移除 `TENANT_ID`，保留 Web TLS | 编码与本地自动化验证完成；公网证书/浏览器及真实多人语音仍待 D07 |
| D14 | 隔离内网文本模型与 CueKB 传输协议。`config.py`、部署模板/文档 | `AGENT_PROVIDER=compatible` 的 base URL 和启用中的 CueKB 接受 HTTP/HTTPS；拒绝非 HTTP 协议或缺少 host；公网门户仍为 HTTPS | 编码与本地契约验证完成；VoiceChat 当时保持 WSS 的边界已由 D15 替换；真实内网服务调用待 D07 |
| D15 | 同主机/隔离内网 VoiceChat WS/WSS。`config.py`、部署模板/文档 | `VOICECHAT_WS_URL` 接受含 host 的 WS/WSS endpoint；WS 仅用于同主机或受控隔离内网；浏览器 HTTPS/WSS 不变 | 编码、模板/文档和本地契约验证完成；真实 WS 路由、防火墙和 VoiceChat 握手待 D07 |
| D16 | 现场知识调用、语音 Turn 与收音诊断修复。Runtime、registry、VoiceGateway、门户音频/UI | 知识请求未调用工具或工具失败不能伪装为无资料；语音必须桥接业务 Runtime；语音问答按 Turn 展示，实时字幕有界；门户显示实际麦克风处理能力；阶段日志不泄密 | 编码和本地自动化验证完成；真实模型/CueKB/VoiceChat/浏览器听音归 D07 |
| D17 | 语音输入绑定、字幕交接与合法口述放行。VoiceChat adapter、VoiceGateway、门户 Live captions | 无有效用户输入的工具调用不创建 Turn；最终 ASR 是展示和业务执行的权威文本；完成字幕保留至持久 Turn 接管；合法 bridge 后的音频不因新输入状态被误拒 | 编码和本地自动化验证完成；真实声学验收归 D07 |
| D18 | 文字问答总预算与答案终态一致性。Runtime、配置/部署模板、门户状态 | 两次模型调用和 CueKB 检索使用明确的总预算；模型不能把成功检索且已有依据的答案标成系统失败；失败答案不携带成功来源 | 编码和本地自动化验证完成；真实文本模型/CueKB 延迟与终态复测归 D07 |

## 3. 待完成与建议顺序

D13 已替换 D12 的共享身份设计并通过本地回归；D14 已放开隔离内网文本模型与 CueKB 的 HTTP/HTTPS 选择；D15 已放开同主机/受控隔离内网 VoiceChat 的 WS/WSS 选择；D16–D18 已完成现场缺陷的本地修复。HTTPS 门户继续作为唯一公网入口。D07 的真实服务验收仍待执行。Q 系列是审计提出的增量设计，尚未编码或安排，不把它们自动追加为原有编码交付前提。

| 顺序 / ID | 工作与状态 | 依赖 / 完成条件 | 方案入口 |
| --- | --- | --- | --- |
| P0 / D09 | 单一配置与本地测试账号，历史完成、部署入口由 D12 替换 | 原账号 JSON、密码、Cookie/JWT 流程不再用于本期验证；底层范围过滤保留 | [验收历史](acceptance-report.md#2026-09-23-d09-单一配置与受限测试认证) |
| P0 / D10 | Web HTTPS 与私网 API，历史完成、私网 API 由 D12 移除 | PostgreSQL/Redis digest 和 Web `8087` 保留；API 不再映射宿主 `8088` | [验收历史](acceptance-report.md#2026-09-23-d10-镜像与公网-https--私网-api-端口) |
| P0 / D11 | 入口文档、任务路线图与设计/证据同步，历史完成 | 当时记录的账号与私网 API 边界已由 D12 更新 | [验收 §1](acceptance-report.md#1-按日期记录的编码与部署前验证) |
| P0 / D07-A | 发布基线与云端环境，待执行 | 固定版本/配置/样本，按公网 HTTPS Web 的单一流程验收；迁移、readiness、TLS/浏览器麦克风与恢复证据齐全 | [部署 D07](deployment.md#d07-分阶段执行设计) |
| P0 / D07-B | 独立 call capability、真实文本模型与 CueKB，待执行 | 多用户并发隔离、服务端 KB 范围/管理拒绝、M3 命中/故障/限额和有依据回答通过 | 同上；V02–V04、V10 |
| P0 / D07-C | 英文基础语音闭环，待执行 | 实际录音→真实检索→实际口述，型号/版本/否定条件准确；基础模式报告 | 同上；V01、V03、V07、V09 |
| P0 / D07-D | 竞态、恢复与能力分级，待执行 | 旧结果零泄漏、pending call 关闭/结清、轮换/断网；增强能力单独裁定 | 同上；V05、V06、V08 |
| P0 / D07-E | 容量与故障恢复，待执行 | 真实 PG/Redis、drain、备份恢复、预算；冻结测量阈值并复测 | 同上；V11、V12 |
| P0 / D07-F | 放行与发布证据，待执行 | 核对各 V 项的证据、失败和不适用原因；配置声明与实际能力一致 | [验收 §5](acceptance-report.md#5-测量与放行) |
| P0 / D12 | 功能验证部署配置收敛，历史完成并由 D13 替换身份 | 当时固定 customer/tenant/KB、移除登录/JWT和宿主 API 端口；共享身份由 D13 替换，HTTPS 入口保留 | [部署](deployment.md#配置与独立测试通话)、[验收 D12](acceptance-report.md#2026-09-24-d12-功能验证配置收敛) |
| P0 / D13 | 每通话独立 capability、实时字幕与 HTTPS 门户，本地完成 | 每标签页只持有本次 call token，开始通话新建 conversation，结束撤销；输入/输出字幕实时显示；公网入口由 Web Nginx 终止 TLS | [门户](portal-protocol.md#1-简单门户)、[验收 D13](acceptance-report.md#2026-09-25-d13-独立测试通话实时字幕与-https-门户) |
| P0 / D16 | 现场知识调用、语音 Turn 与收音诊断修复，本地完成 | 强制 `search_knowledge`、错误状态不冒充未命中；VoiceChat 完整请求必须桥接；门户按业务 Turn 显示并收敛 Live 字幕；记录实际麦克风处理设置 | [门户](portal-protocol.md)、[接入](integration.md)、[验收 D16](acceptance-report.md#2026-09-27-d16-知识调用语音-turn-与收音诊断) |
| P0 / D17 | 语音输入绑定、字幕交接与合法口述放行，本地完成 | 工具调用绑定真实 input item；最终 ASR 驱动 Turn；无输入不自动查询；已桥接 response 的字幕/音频按响应授权 | [门户](portal-protocol.md)、[接入](integration.md)、[验收 D17](acceptance-report.md#2026-09-27-d17-语音输入绑定字幕交接与口述放行) |
| P0 / D18 | 文字问答总预算与答案终态一致性，本地完成 | 总预算覆盖模型→检索→模型完整链路并在部署模板可见；运行失败由服务端判定；有依据答案的 Answer/Turn/来源状态一致 | [门户](portal-protocol.md)、[验收 D18](acceptance-report.md#2026-09-27-d18-文字问答总预算与答案终态一致性) |
| P1 / Q01 | 交付状态可追溯，建议待排期 | 业务提交/供应商写回/播放估计分离；断线后可查 unknown，不自动重播 | [架构 §10.1](architecture.md#101-q01交付记录与恢复语义) |
| P1 / Q02 | 证据与口述质量回归，建议待排期 | 型号/版本/冲突/裁剪/数字/ASCII 降级样本；独立统计文字与口述正确性 | [架构 §10.2](architecture.md#102-q02证据充分性与口述质量) |
| P1 / Q03 | 运维指标与验收报告完整性，建议待排期 | 可分解延迟/故障；报告显示缺测与预期分母，避免部分样本冒充整体通过 | [架构 §10.3](architecture.md#103-q03可观测性与报告完整性) |
| P2 / Q04 | 鉴权原件查看，待业务与上游契约确认 | 版本固定、撤权复查、受控下载；未确认接口前不创建假入口 | [接入 §7](integration.md#7-q04原件查看的后续设计) |

D16–D18 在保留 SessionCoordinator、revision/epoch、单写入器和 pending call 结清语义的前提下加固 VoiceGateway、BusinessRuntime、CueKB adapter、文字答案终态与门户字幕交接；本地夹具测试不记为真实服务验收。下一步按 D07-A–D 执行端到端全双工验证；云端实测缺陷回到原模块修复并复测。

## 4. 文档整理里程碑

2026-09-22 的 M1–M4 为历史审计记录；D11 按当前代码和部署边界再次核对入口、主题设计和验收证据。

| 里程碑 | 状态 | 交付 |
| --- | --- | --- |
| M1 代码、提交、任务与证据核对 | 完成 | 当前基线、已完成清单与真实缺口 |
| M2 增量方案与验收分解 | 完成 | D07-A–F、Q01–Q04，归入现有主题文档 |
| M3 入口精简与文档去重 | 完成 | 精简 AGENTS，索引按问题路由，README 不重复状态明细 |
| M4 文档验证 | 完成 | 63 处本地链接及锚点、代码块闭合、状态/实现核对、差异检查通过；仅修改 9 份 Markdown，详见验收 §0 |
| M5 D11 当前文档与发布准备 | 完成 | 按需入口、Web/API 网络与认证边界、历史/当前状态区分；本地验证见验收 §1，GitHub `main` 推送状态以仓库记录为准 |

## 5. 实施约束与暂缓项

- 编码和文档阶段边界以 [AGENTS](../AGENTS.md) 为准；真实环境缺失不阻塞按契约编码，不能据此声称生产验收完成。
- KnowledgeAgent 仅在复杂知识样本证明收益后评估；当前不新增子任务调度、递归多 Agent 或队列服务。
- 本期仅英文知识服务；中文、天气/股票、WebRTC/SIP、交易和多组织完整多租户均未纳入。匿名 call capability 只用于隔离测试通话，不等同正式客户认证。本项目不更换语音模型、不建设 CueKB 索引。
- 新需求归主题文档与本表；不新增并行总设计或长期聊天纪要。
