# Breeze

> 面向代码场景的 Token 高效 Agent。

Breeze 是一个面向代码仓库的 CLI Agent，目标是在代码问答、检索、编辑、测试等任务中，以更少的 Token 完成同等或更好的任务。它不追求成为通用聊天助手，也不急于构建多 Agent 平台，而是聚焦代码场景，通过仓库地图、混合检索、上下文压缩、缓存与模型路由等机制，降低 LLM 调用成本，并让 Agent 的行为可观测、可评估、可迭代。

当前状态：v0.6.0 — 工具系统、ReAct Loop、CLI、Trace 与基线评估已可用。

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
Agent Loop
  |
  +-- Context Manager
  |     +-- Repo Map                (已实现，效果待验证)
  |     +-- Hybrid Retriever        (计划中)
  |     +-- Rolling Summary         (已实现，低频触发)
  |     +-- Structured State        (计划中)
  |     +-- Token Budget            (已实现)
  |
  +-- LLM Router
  |     +-- Small Model             (计划中)
  |     +-- Large Model
  |     +-- Semantic Cache          (计划中)
  |
  +-- Tool System
  |     +-- list_files              (已实现)
  |     +-- read_file               (已实现)
  |     +-- grep                    (已实现)
  |     +-- repo_map                (已实现)
  |     +-- run_shell               (已实现)
  |     +-- apply_patch             (计划中)
  |     +-- git_diff                (计划中)
  |     +-- run_tests               (计划中)
  |
  +-- Sandbox                       (计划中)
  |
  +-- Trace / Eval                  (已实现)
```

---

## Baseline

Breeze v0.4.0 的朴素 ReAct Agent 在 20 个代码任务上的基线表现。所有后续优化均与该基线对比。

| Metric | Value |
|---|---|
| Model | `openai/Pro/deepseek-ai/DeepSeek-V3.2` |
| Success rate | **80.0%** (16/20) |
| Avg tokens per task | **9,334.0** |
| Avg LLM calls | 3.5 |
| Avg tool calls | 2.5 |
| Avg duration | 36.2s |
| Errors | 1 |

任务集覆盖四类：解释（explain）、定位（locate）、测试（test）、分析（analyze），每类 5 个。任务定义与成功标准见 [`tasks/baseline.yaml`](tasks/baseline.yaml)，完整数据见 [`reports/baseline_v1.md`](reports/baseline_v1.md)。

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

## 路线图

| 版本 | 内容 | 状态 |
|---|---|---|
| v0.1.0 | 项目初始化、LLM 客户端 | 已完成 |
| v0.2.0 | 工具系统（registry、fs、search） | 已完成 |
| v0.3.0 | run_shell 与安全加固 | 已完成 |
| v0.4.0 | Trace、ReAct Loop、CLI | 已完成 |
| v0.5.0 | 20 个任务集与基线报告 | 已完成 |
| v0.6.0 | repo_map、上下文压缩、gitignore 支持 | 已完成 |
| v0.7.0 | 评估稳健性、工具 schema 精简 | 计划中 |
| v0.8.0 | 代码编辑与沙箱 | 计划中 |
| v0.9.0 | 模型路由与语义缓存 | 计划中 |

---

## 快速开始

### 环境要求

- Python 3.11
- Miniconda 或类似环境管理工具
- 硅基流动 API Key

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

### 使用

```bash
# 代码问答
breeze ask "解释 src/breeze/tools/registry.py 的作用"

# 限制步数
breeze ask "找出 read_file 的实现位置" --max-steps 4

# 只输出答案，不显示摘要
breeze ask "运行 pytest 并总结结果" --quiet

# 指定仓库
breeze ask "解释主流程" --repo /path/to/other/repo
```

---

## 评估

```bash
# 列出所有任务
python scripts/eval.py --dry-run

# 运行全部任务
python scripts/eval.py

# 运行单个任务
python scripts/eval.py --task e01

# 用假 LLM 验证脚本本身
python scripts/eval.py --no-llm

# 生成 Markdown 报告
python scripts/report.py
```

输出：

- `reports/baseline_v1.json`：机器可读的原始数据
- `reports/baseline_v1.md`：人类可读的报告
- `traces/*.jsonl`：每次运行的详细 trace

---

## 项目结构

```text
breeze/
├── src/breeze/
│   ├── agent.py          # ReAct 循环
│   ├── cli.py            # CLI 入口
│   ├── config.py         # 配置
│   ├── eval.py           # 评估核心逻辑
│   ├── llm.py            # LLM 客户端
│   ├── trace.py          # JSONL trace
│   └── tools/
│       ├── registry.py   # 工具注册
│       ├── safety.py     # 路径安全
│       ├── fs.py         # list_files, read_file
│       ├── search.py     # grep
│       └── shell.py      # run_shell
├── scripts/
│   ├── eval.py           # 评估入口
│   └── report.py         # 报告生成
├── tasks/
│   └── baseline.yaml     # 20 个基线任务
├── tests/
├── reports/
└── traces/
```
