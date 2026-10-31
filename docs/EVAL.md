# 评估方案（v2：固定任务集 + 自动判定）

## 目标

回答一个具体问题：**改动 Agent 之后，任务解决率是变高还是变低？**

v1 用过 20 个人工评分短任务，问题有三个：

- 人工判定不可复现，同一条回答不同人给出不同结论；
- `fix` 类任务的 `eval_command` 是 `pytest -q`，模型改测试即可通过，
  且没有约束它按"仓库里已有测试"的标准修 bug；
- 不同批次的任务集不同，token 与成功率无法纵向比较。

v2 的做法是把评分完全交给代码：固定 17 个任务，每个任务由 `setup`
制造初始状态，由 `eval_command` 退出码判定结果，`protect` 锁住验收条件。
评估报告只回答三个数：**按 run 的通过率、全通过任务数、每个任务的 token 中位数**。

## 任务集

文件：`tasks/quality.yaml`，`version: 2`。

| 档位 | 任务 | 数量 | 用途 |
|---|---|---|---|
| `core` | fix01-05、feat01-02、refactor01 | 8 | 日常回归、A/B 对比 |
| `chat` | chat01-02 | 2 | 多回合会话（`breeze chat` 路径） |
| `full` | core + feat03、refactor02、ext01-03、mt01-02 + chat01-02 | 17 | 发版前全量 |

按类型：

| 类型 | 任务 | 判定依据 |
|---|---|---|
| fix | fix01-05 | 仓库中**已有**测试恢复通过（问题描述点名失败用例） |
| feature | feat01-03 | `setup` 注入的验收测试通过 |
| refactor | refactor01-02 | 结构测试 + 调用方回归测试通过 |
| external | ext01-03 | Node 检查脚本退出码 0（Agent 无运行时反馈） |
| multiturn | mt01-02 | 复合指令的两个阶段都完成，且回归测试通过 |
| chat | chat01-02 | 多回合会话后最终仓库状态通过 `eval_command` |

fix 类任务的标准就是"已有测试的断言"。例如 fix03 的测试断言
`pytest.raises(ValueError, match="Path traversal")`，任务描述直接写这条，
不引入新的期望值。新任务遵守同一原则；想改行为，先改测试，再把测试
作为验收条件注入。

`core` 与 `full` 的任务 id 完全重叠，`full` 是超集，因此两档的数字可直接比较。

## 单回合（ask）与多回合（chat）

除 `chat` 档外，任务都是单回合：harness 直接构造 `Agent` 跑一次
`run(question)`，等价于 `breeze ask`。`chat` 任务在 YAML 中用 `turns`
给出回合序列，harness 调用 `run_chat`，与 `breeze chat` 走同一条路径：

- Agent 跨回合存活，`TurnLedger` 把上一回合的问答结论注入下一回合；
- 每回合生成独立 trace，指标按回合求和；
- `question` 只用于报告与 dry-run 描述，真正的输入是 `turns`。

这样"第二回合依赖第一回合结论"的场景才可被测量；旧的 `mt01/mt02`
是单回合复合指令，不能替代多回合验证。`chat` 档目前不覆盖会话落盘与
`--resume`，那部分由 `tests/test_session.py` 与手测负责。

## 三条防作弊边界

1. **worktree 隔离**：每个任务在 `git worktree add --detach` 出来的临时
   目录里运行，`settings.repo_root` 指向 worktree。模型改不到主仓库。
2. **protect**：任务声明的受保护路径（几乎都是 `tests/`）在 `run_task`
   之后用 `git status --porcelain -- <paths>` 检查。修改、删除、**新增**
   都算违规——`git diff` 看不到新增文件，`git status` 可以。命中即判失败
   并跳过 `eval_command`，`notes` 里记录脏文件。
3. **{python}**：`eval_command` 中的 `{python}` 在运行时替换为执行评估的
   解释器。避免 PATH 上的另一个 `python` 缺少依赖，导致"任务失败但
   其实只是环境不对"。

`setup` 产生的改动在 Agent 运行前由 harness 提交（`setup: <task_id>`），
因此 protect 检查的基线是"注入缺陷之后"的状态；feature/refactor 注入的
验收测试文件也就不会误报。

## 预检

新增或修改任务后，先跑预检，不消耗 API：

```bash
python scripts/check_tasks.py --suite core     # 或 --task fix01，--keep 保留现场
```

对每个任务，预检在独立 worktree 里执行 `setup`，然后断言：

- `protect` 路径在 setup 之后是干净的——否则 Agent 一开局就违规；
- `eval_command` 在 setup 之后**失败**——否则任务未修复就能通过；
- 有 `setup` 的任务，在 setup 之前应当通过（feature/refactor 的
  "干净树也失败"是预期的，因为验收测试本身由 setup 注入）。

预检失败的任务不进评估集。这是防止"任务越改越松"的唯一自动化手段。

## 运行

评估耗时较长，建议在 PowerShell 中运行（仓库根目录）：

```powershell
# 日常：8 个单回合任务 × 3 次
python scripts/eval.py --suite core --repeat 3 --output reports/quality_core.json

# 多回合会话：2 个 chat 任务 × 3 次
python scripts/eval.py --suite chat --repeat 3 --output reports/quality_chat.json

# 发版前：17 个任务 × 3 次
python scripts/eval.py --suite full --repeat 3 --output reports/quality_full.json

# 只看单个任务（定位失败时）
python scripts/eval.py --task fix03 --repeat 3 --output reports/fix03.json

# 生成 Markdown
python scripts/report.py --input reports/quality_core.json --output reports/quality_core.md
```

`repeat` 建议 ≥ 3：单次运行受模型采样影响很大，报告里的
`tokens_median` 与 `success_rate`（按 run 计）才是可比较的量。
`tokens_samples` 保留原始值，便于判断某次异常是波动还是退化。

比较两个版本时，用同一份 `tasks/quality.yaml`、同一个模型、同一组
`effective_config`（报告 JSON 里有），否则数字不可比。

## 看结果

报告 JSON 的 `summary`：

| 字段 | 含义 |
|---|---|
| `success_rate` | 通过的 run 数 / 总 run 数，主指标 |
| `passes` / `total_runs` | 上面这个比例的分子分母 |
| `all_pass_tasks` | N 次运行全部通过的任务数，衡量稳定性 |
| `graded_tasks` | 自动判定任务数（不含人工评分任务） |
| `total_tokens` / `avg_tokens` / `std_tokens` | token 成本 |
| `avg_llm_calls` / `avg_tool_calls` | 交互轮次 |
| `avg_duration_ms` | 墙钟时间 |

每个任务的 `results[i]`：

| 字段 | 含义 |
|---|---|
| `passes` | 每次 run 的通过与否，如 `[true, false, true]` |
| `pass_count` / `success_rate` / `pass_all` | 该任务的按 run 统计 |
| `tokens_median` / `tokens_std` / `tokens_min` / `tokens_max` | token 分布 |
| `notes` | setup 失败、protect 违规、agent 异常、eval 失败输出尾部 |
| `run_details` | 每次 run 的 `success`、`trace_paths`、token、调用数与答案摘要 |
| `trace_path` | 代表性运行的 trace（chat 任务为最后一回合） |
| `trace_paths` / `turn_answers` | chat 任务每回合的 trace 与答案 |

## 失败分析

1. 看 `notes`：`protected paths modified` 说明模型改测试；`setup failed`
   说明任务本身有问题（跑预检）；`eval failed:` 后面是 pytest 输出尾部，
   直接指出失败的用例与断言。
2. 看 `run_details`：确认是单次波动还是稳定失败，并拿到每次 run 的 trace。
   聚合字段的 token 与调用数是中位数，`run_details` 保留原始值。
3. 打开对应 trace，检查：是否读错文件、是否重复读取、是否在最后一步才编辑、
   是否触发 `max_steps`。trace 的工具结果预览保留头尾，末尾通常就是失败摘要。
4. 修复后只重跑失败任务（`--task`），确认方向后再跑 `--suite core`。

## 已知边界

- 解释、定位、分析类任务没有可靠的自动判定方式，仍留在旧
  `tasks/baseline.yaml` 中供人工抽查，不计入固定基线。
- `ext` 任务依赖本机安装 Node；未安装时预检标 SKIP、评估会判失败，
  跑 full 前先确认 `node -v`。
- worktree 要求 `repo_root` 是 git 仓库且工作区没有未提交改动
  （`git worktree add` 以 HEAD 为基线）。
- 评估集本身是基线：**不修改任务**，否则历史报告不可比。需要新任务时
  追加新 id，并同时加入 `suites`，在 CHANGELOG 里注明基线版本。
