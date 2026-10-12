# speech 启动更新与 VoiceChat 接入边界

2026-10-11。本目录保留 D19 WebSocket 传输修复，并新增 D21 漏字/未桥接故障的 WS 与 Triton backend 诊断。补丁由 voice-service-agent 仓库发布，speech 本机源文件已同步；用户已补齐现场 WS/backend 诊断，云端运行文件和模型产物的一致性仍待核对。D21 不改变 RNNT 门槛、工具状态机、模型权重、离线转换或自然插话逻辑；现场故障仍需固定音频对照，不能将诊断交付当作 ASR 修复成功。2026-10-12 补充原生工具通道的三项只读核对命令。

## 为什么原生 HTML 正常，本项目仍需检查传输边界

`voicechat-client.html` 收到 PCM 就播放，按 input transcription.completed 与 output transcript.done 分气泡，不按 response ID 授权；speech 自带 agent 网关也透传音频。原服务可以正常对话和插话，不应把其可用性否定为“模型没有打断”。

本项目另有显式 Stop playback、取消查询和失效结果拒绝。原基线 WebSocket 层对静音也打开 response，只有断线才发 audio.done，工具另造 response ID；音频在 send_loop 中读取全局 ID，而字幕在推理协程中直接发。慢发送时 transcript.done 可以早于该轮缓存 PCM，且同一 ID 跨轮复用。仅在本项目把 transcript.done 换成 audio.done 或随机改 ID，不能恢复缺失的采样边界；直接放行所有 PCM 又会撤销现有业务授权保证。

因此，此份确认同源的 speech 基线保留有界输出队列、事件生成时固定 ID、逐轮尾帧/结束以及工具先于同批 ACK 的修复。它是当前业务隔离要求下选定的 WebSocket 接入方案，不是 NVIDIA 模型实现自然打断的必要条件，也不代表所有 VoiceChat API 都须修改。其它部署版本若已提供等价的有序归属和结束事件，应在 adapter 验证后直接接入。

## 实际需要更新什么

| 文件或配置 | 部署动作 | 用途 |
| --- | --- | --- |
| `s2s/audio_server.py` → 容器 `/s2s/audio_server.py` | 应用 [response-lifecycle.patch](response-lifecycle.patch) 并更新运行文件 | 音频/工具/字幕有序发送、每轮 ID、尾帧、PCM16 满幅与工具参数编码 |
| `s2s/prompt_template.jinja` → `/s2s/prompt_template.jinja` | 保留现有源码版本，并确保镜像中存在同份文件 | 使用原有 Jinja 模板；本次未修改它 |
| `USE_JINJA_TEMPLATE_PROMPT=1` | **在 WebSocket Python 进程启动前设置** | 避免默认模板追加“常识直接回答”的路由；本次已恢复默认模板原文，无需改它 |
| `docs/voicechat-api.md` | 更新说明 | 当前传输契约、原生能力与业务控制边界 |
| `s2s/tests/test_response_lifecycle.py`、`s2s/tests/requirements.txt` | 仅 CPU 测试环境使用 | 不安装到现有 GPU 运行环境 |
| `s2s/audio_server.py`、backend `model.py/runtime_setup.py/data_types.py` | 在 D19 基线之上应用 [diagnostics.patch](diagnostics.patch) | 输入/推理样本计数、ASR final/reset、BOS 来源、raw function 类别、解析/入队/发送顺序；仅长度、计数与标识 |
| `VOICECHAT_DIAGNOSTICS=1` | WS 与 Triton 进程启动前设置，重新加载这两个服务 | 启动级开关；关掉后不产生新增边界日志，不影响原有业务行为 |
| `s2s/tests/test_voice_diagnostics.py` | 仅 CPU 测试环境使用 | 从实际模型源码提取边界方法，验证启闭诊断不改转写/BOS 行为、隐私与容量 |

D19 只修改 WS；D21 增加上述三个 backend 文件，必须同步加载，不能只更新 `/s2s`。模型仓库/权重、部署脚本和已验证的 GPU、端口、共享内存、模型卷参数保持原样，不需要重新转换模型。自然插话由模型处理，应用不会在每次 speech_started 时自动取消查询。

## 源码核对与本地测试

[source-manifest.json](source-manifest.json) 给出补丁涉及文件的前后 SHA-256。针对原始基线，在 speech 源码目录执行；路径替换为实际值：

```sh
git apply --check /path/to/voice-service-agent/deploy/voicechat/response-lifecycle.patch
git apply /path/to/voice-service-agent/deploy/voicechat/response-lifecycle.patch
python3.12 -m venv .venv-d19
. .venv-d19/bin/activate
python -m pip install -r s2s/tests/requirements.txt
python -m pytest s2s/tests/test_response_lifecycle.py -q
python -m py_compile s2s/audio_server.py
```

`git apply` 可用于非 Git 源码目录。已应用本轮最终版本时用 after hash 核对，不重复应用。上一轮临时 D19 文件与本轮最终版不同，尤其默认工具模板已恢复；应使用保存的原始基线重新应用最终补丁。先备份已有修改，不用 `--reject` 或跳过冲突。

D21 是增量补丁，输入基线为 D19 的 after 文件及设计审计的三个 backend 文件。[diagnostics-source-manifest.json](diagnostics-source-manifest.json) 的 before/after hash 由实际源码计算。已应用 D19 时直接执行：

```sh
git apply --check /path/to/voice-service-agent/deploy/voicechat/diagnostics.patch
git apply /path/to/voice-service-agent/deploy/voicechat/diagnostics.patch
python -m pytest s2s/tests -q
python -m py_compile s2s/audio_server.py \
  opt/tritonserver/backends/nemotron-voicechat/model.py \
  opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py \
  opt/tritonserver/backends/nemotron-voicechat/data_types.py
```

环形元数据每会话/sequence 最多 256 条、64 KiB，超限记录 dropped_events。音频仅汇总相邻且同 owner 的区间，不跨 ASR/tool 等控制边界；WS 日志区分 queued/sent 的 output_seq。模型日志记录覆盖 ACK/工具结果 token 之前的 function 类别与累计数，ASR gate/frame/publish/final/reset、原生/ACK/forced EOU BOS、提取取消和 decode step 范围。默认 INFO 的 prompt、工具参数/结果和首 PCM 字节改为长度；不要开启含原文的全局 verbose。开关关闭可停日志，文件回退应恢复完整 before 组合并重载 WS/Triton。

## 更新运行镜像并启动

沿用现场已经验证的离线镜像。可使用本目录 [Dockerfile](Dockerfile) 覆盖 WS、既有模板和 D21 的三个 backend 文件（构建上下文必须是应用两份补丁后的 speech 目录）：

```sh
voicechat_base_image='YOUR_VALIDATED_OFFLINE_IMAGE@sha256:YOUR_DIGEST'
voicechat_release_image='YOUR_REGISTRY/voicechat:YOUR_RELEASE'
docker build --build-arg VOICECHAT_BASE_IMAGE="$voicechat_base_image" \
  -f /path/to/voice-service-agent/deploy/voicechat/Dockerfile \
  -t "$voicechat_release_image" /path/to/speech
```

该派生镜像不安装依赖或下载权重，继承原镜像 entrypoint、command 和运行环境，默认开启 Jinja 和 D21 元数据诊断。随后在**现场原有启动命令/Compose**中替换镜像，显式加入 `-e USE_JINJA_TEMPLATE_PROMPT=1 -e VOICECHAT_DIAGNOSTICS=1`（Compose 使用 environment），保留全部原参数并重建容器。仅 `docker restart` 不会应用新镜像或新环境变量。若 `/s2s` 或 `/opt/tritonserver/backends` 被 bind mount 覆盖，COPY 不生效，须同步更新挂载源文件。核对实际后端路径与 manifest；不同版本发生冲突时先比较源码，不能强行套补丁。

在已配置的容器 shell 中直接运行现有脚本时，对应命令为：

```sh
USE_JINJA_TEMPLATE_PROMPT=1 VOICECHAT_DIAGNOSTICS=1 /s2s/run_s2s_server.sh
```

不要在已有服务进程运行时再启动一份。启动完成后核对环境、实际文件 hash 与镜像：

```sh
voicechat_container='YOUR_VOICECHAT_CONTAINER'
docker inspect --format '{{.Image}}' "$voicechat_container"
docker exec "$voicechat_container" printenv USE_JINJA_TEMPLATE_PROMPT
docker exec "$voicechat_container" printenv VOICECHAT_DIAGNOSTICS
docker exec "$voicechat_container" sha256sum /s2s/audio_server.py /s2s/prompt_template.jinja \
  /opt/tritonserver/backends/nemotron-voicechat/model.py \
  /opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py \
  /opt/tritonserver/backends/nemotron-voicechat/data_types.py
```

上面的 shell 前缀只影响启动的子进程；另一次 `docker exec printenv` 不证明已有 Python/Triton 进程继承了变量。应在实际进程环境与新会话 `voice_ws_diagnostic` / `voice_model_diagnostic` 日志中确认。仅重启 WS 不会加载 backend 改动；按现场既有方式重新加载 Triton，或重建同时运行两者的容器。

本项目也需发布匹配的 API/Web 镜像。发生回退时使用已记录的源码/镜像组合；回到旧 WebSocket 边界后不能继续宣称本项目的逐轮停止/授权已通过。

## D21：原生工具通道的只读核对

在实际 VoiceChat 容器中，使用与 Triton backend 相同环境的 Python。以下检查只读包源码、JSON、safetensors header 与 tokenizer 文件；不创建 vLLM engine、不加载模型权重、不运行转换或修改配置。磁盘源码/元数据仍须与当前进程的启动加载记录对齐。

### 推荐：统一核对 CLI

本项目新增 [audit_voicechat_artifacts.py](../../scripts/audit_voicechat_artifacts.py)，可在已有 Python 环境执行，无需安装依赖。先由加载日志确定真实模型版本目录；不要直接把 nano-v2-vllm 子目录当作 --model-dir。在能读取实际产物的环境中执行：

```sh
python scripts/audit_voicechat_artifacts.py \
  --model-dir /ACTUAL_MODEL_VERSION_DIR
```

报告中的 vllm_package 定位当前 Python 的顶层包，vllm_registry 给出磁盘 architecture 映射及标记行号。按下一节手工读取 registry 上下文，选定实际模型源码，再执行完整检查：

```sh
python scripts/audit_voicechat_artifacts.py \
  --model-dir /ACTUAL_MODEL_VERSION_DIR \
  --vllm-model-source /ACTUAL_MODEL_IMPLEMENTATION.py \
  --check-tokenizer
```

若脚本在宿主机仓库、模型在现有容器内，可流式传入同一脚本，不复制模型或启动新容器：

```sh
voicechat_container='YOUR_VOICECHAT_CONTAINER'
docker exec -i "$voicechat_container" python - \
  --model-dir /ACTUAL_MODEL_VERSION_DIR \
  --vllm-model-source /ACTUAL_MODEL_IMPLEMENTATION.py \
  --check-tokenizer < scripts/audit_voicechat_artifacts.py
```

--llm-dir 可明确指定不同的 LLM 目录；多个 index 用 --index 指定 LLM 目录内文件。--vllm-package-dir 可指定要审计的磁盘包根目录，但这一模式不证明它属于当前 Python/运行进程。--check-tokenizer 仅使用环境已有 transformers，从模型版本目录的 tokenizer 本地加载，offline/local_files_only、禁 remote code；依赖或加载不可用时保留 unknown，不自动安装/下载。

JSON 输出上限 64 KiB，scope=disk_metadata_only；每项 verified/unknown/failed 均有 reason。verified 只证明所述文件/布局/编码检查，退出码 0 表示没有文件检查失败，**不是工具能力通过**。runtime_class、head_loaded、tool_execution 和 capability_status 始终 unknown，仍需启动加载证据和完整输入后的真实工具往返。CLI读取 safetensors 的 8-byte 前缀及有界 header，不读权重 payload；检查 index/shard 一致性、shape/dtype/offset、function_head 键与独立 embeddings，token ID 仅在已确认的常规全矩阵布局下核对行数。遇到路径逃逸、重复/损坏 index/header 或超限会报告 failed；不计算全权重 hash，不输出 prompt、音频、业务参数或 token 凭证。

以下三节保留逐项人工检查，供解释 CLI 标记及取得实际实现上下文使用。本机没有读取现场权重或执行容器/GPU检查。

### 1. 确认实际模型目录和 vLLM 源码

先在宿主机读取已有启动日志，容器名称替换为实际值：

```sh
docker logs YOUR_VOICECHAT_CONTAINER 2>&1 | grep -F 'Using local tokenizer from model repo:'
docker exec -it YOUR_VOICECHAT_CONTAINER bash
```

进入该加载记录对应的模型版本目录下的 nano-v2-vllm。若已在正确目录，无需重复进入。设置仅供本次检查的路径：

```sh
cd /ACTUAL_MODEL_VERSION_DIR/nano-v2-vllm
export VOICECHAT_LLM_DIR="$PWD"
export VOICECHAT_MODEL_DIR="$(dirname "$VOICECHAT_LLM_DIR")"
```

第一段脚本仅查询顶层 vLLM 包位置，不导入嵌套模型模块：

```sh
PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
import importlib.metadata
import importlib.util
import sys
from pathlib import Path

print("python:", sys.executable)
try:
    print("vllm_version:", importlib.metadata.version("vllm"))
except importlib.metadata.PackageNotFoundError:
    print("vllm_version: metadata unavailable")
spec = importlib.util.find_spec("vllm")
if spec is None or not spec.submodule_search_locations:
    raise SystemExit("This Python cannot locate vllm; check the backend environment")
for location in spec.submodule_search_locations:
    root = Path(location)
    print("vllm_package_root:", root)
    print("registry:", root / "model_executor/models/registry.py")
    for source in sorted((root / "model_executor/models").glob("*nemotron*.py")):
        print("model_candidate:", source)
PY
```

将输出的包路径填入，再查看 registry 映射：

```sh
voicechat_vllm_root='/ACTUAL_VLLM_PACKAGE_ROOT'
grep -nE -C 3 'NemotronHForCausalLM|nemotron_h' \
  "$voicechat_vllm_root/model_executor/models/registry.py"
```

按映射选择源码；只有映射确实为 nemotron_h.py 时使用下面路径：

```sh
voicechat_model_source="$voicechat_vllm_root/model_executor/models/nemotron_h.py"
grep -nE -C 8 \
  'function_head|function_tokens|function_logits|use_function_head|custom_outputs|argmax|pad_token_id|WeightsMapper|stt_model|load_weights' \
  "$voicechat_model_source"
```

有 rg 时可用 rg -n -C 8 替代 grep。输出应能定位 head 创建条件、权重 mapping/load、function output 生成/采样条件；若上下文不足，按命中行号读取完整对应方法。registry、auto_map 和磁盘文件不单独证明运行中的 class；还需现有启动日志/实际加载记录。没有加载记录时将这一项保持待证，不通过启动第二个 engine 获取证明。

### 2. 读取 function head 和输入 embedding 的权重元数据

以下脚本有 index 时只读取其引用 shard，无 index 时列举当前 LLM 目录的 safetensors；同时检查 backend 单独加载的 embeddings.safetensors。只读每个文件的 header，不读取 tensor payload：

```sh
PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
import json
import os
import struct
from pathlib import Path

llm = Path(os.environ["VOICECHAT_LLM_DIR"]).resolve()
model = Path(os.environ["VOICECHAT_MODEL_DIR"]).resolve()

def header(path):
    with path.open("rb") as f:
        prefix = f.read(8)
        if len(prefix) != 8:
            raise ValueError(f"Invalid safetensors prefix: {path}")
        size = struct.unpack("<Q", prefix)[0]
        if not 0 < size <= min(100 * 1024 * 1024, path.stat().st_size - 8):
            raise ValueError(f"Invalid/oversized header: {path}")
        result = json.loads(f.read(size))
    return {k: v for k, v in result.items() if k != "__metadata__"}

indices = sorted(llm.glob("*.safetensors.index.json"))
if len(indices) > 1:
    raise SystemExit("Multiple indices: choose the actual artifact before continuing")
weight_map = None
if indices:
    weight_map = json.loads(indices[0].read_text())["weight_map"]
    files = []
    for name in sorted(set(weight_map.values())):
        path = (llm / name).resolve()
        if not path.is_relative_to(llm):
            raise SystemExit(f"Shard outside model directory: {name}")
        files.append(path)
    print("index:", indices[0])
else:
    files = sorted(llm.glob("*.safetensors"))
if not files:
    raise SystemExit("No LLM safetensors found; verify the actual model directory")

seen = {}
function_keys = []
for path in files:
    tensors = header(path)
    print("weight_file:", path, "tensor_count:", len(tensors))
    for key, meta in tensors.items():
        if key in seen:
            raise SystemExit(f"Duplicate tensor across files: {key}")
        seen[key] = str(path.relative_to(llm))
        if "function_head" in key:
            function_keys.append(key)
        if any(part in key for part in ("function_head", "lm_head", "embed_tokens")):
            print(key, "dtype:", meta["dtype"], "shape:", meta["shape"])
print("function_head_keys:", len(function_keys))
if weight_map is not None:
    mismatch = sorted(k for k in set(weight_map) | set(seen)
                      if weight_map.get(k) != seen.get(k))
    print("index_mismatch_count:", len(mismatch))
    for key in mismatch[:20]:
        print("index_mismatch:", key, weight_map.get(key), seen.get(key))

embedding = model / "embeddings.safetensors"
if not embedding.is_file():
    print("backend_embedding: missing", embedding)
else:
    print("backend_embedding:", embedding)
    for key, meta in header(embedding).items():
        if "embed" in key:
            print(key, "dtype:", meta["dtype"], "shape:", meta["shape"])
PY
```

检查 function head 是否存在、index 是否一致，以及完整未量化 Linear 权重是否具有符合 vocabulary/hidden size 的维度。当前配置是 vocab_size=131072、hidden_size=4480，完整未分片 head 通常为 [131072,4480]；量化/分片布局或词表 padding 应按实际 loader 解释，不仅凭 shape 不同判故障。header 只证明文件内容结构，不能证明 vLLM 实际加载、权重数值有效或工具触发正常。

### 3. 核对实际 tokenizer 的特殊 ID

当前 backend 强制使用模型版本目录下的 tokenizer/，并把 PAD 指定为 <SPECIAL_12>。该脚本仅加载本地 tokenizer，禁止网络下载；角色赋值只作用于本次 Python 进程内存，不新增或保存 token：

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
import json
import os
from pathlib import Path
from transformers import AutoTokenizer

path = Path(os.environ["VOICECHAT_MODEL_DIR"]) / "tokenizer"
if not path.is_dir():
    raise SystemExit(f"Missing actual bundled tokenizer: {path}")
tok = AutoTokenizer.from_pretrained(
    str(path), local_files_only=True, use_fast=True, trust_remote_code=False
)
vocab = tok.get_vocab()
for token in ("<s>", "</s>", "<SPECIAL_12>"):
    if token not in vocab:
        raise SystemExit(f"Required token absent: {token}")
tok.bos_token = "<s>"
tok.eos_token = "</s>"
tok.pad_token = "<SPECIAL_12>"
print(json.dumps({
    "tokenizer_path": str(path), "class": type(tok).__name__, "is_fast": tok.is_fast,
    "base_vocab_size": tok.vocab_size, "tokenizer_length": len(tok),
    "max_vocab_id": max(vocab.values()), "bos_id": tok.bos_token_id,
    "eos_id": tok.eos_token_id, "pad_id": tok.pad_token_id, "unk_id": tok.unk_token_id,
}))
for token in ("<SPECIAL_12>", "<SPECIAL_20>", "<SPECIAL_21>"):
    ids = tok.encode(token, add_special_tokens=False)
    valid = (token in vocab and len(ids) == 1 and ids[0] == vocab[token]
             and (tok.unk_token_id is None or ids[0] != tok.unk_token_id)
             and tok.convert_ids_to_tokens(ids[0]) == token)
    print(json.dumps({"token": token, "encoded_ids": ids,
                      "in_vocab": token in vocab, "single_known_token": valid}))
PY
```

三个标记应存在、各编码为一个已知 token；它们的 ID 与 max_vocab_id 均应处于实际 embedding/head 词表行范围内，再与 vLLM 源码使用的 PAD/SOTC/EOTC 对齐。词表行数大于 tokenizer 长度可能只是 padding，不能直接认定错误。NeMo wrapper 在 fast tokenizer 加载失败时可尝试 slow；这里 fast 加载异常应回报原异常并检查实际加载分支，不安装依赖或补 token 掩盖问题。

回传三段输出及 vLLM 命中方法的上下文即可；无需提供整份权重、业务 prompt、音频或凭据。这些是只读配置/产物核对，真实输入、原生工具闭环和听音仍须按 D21/D07 单独验证。

## 复测与边界

先用原生 HTML 复测问答、停顿、插话和字幕，再用门户测试知识查询、快速/延迟工具结果、Stop playback 后下一轮、取消旧任务、短音频尾部和断线。默认 `MODEL_STEPS_PER_CALL=2` 的相邻 EOS/BOS 按两个 80 ms codec 帧验证；不同批次或无法确定的多轮边界须重新验收，不靠幅值猜测。

VoiceChat 内部工具等待与本项目 30 秒业务预算独立，不能认为本项目 env 会改变 GPU 服务。CPU/Chromium 夹具不证明真实 ASR 或听音质量。部署/性能步骤见 [部署](../../docs/deployment.md)，证据见 [验收](../../docs/acceptance-report.md)。
