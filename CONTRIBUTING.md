# 贡献指南

Breeze 是个人项目，但仍按"分支开发 → PR 合并 → 打 tag"的流程演进。
提 PR 前请通读本文件。

## 开发环境

- Python >= 3.11（`pyproject.toml` 声明；`pathlib` 部分新 API 仅评估脚本用到时注意版本）
- Node.js 18+（仅运行 `tasks/external.yaml` 评估时需要）
- 安装与运行测试：

```bash
pip install -e ".[dev]"
python -m pytest            # 全量测试
python -m ruff check src tests
python -m ruff format src tests
```

- API Key：仓库根或用户目录放 `.env`，写入 `SILICONFLOW_API_KEY=...`。
  密钥永远不进配置文件，也不进 git。

## 代码规范

- 所有模块、公开函数使用中文 Google 风格 docstring（`Args:` / `Returns:` /
  `Raises:`），私有辅助函数可简化为一行说明。
- 运行时错误消息用中文，`--help` 文案用英文；测试断言依赖中文关键词，
  修改错误消息时同步检查 `tests/`。
- 新配置项必须同时出现在四处：`DEFAULTS`、`Settings.__init__`、
  `breeze.toml.example`（如适用）以及对应测试。
- 安全防线不开放配置：`ALLOWED_EXECUTABLES`、`FORBIDDEN_*_OPTIONS`、
  `PROTECTED_PREFIXES`/`PROTECTED_FILES`、补丁与输出大小限制、工具 schema
  中声明的默认值。修改这些常量需要在 PR 里单独说明理由，并补防回归测试。
- commit 前必须跑 `ruff check` 与 `ruff format`。

## 分支与发布流程

1. 从 `main` 切出 `feat/...`、`fix/...` 或 `docs/...` 分支；不在 main 上直接提交。
2. 推送后创建 PR，由维护者 review 并合并（merge commit，保留分支历史）。
3. 合并后删除远端与本地已合并分支。
4. 里程碑用带注释 tag：`git tag -a v0.x.0-<slug> -m "..."`，命名延续
   `v0.9.0-long-compression` 风格。

## 添加评估任务

任务集在 `tasks/*.yaml`，字段说明见 `src/breeze/eval.py` 的 `TaskSpec`。

- 长任务通过 `setup: [{patch: xxx.patch}]` 注入初始状态，patch 风格与
  `fixtures/` 现有文件一致：以 `--- /dev/null`（新文件）或 `--- a/...`
  开头，不带 `diff --git` 头。
- `eval_command` 一律加完整性守卫，防止模型篡改验收条件：

  ```yaml
  eval_command: "git diff --quiet -- tests/test_xxx.py && pytest -q tests/test_xxx.py"
  ```

- 新任务必须做红/绿验证（不消耗 API）：

  ```bash
  git worktree add --detach .tmp-verify main   # 或目标分支
  cd .tmp-verify
  git apply --whitespace=nowarn fixtures/xxx.patch && git add -A && git commit -qm setup
  <eval_command>                               # 必须失败（红）
  <放参考实现>
  <eval_command>                               # 必须通过（绿）
  ```

  注意模拟 shell 的 PATH 需能找到 `pytest`（评估 worktree 里由框架保证，
  手工模拟时建议用 `python -m pytest` 或把环境 Scripts 目录加入 PATH）。

- 评分只认 `eval_command` 退出码；断言不要依赖 `rg` 等可选外部工具，
  `git` 与 `pytest` 可视为必然存在，Node 仅 external 集依赖。

## Trace 与报告

- 每次运行写 JSONL trace（`traces/`），事件类型：`run_start`、`llm_call`、
  `tool_call`、`run_summary`。分析失败任务时优先看 `llm_call` 的
  `prompt_tokens` 变化（压缩触发点）与 `content_preview`。
- `python scripts/report.py --input reports/xxx.json --output reports/xxx.md`
  生成 Markdown 汇总。

## 提交 PR 检查清单

- [ ] `python -m pytest` 全绿
- [ ] `ruff check` / `ruff format` 无改动
- [ ] 新配置项走 DEFAULTS → Settings → example → 测试 四处
- [ ] 新评估任务完成红/绿验证
- [ ] 用户可见行为变化已同步 README 对应小节
