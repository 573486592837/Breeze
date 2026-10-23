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
  |     +-- Summary Findings Extraction      (已实现，v1.0.0)
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
  |     +-- create_file                      (已实现，v0.10.0)
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
| Success rate | **100%** (5/5 per majority vote; 13/15 individual runs) |
| Avg tokens per task | **83,676.4** |
| Std tokens (cross-task) | 38,822.5 |
| Avg LLM calls | 14.8 |
| Avg tool calls | 13.8 |
| Avg duration | 158.1s |

### 单任务数据

| ID | Median tokens | Std | Individual runs |
|---|---|---|---|
| fix01 | 97,389 | 14,190 | [114,607, 86,461, 97,389] |
| fix02 | 50,521 | 10,274 | [63,460, 50,521, 43,167] |
| fix03 | 120,058 | 26,837 | [122,934, 120,058, 75,080] |
| **fix04** | **34,745** | **124** | **[34,626, 34,873, 34,745]** |
| fix05 | 115,669 | 15,198 | [132,122, 101,761, 115,669] |

## Context Compression on Long Tasks (v0.9.0)

v0.9.0 在 v0.8.0 基线上开启了上下文压缩，
`max_recent_tokens=6000`、`min_recent_pairs=2`、`max_recent_pairs=4`。

### 对比

| Metric | v0.8.0 (no compression) | v0.9.0 (6000 budget) | Change |
|---|---|---|---|
| Avg tokens | 83,676.4 | 57,553.6 | **-31.2%** |
| Std tokens | 38,822.5 | 17,977.1 | **-53.7%** |
| Avg LLM calls | 14.8 | 14.2 | -4.1% |
| Success (majority) | 5/5 | 5/5 | same |
| Success (individual) | 11/15 | 14/15 | +3 |

### Per-task

| ID | v0.8.0 | v0.9.0 | Change | Notes |
|---|---|---|---|---|
| fix01 | 97,389 | 57,637 | -40.8% | 调用最多，压缩收益最大 |
| fix02 | 50,521 | 45,662 | -9.6% | 调用少，收益有限 |
| fix03 | 120,058 | 75,205 | -37.4% | 与 fix01 类似 |
| fix04 | 34,745 | 34,351 | -1.1% | 9 次调用，压缩窗口不足 |
| fix05 | 115,669 | 74,913 | -35.2% | 1/3 失败为压缩副作用 |

### 观察

1. **调用次数越多，压缩收益越大**：fix01/fix03/fix05（16-19 次调用）
   收益 -35% 到 -41%，fix02/fix04（10-12 次）收益 -10% 以内。

2. **std 大幅下降**：38,822 → 17,977（-54%）。压缩消除了"长路径"
   vs "短路径"的 token 差异。

3. **成功率反而提升**：11/15 → 14/15。压缩减少了上下文噪声，
   模型判断更聚焦。

4. **唯一失败**：fix05 run 2 在压缩触发点丢失了已推导的结论，
   见 Known Limitation。

### 结论

压缩在长任务上是**净收益**：
- Token 减少 31%
- 方差减少 54%
- 成功率提升

代价是偶发的"结论遗忘"（1/15 的运行），可通过未来改进
summary 的丰富度来缓解，而非回退压缩。

### 观察

1. **fix04 表现出极高的稳定性**（std=124）：当模型识别出缺陷模式时，工具链产生的 token 使用量保持一致。这验证了工具改进：
   - `read_file` 的总行数提示消除了探索性重复读取
   - `run_tests` 的 traceback 提供直接的失败上下文
   - `status: verified` 提供清晰的完成信号

2. **fix01 和 fix05 各失败 1/3，原因是模型行为，而非工具限制。** 模型找到了根因，但选择“报告”而不是“执行”：
   - fix01：模型知道需要恢复 `Path out of bounds:` 消息，但由于“其他测试可能依赖它”而犹豫
   - fix05：模型知道需要将 `-L` 添加到 FORBIDDEN_RG_OPTIONS，但在分析后停止，没有应用补丁
   
   这些是决策边界问题，需要要么：
   - agent 层级的“必须修复”约束（不是工具层级）
   - 更简单的任务设计（移除 match 或明确意图）

3. **任务复杂度驱动 token 方差，而非工具稳定性。** fix04（约 35K）与 fix05（约 116K）相差 3.3 倍，由探索广度驱动，而非工具行为。

### 已知限制：压缩与推理遗忘

在 fix05 第 2 次运行中（3 次中的 1 次），模型在第 8 步识别出了根因（`-L` 不在 `FORBIDDEN_RG_OPTIONS` 中），但在第 18 步触发压缩后（`prompt_tokens` 从 3501 降到 2212），它丢失了该结论，并幻觉出了另一个原因（`rg --help` 被错误阻止）。

根因：`ContextManager._render_summary` 记录了工具名称和参数，但没有记录模型的推理结论。当压缩在关键决策点触发时，模型会基于不完整的摘要重新推导。

这影响约 7% 的单个运行（v0.9.0 中为 1/15），出现在需要在多个步骤中保持某个特定事实的任务上。它不影响整体成功率（14/15 = 93%）。

未来可能的修复方向：
- 用从助手消息中提取的“结论”丰富摘要
- 在摘要中添加“已验证事实”部分
- 在摘要中保留助手消息（而不仅仅是工具调用）
- 或者：接受这是压缩的固有成本

---

## Evaluation Suite Expansion (v0.10.0)

v0.10.0 将评估从"bug 修复"单一形态扩展到 5 类任务，并完成若干速赢修复。

### 新增任务集（repeat=1，DeepSeek-V3.2，压缩开启 6000 budget）

| 任务集 | 任务 | 形态 | 初始结果 | Tokens |
|---|---|---|---|---|
| feature.yaml | feat01-03 | 新工具 + 验收测试注入 | 红/绿已验证，基线待跑 | - |
| refactor.yaml | refactor01-02 | 结构调整 + 回归守卫 | 红/绿已验证，基线待跑 | - |
| multiturn.yaml | mt01-02 | 条件分支复合指令 | mt01 PASS / mt02 FAIL | 71,530 / 76,241 |
| external.yaml | ext01-03 | 非 Python（Node.js 样例） | 3/3 PASS | avg 20,433 |

所有长任务评分使用统一守卫模式：
`git diff --quiet -- <验收文件> && <测试/检查命令>`，防止修改验收条件本身。

### 关键观察：mt02 失败归因

mt02（修复 `safe_path` 后，条件分支应结束）中模型多做了一步——
新建测试文件却写错导入并遗漏 fixture 定义，步数耗尽后在最终答案中
**声称已修复但从未执行编辑**。压缩（prompt 稳定在 2.4K–4.5K）丢失了
早期读到的导入风格与 fixture 位置，导致重复读取与错误重推导。
这与 v0.9.0 的 fix05 遗忘是同一根因，直接催生了 v1.0.0 的
summary "Confirmed findings" 结论提取改进。

### 工具与评估修复

- `create_file` 工具：新文件不再需要绕道 `apply_patch`
- `run_shell` 白名单移除裸 `pytest`，统一走 `run_tests`（特殊参数用 `python -m pytest`）
- `grep` 输出跨环境归一化（rg 存在与否路径格式一致）
- `run_eval` 打印生效配置摘要；未写 `max_steps` 的任务回退到
  `settings.agent_max_steps`（此前硬编码 6），任务起始日志显示实际生效值

---

## Full Evaluation (v1.0.0)

发布时共 35 个评估任务（20 短任务 + 15 长任务，长任务分属 5 类任务集）。
模型 DeepSeek-V3.2，压缩开启（`max_recent_tokens=6000`）；long.yaml 取
repeat=3 中位数，其余长任务集 repeat=1。原始数据见 `reports/v1_*.json`。

### 结果总览

| 任务集 | 类型 | repeat | 成功 | 平均 tokens | 平均 LLM 调用 |
|---|---|---|---|---|---|
| baseline.yaml | 短任务（解释/定位/测试/分析） | 3 | 未自动判定¹ | 15,170 | 3.5 |
| long.yaml | bug 修复 | 3 | 5/5 | 54,997 | 12.4 |
| feature.yaml | 功能添加 | 1 | 3/3 | 54,847 | 10.7 |
| refactor.yaml | 结构重构 | 1 | 1/2 | 76,709 | 14.0 |
| multiturn.yaml | 条件分支复合指令 | 1 | 2/2 | 67,321 | 17.5 |
| external.yaml | 非 Python（Node.js） | 1 | 3/3 | 19,935 | 6.3 |

¹ 短任务的正确性由人工评定，历史判定见 v0.5 基线一节。

长任务合计成功率：**14/15（93.3%）**。

### long.yaml 跨版本演进（同任务集、同模型）

| 指标 | v0.8.0 无压缩 | v0.9.0 压缩 | v1.0.0 压缩+summary 结论 |
|---|---|---|---|
| Avg tokens | 83,676 | 57,554 | 54,997 |
| Avg LLM calls | 14.8 | 14.2 | 12.4 |
| 个体成功 | 11/15 | 14/15 | 15/15 |

- 调用次数下降主要来自工具端提示（`read_file` 总行数与下一符号提示、
  `run_tests` 状态行、`create_file`/`apply_patch` 职责划分、`grep` 路径归一化）。
- 个体成功 14/15 → 15/15 的直接原因是压缩摘要新增 "Confirmed findings" 段
  （B1）：fix05 从此前的 1/3 失败到 3/3 通过。

### 长任务 per-task 明细

| ID | 任务集 | Tokens | LLM 调用 | 结果 |
|---|---|---|---|---|
| fix01 | long | 48,915 | 13 | PASS |
| fix02 | long | 52,669 | 13 | PASS |
| fix03 | long | 63,622 | 13 | PASS |
| fix04 | long | 37,410 | 10 | PASS |
| fix05 | long | 72,367 | 13 | PASS |
| feat01 | feature | 15,529 | 5 | PASS |
| feat02 | feature | 69,727 | 13 | PASS |
| feat03 | feature | 79,286 | 14 | PASS |
| refactor01 | refactor | 76,466 | 14 | PASS |
| refactor02 | refactor | 76,951 | 14 | FAIL |
| mt01 | multiturn | 74,676 | 19 | PASS |
| mt02 | multiturn | 59,965 | 16 | PASS |
| ext01 | external | 15,129 | 5 | PASS |
| ext02 | external | 14,704 | 5 | PASS |
| ext03 | external | 29,972 | 9 | PASS |

（fix01-05 的 tokens 为 3 次运行的中位数。）

### 遗留 Known Limitations

1. **重构类"报告而不执行"**：refactor02 的 13 次读取已完整覆盖两处原子写
   逻辑与验收测试要求，但模型在第 14 步输出了纯文字方案总结并结束，
   未调用 `apply_patch`。与 v0.9.0 fix05 run2、v0.10.0 mt02 同属一个失败
   家族（决策边界选择"报告"而非"执行"）；prompt 规则 9/10 与摘要结论段
   消除了其中两类，纯重构指令下仍会发生（本板 1/15）。
2. **重复读取**：refactor02 压缩触发后重读了 step 3-6 已读的相同区段——
   当被保留的"结论"多为疑问式规划文本（"现在我需要查看 X"）时，
   摘要仍不足以阻止循环，是下一步（Structured State）的动机。
3. **token 方差大**：单轮长任务 tokens std 常见 ±30%，跨版本结论须以
   repeat=3 中位数与成功率为准，单跑数字仅作参考。

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
| v1.1.0 | Small Model 路由、Structured State、Hybrid Retriever | 计划中 |

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
│       ├── edit.py       # apply_patch, create_file, git_diff, git_restore, run_tests
│       └── repo_map.py
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
