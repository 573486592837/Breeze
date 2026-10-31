# Breeze 变更史

本文保存逐版本的评估数据与失败案例分析。原始数据在 `reports/`，
每版的 JSONL trace 是本机产物、不入库。

## 短任务基线 (v0.5.0)

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

任务集覆盖四类：解释（explain）、定位（locate）、测试（test）、分析（analyze），每类 5 个。任务定义见 [`tasks/baseline.yaml`](../tasks/baseline.yaml)，完整数据见 [`reports/v0.5.0_baseline.md`](../reports/v0.5.0_baseline.md)。

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

## 上下文优化首版 (v0.6.0)

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

完整对比见 [`reports/v0.6.0_context.md`](../reports/v0.6.0_context.md)。

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
- 任务定义见 [`tasks/long.yaml`](../tasks/long.yaml)，patch 见 [`fixtures/`](../fixtures/)

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
repeat=3 中位数，其余长任务集 repeat=1。原始数据已入库：`reports/v1_*.json`，
Markdown 汇总见 `reports/v1_*.md`。

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

## Milestone: Context v2 与多回合会话（v1.1.0 – v1.2.0）

### 背景

v1.0.0 的压缩单位是"轮"（一问一答的工具轮），实测发现长任务的失败不是 Token 省得不够，而是**有效信息被挤出窗口**：模型在编辑前已经丢失了 `old` 原文，于是反复重读、凭记忆写补丁。这一阶段把目标从"省 Token"改成"把任务做对"。

### Context v2（v1.1.0，tag `v1.1.0-context-v2`）

压缩单位改为**用户回合**，工具结果分级：

| 类别 | 工具 | 处理 |
|---|---|---|
| 定位类 | grep、list_files、repo_map | 恒降级为一行指针（事实在 SessionState 索引里） |
| 内容类 | read_file | 回合内原样保留，保证编辑前 `old` 可见 |
| 可再生类 | run_shell、run_tests、git_diff、apply_patch、create_file | 预算溢出时从旧到新降级 |

仅当回合实测 Token（tiktoken cl100k_base）超过 `context.intra_turn_budget = 24000` 才逐级降级；任何降级都会注入 SessionState 状态块。

### Structured State

`SessionState` 由代码从**真实工具结果**解析（不采信模型自述）：符号位置、每个文件的读取范围、编辑结果、最近测试状态、重复读取计数。它同时承担两类问题的答案来源——"这个函数在哪"直接查索引，不必重读文件。

### 多回合会话（v1.2.0，tag `v1.2.0-chat`）

`breeze chat` 让 Agent 跨回合存活。上一回合的工具过程不进入下一回合窗口，只保留 `TurnLedger` 归档的**问题 / 最终答案 / 触碰文件**；事实索引由跨回合累积的 SessionState 继续承担。回合结束落盘到 `~/.breeze/sessions`，`--resume` 同时恢复归档与状态。

### 效果

| 场景 | 指标 | 优化前 | 优化后 |
|---|---|---|---|
| refactor 任务集 | repeat=3 通过率 | 5/6 及以下 | **6/6 PASS** |
| 两回合追问"把刚才的函数列一遍" | LLM 调用 | 5 | **1** |
| 同上 | 工具调用（全文重读） | 4 | **0** |
| 同上 | 回合 Token | 29385 | **3969** |
| 同上 | 函数清单完整性 | 缺 2 个 | 10/10 |

一个诚实的边界：归档策略在**短会话 + 小文件**下比直接保留上一次读取更贵（多花约 900 Token 的归档），它的收益要等会话长到窗口必须封顶时才显现。

同一阶段的架构描述已在 v1.3.0 收：README 的 Context Manager 不再写
"Sliding Window + Token Budget"，改为回合制分级保留 + SessionState /
TurnLedger / SessionStore。

### 遗留

并行工具调用（#28）已实现且 provider 支持，但 DeepSeek-V3.2 在 ReAct 中不主动批量发起调用；换更强模型后再验证，当前记录并搁置。

---

## 会话持久化 (v1.2.1)

`breeze chat` 关掉进程就失忆，长对话仍只能在一个进程内完成。会话改为可存可取：

- `~/.breeze/sessions/<id>.jsonl`（append-only，事件 `session_start` / `turn` / `reset`），
  每回合落盘问答归档、触碰文件、Token 摘要与 **SessionState 快照**。
- `--resume <id|latest>` 同时重建 `TurnLedger` 与 `SessionState`：只恢复结论会让接续后的
  第一句追问重新通读文件，正是此前实测过的失忆成本。
- `breeze chat --list` 列会话（回合数、Token 合计、首问），不需要 API Key 就能用。
- `/new` 写 `reset` 事件作为恢复分界；空会话不再建文件。

实测（两回合 + 跨进程）：接续后追问"我们之前都问过什么"用 1 次 LLM 调用、0 工具、
5,245 tokens 列出全部 5 个历史问题；`/new` 之后同样的问题回答"没看到之前的问题"，
tokens 回落到 2,833（纯 base）。回归 `refactor.yaml` repeat=1 → 2/2 PASS。

过程中暴露一个自己的 bug：测试未隔离 `sessions.dir`，每跑一次 pytest 就往用户真实的
`~/.breeze/sessions` 写 5 个会话文件，导致 `--resume latest` 选中测试垃圾会话而"看起来"
丢了历史。修复 = `tests/conftest.py` autouse 隔离 + 空会话不落盘（见 v1.3.0 的检查清单）。

---

## Release 与开发面分离 (v1.3.0)

wheel 里唯一的开发面泄漏是 `src/breeze/eval.py`（799 行评估框架）：pip 安装的用户
拿到它只能报错，而它依赖 `tasks/`、`fixtures/` 这些根本不在包里的数据。

| 项 | 变化 |
|---|---|
| 评估框架 | `src/breeze/eval.py` → 包外的 `breeze_eval/harness.py`；wheel 不含，sdist 保留 |
| 版本号 | `pyproject` 停在 `0.1.0` 而 tag 已到 `v1.2.1`；改为 `breeze.__version__` 单点定义，`[tool.setuptools.dynamic]` 读取 |
| CLI | 新增 `breeze --version` |
| 发行边界 | `MANIFEST.in`：保留 `tests/`、`breeze_eval/`、`tasks/`、`fixtures/`、`samples/`、`scripts/`，`prune` `traces/`、`reports/` |
| 防回归 | `tests/test_packaging.py` 现场构建 wheel 与 sdist 并断言上述边界 |

验证：wheel 24 → 23 个成员（去掉 `breeze/eval.py`），名为 `breeze-1.3.0-py3-none-any.whl`；
sdist 顶层含 `src/tests/breeze_eval/tasks/fixtures/samples/scripts`，不含 `traces/reports`。

---

## wheel 安装读不到 .env (v1.3.1)

打包分离之后，用 wheel 装的 `breeze` 第一次跑就报 `SILICONFLOW_API_KEY 未设置`，
而运行目录里明明有 `.env`。

原因是 `config.py` 用的是 `load_dotenv()` 的默认查找：python-dotenv 以**调用方文件**
为起点逐级向上找。editable 安装时调用方文件是 `D:\prog\Breeze\src\breeze\config.py`，
向上第一站就是仓库根，`.env` 命中；wheel 安装时起点变成
`<env>\Lib\site-packages\breeze\config.py`，向上走的是 site-packages → env → …，
项目目录根本不在这条链上，于是永远读不到。`python -c` 手测不出来，因为解释器把
交互模式回退成 cwd 查找 —— 只有控制台入口（脚本文件模式）才暴露。

修复：`find_dotenv_path()` 自己从 `Path.cwd()` 逐级向上找，与安装方式无关；
`breeze.config` 模块级加载最近的一份 `.env`，已存在的同名环境变量仍然优先
（`load_dotenv` 默认不覆盖）。

防回归：`tests/test_config.py` 把包复制到独立目录（模拟 site-packages），
用脚本文件子进程从另一个目录运行，断言读到的 key 来自**运行目录**的 `.env`。
这个测试在旧实现下是红的。
