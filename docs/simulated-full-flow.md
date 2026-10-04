# 独立 API 全流程模拟测试

更新：2026-10-04。实现基线为 `codex/q1003` 的 Q07；测试通过真实本地网络连接本项目与外部 API 夹具。执行结果记录在 [验收报告](acceptance-report.md)，本页维护测试结构、覆盖和复现步骤。

## 1. 测试边界

CueKB 与 NVIDIA Speech / nemotron-labs-voicechat 保持独立服务职责。测试不启动它们的应用或模型；按本仓库已确认的 CueKB M3 和 VoiceChat WebSocket 契约提供模拟 API。VoiceChat 逐轮 response ID、ACK 和结束边界遵循 [交付补丁约定](../deploy/voicechat/README.md)，不能据此证明未应用补丁的上游版本兼容。

测试进程内启动四个独立监听端点：

```text
门户 HTTP/SSE/WS → 本项目 API（真实路由、鉴权、Coordinator、Runtime、Registry、Store）
                       ├─ NvidiaAdapter → 模拟 VoiceChat WebSocket
                       ├─ CueKBAdapter  → 模拟 CueKB HTTP /v1/search
                       └─ Agents SDK    → 模拟兼容文本模型 HTTP /v1/chat/completions
```

业务用例不替换生产适配器，不使用 HTTP MockTransport 或 ASGITransport。服务通过各自 `127.0.0.1` 端口通信，默认随机端口；浏览器测试固定应用端口 8000、Vite 5173。它们在同一测试进程内运行，并非跨主机部署验收。设置由夹具显式注入，正常应用部署不启用模拟入口，也不增加另一套 `.env` 或 Compose。

- 模拟 VoiceChat 接收真实 PCM 帧，按脚本产生 ASR、原生工具调用、字幕及音频事件；工具选择不是 Nano 的实际推理。工具结果通过 `conversation.item.create/function_call_output` 回填。
- 默认双工具会话只注册 `lookup_knowledge` / `reason_over_knowledge`，断言原生 WS 不含 `tool_choice`；后台文本模型的 SDK 仍使用其独立的 required/auto 配置。
- 模拟 CueKB 验证测试 Key、服务端 KB 范围，并返回 M3 证据或注入错误；模型 API 支持普通响应和 SSE 工具调用/答案流。
- 每个网络用例创建独立 SQLite 数据库，执行实际 Alembic `upgrade head`（当前 0008），再启动本项目；不使用自动建表。新增连续上下文序列见 [CTX1 / EVAL1](task-context-evaluation.md)。
- PCM 是 500 Hz 合成音，24 kHz、PCM16、80 ms、3840 bytes。浏览器使用合成麦克风和实际 AudioWorklet/传输流程；不验证真实 ASR、TTS、语义、听感或设备麦克风。
- SSE 取消回归单独在真实 SQLite 驱动中设置短暂查询屏障，稳定模拟断开竞态；只有该数据库时序用例注入 Session 子类，外部 API 适配器仍走网络。

## 2. 覆盖和断言

网络测试见 [test_simulated_full_flow.py](../tests/integration/test_simulated_full_flow.py)，共 42 项。每个用例等待实际事件或持久终态，不能以端口可达替代业务断言。

| 场景组 | 项数 | 验证内容 |
| --- | --- | --- |
| 知识路径与外部错误矩阵 | 20 | 直查、复杂推理、证据未评估/冲突/不足/降级/截断/过多、无命中/澄清；CueKB 401/403/422/429/503、错误 JSON/超大响应；外置模型 503、未调必需工具、非法引用 |
| 同会话混合路径 | 1 | 一般回答→直查→复杂推理；一般回答保存 Utterance、零知识 Turn/检索/外置调用；随后两个知识 Turn 各自完成 |
| legacy 和严格策略 | 3 | legacy 桥接与 strict lookup 走外置链；strict 无工具回答在正文/音频放行前拒绝 |
| D2 口述检查 | 3 | 不支持的数字、单位或缺失型号条件均失败；仅清除对应 response 的剩余播放 |
| ASR/重复调用/终态 | 1 | 工具先到、最终 ASR 绑定、重复 call 不重复执行；EvidenceReady 不提前成为 canonical final |
| 原生回答超时 | 2 | 一般回答和直查缺 audio.done，按 VOICE_ANSWER_TIMEOUT 结束 |
| 非法工具/参数/猜测过滤 | 3 | 未注册工具、注入 kb_ids、猜测型号不能检索或得到实质性答案 |
| 检索期间 ACK | 1 | 后台查询被屏障暂停时，固定 ACK 字幕/PCM 已到门户；此时尚无工具结果或最终答案 |
| 整轮预算 | 2 | 直查/复杂执行遇慢 CueKB 均在共享期限失败，不发第二次查询或最终模型调用 |
| 独立 VoiceChat 断开 | 1 | 待原生续答时上游断开，未完成 Turn 取消；新连接可完成新答案 |
| 文字/幂等/SSE 重放 | 1 | 实际流式 SDK 查询/答案、同 key 不重做、不同正文 409、持久 event_id 重放一致 |
| SSE 断开数据库清理 | 1 | 查询进行中断开 HTTP，数据库会话正确关闭、连接归还、新通话可写入 |
| capability/Origin/票据 | 1 | 两通话 token 不同；跨会话/无 token/撤销后 401；非法 Origin 和重复 WS 票据 403 |
| 取消与新 epoch | 1 | 检索期间显式取消，旧结果不回填、不播放；新 epoch 正常回答 |
| 停止播放与继续问答 | 1 | 播放停止保留已完成业务答案，下一次复杂回答仍产生音频和终态 |

三条主路径的请求次数来自实际 API 计数：

| 路径 | CueKB HTTP | 文本模型 HTTP | VoiceChat 工具结果 |
| --- | --- | --- | --- |
| 一般回答 | 0 | 0 | 0 |
| lookup 完整 sufficient 证据 | 1 | 0 | 1（EvidenceReady） |
| lookup 升级且可复用证据 | 1 | 1 | 1 |
| reason_over_knowledge | 1 | 2（工具选择、证据作答） | 1 |

20 项矩阵还检查 canonical final 唯一、DeliveryAttempt sent/response_id、最终 ASR 而非改写参数作为检索请求、KB 范围不被覆盖、门户音频帧及通话撤销。预期失败状态表示故障处理通过，不表示服务故障变为成功答案。

新增浏览器用例见 [simulated-full-flow.spec.ts](../tests/e2e/simulated-full-flow.spec.ts)：不拦截项目 HTTP/WS，连续测试一般问答、直查及复杂推理；断言右侧问题、左侧实际字幕、来源、查询/模型调用次数、播放停止后下一条回答、两个知识 Turn 和结束通话后的 401。其余既有浏览器用例含受控路由或 WS，证据范围按各自测试区分。

## 3. 复现步骤

根目录使用已安装的 Python 3.12 开发环境；安装步骤见 [README](../README.md#验证命令)。不需要真实服务密钥。

只执行网络矩阵：

```sh
.venv/bin/python -m pytest tests/integration/test_simulated_full_flow.py -q \
  -o junit_family=xunit1 --junitxml=artifacts/simulation/network.xml
```

全部 Python 回归：

```sh
.venv/bin/python -m pytest -q -o junit_family=xunit1 \
  --junitxml=artifacts/simulation/python.xml
.venv/bin/ruff check apps/api tests scripts
npm test --prefix apps/web
npm run build --prefix apps/web
```

浏览器使用三个终端。第一个启动显式模拟环境，创建临时数据库，退出后清理：

```sh
PYTHONPATH=apps/api .venv/bin/python scripts/simulate_full_flow.py \
  --serve --port 8000 --origin http://localhost:5173
```

第二个启动实际门户：

```sh
npm run dev --prefix apps/web -- --port 5173
```

第三个执行测试。在云环境已有 `/usr/bin/chromium` 时使用：

```sh
cd apps/web
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/usr/bin/chromium \
  SIMULATED_FULL_FLOW=1 \
  PLAYWRIGHT_JUNIT_OUTPUT_FILE=../../artifacts/simulation/browser.xml \
  npm run test:e2e -- --reporter=list,junit
```

若使用 Playwright 自带已安装 Chromium，省略 `PLAYWRIGHT_CHROMIUM_EXECUTABLE`。未设置 `SIMULATED_FULL_FLOW=1` 时，新增独立 API 浏览器用例明确跳过，不能将该次运行算作完整矩阵。测试运行结束后用 Ctrl-C 关闭自己启动的模拟环境和 Vite。

`/__simulation/plan` 和 `/__simulation/state` 仅存在于此显式夹具实例，分别用于排队脚本和读取计数；不是正式 API，绑定 loopback。不要将它们部署到业务环境。JUnit 和截图写入被忽略的 `artifacts/` / `test-results/`，不提交测试 token、数据库、PCM 录音或真实凭据。

## 4. 验证限制

这套测试验证本项目 API 交互、编排、授权、终态、持久化及浏览器集成。不验证真实 Nano 工具漏/误调用率、真实知识正确性、GPU 音频生成、工具等待期间自由交谈、跨主机/TLS/Nginx、防火墙、PostgreSQL/Redis、Docker、并发容量、生产 p50/p95 或实际听音。真实验收仍归 D07/Q07-E；本地计数和合成音时序不能作为生产性能数据。
