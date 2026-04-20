# 本地自定义改动记录

> 本文件记录了 fork 版本相对于 upstream (NousResearch/hermes-agent) 的所有本地改动，
> 包括改动动机、实施逻辑和冲突解决指引。
> 每次合并 upstream 时请参考此文件，确保本地改动不被意外覆盖。

---

## 一、Mem0 记忆系统：从云端 API 切换到本地自建

### 改动动机
原版 Mem0 插件使用 Mem0 Platform API（海外云端服务），存在以下问题：
1. 国内网络访问延迟高，影响记忆提取和搜索速度
2. 嵌入式 Qdrant 导致 gateway 独占锁冲突，其他进程无法同时访问
3. 记忆提取语言默认英文，不适合中文用户
4. 云端 API 需要额外付费

### 实施方案
替换为本地自建：Mem0 v2 开源 SDK + Qdrant 服务端（v1.17.1，systemd 管理）+ DashScope (qwen-max) LLM + DashScope text-embedding-v3 Embedding。

### 涉及文件及冲突解决指引

| 文件 | 改动 | 冲突解决策略 |
|------|------|-------------|
| `plugins/memory/mem0/__init__.py` | 完全重写：从 Mem0 Platform API 客户端改为本地 SDK 初始化。关键变化：`_load_config()` → `_get_local_mem0()`；`Mem0MemoryProvider` → `Mem0LocalMemoryProvider`；删除 rerank 参数；增加 score > 0.3 过滤；配置从 env vars/mem0.json 改为 `~/.hermes/mem0/config.py` | **如果 upstream 修改了此文件，大概率需要保留本地版本**。upstream 可能会改进云端 API 的 bug 或增加功能，但我们的版本是完全不同的架构。合并时以本地版本为主，只在 upstream 有新的 MemoryProvider ABC 变更时才需要适配接口 |
| `plugins/memory/mem0/plugin.yaml` | description 从英文改为中文描述 | 低风险，直接保留本地版本即可 |
| `hermes_cli/doctor.py` | mem0 检查逻辑：从检查 `MEM0_API_KEY` 改为检查 `~/.hermes/mem0/config.py` 和 `DASHSCOPE_API_KEY` | **如果 upstream 修改了 doctor.py 中 mem0 检查部分**，需要将检查逻辑替换为本地版本（检查 config.py + DashScope key），不要恢复云端 API 检查 |
| `tests/conftest.py` | `_CREDENTIAL_NAMES` 中 `MEM0_API_KEY` → `DASHSCOPE_API_KEY` | 低风险，保留本地改动 |
| `tests/plugins/memory/test_mem0_v2.py` | 删除（云端 API 测试，不再适用） | **如果 upstream 新增了此文件或修改了它**，不需要保留，因为本地版本不使用云端 API。如果 upstream 添加了新的本地测试，可以合入 |

### 本地依赖（不在仓库内）
- `~/.hermes/mem0/config.py` — 本地配置文件（LLM、Embedding、Qdrant 参数）
- Qdrant 服务端运行在 `/opt/qdrant/`，由 systemd `qdrant.service` 管理
- `DASHSCOPE_API_KEY` 在 `~/.hermes/.env` 中配置

---

## 二、Terminal 统一执行模式：自动后台化

### 改动动机
原版 terminal 工具需要用户/agent 手动指定 `background=True` 和 `notify_on_complete=True`，但 agent 无法准确预判命令执行时长，经常导致：
1. 长命令在前台超时卡死
2. 短命令被误设为后台，增加不必要的复杂度

### 实施方案
移除 `background` 和 `notify_on_complete` 参数，改为统一执行模式：
- 命令 5 秒内完成 → 立即返回结果
- 命令超过 5 秒 → 自动转入后台，自动开启通知

### 涉及文件及冲突解决指引

| 文件 | 改动 | 冲突解决策略 |
|------|------|-------------|
| `tools/terminal_tool.py` | 删除 `background`/`notify_on_complete` 参数及相关 schema；删除 `_rewrite_compound_background()` 函数（约160行）；增加 5 秒自动后台化逻辑 | **高风险冲突区**。upstream 可能修复 background 相关 bug 或增加新参数。合并时需仔细对比：保留自动后台化核心逻辑，如果 upstream 新增的功能与 background 参数相关，需要适配为自动后台化模式下的等效行为 |
| `skills/` 下多个 SKILL.md | 更新了 codex、hermes-agent、opencode 等技能中 terminal 用法示例（去掉 background=True） | 低风险，本地版本直接保留 |
| `tests/tools/test_terminal_auto_background.py` | 新增 10 个自动后台化测试 | 如果 upstream 也有 terminal 测试改动，需确保两边测试都通过 |
| `tests/tools/test_terminal_tool_pty_fallback.py` | 更新 4 个测试适配新行为 | 低风险 |

---

## 三、会话持久化修复

### 改动动机
Gateway 重启时丢失 agent 消息，导致对话不完整。原因是 agent 循环中没有增量持久化，只在最终结果返回时才写入 session DB。

### 实施方案
1. `gateway/run.py`：gateway shutdown 时 flush 未持久化的 agent 消息
2. `run_agent.py`：在 5 个关键循环续行点增加 `_flush_messages_to_session_db` 调用

### 涉及文件及冲突解决指引

| 文件 | 改动 | 冲突解决策略 |
|------|------|-------------|
| `gateway/run.py` | 增加 shutdown 时 flush 消息的逻辑（约12行新增） | **中等风险**。upstream 的 gateway/run.py 变动非常多（差异超过 600 行）。合并时需要在 shutdown 流程中保留 flush 逻辑，确保消息不丢失。特别注意 shutdown 顺序：flush 消息应在关闭 adapter 之前执行 |
| `run_agent.py` | 在 5 个循环续行点增加 flush 调用 | **高风险**。run_agent.py 与 upstream 差异巨大。合并时需仔细识别哪些是本地新增的 flush 调用，哪些是 upstream 的其他改动。flush 调用的位置应保留在：continuation、codex incomplete、tool calls、empty retry、codex ack 这5个点 |

---

## 四、Prompt Builder 调整

### 改动动机
1. 增加技能修改指引（SKILL_MODIFICATION_GUIDANCE），引导 agent 在修改 skill 时检查 REQUIREMENTS.md/TODO.md/CHANGELOG.md
2. 简化 MEMORY_GUIDANCE（移除"写声明式事实"等约束，因为 mem0 本地模式自己提取，不需要约束 agent 写入格式）
3. skills 缓存 key 优化：disabled skills 移到缓存查询之后计算

### 涉及文件及冲突解决指引

| 文件 | 改动 | 冲突解决策略 |
|------|------|-------------|
| `agent/prompt_builder.py` | 增加 `SKILL_MODIFICATION_GUIDANCE` 常量；简化 `MEMORY_GUIDANCE`；调整 `build_skills_system_prompt` 中 disabled 计算时机 | **中等风险**。如果 upstream 修改了 MEMORY_GUIDANCE 或 skills prompt 构建，需要：保留本地新增的 SKILL_MODIFICATION_GUIDANCE；MEMORY_GUIDANCE 的简化需要评估——如果 upstream 增加了新的有用指引，考虑合入；disabled 计算优化如果 upstream 也改了缓存逻辑，需重新评估 |

---

## 五、飞书（Feishu）适配器改动

### 改动动机
1. 简化 markdown post 渲染：原版的 `_build_markdown_post_rows()` 尝试在代码块边界拆分行，但实际效果不佳（飞书 md 渲染器对代码块有 bug），改为直接用单个 md 元素包裹
2. 简化 bot 身份获取：删除了 `/bot/v3/info` probe 逻辑，直接用 application info endpoint

### 涉及文件及冲突解决指引

| 文件 | 改动 | 冲突解决策略 |
|------|------|-------------|
| `gateway/platforms/feishu.py` | 删除 `_MARKDOWN_FENCE_OPEN_RE`/`_MARKDOWN_FENCE_CLOSE_RE`；简化 `_build_markdown_post_payload`（删除 40 行 `_build_markdown_post_rows`，改为直接包裹）；简化 `_hydrate_bot_identity`（删除 `/bot/v3/info` probe 约 30 行） | **高风险**。feishu.py 是 upstream 活跃修改的文件。合并时需：markdown 渲染部分——如果 upstream 修复了代码块渲染 bug 并改进了 `_build_markdown_post_rows`，可以考虑合入；如果 upstream 只是维护旧逻辑，保留本地简化版。bot 身份获取——如果 upstream 增加了更好的 probe 逻辑，可以合入；否则保留简化版 |

---

## 六、Gateway 通用改动

### 改动动机
1. 删除 Docker volume media delivery 相关代码（我们不使用 Docker 模式）
2. 删除 interrupt control message 常量和 `_is_control_interrupt_message()` 函数

### 涉及文件及冲突解决指引

| 文件 | 改动 | 冲突解决策略 |
|------|------|-------------|
| `gateway/run.py` | 删除 `_DOCKER_VOLUME_SPEC_RE`、`_DOCKER_MEDIA_OUTPUT_CONTAINER_PATHS`、`_INTERRUPT_REASON_*` 常量、`_is_control_interrupt_message()`、`_warn_if_docker_media_delivery_is_risky()` | **中等风险**。如果 upstream 新增了 Docker media 功能或改进了 interrupt 处理，评估是否对我们有用。Docker volume 相关大概率不需要；interrupt 处理如果 upstream 有更好的实现，需要考虑合入 |

---

## 七、项目清理

### 改动动机
删除了 fork 中不需要的文件：.github workflows/issue templates、.plans 文档等。这些是 upstream 仓库管理用的，fork 不需要。

### 涉及文件及冲突解决指引

| 文件 | 改动 | 冲突解决策略 |
|------|------|-------------|
| `.github/` 目录下多个文件 | 全部删除 | **低风险**。合并 upstream 时如果恢复了这些文件，直接忽略即可（我们不需要 CI/issue template） |
| `.plans/openai-api-server.md`、`.plans/streaming-support.md` | 删除 | 低风险，不需要 |
| `agent/gemini_native_adapter.py` | upstream 删除的文件，我们也同步删除了 | 无冲突 |

---

## 八、Skills 精简（不在 git 仓库改动范围内）

### 改动动机
70+ skills 导致 system prompt 过长，加载和解析耗时。精简后 51 个，飞书 23→1，GitHub 7→1。

### 涉及内容
- 飞书系列合并为统一 `lark` skill（核心功能在 SKILL.md，扩展功能在 references/extended-features.md）
- GitHub 系列合并为统一 `github` skill
- 字幕技能合并为 `bilingual-subtitle`
- 删除了 codex、modal-serverless-gpu、lm-evaluation-harness、gguf、stable-diffusion、whisper、peft、amap-cli-skill、macos-app-screen-capture-ocr、macos-wechat-full-chat-export 等低频技能

### 冲突解决
这些改动在 `~/.hermes/skills/` 目录，不在 git 仓库内，所以不会产生 merge 冲突。但注意：
- 如果 upstream 新增了 skill 机制（如加载逻辑变更），需要确保本地精简后的 skill 仍然兼容
- `skills-lock.json` 在仓库内，如果 upstream 更新了它，需要重新生成以反映本地精简后的技能列表

---

## 九、其他小改动

| 文件 | 改动 | 说明 |
|------|------|------|
| `agent/title_generator.py` | 删除 1 行 import | 无关紧要，随 upstream 走即可 |
| `scripts/whatsapp-bridge/package-lock.json` | 版本号更新 | 随 upstream 走即可 |
| `run_agent.py` | 删除 `_get_proxy_from_env()` 函数（14行）、简化 prompt caching 逻辑 | 如果 upstream 保留了这些函数或有改进版本，评估是否需要恢复 |

---

## 合并策略总原则

1. **Mem0 插件**：本地版本是完全不同的架构，合并时几乎总是保留本地版本，只在 MemoryProvider ABC 接口变更时适配
2. **Terminal 自动后台化**：核心逻辑必须保留，upstream 的 background 相关改动需适配为自动后台化模式
3. **会话持久化**：flush 逻辑是功能性修复，必须在合并后保留
4. **Prompt builder**：SKILL_MODIFICATION_GUIDANCE 是本地新增，必须保留；MEMORY_GUIDANCE 的简化需与 upstream 变更合并评估
5. **飞书适配器**：markdown 渲染和 bot 身份获取的简化需根据 upstream 改进程度决定保留或合入
6. **项目清理**：.github 等文件不需要，合并后再次删除即可

### 合并操作建议
```bash
# 1. 拉取 upstream 最新代码
git fetch upstream

# 2. 创建合并分支
git checkout -b merge/upstream-YYYYMMDD

# 3. 尝试合并
git merge upstream/main

# 4. 如果有冲突，参考本文件逐个解决
# 5. 解决后测试关键功能：
#    - mem0 记忆搜索和写入
#    - terminal 短命令立即返回、长命令自动后台
#    - gateway 消息持久化
#    - 飞书消息发送和接收
#    - doctor 检查

# 6. 合并到 main
git checkout main
git merge merge/upstream-YYYYMMDD
```

---

*最后更新：2026-04-20*
*维护者：时允 (windwhinny) + 奇点 (Singularity AI Agent)*