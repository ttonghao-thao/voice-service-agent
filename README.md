# Voice Service Agent · 全双工知识客服

系统同时提供天气查询（QWeather）、通用知识查询（Tavily）和专业知识库查询（CueKB）；通用知识检索承担受控兜底。Sophon Live 负责语音理解和原生委派，Router 解析 task，Coordinator / Runtime / Registry 维护提交、权限与执行边界。ASR 仅展示字幕。

当前三类查询接入路径是 Sophon Live + LiveKit。门户沿用英文，语音语言按 Provider 配置。NVIDIA 和旧 WebSocket 代码的范围限制记录在架构中，不代表系统提供仅知识库的产品模式。

从 [AGENTS.md](AGENTS.md) 开始，按任务只选一个文档入口：

| 要做什么 | 入口 |
| --- | --- |
| 查确认需求 | [交付边界](docs/delivery.md) |
| 查当前实现、调用链 | [架构](docs/architecture.md) |
| 查后续设计与实施顺序 | [待实施方案](docs/implementation-plan.md) |
| 制作镜像、部署、迁移、回滚 | [容器部署手册](docs/deployment.md) |
| 查未完成事项 | [任务板](docs/TASK_BOARD.md) |
| 执行验证或查看真实结果 | [唯一验证与执行记录](docs/validation.md) |
| 定位具体接口、Provider 或维护规则 | [按需索引](docs/README.md) |

发布目标为 origin/main。API/Web 使用同一版本标签的预构建镜像，migrate 复用 API 镜像；LiveKit、Sophon、CueKB 独立部署。推送代码不等于已构建、迁移或完成容器/语音验收，部署前先核对手册中的阻塞项。
