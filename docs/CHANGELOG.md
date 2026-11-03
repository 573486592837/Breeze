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

---

## 装好之后跑不起来：litellm 1.106 的未声明依赖 (v1.3.2)

`.env` 修好之后，wheel 安装第一次真正调用模型报：

```
LLM call failed: No module named 'mcp'
```

traceback 落在 litellm 内部：1.106.0 起 `main.py` 的 `completion()` 有

```python
if not skip_mcp_handler and tools:
    from litellm.responses.mcp.chat_completions_handler import acompletion_with_mcp
```

这条链最终 `from mcp.types import CacheableResult`。也就是说**只要传 tools 就要装
第三方 `mcp` 包**，而 litellm 的 METADATA 只把 `mcp` 声明在 `proxy` / `mcp` 两个
extra 下，普通安装不会带上。Breeze 每轮都带 10 个工具 schema，所以必踩。

| 现象 | 结论 |
|---|---|
| 开发环境（litellm 1.103.0）跑通所有评估 | 不是 Breeze 代码问题 |
| 1.106.6 + tools → `ModuleNotFoundError: mcp`；1.105.7 + tools → 走到 HTTP 层 | 引入点在 1.106.0 |
| `pip install --dry-run "litellm>=1.40,<1.106"` | 解析到 1.105.7，可用 |

修复：`pyproject.toml` 把依赖写成 `litellm>=1.40,<1.106`，注释说明理由。
不选 `litellm[mcp]`（为一个用不上的网关子系统拖进 mcp + httpx2 + pydantic>=2.12），
也不选精确锁死版本（评估只需可复现的**上界**，具体版本由环境决定）。

防回归：`tests/test_packaging.py` 解析 pyproject，断言 litellm 的 specifier
不含 1.106.6、含 1.105.7。上游修好后放宽上界时，这条测试会红，提醒重新实测。

已装过 1.106 的环境需要手动回退：`pip install "litellm==1.103.0"`（与评估环境一致）。

---

## Agent 可用性、权限模式与评估基线 (v1.5.0)

自 v1.3.2 起累计 46 个提交。原计划的 v1.4.0 QoL 波次没有单独发版，
与 2048 网页真实试跑后的可用性改进一并发布为 v1.5.0：压缩档位和会话内
配置、更可信的固定评估集、AGENTS.md 与计划注入、按环境裁剪工具、删除
能力、命令权限模式，以及一轮轮 trace 里暴露出来的提示词与执行纪律修正。

### 压缩档位与会话内配置

- `[context] profile` 一次设定压缩强度，同时覆盖回合预算与会话归档预算：
  short（12000 / 800 / 4 / 3000）、medium（24000 / 1500 / 8 / 6000，默认）、
  long（48000 / 3000 / 16 / 12000）；`custom` 完全忽略档位，按显式配置执行。
  显式参数与环境变量优先于档位，`BREEZE_CONTEXT_PROFILE` 可整体切换。
- 三档实测（long.yaml repeat=3，DeepSeek-V3.2）：A 5/5 459K、B 4/5 492K、
  C 5/5 460K tokens，差距在噪声内。结论是档位作为“默认舒适区”，不宣称
  token 收益；评估报告同步记录 `effective_config`。
- `breeze chat` 新增 `/model`、`/trace`、`/profile`、`/set`、`/config`：
  切换模型、trace 前缀、档位与 max_steps / max_edit_attempts / temperature；
  变更以 `config` 事件落盘，`--resume` 恢复，优先级为 CLI 参数 >
  会话内保存值 > 配置文件与环境变量。
- 答案改为 Panel + Markdown 渲染，每回合显示本轮 Token 与 Trace Summary，
  `--trace-name` 支持前缀命名；`chat --list --search <关键词>` 可检索历史会话。

### 固定评估集 v2

- 新的 `tasks/quality.yaml`（17 个自动判定任务，`core` 8 / `chat` 2 / `full` 17）
  取代人工评分的 baseline.yaml 作为默认；`scripts/eval.py` 新增 `--suite`。
- 三条防作弊边界：每个任务在独立 worktree 中执行；`protect` 用
  `git status --porcelain` 检查（改、删、新增测试都算违规）；`{python}`
  固定解释器。评分改为按 run 统计 `passes` / `success_rate` / `pass_all`。
- `chat` 档用多回合 `turns` 走 `run_chat`，覆盖 `breeze chat` 的真实路径；
  报告按 run 输出失败原因，便于定位是模型问题还是任务本身。
- 新增 `scripts/check_tasks.py` 预检（不耗 API）：setup 后 protect 必须干净、
  修复前必须失败。预检当场抓到 feat03 的过期 fixture patch，任务已修。
- 移除 `repo_map`：真实 trace 中从未被调用，只增加 schema 与提示噪声。
- 评估子进程输出优先按 UTF-8 解码；放宽全量测试与 shell 超时，避免长任务
  被误判超时；sdist 测试不再假定 fixtures 存在。

### 运行过程可感知

- 新增 `AgentEvent`（llm_start / llm_end / tool_start / tool_end）与
  `Agent.on_event` 回调；交互式终端显示单行进度，非 TTY 不输出，
  事件不会进入 prompt 或 trace，也不改变追问结果。

### 持久指引：AGENTS.md 与会话计划


长场景开发需要两类信息不随对话增长而丢失：用户给 Agent 的总指引，以及
Agent 自己维护的短期计划。两者都改为“每次调用从持久状态重新渲染”，而不是
留在消息历史里。

- **AGENTS.md**：仓库根目录的 `AGENTS.md` 在会话启动时读入 system prompt。
  system 始终是上下文头部，因此轮数累计和回合内压缩都动不到它；`/new`
  会重新读取，超过 8000 字符截断。指引不能放宽工具白名单与路径限制。
- **`update_plan` 工具**：Agent 用完整的新计划覆盖旧计划，内容存进
  `SessionState.plan`（上限 2000 字符，超长返回 Error 且不覆盖）。每步 LLM
  调用前由 `ContextManager` 重新注入到 system 之后、回合归档之前，并计入
  回合 Token 预算。
- **持久化与查看**：计划随 `turn` 事件里的状态快照落盘，`--resume` 后恢复；
  `breeze chat` 新增 `/plan` 命令查看当前计划。

防回归：`tests/test_instructions.py`、`tests/test_plan_tool.py`，以及
context / state / session / agent / chat 命令测试中的计划注入与 round-trip 断言。

### run_shell 支持 node

用 Breeze 从零开发 2048 网页时，Agent 写出了 `node:test` 用例却无法执行：
`run_shell` 的白名单里没有 node，它只能反复尝试 `npm`、`npx`、`dir`，最后在
回答里声明“工具限制无法验证”。核心逻辑虽然事后人工验证 18/18 通过，但 Agent
自己拿不到执行反馈。

- `node` 加入 `ALLOWED_EXECUTABLES`，允许 `node --test`、`node <仓库内脚本>`
  与 `node --version`；无参数的 node 会进入 REPL，直接拒绝。
- 新增 `FORBIDDEN_NODE_OPTIONS`：内联执行（`-e` / `--eval` / `-p` / `--print`）、
  模块预加载（`-r` / `--require` / `--import` / `--loader`）、调试端口
  （`--inspect*`）与 watch 模式仍被拒绝；短选项拼接形式（如 `-ep`）按前缀拦截。
- 包管理器（npm / npx / yarn / pnpm / bun / deno）被拒时提示改用 `node --test`；
  缺少 node 时给出明确提示而非泛化的 not found。
- 系统提示补充 JS 项目的测试入口，并提醒目录参数在部分 Node 版本上不可用。
- 顺带修正 `run_shell` 的 timeout 参数描述：原先写死"默认为 60"，实际默认值
  是 180。

防回归：`tests/test_shell.py` 新增 node 白名单、危险选项、路径越界与提示文案
测试。

### 最终答案自检：阻止“声称已改但没编辑”

2048 试跑中，用户报告分数错误后，Agent 连续调用 9 次只读工具，没有发出任何
`apply_patch`，却在最终回答里贴出完整的“修复代码”并声明已完成；另一次修复
声称同时改了按钮和键盘处理，实际只改了按钮。两者 trace 里都没有编辑调用，也
没有工具错误，说明这是模型侧的成功幻觉，而不是工具失败。

Agent 主循环在“无工具调用的最终回答”处新增自检：若回答出现第一人称完成声明
（如“我已经修复了”“完成了修复”“I have fixed”），但本回合没有一次成功的
`apply_patch` / `create_file` / `git_restore`（返回 `Error` 不算成功），则把该
回答作为 assistant 消息追加，再注入一条用户提醒，要求实际执行编辑或明确声明
“本轮未修改”，然后继续循环。每回合最多提醒一次，避免死循环；编辑工具结果现在
会回传给主循环用于判定。

系统提示规则 11 同步补充：“已修改 / 已修复 / 已创建”只能对应本轮成功的编辑
工具调用，只读文件不算完成。

防回归：`tests/test_agent.py` 新增完成声明识别、无编辑时重申、仅提醒一次、编辑
成功后不提醒、编辑失败仍提醒等用例。

### 小文件整读

同一次试跑中，模型为了定位 `startNewGame`，对只有 272 行的 `js/ui.js` 先 grep
再分段读取，花了 5 次调用才拿到目标区域。系统提示规则 2 补充：`read_file`
结果会显示文件总行数，文件不超过约 400 行时直接整读，不再 grep + 分段探测。

### run_shell 拒绝命令串联

2048 试跑中，模型多次把多条命令写成 `git status --short && git log --oneline -5`
或 `git ls-files && ls -la`。由于 `run_shell` 以 `shell=False` 执行，`&&` 不会
被解释，而是作为参数传给 git，返回 `unknown option/switch`（exit 129），模型
只能重复尝试，浪费了数步。

- 新增 `SHELL_OPERATORS` 校验：`&&`、`||`、`;`、`|`、`>`、`2>&1` 等命令串联、
  管道与重定向在分词后直接拒绝，并提示“一次只跑一条命令”。
- 引号内的正则（如 `rg "foo|bar"`）不会被误拒。
- 系统提示规则 13 同步说明：一次 run_shell 只跑一条命令；列目录用 list_files，
  确认仓库结构时不要重复列举同一目录。

防回归：`tests/test_shell.py` 新增串联/管道/重定向拒绝与引号内 `|` 放行测试。

### 编辑上限提示改为“如实汇报”

2048 试跑中模型在一回合内按函数逐个打补丁，12 次 `apply_patch` 用满后收到
`Use git_restore to revert changes or report the failure`，容易诱导整体回滚。

- 达到 `max_edit_attempts` 时的错误信息改为：停止编辑，准确汇报已完成与未完成
  的修改，不要声称任务完成。

### API Key 变量名与供应商解耦

- 默认 `api_key_env` 从 `SILICONFLOW_API_KEY` 改为 `BREEZE_API_KEY`，换模型
  供应商时不必再沿用旧名字。
- 新变量为空且仍使用默认名时，自动回退读取 `SILICONFLOW_API_KEY`，已有
  `.env` 无需立即修改；显式配置 `[llm] api_key_env` 或 `BREEZE_API_KEY_ENV`
  时只读指定变量，不回退。
- 同步更新 `.env.example`、`breeze.toml.example`、README 与 CONTRIBUTING。

### 按环境裁剪工具集

Agent 启动时探测一次环境（git 可执行且是否在工作区、node、rg、是否 Python
项目），只把当前可用的工具下发给模型，避免对注定失败的工具反复尝试：

- 非 git 工作区隐藏 `git_diff` / `git_restore`；系统提示同步把“用 git_restore
  回滚”替换为“重新读取后用 apply_patch 修正”。
- 未检测到 Python 项目时隐藏 `run_tests`，环境说明提示改用 `run_shell`
  （如 `node --test`）。
- 环境说明继续列出缺失的外部命令与替代工具。

新增 `capabilities` 模块与 `registry.schemas(only=...)`；git 工作区与 Python
项目检测从 `edit.py` 收敛到该模块，避免两套判断。防回归：`tests/test_capabilities.py`、
`tests/test_registry.py` 的过滤测试，以及 agent 测试中的工具集/提示词断言。

### 文件与目录删除

2048 试跑中，模型为验证遮罩可见性创建了临时脚本 `verify-overlay.mjs`，并在
回答里明确说明“当前工具集没有删除文件的能力，故保留”。补上删除能力：

- 新增 `delete_path(path, recursive=False)`：文件直接删除；空目录可删；非空
  目录必须显式 `recursive=true`。
- 沿用路径与保护规则：拒绝仓库根目录、`.git/`、`traces/`、`reports/` 与
  `.env`；符号链接只删除链接本身，不触碰指向的目标。
- `delete_path` 计入编辑工具集合，参与“声称已改但未编辑”的完成声明自检。
- 提示词补充“删除用 delete_path，不要用 run_shell 的 rm / del”。
- `create_file` 原本就会创建父目录，因此不新增独立建目录工具。

### 命令权限模式

`run_shell` 对非白名单命令原本只有“直接拒绝”一种处理，模型只能反复换写法。
新增三档权限：

- `strict`（默认）：保持原白名单与选项限制。
- `confirm`：命令未通过校验时逐条询问用户；`ask`、eval、管道输入等非交互
  场景没有确认回调，按拒绝处理。
- `open`：跳过可执行文件白名单直接执行，供信任场景使用；仍拒绝命令串联、
  管道与重定向，并保留 cwd 边界、超时与输出上限。

配置入口：`[shell] permission`、`BREEZE_PERMISSION`、`--permission` 与 chat
内 `/set permission`；`/config` 显示当前模式。`shell` 新增无副作用的
`check_command` 与 `run_unlisted`，Agent 按模式选择执行路径；每次判定写入
trace 的 `permission` 事件（不计入 errors）。

防回归：shell 预检/开放执行、agent 三种模式、trace 事件、chat 命令、配置与
CLI 参数测试。

### 权限模式提示词同步

权限模式加入后，系统提示仍写死“可执行文件仅限白名单”，模型在 confirm/open
下不会主动尝试非白名单命令，模式实际用不起来。

- 提示词规则 13 按模式改写：strict 保留白名单；confirm 说明会先询问用户；
  open 说明可执行任意命令（仍为 shell=False、一次只跑一条）。
- Agent 构造与 `/new` 重载提示时传入当前 permission_mode。
- 防回归：三种模式的默认提示与 Agent 系统提示测试。

### 验证

全量 560 passed / 2 skipped，ruff check 全绿；packaging 测试覆盖 wheel/sdist
边界与 `breeze.__version__` 单点（构建产物为 `breeze-1.5.0.dist-info`）。
新能力另做四项真实探针：非 git 目录不再出现 git 工具调用；`delete_path`
删除临时脚本且无其它改动；`open` 模式直接执行 `whoami`（trace 记
`permission allowed/open`）；`confirm` 在非交互输入下拒绝并返回
“no interactive user is available”（trace 记 `permission denied/confirm`）。

---

## 成本与缓存可观测性（未发布）

此前只优化 prompt tokens，没有记录 provider 的缓存命中与单次调用费用；而 prompt
变小不一定等于成本下降，缓存命中与固定开销同样决定账单。本阶段先补测量，不改
上下文策略。

- `LLMResponse` 提取 `cache_hit_tokens` / `cache_miss_tokens` / `cost_usd`，兼容
  DeepSeek 的 `prompt_cache_hit_tokens`、OpenAI 的
  `prompt_tokens_details.cached_tokens` 与 Anthropic 的 `cache_read_input_tokens`；
  字段缺失时保持 `None`，不当成 0 命中。
- `ContextManager.build_with_stats()` 输出每次提示词按
  system / plan / archive / user / state / body 的组成，以及与上一次调用完全相同的
  消息前缀 Token（缓存可复用的上界）。
- trace 的每次 `llm_call` 记录组成、缓存与费用；summary 汇总命中率、前缀复用率与
  费用。CLI 摘要只在 provider 真正报告缓存时显示命中率，并标注
  `cache_reported x/y calls`。
- 新增 `scripts/cache_report.py`，从 trace JSONL 聚合每次运行与总体的调用、组成、
  前缀复用、缓存与费用。

### 基线（2026-11-02，task-cli 两回合 `--sort` / `--reverse`）

| 指标 | 值 |
|---|---|
| 调用 / 工具调用 | 22 / 24 |
| prompt / completion | 149,690 / 6,816 |
| 费用（LiteLLM 报告） | $0.156506 |
| 本地前缀复用 | 46.3%（51,668 / 111,671 消息 Token） |
| provider 缓存报告 | 3/22 次，命中均为 0 |
| 任务结果 | 28 passed，0 errors |

消息组成：system 38.0%、body 48.9%、state 6.4%、archive 3.8%、plan 1.4%、
user 1.5%；再加工具 schema/格式开销 38,019 Token，固定开销约占实际 prompt 的
53.8%。

结论：当前交大 endpoint 不返回稳定的缓存字段，且 `response_cost` 与 total_tokens
严格线性（约 $1/M），本次无法证明缓存能降本。最大可优化项是每次调用的固定开销
（system + 工具 schema）与调用次数。结构上 state 块几乎每次调用都变化且位于 body
之前，是前缀复用的主要破坏点；若改用支持缓存的 provider，应优先把它移到末尾。

### SiliconFlow 对照（2026-11-02，`openai/deepseek-ai/DeepSeek-V4-Flash`）

| 指标 | 交大 deepseek-reasoner | SiliconFlow V4 Flash |
|---|---|---|
| 调用 / 工具 | 22 / 24 | 22 / 22 |
| prompt / completion | 149,690 / 6,816 | 122,982 / 3,613 |
| LLM 延迟 | 208.6s | 54.5s |
| provider 缓存 | 3/22 次，0 命中 | 22/22 次，62.7% |
| 本地前缀复用 | 46.3% | 62.8% |
| 任务结果 | 28 passed | 17 passed |
| errors | 0 | 1（`ls` 被 strict 拦截） |
| 费用 | $0.156506 | LiteLLM 无该模型费用映射 |

- 本地前缀复用与 provider 命中率几乎重合（62.8% vs 62.7%），说明该指标可以作为
  缓存收益的先验。
- provider 命中包含工具 schema，因此单次命中可高于只统计消息的本地前缀；总量仍
  对齐。
- state 块变化仍是最明显的断点：turn 1 step 11 的 state 从 211 增至 252 Token，
  命中从 5,888 掉到 3,584；把 state 移到 body 之后可让前缀继续复用。
- 最后一步 `tools=[]` 会移除工具 schema，导致该次调用完全 miss（step 14：5,156
  prompt，hit=0）；保留 schema 并用 `tool_choice=none` 禁止调用，可保住这次缓存。
- 质量：两次都实现了 `--sort` / `--reverse`；V4 Flash 回合 1 用满 15 步但未补测试，
  回合 2 补 4 个用例后 17 passed。单样本不足以判断质量差异。
- SiliconFlow 的 `api_base` 需要模型名带 `openai/` 前缀；`.env` 里直接写
  `deepseek-ai/DeepSeek-V4-Flash` 会被 LiteLLM 拒绝（Provider NOT provided）。

### 缓存结构优化（2026-11-03，SiliconFlow V4 Flash）

- SessionState 状态块从 body 之前移到末尾：它几乎每次调用都变化，放在前面会
  切断整段历史前缀；移到末尾后，变化只影响自身。
- 最后一步不再传 `tools=[]`，而是保留 schema 并设置 `tool_choice="none"`：
  避免移除 schema 改变前缀导致整次 miss；若 provider 无视 `tool_choice` 仍返回
  工具调用，最终步直接忽略并如实返回文本或步数耗尽提示。

同一两回合任务单样本对照：

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 调用 / 工具 | 22 / 22 | 16 / 17 |
| prompt / completion | 122,982 / 3,613 | 86,334 / 2,767 |
| 本地前缀复用 | 62.8% | **79.7%** |
| provider 命中 | 62.7%（77,056） | 59.3%（51,200） |
| 未命中 prompt | 45,926 | **35,134** |
| 最终步缓存 | 0 命中（5,156 prompt） | 6,144 命中 / 681 miss |
| 任务结果 | 17 passed，1 error | 17 passed，0 errors |

命中率下降是因为总 prompt 和调用次数都变少，且 provider 按 2,048 Token 块计缓存；
**绝对未命中 prompt 下降 23.5%**，这才是账单侧的直接收益。单样本，调用数也受模型
行为影响，不能把差异全部归因于布局。plan 块仍在头部，step 6 更新 plan 时前缀仍会
回落到 system，可作为下一步优化。

### 验证

584 passed / 2 skipped，ruff check 全绿。

## 默认步数预算上调到 15（未发布）

Python 试跑里，任务在 10 步预算下于收尾阶段耗尽步数，只能跨回合续跑。用同一
基线提交（`trial-python-baseline`）建两个 worktree，同提示、temperature=0，仅
改 `--max-steps` 做 A/B：

| 预算 | LLM 调用 | 工具调用 | Token | 结果 |
|---|---|---|---|---|
| 10 | 10 | 14 | 63,271 | 未完成，需第二回合续跑 |
| 12 | 12 | 18 | 83,390 | 一回合完成，20 passed |

第二回合要重发完整上下文（m10 单回合 prompt 已达 59,150 tokens，续跑成本与
首回合同量级），跨回合续跑的总成本反而高于一次更长的单回合；而 m12 恰好用满
12 步才完成，没有余量，换任务或模型波动就会再次触顶。因此把默认 `max_steps`
从 10 上调到 15，留出安全余量。

- `[agent] max_steps` 默认值 10 → 15；`breeze.toml.example`、配置默认值与
  相关测试同步，chat 内仍可用 `/set max_steps` 覆盖（上限 50）。
- 环境变量与旧配置不受影响；已有 `breeze.toml` 里显式写 10 的仍按 10 执行。
- 顺带修正 v1.5.1 章节里重复粘贴的「编辑上限」段落。

## 空答案、编辑上限与 run_tests (v1.5.1)

v1.5.0 发布后的三处修正，不引入新能力：推理模型在最后一步给出空回答、
编辑上限的口径从“调用次数”改为“连续失败次数”，以及 `run_tests` 不再依赖
PATH 上的 `pytest`。

### 最终回答为空时的恢复

2026-11-02 的既有项目多回合试跑（2048 增加撤销功能）里，V4 Flash
（`openai/deepseek-reasoner`）在第 10 步 —— 工具已收起、只要求给最终答案的
那一步 —— 返回了 content 为空的响应。Agent 直接把 `(empty response)` 当作
答案交给用户，会话归档里也存了一条空结论；同一次修复回合则正常给出了“哪些
完成、哪些没完成”的汇报。

- 无工具调用且 content 为空白时，追加一次总结请求（本轮不再提供工具），
  拿到内容即作为最终答案；trace 写入 `guard` 事件说明原因。
- 恢复调用仍为空时保留 `(empty response)` 并结束，不做第二次重试，避免推理
  模型反复空转。
- 防回归：`tests/test_agent.py` 覆盖“恢复成功”和“恢复仍为空只调用一次”。

### 编辑上限改为连续失败次数

同一轮撤销功能试跑里，V4 Flash 在一个步骤内并行发起多个 `apply_patch`；
旧的 `max_edit_attempts` 按调用次数计数（默认 5），第 6 个补丁——包括已经
写好的样式与测试——会被直接拒绝，即使前面全部成功。并行编辑是这类模型的
正常工作方式，按次数限制反而打断任务。

- `[agent] max_edit_attempts` 改名为 `max_edit_failures`，默认 3；对应环境
  变量 `BREEZE_MAX_EDIT_FAILURES`，chat 内用 `/set max_edit_failures`。
  旧键不再读取，已有 `breeze.toml` 需要改名（仓库配置与示例已同步）。
- 只统计写工具（`apply_patch` / `create_file` / `delete_path`）的**连续失败**：
  任意一次成功编辑后清零；连续失败达到上限后，本回合后续写操作被拒绝，并提示
  停止编辑、如实汇报已完成与未完成的部分。
- 读工具与 `git_restore` 不占失败配额；计数按回合独立，不会跨 run 累积。
- 半程“还没有执行任何编辑”的提醒改用**成功编辑**计数，失败补丁不再算作已编辑。
- 防回归：`tests/test_agent.py` 覆盖并行成功编辑不触发上限、连续失败拦截、
  成功清零、跨 run 独立，以及读工具不占配额。

### run_tests 使用当前解释器

Python 试跑（`trials/task-cli-python`）里，模型调用 `run_tests` 得到
`Error: Command not found: pytest`：工具硬编码执行 `pytest`，依赖 PATH 上
恰好有该命令。Breeze 以完整路径启动、或项目环境未激活时，工具虽然可用却
必然失败，模型只能改用 `run_shell` 兜底。

- 优先使用仓库内 `.venv` / `venv` 的解释器（Windows `Scripts/python.exe`，
  其他平台 `bin/python`），否则使用运行 Breeze 的 `sys.executable`，统一以
  `python -m pytest -q` 执行。
- 当前解释器缺少 pytest 时，退回 PATH 上的 `pytest` 一次；仍不可用则返回
  明确错误，提示安装 pytest 或改用 `run_shell`。
- 防回归：`tests/test_edit.py` 覆盖解释器选择、PATH 回退与缺失提示；在试跑
  仓库实测 `run_tests` 返回 `exit_code=0 / 22 passed`。

### 验证

全量 567 passed / 2 skipped，ruff check 全绿；`breeze.__version__` 与
packaging 测试同步到 1.5.1。

- 空答案恢复：单测覆盖“恢复成功”和“恢复仍为空只调用一次”，恢复调用不携带工具。
- 编辑失败上限：受控探针让 V4 给 6 个文件各追加一行注释，模型在同一个步骤内
  并行发出 6 个 `apply_patch`，6/6 成功、0 拒绝、0 errors（旧上限会在第 6 个
  拒绝），6 个文件均正确更新。原《撤销功能》场景复跑只打了 1 个补丁便步数用尽，
  未触及上限，因此不作为该改动的证据。



