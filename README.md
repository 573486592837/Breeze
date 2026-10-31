# Breeze

> 面向代码场景的 Token 高效 Agent。

Breeze 是一个面向代码仓库的 CLI Agent，目标是在代码问答、检索、编辑、测试等任务中，以更少的 Token 完成同等或更好的任务。它不追求成为通用聊天助手，也不急于构建多 Agent 平台，而是聚焦代码场景，通过仓库地图、混合检索、上下文压缩、缓存与模型路由等机制，降低 LLM 调用成本，并让 Agent 的行为可观测、可评估、可迭代。

当前状态：v0.8.0 — 工具系统、ReAct Loop、CLI、Trace、编辑能力与长任务评估已可用。

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
- 当前阶段不支持代码编辑（写操作），仅支持只读任务。

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
Agent Loop  (ReAct；步数预算、半程收敛提醒、编辑次数上限)
  |
  +-- Context Manager  策略 v2：压缩单位是"用户回合"，不是"轮"
  |     +-- 定位类结果恒降级为一行指针 (grep/list_files)           (已实现)
  |     +-- 内容类回合内保留原文 (read_file)，超 24K 实测预算才降级  (已实现)
  |     +-- 可再生类最后降级 (run_shell/run_tests/git_diff/apply_patch…)  (已实现)
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
  |     +-- apply_patch / create_file / git_diff / git_restore / run_tests  (已实现)
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
| v1.4.0 | QoL：Small Model 路由、压缩参数默认值调优、Hybrid Retriever | 计划中 |

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
SILICONFLOW_API_KEY=sk-your-key-here
BREEZE_MODEL=openai/Pro/deepseek-ai/DeepSeek-V3.2
BREEZE_API_BASE=https://api.siliconflow.cn/v1
LITELLM_LOCAL_MODEL_COST_MAP=True
```

可选：复制 `breeze.toml.example` 为 `breeze.toml`，调整 Agent 参数。

`.env` 的查找位置是**当前工作目录及其父目录**，最近的一份生效（不依赖
安装方式：editable 与 wheel 安装都从运行目录往上找），已存在的同名环境
变量优先级更高；不需要调用模型的命令（`breeze --version`、
`breeze chat --list`）没有 API Key 也能用。

最终答案按 Markdown 渲染后放进绿色面板；`ask` 与 `chat` 在每回合结束后
都会显示 Trace Summary（调用次数、Token、trace 路径）。`--quiet` 仍输出
纯文本答案，便于脚本消费。

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

---

## 配置

Breeze 从分层配置系统读取运行参数。

### 优先级（从低到高）

1. 代码默认值（`src/breeze/config.py` → `DEFAULTS`）
2. 用户配置：`~/.breeze/config.toml`
3. 仓库配置：`<repo>/breeze.toml`
4. 环境变量：`BREEZE_*`
5. CLI 参数（如 `--max-steps`）

### 可配置项

- `[llm]`：model、api_base、api_key_env（密钥所在环境变量名）、temperature
- `[agent]`：max_steps、max_edit_attempts
- `[context]`：enable、profile（压缩档位）、intra_turn_budget（回合实测 Token 预算）、
  state_max_entries、tool_result_max/head/tail_bytes
- `[chat]`：answer_preview、max_records、max_chars（跨回合归档预算）
- `[sessions]`：dir（会话文件目录，默认 `~/.breeze/sessions`）
- `[trace]`：dir

### 上下文档位

`[context] profile` 一次设定压缩强度，同时覆盖回合预算与会话归档预算：

| profile | intra_turn_budget | answer_preview | max_records | max_chars |
|---|---:|---:|---:|---:|
| `short` | 12000 | 800 | 4 | 3000 |
| `medium`（默认） | 24000 | 1500 | 8 | 6000 |
| `long` | 48000 | 3000 | 16 | 12000 |
| `custom` | 按下方显式配置 | | | |

档位只是默认值之上的一层：`intra_turn_budget` 与 `[chat]` 下的显式值
（配置文件或环境变量）仍会覆盖它。设为 `custom` 可完全忽略档位预设，
只用手写的数值。也可以用 `BREEZE_CONTEXT_PROFILE` 临时切换
（如 `BREEZE_CONTEXT_PROFILE=short`）。long.yaml 实测 short/medium/long
三档成功率与 Token 差距都在噪声范围内，短档更适合日常。

### 不可配置项

- 安全白名单（`ALLOWED_EXECUTABLES`、`FORBIDDEN_*_OPTIONS`）
- 保护路径（`PROTECTED_PREFIXES`）
- 补丁、diff、测试输出的大小限制
- 工具 JSON schema 里声明的默认值

这些是防御边界。改动会削弱安全模型，或导致模型感知与实际行为不一致。

API Key 只从环境变量读取，永不写入配置文件；变量名默认是
`SILICONFLOW_API_KEY`，可用 `[llm] api_key_env` 或 `BREEZE_API_KEY_ENV`
改成其他服务商的变量名（如 `OPENAI_API_KEY`），同时用 `model` 与
`api_base` 指向对应服务。

---

## 评估

```bash
# 冒烟检查（短任务，仅快速验证链路；自 v1.0.0 起不计入发布回归）
python scripts/eval.py --tasks tasks/baseline.yaml

# 发布回归（5 个长任务集）
python scripts/eval.py --tasks tasks/long.yaml --repeat 3
python scripts/eval.py --tasks tasks/feature.yaml
python scripts/eval.py --tasks tasks/refactor.yaml
python scripts/eval.py --tasks tasks/multiturn.yaml
python scripts/eval.py --tasks tasks/external.yaml

# 单个任务
python scripts/eval.py --tasks tasks/long.yaml --task fix01

# 用假 LLM 验证脚本
python scripts/eval.py --no-llm

# 生成报告
python scripts/report.py --input reports/long_baseline.json --output reports/long_baseline.md
```

---

## 项目结构

```text
breeze/
├── src/breeze/
│   ├── agent.py          # ReAct 循环
│   ├── chat.py           # 交互式多回合会话循环
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
│       └── edit.py       # apply_patch, create_file, git_diff, git_restore, run_tests
├── breeze_eval/
│   └── harness.py        # 评估框架（包外的开发面，不进 wheel）
├── scripts/
│   ├── eval.py
│   └── report.py
├── tasks/
│   ├── baseline.yaml     # 20 个短任务
│   ├── long.yaml         # 5 个 bug 修复任务
│   ├── feature.yaml      # 3 个功能添加任务
│   ├── refactor.yaml     # 2 个重构任务
│   ├── multiturn.yaml    # 2 个条件分支复合任务
│   └── external.yaml     # 3 个非 Python（Node.js）任务
├── fixtures/             # bug 注入与验收测试 patch
├── samples/node-app/     # 非 Python 评估样例与检查脚本
├── tests/
├── docs/
│   └── CHANGELOG.md      # 逐版本评估数据与失败案例分析
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
