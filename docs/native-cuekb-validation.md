# 英文知识问答验证

本期路径：Web ⇄ API relay ⇄ VoiceChat；原生 search_knowledge_base 调用触发 CueKB，结果回填后由 VoiceChat 口述。当前批次修复的部署/最小测试统一按 [批次验收](native-tool-batch-validation.md)，完整四文件对照按 [Web/脚本操作](web-wav-comparison.md)。本页维护单 case 入口、判据与只读答案检查，不提供另一套升级命令。

## 1. 输入与环境

配置为 VOICE_PROVIDER=nvidia、AGENT_PROVIDER=disabled、QA_EXECUTION_MODE=dual_tools、QA_ANSWER_POLICY=general_qa，默认每 input 两次实际查询且无 HTTP 自动重试。

API 镜像内置 `artifacts/native-cuekb/` 的 cases.json 和四段 WAV，路径 `/app/validation/native-cuekb/`。所有 Python 在容器执行，验证时不使用 docker cp，不手动创建输入目录。脚本自动写可写 `/tmp` 报告及回答音频。容器重建会清理 tmpfs，需保存的结果先用 docker exec cat 导出。

## 2. 单 case 入口

宿主机项目根目录执行，使用新连接和原始录音：

```sh
API_CONTAINER=$(docker compose --env-file .env -f deploy/compose.production.yaml ps -q api)
if docker exec "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py \
  --case-id black_pig_crisp_heart --case-timeout 120
then
  VALIDATION_EXIT=0
else
  VALIDATION_EXIT=$?
fi
printf 'host_exit_code=%s\n' "$VALIDATION_EXIT"
```

当前 Golden Yolk 批次复测使用专门手册的唯一输出路径，避免覆盖/误读旧报告。无 `--case-id` 则顺序执行全部四 case 于一个连接；若遇到连接轮换，不能当作后续 case 已验收，也不能把错误归为 ASR。

默认 base-url 为 API 容器内部 `http://127.0.0.1:8000`，Origin 来自 PUBLIC_ORIGIN。EOF 后持续送静音，等待最新 input 的非 ACK 完整回答；不把 WAV 停顿前第一段完成标记用于第二段。

## 3. 必要判据与输出

| 输出 | 含义 |
| --- | --- |
| selection / actual_tools | 实际工具选择；无调用不能伪造成功 |
| retrieval / voice_evidence | 检索和可交付证据状态，不代表语义充分 |
| retrieval_executions / retrieval_limit | 当前 input 实际请求数与上限 |
| duplicate_queries / queued_calls_settled | 相同查询复用，以及批次超额成员结清 |
| tool_batches_submitted / terminal_results_submitted | 批次结果和结束检索结果计数，不证明回答完成 |
| post_terminal_calls | 终态提交后收到的新批次调用，非零为阻塞 |
| answer_complete | 非 ACK 文本、非空音频、结束事件齐全 |
| answer_status / answer_sources / reason | 门户业务提交与引用/检查原因 |

产品 case 应为 answer_complete=passed、answer_status=answered、answer_sources 大于零、reason/portal_error 为空，并且查询数不超过预算。第一次有据回答即可结束，无需人为耗尽两次预算。第二次未命中不应丢弃第一次有效来源。

问候/TCP-UDP 不要求工具和引用。arguments=manual_review_required、cuekb_evidence=unassessed 不得冒充已完成语义评估；产品事实、数值、条件及缺失信息仍须核对 Sources 和听音。诚实部分回答可以通过，编造或漏实质条件不能因音频结束而通过。

只回传 preflight、窗口/CID、case、validation、退出码及必要时间线；若有 answer_check，保留最多三个具体失败条件，不提交全量日志或令牌。

## 4. 只读答案与音频

使用本次报告的真实 ID；下列占位符必须替换：

```sh
CONVERSATION_ID='本次 conversation_id'
INPUT_ITEM_ID='对应产品段 input_item_id'
docker exec "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py \
  --inspect-answer "$CONVERSATION_ID" --input-item-id "$INPUT_ITEM_ID"
```

输出仅检查已有记录，不调用 CueKB、不发送 WAV。unavailable 表示缺少可检查记录，不能据此认定模型回答有据或数据库已提交。内容检查失败先核对具体条件，不重复跑录音或直接放宽规则。

默认输出路径的单 case 结果可导出：

```sh
docker exec "$API_CONTAINER" cat /tmp/native-cuekb/report.json > native-cuekb-report.json
docker exec "$API_CONTAINER" cat /tmp/native-cuekb/replies/case-01.wav > native-cuekb-reply.wav
```

自定义输出目录时替换为同次路径；case-01 是当前运行的第一条，不是固定对应 hello。报告和录音不提交仓库。工具传输和实际播放、真实内容验收分开记录。
