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
3. **先检索，再生成**：通过 repo map、grep、混合检索定位代码，再读取片段。
4. **上下文可压缩**：旧对话、工具输出和中间状态应被摘要为结构化状态。
5. **工具结果要裁剪**：错误日志、测试输出、文件内容都应限制大小并结构化。
6. **模型分级**：小模型做分类、摘要、检索；大模型做规划与复杂推理。
7. **全程可追踪**：所有调用写入 JSONL trace，便于复盘与评估。
8. **安全默认开启**：路径限制、命令白名单、超时、沙箱与回滚。

---

## 核心架构

```text
CLI
  |
  v
Agent Loop
  |
  +-- Context Manager
  |     +-- Sliding Window + Token Budget    (已实现)
  |     +-- Tool Result Compaction           (已实现)
  |     +-- Repo Map                         (已实现，效果待验证)
  |     +-- Hybrid Retriever                 (计划中)
  |     +-- Structured State                 (计划中)
  |
  +-- LLM Router
  |     +-- Large Model                      (已实现)
  |     +-- Small Model                      (计划中)
  |     +-- Semantic Cache                   (计划中)
  |
  +-- Tool System
  |     +-- list_files / read_file / grep    (已实现)
  |     +-- repo_map                         (已实现，效果待验证)
  |     +-- run_shell                        (已实现，白名单)
  |     +-- apply_patch / git_diff / git_restore / run_tests  (已实现)
  |     +-- create_file                      (计划中)
  |
  +-- Worktree Isolation                     (已实现，用于评估)
  |
  +-- Trace / Eval                           (已实现)
```

---

## Baseline: 短任务

v0.5.0 的朴素 ReAct Agent 在 20 个代码任务上的基线表现。

| Metric | Value |
|---|---|
| Model | `openai/Pro/deepseek-ai/DeepSeek-V3.2` |
| Success rate | **80.0%** (16/20) |
| Avg tokens per task | 9,334.0 |
| Avg LLM calls | 3.5 |
| Avg tool calls | 2.5 |
| Avg duration | 36.2s |
| Errors | 1 |

任务集覆盖四类：解释（explain）、定位（locate）、测试（test）、分析（analyze），每类 5 个。任务定义见 [`tasks/baseline.yaml`](tasks/baseline.yaml)，完整数据见 [`reports/baseline_v1.md`](reports/baseline_v1.md)。

### 失败案例分析

4 个失败任务暴露出朴素 ReAct 的三类问题：

| ID | 问题类型 | 具体表现 |
|---|---|---|
| e05 | 信息提取深度不足 | 读了 `llm.py`，但未注意到 `model` 需带 `openai/` 前缀才能让 LiteLLM 路由到硅基流动 |
| a02 | 技术表述错误 | 把上下文 Token 增长描述为“指数增长”，实际应为线性 |
| a03 | 先验知识编造 | 编造不存在的符号链接漏洞，未基于代码观察（`safe_path` 实际使用 `Path.resolve()`） |
| a05 | 分析视角缺失 | 能描述现状，但未指出“这是优化点” |

这三类问题是后续优化的直接目标：更强的检索、更聚焦的上下文、更明确的提示。

### 优化目标

**在成功率不下降的前提下，显著降低 `avg tokens per task`。** 当前 9,334 tokens/任务的构成中，大部分来自历史对话的重复传递，这是后续上下文压缩和检索要攻克的核心。

## Context Optimization

v0.6.0 引入三项上下文优化：

1. **repo_map 工具**：用 AST 提取符号，输出紧凑项目结构。
2. **滑动窗口 + Token 预算**：保留最近 K 轮，旧轮压缩成结构化摘要。
3. **工具结果压缩**：超过 4KB 的结果保留头尾，中间省略。
4. **.gitignore 支持**：`list_files` 和 `repo_map` 忽略被 git 忽略的路径。

### 效果对比

| Metric | Baseline (v0.5.0) | Context (v0.6.0) | Change |
|---|---|---|---|
| Success rate | 80.0% (16/20) | **90.0% (18/20)** | +12.5% |
| Avg tokens per task | 9,334.0 | 9,143.3 | -2.0% |
| Avg LLM calls | 3.5 | 3.95 | +12.9% |
| Avg tool calls | 2.5 | 2.95 | +18.0% |

完整对比见 [`reports/context_v1.md`](reports/context_v1.md)。

**结论：**

- 成功率提升 2 个任务（a03 从失败转通过，模型不再编造漏洞）。
- Token 变化在评估噪声范围内（同配置多次运行波动 ±20%）。
- 压缩机制在当前短任务上低频触发（p90 prompt_tokens = 3,988），
  主要价值预期在多轮、长任务场景。
- `repo_map` 增加了 1 步探索，未减少 Token，需要后续重构或移除。

---

## Long Task Baseline (v0.8.0)

v0.8.0 引入编辑能力（`apply_patch`、`git_diff`、`git_restore`、`run_tests`）
和长任务评估。5 个 bug 修复任务在禁用压缩的朴素 ReAct 下运行，`--repeat 3` 取中位数。

### 评估设计

- 每个任务在独立 git worktree 中运行
- `setup` 用 patch 注入 bug
- `eval_command` 运行对应测试文件，退出码 0 判定成功
- 任务定义见 [`tasks/long.yaml`](tasks/long.yaml)，patch 见 [`fixtures/`](fixtures/)

### 结果

| Metric | Value |
|---|---|
| Model | `openai/Pro/deepseek-ai/DeepSeek-V3.2` |
| Success rate | **100% (5/5)** |
| Avg tokens per task | 94,310.6 |
| Std tokens (cross-task) | 29,170.28 |
| Avg LLM calls | 14.0 |
| Avg tool calls | 13.0 |
| Avg duration | 253.4s |

### 单任务数据

| ID | Tokens (median) | Std | 采样 |
|---|---|---|---|
| fix01 | 81,475 | 12,480.84 | 60,955 / 81,475 / 83,524 |
| fix02 | 55,492 | 3,618.19 | 49,266 / 55,492 / 55,573 |
| fix03 | 122,439 | 2,368.31 | 118,391 / 122,439 / 122,545 |
| fix04 | 87,852 | 1,000.56 | 86,978 / 87,852 / 88,974 |
| fix05 | 124,295 | 1,069.27 | 122,488 / 124,295 / 124,382 |

### 观察

1. **所有任务都是 14 次 LLM 调用、13 次工具调用。** 高度一致的模式说明模型走了固定流程，可能包含冗余验证（如测试通过后再次运行 `pytest -v` 或重新读取源码）。

2. **任务间 Token 差异 2.25 倍。** fix02 最低 55K，fix05 最高 124K。涉及 `test_shell.py` 的 fix03 和 fix05 都超过 120K，可能与 33 个测试的输出量有关。

3. **任务内方差分化。** fix01 的 std 达 12,480（均值 15%），其余四个小于 4%。fix01 的不稳定可能来自 `apply_patch` 的 `old` 匹配失败次数差异。

4. **评估覆盖不足的隐患。** fix01 的答案提到"还有一个测试 `test_run_shell_rejects_cwd_escape` 失败，是因为错误消息格式不匹配"。模型修改 `ValueError` 消息与 `test_shell.py` 期望不一致，但 `eval_command` 只跑 `test_fs.py`，未捕获这个回归。**后续应扩展 `eval_command` 的测试范围。**

### 对比短任务的观察

- 长任务 Token 是短任务的 10 倍（94K vs 9K），LLM 调用是 4 倍（14 vs 3.5）。
- 压缩机制在长任务上应该触发更频繁，但当前基线是禁用状态，需要后续对比。
- 12 到 14 次调用才完成 4 到 5 行代码的修复，说明探索和验证成本远高于修改成本。

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
| v0.9.0 | 长任务压缩对比、冗余调用优化 | 计划中 |
| v1.0.0 | 功能添加任务、模型切换、沙箱执行 | 计划中 |

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

### 配置

复制 `.env.example` 为 `.env`，填入 API Key：

```bash
SILICONFLOW_API_KEY=sk-your-key-here
BREEZE_MODEL=openai/Pro/deepseek-ai/DeepSeek-V3.2
BREEZE_API_BASE=https://api.siliconflow.cn/v1
LITELLM_LOCAL_MODEL_COST_MAP=True
```

可选：复制 `breeze.toml.example` 为 `breeze.toml`，调整 Agent 参数。

### 使用

```bash
# 代码问答
breeze ask "解释 src/breeze/tools/registry.py 的作用"

# 编辑代码
breeze ask "修复 src/breeze/tools/safety.py 的路径逃逸问题"

# 限制步数
breeze ask "找出 read_file 的实现位置" --max-steps 4
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

- `[llm]`：model、api_base、temperature
- `[agent]`：max_steps、max_edit_attempts
- `[context]`：enable、max_recent_tokens、min/max recent pairs、工具结果 head/tail 大小
- `[trace]`：dir

### 不可配置项

- 安全白名单（`ALLOWED_EXECUTABLES`、`FORBIDDEN_*_OPTIONS`）
- 保护路径（`PROTECTED_PREFIXES`）
- 补丁、diff、测试输出的大小限制
- 工具 JSON schema 里声明的默认值

这些是防御边界。改动会削弱安全模型，或导致模型感知与实际行为不一致。

`SILICONFLOW_API_KEY` 从环境变量读取，永不写入配置文件。

---

## 评估

```bash
# 短任务基线
python scripts/eval.py --tasks tasks/baseline.yaml

# 短任务带重复
python scripts/eval.py --tasks tasks/baseline.yaml --repeat 3

# 长任务（需要 git 仓库）
python scripts/eval.py --tasks tasks/long.yaml --repeat 3

# 单个任务
python scripts/eval.py --tasks tasks/long.yaml --task fix01

# 用假 LLM 验证脚本
python scripts/eval.py --no-llm

# 生成报告
python scripts/report.py --input reports/long_baseline.json --output reports/long_baseline.md

# 对比两份报告
python scripts/compare.py --baseline reports/long_baseline.json --current reports/long_compressed.json
```

---

## 项目结构

```text
breeze/
├── src/breeze/
│   ├── agent.py          # ReAct 循环
│   ├── cli.py            # CLI 入口
│   ├── config.py         # 分层配置
│   ├── context.py        # 上下文管理
│   ├── eval.py           # 评估核心（含 worktree 隔离）
│   ├── llm.py            # LLM 客户端
│   ├── trace.py          # JSONL trace
│   └── tools/
│       ├── registry.py
│       ├── safety.py
│       ├── fs.py
│       ├── search.py
│       ├── shell.py
│       ├── edit.py       # apply_patch, git_diff, git_restore, run_tests
│       └── repo_map.py
├── scripts/
│   ├── eval.py
│   ├── report.py
│   └── compare.py
├── tasks/
│   ├── baseline.yaml     # 20 个短任务
│   └── long.yaml         # 5 个 bug 修复任务
├── fixtures/             # 长任务的 bug 注入 patch
├── tests/
├── reports/
├── traces/
├── breeze.toml.example
└── README.md
```

---

## 技术栈

- **CLI**：Typer + Rich
- **LLM**：LiteLLM（路由至硅基流动 OpenAI 兼容端点）
- **代码解析**：Python `ast`
- **配置**：TOML（`tomllib`）+ python-dotenv
- **测试**：pytest
- **代码规范**：Ruff（lint + format）

---

## 开发规范

- 遵循 Conventional Commits 提交格式。
- 代码风格由 Ruff 统一管理，保存时自动格式化。
- 每个功能通过独立 commit 提交，合并前开 PR 自查。
- 测试代码中禁止出现破坏性命令字符串。
- 涉及 shell 命令的测试用 `sys.executable -c` 而非 Unix 命令。
- 每次 commit 前运行 `pytest -q` 与 `ruff check`。
- 评估结果必须记录在 `reports/` 中，不得修改成功标准以美化数字。
