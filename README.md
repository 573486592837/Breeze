# Breeze

> 面向代码场景的 Token 高效 Agent。

Breeze 是一个面向代码仓库的 CLI Agent，目标是在代码问答、检索、编辑、测试等任务中，以更少的 Token 完成同等或更好的任务。它不追求成为通用聊天助手，也不急于构建多 Agent 平台，而是聚焦代码场景，通过上下文压缩、按环境裁剪的工具集与可重复的固定评估集，降低 LLM 调用成本，并让 Agent 的行为可观测、可评估、可迭代。

当前状态：v1.5.1 — 工具系统、ReAct Loop、多回合会话、持久指引、命令权限模式与固定评估集已可用。

---

## 背景与动机

通用 Agent 在代码场景中经常面临以下问题：

- 将大量无关文件、日志和历史对话塞入上下文，导致 Token 消耗高。
- 工具调用缺少针对性，反复读取整文件或重复检索。
- 上下文只增不减，长任务中成本迅速膨胀。
- 缺少 Token、延迟、成功率等量化指标，优化效果无法证明。
- 多 Agent 架构容易增加调用次数和总 Token，而非真正降低成本。

Breeze 的核心假设是：

> 在代码场景中，Token 效率来自更高的信息密度，而不是更大的上下文窗口。

因此，Breeze 将围绕“代码理解 + 精准检索 + 上下文管理 + 可评估优化”构建。

---

## 核心目标

- **面向代码场景**：代码问答、符号检索、仓库理解、补丁生成、测试执行与修复。
- **降低 Token 消耗**：在相近任务成功率下，显著减少每任务 Token 与总成本。
- **可观测**：记录每次 LLM 调用、工具调用、Token 用量与关键决策。
- **可评估**：建立任务集与基线，用数据验证优化是否有效。
- **单 Agent 优先**：先做好单 Agent 的上下文与工具效率，再考虑多 Agent。
- **安全执行**：文件修改、Shell 命令与测试执行必须在受控环境中进行。

---

## 非目标

Breeze 当前不计划做：

- 通用聊天助手。
- 全功能 Agent 框架。
- 过早引入多 Agent 协作。
- 无沙箱、无限制地执行任意命令。
- 只靠截断上下文来“优化” Token。
- 没有评估集就宣称效果提升。
- 没有约束的自动改写（写操作受路径边界、权限模式与连续失败上限约束）。

---

## 设计原则

1. **信息密度优先**：给模型看最相关、最结构化的信息，而不是最多信息。
2. **评估驱动**：先建立朴素 ReAct 基线，再逐步优化。
3. **先检索，再生成**：通过 grep、list_files 定位代码，再读取片段。
4. **上下文可压缩**：旧对话、工具输出和中间状态应被摘要为结构化状态。
5. **工具结果要裁剪**：错误日志、测试输出、文件内容都应限制大小并结构化。
6. **模型分级**：小模型做分类、摘要、检索；大模型做规划与复杂推理。
7. **全程可追踪**：所有调用写入 JSONL trace，便于复盘与评估。
8. **安全默认开启**：路径限制、命令白名单、超时、沙箱与回滚。

---

## 核心架构

```text
CLI  (breeze ask 单轮 | breeze chat 多回合 / --resume / --list)
  |
  v
Agent Loop  (ReAct；步数预算、半程收敛提醒、连续编辑失败上限)
  |
  +-- Context Manager  策略 v3：回合内只追加，硬上限兜底
  |     +-- 定位类结果小体积透传、大体积保留预览 (grep/list_files)   (已实现)
  |     +-- read_file 原文回合内保留，避免重读并维持前缀缓存          (已实现)
  |     +-- 软预算只降级可再生输出 (run_shell/run_tests/git_diff…)    (已实现)
  |     +-- 硬上限才降级 read_file：大结果优先、保护最近读取           (已实现)
  |     +-- Tool Result Compaction（工具端上限 + 兜底裁剪）        (已实现)
  |     +-- Hybrid Retriever                                     (计划中)
  |
  +-- SessionState   从真实工具结果解析的符号/读取范围/编辑/测试索引  (已实现，v1.1.0)
  +-- TurnLedger     跨回合的问答归档（结论进窗口，工具过程不进）    (已实现，v1.2.0)
  +-- SessionStore   ~/.breeze/sessions 持久化与恢复               (已实现，v1.2.1)
  |
  +-- LLM Router
  |     +-- Large Model / parallel_tool_calls                      (已实现)
  |     +-- Small Model 路由                                        (计划中)
  |     +-- Semantic Cache                                          (计划中)
  |
  +-- Tool System
  |     +-- list_files / read_file / grep                         (已实现)
  |     +-- run_shell（可执行文件与子命令白名单、shell=False）        (已实现)
  |     +-- apply_patch / create_file / delete_path / git_diff / git_restore / run_tests  (已实现)
  |
  +-- Trace      每回合一个 JSONL（run_start/llm_call/tool_call/run_summary）(已实现)
  +-- Eval       包外的 breeze_eval/ + worktree 隔离，不进发行包     (已实现)
```

---

## 评估摘要

逐版本的完整数据、失败案例与归因在 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)，
原始 JSON/Markdown 报告在 `reports/`。这里只留关键节点：

| 版本 | 里程碑 | 关键数据 |
|---|---|---|
| v0.5.0 | 朴素 ReAct 基线（20 短任务） | 80.0% (16/20)，9,334 tokens/任务 |
| v0.9.0 | 长任务引入上下文压缩 | 83,676 → 57,554 tokens/任务，个体 11/15 → 14/15 |
| v1.0.0 | 5 个长任务集全量回归 | 长任务 **14/15（93.3%）** |
| v1.1.0 | Context v2（回合制分级保留）+ Structured State | refactor 集 **6/6 PASS**（此前 5/6 及以下） |
| v1.2.x | 多回合 `breeze chat` + 会话持久化 | 追问"把刚才的函数列一遍"：5 次调用 / 4 次全文重读 / 29,385 tokens → **1 次调用 / 0 工具 / 3,969 tokens** |
| v1.3.0 | release/debug 分离 | wheel 不再含评估框架；版本号与 tag 同源 |

未达标项（同一份 CHANGELOG 里记录）：编辑类任务的**结果正确性**仍受
DeepSeek-V3.2 能力限制——P3 手测中它报告"已完成收紧符号链接校验"，实际写出的
`raise` 被同一个 `try` 的 `except` 吞掉，是个 no-op。

---

## 路线图

| 版本 | 内容 | 状态 |
|---|---|---|
| v0.1.0 | 项目初始化、LLM 客户端 | 已完成 |
| v0.2.0 | 工具系统（registry、fs、search） | 已完成 |
| v0.3.0 | run_shell 与安全加固 | 已完成 |
| v0.4.0 | Trace、ReAct Loop、CLI | 已完成 |
| v0.5.0 | 20 个任务集与基线报告 | 已完成 |
| v0.6.0 | repo_map、上下文压缩、gitignore 支持 | 已完成 |
| v0.7.0 | 编辑能力（apply_patch、git_diff、git_restore、run_tests） | 已完成 |
| v0.7.1 | 分层 TOML 配置 | 已完成 |
| v0.8.0 | 长任务评估（5 个 bug 修复任务） | 已完成 |
| v0.9.0 | 长任务压缩对比、冗余调用优化 | 已完成 |
| v0.10.0 | 任务集扩展（feature/refactor/multiturn/external）、create_file、评估守卫与配置修复 | 已完成 |
| v1.0.0 | 压缩 summary 增强、全量评估、README/CONTRIBUTING 文档收尾 | 已完成 |
| v1.1.0 | 检索纪律（grep-first）、Structured State、工具输出上限对齐、Context v2（回合制分级保留） | 已完成 |
| v1.2.0 | 交互式多回合会话 `breeze chat`、并行工具调用 | 已完成 |
| v1.2.1 | 会话持久化 `~/.breeze/sessions`、`--resume` / `--list` | 已完成 |
| v1.3.0 | release/debug 打包分离（评估框架移出包）、版本单点化与 `breeze --version`、README 变更史拆分 | 已完成 |
| v1.3.1 | `.env` 查找改为从 cwd 向上，wheel 安装也能读到 | 已完成 |
| v1.3.2 | litellm 上界锁到 1.106 之前，避开未声明的 `mcp` 依赖 | 已完成 |
| v1.4.0 | QoL 波次（压缩档位、会话内命令、评估基线 v2、移除 repo_map），并入 v1.5.0 | 已完成 |
| v1.5.0 | Agent 可用性：AGENTS.md 与计划注入、按环境裁剪工具、`delete_path`、命令权限模式、2048 真实试跑修正 | 已完成 |
| v1.5.1 | 空答案恢复、编辑上限改为连续失败次数（`max_edit_failures`）、`run_tests` 使用当前解释器 | 已完成 |

---

## 快速开始

### 环境要求

- Python 3.11
- Miniconda 或类似环境管理工具
- 硅基流动 API Key
- Git（长任务评估需要）

### 安装

```bash
git clone https://github.com/573486592837/breeze.git
cd breeze
conda create -n breeze python=3.11 -y
conda activate breeze
pip install -e ".[dev]"
```

`pip install` 会解析到 `litellm<1.106`：1.106.0 起带 `tools` 的调用会 import 未声明的
`mcp` 包（Breeze 每轮都带工具 schema），已装 1.106.x 的环境用
`pip install "litellm==1.103.0"` 回退。

### 配置

复制 `.env.example` 为 `.env`，填入 API Key：

```bash
BREEZE_API_KEY=sk-your-key-here
BREEZE_MODEL=openai/Pro/deepseek-ai/DeepSeek-V3.2
BREEZE_API_BASE=https://api.siliconflow.cn/v1
LITELLM_LOCAL_MODEL_COST_MAP=True
```

可选：复制 `breeze.toml.example` 为 `breeze.toml`，调整 Agent 参数。

`.env` 的查找位置是**当前工作目录及其父目录**，最近的一份生效（不依赖
安装方式：editable 与 wheel 安装都从运行目录往上找），已存在的同名环境
变量优先级更高；不需要调用模型的命令（`breeze --version`、
`breeze chat --list`）没有 API Key 也能用。

最终答案按 Markdown 渲染后放进绿色面板；`ask` 结束后显示 Trace Summary。
运行期间终端会显示一行状态（第几步、等待模型、正在调用哪个工具、工具耗时）。
等待模型时，状态行还会显示上一轮 `reasoning_content` 的尾部摘要（最多 6 行）
与实时等待秒数；下一步开始后摘要被新的思考替换，不累积历史。状态只读取 Agent
的进度事件，不进入 prompt 与 trace，重定向输出时自动关闭。
`breeze chat` 每回合只报本轮 Token 消耗，退出时再显示整场对话的
Trace Summary（回合数、调用次数、Token 与耗时合计）。`--quiet` 仍输出
纯文本答案，便于脚本消费。

`breeze chat` 的输入支持多行粘贴：整块文本先留在输入缓冲区，按一次回车
才作为一个问题提交，不会逐行拆成多个回合。stdin 不是终端时（管道、评估
脚本）仍按行读取。

### 使用

```bash
# 代码问答
breeze ask "解释 src/breeze/tools/registry.py 的作用"

# 编辑代码
breeze ask "修复 src/breeze/tools/safety.py 的路径逃逸问题"

# 限制步数
breeze ask "找出 read_file 的实现位置" --max-steps 4

# 交互式多回合会话（/new 重置，/help 命令，/exit 退出）
breeze chat

# 给 trace 文件加可辨认的前缀（文件名仍带时间戳与短 ID，不会覆盖）
breeze ask "解释配置加载顺序" --trace-name explain-config
breeze chat --trace-name refactor-run  # chat 默认用 session id 作前缀

# 列出并接续已保存的会话（会话文件默认在 ~/.breeze/sessions）
breeze chat --list
breeze chat --list --search safe_path  # 按关键词过滤（匹配 reset 后的提问与回答）
breeze chat --resume latest
breeze chat --resume 20261028T143041_a9a3ac  # 也可用 breeze chat --list 里的任意 session id

# 版本
breeze --version
```

### 会话内命令

进入 `breeze chat` 后可随时用 `/` 开头的命令调整本次会话：

| 命令 | 作用 |
|---|---|
| `/model [name]` | 查看或切换模型 |
| `/trace [name\|auto]` | 固定 trace 文件（后续回合追加）或恢复每回合独立文件 |
| `/profile [name]` | 查看或切换压缩档位（short / medium / long / custom） |
| `/set <key> <value>` | 调整 max_steps / max_edit_failures / temperature |
| `/config` | 查看模型、会话、trace 与实际预算 |
| `/plan` | 查看 Agent 维护的当前任务计划 |
| `/new` | 重置对话（运行时配置保留） |
| `/help` | 显示命令帮助 |
| `/exit` | 退出（Ctrl+C / Ctrl+D 同样有效） |

配置变更以 config 事件写入当前会话，`--resume` 时自动恢复。优先级为
CLI 参数（`--model`、`--max-steps`、`--trace-name`）> 会话内保存的配置 >
配置文件与默认值。只执行命令、没有跑过回合的会话不会留下空文件。

`breeze chat` 默认把同一会话的全部回合追加到 `traces/<session_id>.jsonl`
（开启完整 prompt 捕获时还有 `<session_id>.messages.jsonl`），`--resume` 后
继续写同一个文件。`--trace-name m3-retry` 直接把文件名固定为
`traces/m3-retry.jsonl`，不再拼接时间戳与 session id，并写回会话配置。

`/trace dev` 可在会话中把 trace 固定为 `traces/dev.jsonl`；若目标文件已存在则
拒绝并提示换名，避免把新会话混进旧记录。`/trace auto` 恢复每回合独立文件
（`<前缀>_<时间戳>_<run_id>.jsonl`），适合需要按回合切分 trace 的评估。

### 持久指引

长场景开发里有两类信息需要“始终在上下文里”，Breeze 把它们固定在系统提示
附近，而不是留在对话历史中：

- **AGENTS.md**：在仓库根目录放一份 `AGENTS.md`，写清本项目的协作约定、
  常用命令与代码风格。会话启动时读入，作为**独立的 system 消息**插在主系统
  提示之后（不再拼接进同一条提示），因此不会随轮数累计或回合内压缩被挤掉。
  使用默认系统提示时，`/new` 会重新读取；超过 8000 字符会截断。它只补充行为
  约定，不能放宽工具白名单与路径限制；显式传入 `system_prompt` 时不会注入。
- **会话计划**：多步任务开始时 Agent 用 `update_plan` 写一份 3-7 步计划，
  每完成一步或方向变化后用完整的新计划覆盖。计划存放在会话状态里，每次
  调用 LLM 前追加到消息末尾（body 之后），同样不会被压缩掉；随回合状态写入
  会话文件，`--resume` 后可恢复。`/plan` 可随时查看。

---

## 配置

Breeze 从分层配置系统读取运行参数。

### 优先级（从低到高）

1. 代码默认值（`src/breeze/config.py` → `DEFAULTS`）
2. 用户配置：`~/.breeze/config.toml`
3. 仓库配置：`<repo>/breeze.toml`
4. 环境变量：`BREEZE_*`
5. CLI 参数（如 `--max-steps`）

`breeze chat` 的会话内命令会写入会话，`--resume` 时在环境变量之后、
CLI 参数之前生效；同一会话下次接续仍沿用这些值。

### 可配置项

- `[llm]`：model、api_base、api_key_env（密钥所在环境变量名）、temperature、
  timeout（读取超时秒数）、connect_timeout（连接超时秒数）、max_retries
  （瞬时网络错误重试次数）
- `[agent]`：max_steps、max_edit_failures（连续编辑失败上限）
- `[shell]`：permission（strict / confirm / open，默认 strict）
- `[tools]`：read_roots（只读参考目录；read_file/list_files/grep 可访问，
  写工具仍限制在仓库内）
- `[context]`：enable、cross_turn（archive / append，默认 archive；append 保留全部
  历史，用于成本对照）、profile（压缩档位）、intra_turn_budget（回合软预算）、
  hard_budget（回合硬上限）、snapshot_max_files / snapshot_max_chars（跨回合
  工作集快照；max_files=0 关闭）、state_max_entries、tool_result_max/head/tail_bytes
- `[chat]`：answer_preview、max_records、max_chars（跨回合归档预算）
- `[sessions]`：dir（会话文件目录，默认 `~/.breeze/sessions`）
- `[trace]`：dir、capture_messages（调试开关，默认 false；开启后把每次发送给 LLM
  的完整 messages/tools/tool_choice/temperature 和完整响应写入独立的
  `<trace>.messages.jsonl`）

### 上下文档位

`[context] profile` 一次设定压缩强度，同时覆盖回合预算与会话归档预算：

| profile | intra_turn_budget | hard_budget | answer_preview | max_records | max_chars |
|---|---:|---:|---:|---:|---:|
| `short` | 12000 | 12000 | 800 | 4 | 3000 |
| `medium`（默认） | 24000 | 24000 | 1500 | 8 | 6000 |
| `long` | 48000 | 48000 | 3000 | 16 | 12000 |
| `custom` | 按下方显式配置 | 按下方显式配置 | | | |

档位只是默认值之上的一层：`intra_turn_budget`、`hard_budget` 与 `[chat]` 下的
显式值（配置文件或环境变量）仍会覆盖它。设为 `custom` 可完全忽略档位预设，
只用手写的数值。也可以用 `BREEZE_CONTEXT_PROFILE` 临时切换
（如 `BREEZE_CONTEXT_PROFILE=short`）。long.yaml 实测 short/medium/long
三档成功率与 Token 差距都在噪声范围内，短档更适合日常。

系统中维护的元状态统一使用 `system` 角色：AGENTS.md、回合归档、工作集快照、
当前计划、Session state、半程收敛/收尾提醒以及预算提示。只有用户真正输入的
问题保留 `user` 角色。这样模型能区分“用户在说话”和“系统在维护上下文”，不会
把状态索引或提醒误当成新的用户要求。这些块按固定顺序插在消息头部或末尾，更新
只影响末尾前缀。若消息最终以 `system` 结尾，会再补一条固定的 user 续跑消息
“继续当前任务。”：部分网关（如 SJTU 的 `deepseek-reasoner`）在最后一条是
`system` 时直接返回空内容，这条消息不含新要求，只用于让请求以可回应的角色结束。

回合内压缩分两级：

- 超过 `intra_turn_budget` 时先降级 `run_shell`、`run_tests`、`git_diff` 等
  可再生输出；`read_file` 原文尽量保留，避免模型为了 `apply_patch` 重新读取。
- 降级不再只留一行 `exit_code`：每条结果带上工具名与关键参数（如命令、测试
  范围），并保留该工具的有效摘要——`run_shell` 保留 stderr 首行与 stdout 首尾，
  `run_tests` 保留 pytest 汇总和失败项，`git_diff` 保留 diffstat。并行调用超过
  一条时，结果前再加 `[call i/n 工具: 参数]`，避免多条匿名结果互相混淆。
- 工具错误（`Error`、`Tool execution failed`、拒绝执行等）保留完整错误正文，
  方便模型改正参数，而不是被压成一行后盲目重试。
- 同一回合内的 `read_file` 只在没有可见副本时重读：只要上一份提示词里仍有
  原文，相同参数的再次读取直接抑制；整文件读取还会覆盖后续落在其范围内的
  行读取（结果被截断时除外）。旧副本已被压成指针时允许再读一次，避免“提示
  使用已有副本但模型看不到内容”的死循环。抑制结果会指出已有副本并附带一段
  预览，引导模型使用而不是重读。`Repeated reads` 提示在未触发压缩时也会注入。
- 同一路径连续第 3 次被抑制时直接终止本回合，返回明确的未完成说明。真实
  试跑中模型会在文件只有一行占位内容时反复请求同一文件，抑制提示不足以让它
  继续；终止比继续消耗步数更好。期间任何工具成功执行都会清零该计数，避免把
  正常的多处读取误判成死循环。
- `apply_patch` / `create_file` / `delete_path` 的结果不参与预算降级：写操作
  不可再生，压缩成 “re-run for more” 会诱导模型重复应用同一 patch。写结果
  本身很短，始终保留原文。
- 同一回合内同参数且已成功的写操作不会再次执行，返回
  `[duplicate edit suppressed]`，提示验证结果或换一处修改。真实试跑中模型曾
  因压缩提示连续 6 次应用完全相同的 patch，把同一函数插入了 6 份。
- `update_plan` 的调用与确认不再留在历史中（计划已固定在消息末尾），避免同一
  份计划重复占预算。
- 消息用量达到 `hard_budget` 的 75% 且本回合还没有编辑时，Agent 追加一次
  收敛提醒：停止扩大阅读范围，先 `update_plan`，随后补测试与实现。它针对的
  是“读完整个仓库才开始动手”导致上下文过长、响应变慢的情况。
- 每次构建提示词都会在消息末尾追加一条 `system` 预算提示：已用/剩余 Token、
  当前仍可见的 `read_file` 文件与 token 体积，以及“超限时旧读取先变成指针”
  的淘汰顺序。提示很小且不参与降级；模型每轮都能看到预算和工作集，在继续
  调研前自己收敛，而不是等压缩发生后才被动重读。预算块之后固定补一条 user
  续跑消息，避免请求以 system 结尾导致部分网关返回空。
- 若仍超过 `hard_budget` 才降级 `read_file`：保护每个文件最近一次读取，并按
  消息顺序从旧到新降级更早的读取，保证新读到的内容不会刚返回就被压掉。同一
  文件后面还有可见副本时，旧指针会改写成 `[read_file superseded]`，引导模型
  使用新副本而不是照着旧指针重读。仍超限时才放开保护并接受最新读取也可能被
  降级；`hard_budget` 默认与软预算相同，低于软预算时按软预算处理。它统计的
  是消息 Token，不含工具 schema，应配置在模型上下文以内。
- 调高 `hard_budget` 会给读结果留出不被压缩的余量，但只在 provider 前缀缓存
  有实际回报时值得。M2 A/B 中 24K 与 64K 的任务完成度相同，64K 多花约 20%
  token，而该 provider 的 `cache_hit_tokens` 始终为 0。
- 边界：一旦触发 `hard_budget`，被降级的 `read_file` 只剩指针，模型仍可能重读
  该范围。现在旧指针会优先丢弃更早的读取，并在同一文件有更新副本时改写为
  `superseded`；若模型仍重读，至少不会因为旧副本不可见而被 L0 错误拦截。要
  彻底避免，应调高 `hard_budget`（缓存有回报时）或把大任务拆成多个用户回合。

### 验证结果核对

Agent 会记录本回合最后一次 `run_tests` / `run_shell` 中的 pytest、ruff 结果。
若最终回答声称“通过/全绿”而最后一次验证实际失败，Agent 会先追加一次纠正提示；
仍无法纠正时在回答前标注 `[验证未通过]` 与失败摘要，避免把失败回合报告成完成。
命令因权限、管道等原因被拒绝而没有真正执行时，不算作验证结果，不会触发纠偏。
达到步数上限时：若最后一步仍返回工具调用，Agent 不执行这些调用，也不把模型
在工具调用前的前言当作答案，而是明确返回
`Reached max steps ... Task not completed.`；若最后一步是纯文本答案，则保留
答案并照常做编辑/验证声明核对。纯文本答案若声称完成了编辑而本轮没有成功的
编辑，则在答案前标注 `[未检测到成功的编辑]`。

### 计划式回答

模型有时不调用工具，而是用“Let me fix ...”“我将先修改 ...”“首先添加 ...”
“继续执行 ...”“给 cli.py 追加 ...”这类下一步计划作为最终回答。Agent 在句首
识别第一人称意图、继续执行与命令式编辑措辞（不匹配最终报告里的“建议下一
步：”或完成声明），发现本轮没有再调用工具时追加一次纠正提示，要求真正执行
计划；只纠正一次，仍以计划结尾则按原样归档。

### 推理模型的思考过程

当 provider 返回 `reasoning_content`（如 DeepSeek reasoner）时，Agent 会把它
随工具调用一起回传到后续请求，避免模型在多步工具循环中忘记目标。最近两步的
思考保留全文，更早的截断为短摘录，防止提示词随步数线性膨胀；思考内容会计入
上下文预算统计。交互式终端在等待模型时显示的思考摘要只做展示，不改变消息内容。

### 跨回合上下文

新回合的第一次请求会注入上一回合遗留的状态索引（reads、symbols、edits、
tests），而不是等到压缩发生才注入。这些事实来自真实工具结果，并且：

- `apply_patch` / `create_file` / `delete_path` / `git_restore` 成功后，该文件的
  读取范围、符号位置、重复读取计数与工作集快照立即失效，避免模型用旧行号编辑；
- 编辑过后，上一次测试结果标记为 stale，提醒重新验证；
- 状态块有条目上限并放在消息末尾，不切断回合内前缀。

回合结束时还会从磁盘捕获**有界的工作集快照**：最多 `snapshot_max_files`
（默认 4）个文件的当前内容，整块不超过 `snapshot_max_chars`（默认 32000
字符，约 8K Token），下一回合第一次请求时作为不可降级的 user 消息注入。
入选顺序是“本回合编辑 > 计划提及 > 跨回合读取频次 > 最近触碰”，因此刚改过
的文件和反复查看的测试文件不必重新读取。注入前用 mtime + size 校验，文件
在回合间被外部改动或删除时旧快照直接丢弃；快照存放在会话旁的
`<session>.snapshots.json`，`--resume` 时恢复，`/new` 时删除。`max_files=0`
可关闭该功能。

### 仓库虚拟环境

`run_shell` 默认只允许系统 PATH 上的 `python -m <module>`。若仓库自带
`.venv` 或 `venv`，可直接写 `.venv\Scripts\python.exe -m pytest` 这类路径；
解释器必须位于仓库内，模块白名单仍然生效。路径前缀的其它解释器会被拒绝并
提示改用 `run_tests`。


跨回合仍由 `cross_turn` 控制：`archive`（默认）在回合边界把已完成回合压成
归档，适合长时间会话；`append` 只追加历史，前缀缓存最好，但上下文会持续增长。
两者都会在单个回合内遵守上面的 `read_file` 保留规则。

### 不可配置项

- 安全白名单（`ALLOWED_EXECUTABLES`、`FORBIDDEN_*_OPTIONS`）
- 保护路径（`.git/`、`.env*` 与 `[trace] dir`；`.env.example` / `.env.sample`
  可读可写）
- 补丁、diff、测试输出的大小限制
- 工具 JSON schema 里声明的默认值

这些是防御边界。改动会削弱安全模型，或导致模型感知与实际行为不一致。
`read_file`、`list_files`、`grep` 也会拒绝读取 `.env*` 与 `.git/`，避免密钥
进入 prompt 或完整 prompt 捕获文件。

`read_file` 遇到超过 `max_bytes` 的单行时会保留头部并标注原始字节数；
`run_shell` 只拒绝未加引号的 `; | & < >`，引号内的同名符号按普通参数处理。
`python -c` 仍被禁用，文件检查请用 `read_file` / `grep`。

API Key 只从环境变量读取，永不写入配置文件；变量名默认是
`BREEZE_API_KEY`，可用 `[llm] api_key_env` 或 `BREEZE_API_KEY_ENV`
改成其他服务商的变量名（如 `OPENAI_API_KEY`），同时用 `model` 与
`api_base` 指向对应服务。旧变量名 `SILICONFLOW_API_KEY` 已不再读取，
仍在使用的 `.env` 需要改名为 `BREEZE_API_KEY`。

### 断网与超时

LLM 请求默认使用 `timeout=600s`、`connect_timeout=15s`、`max_retries=2`。
断网或连接被重置时，连接超时会快速失败，litellm 自动重试；推理模型单次
生成较慢时仍有 10 分钟读取窗口。可用 `BREEZE_LLM_TIMEOUT`、
`BREEZE_LLM_CONNECT_TIMEOUT`、`BREEZE_LLM_MAX_RETRIES` 覆盖。重试耗尽后
该回合会以明确错误结束，已完成编辑保留；当前回合不会写入新的会话记录，
可以重新发送同一问题继续。

### 工具集裁剪

Agent 启动时会探测环境，只向模型下发当前可用的工具：非 git 工作区不会出现
`git_diff` / `git_restore`，未检测到 Python 项目时不会出现 `run_tests`。
系统提示会同步给出替代做法，减少注定失败的工具调用。

### 命令权限模式

`run_shell` 遇到非白名单命令时，按 `[shell] permission`（或 `--permission`、
`BREEZE_PERMISSION`、chat 内 `/set permission`）处理：

| 模式 | 行为 |
|---|---|
| `strict`（默认） | 直接拒绝 |
| `confirm` | 在交互式 chat 中逐条询问；`ask`、eval、管道输入等非交互场景按拒绝处理 |
| `open` | 跳过白名单直接执行（高风险，确认你信任当前任务） |

三种模式都拒绝命令串联/管道/重定向，并保留 cwd 边界、超时与输出上限。
每次权限判定会写入 trace 的 `permission` 事件，便于事后审计。

---

## 评估

固定评估集是 `tasks/quality.yaml`（17 个自动判定任务，`core` / `chat` / `full` 三档）。
评分不看模型自述，只看 `eval_command` 退出码；`protect` 里的路径
（通常是 `tests/`）被改动或新增时直接判失败，因此改测试骗分不再有效。
`chat` 档用 `turns` 定义多回合会话，由 `run_chat` 执行，覆盖 `breeze chat`
的真实使用路径；其余任务为单回合（等价于 `breeze ask`）。

```bash
# 加任务后先预检：每个任务必须"修复前失败、protect 干净"
python scripts/check_tasks.py --suite core

# 日常回归（core，8 个单回合任务）
python scripts/eval.py --suite core

# 多回合会话（chat，2 个任务）
python scripts/eval.py --suite chat --repeat 3

# 发版前全量（full，17 个任务），repeat 3 取中位数与按 run 通过率
python scripts/eval.py --suite full --repeat 3

# 单个任务
python scripts/eval.py --task fix01

# 用假 LLM 验证脚本本身
python scripts/eval.py --no-llm

# 生成 Markdown 报告
python scripts/report.py --input reports/quality_core.json --output reports/quality_core.md
```

历史任务集（`baseline.yaml` 人工评分、`long/feature/refactor/multiturn/external.yaml`）
保留用于对照旧数据，新评估请用 `quality.yaml`。

---

## 项目结构

```text
breeze/
├── src/breeze/
│   ├── agent.py          # ReAct 循环
│   ├── chat.py           # 交互式多回合会话循环
│   ├── chat_commands.py  # 会话内 /model /trace /profile /set
│   ├── cli.py            # CLI 入口
│   ├── config.py         # 分层配置
│   ├── context.py        # 上下文管理
│   ├── llm.py            # LLM 客户端
│   ├── session.py        # 会话持久化（~/.breeze/sessions）
│   ├── state.py          # 跨压缩的会话事实索引
│   ├── trace.py          # JSONL trace
│   └── tools/
│       ├── registry.py
│       ├── safety.py
│       ├── fs.py
│       ├── search.py
│       ├── shell.py
│       └── edit.py       # apply_patch, create_file, delete_path, git_diff, git_restore, run_tests
├── breeze_eval/
│   └── harness.py        # 评估框架（包外的开发面，不进 wheel）
├── scripts/
│   ├── eval.py
│   ├── check_tasks.py    # 任务预检（修复前必须失败）
│   └── report.py
├── tasks/
│   ├── quality.yaml      # 固定评估集（17 个自动判定任务，core/chat/full）
│   ├── baseline.yaml     # 20 个人工评分短任务（旧）
│   ├── long.yaml         # 5 个 bug 修复任务
│   ├── feature.yaml      # 3 个功能添加任务
│   ├── refactor.yaml     # 2 个重构任务
│   ├── multiturn.yaml    # 2 个条件分支复合任务
│   └── external.yaml     # 3 个非 Python（Node.js）任务
├── fixtures/             # bug 注入与验收测试 patch
├── samples/node-app/     # 非 Python 评估样例与检查脚本
├── tests/
├── docs/
│   ├── CHANGELOG.md      # 逐版本评估数据与失败案例分析
│   └── EVAL.md           # 评估方案：任务集、防作弊边界、预检与看数方法
├── reports/              # 评估报告（发布版本入库，其余 gitignore）
├── traces/
├── MANIFEST.in           # sdist 边界（开发面进 sdist、不进 wheel）
├── breeze.toml.example
└── README.md
```

---

## 技术栈

- **CLI**：Typer + Rich
- **LLM**：LiteLLM（路由至硅基流动 OpenAI 兼容端点）
- **代码解析**：Python `ast`
- **Token 计量**：tiktoken（cl100k_base 实测，编码器不可用时回退字符估算）
- **配置**：TOML（`tomllib`）+ python-dotenv
- **测试**：pytest
- **代码规范**：Ruff（lint + format）
- **打包**：setuptools（版本单点来自 `breeze.__version__`，边界见 `MANIFEST.in`）

---

## 开发规范

- 遵循 Conventional Commits 提交格式。
- 代码风格由 Ruff 统一管理，保存时自动格式化。
- 每个功能通过独立 commit 提交，合并前开 PR 自查。
- 测试代码中禁止出现破坏性命令字符串。
- 涉及 shell 命令的测试用 `sys.executable -c` 而非 Unix 命令。
- 每次 commit 前运行 `pytest -q` 与 `ruff check`。
- 评估结果必须记录在 `reports/` 中，不得修改成功标准以美化数字。
- 发布流程、打包边界（哪些代码进 wheel）与版本 bump 规则见
  [`CONTRIBUTING.md`](CONTRIBUTING.md)；逐版本数据见
  [`docs/CHANGELOG.md`](docs/CHANGELOG.md)。
