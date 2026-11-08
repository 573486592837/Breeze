# Breeze 架构与流程

> 基于当前源码整理，不绑定具体版本号。行为变更时同步更新本文；与代码不一致时
> 以 `src/breeze/` 为准。README 里标为“计划中”的 Hybrid Retriever、小模型路由、
> Semantic Cache 未画入主链路。

## 1. 一句话架构

Breeze 是单 Agent 的 CLI 代码助手：**CLI 负责入口与会话，Agent 负责 ReAct 循环，
ContextManager 负责把“当前回合”压进 Token 预算，SessionState/TurnLedger 负责跨压缩、
跨回合保留事实，Tool System 负责受控执行，RunTrace 负责全程可观测。**

```text
breeze ask ──> cli.ask ──> Agent.run ──┬─> ContextManager.build_with_stats ──> LLMClient
                                       ├─> Tool Registry ──> fs / search / shell / edit
                                       ├─> SessionState.update（每次工具结果后）
                                       └─> TurnLedger.record（回合结束时）
                                                  │
                                                  └─> RunTrace（JSONL）
```

## 2. 分层组件图

```mermaid
flowchart TB
    subgraph L1["入口层"]
        direction LR
        CLI["cli.py<br/>Typer：ask / chat"]
        INPUT["chat_input.py<br/>终端输入 / 多行粘贴"]
        CMD["chat_commands.py<br/>ChatController"]
    end

    subgraph L2["编排层"]
        direction LR
        CHAT["chat.py<br/>多回合循环"]
        AGENT["agent.py<br/>ReAct Loop"]
        CAP["capabilities.py<br/>环境能力探测"]
        INS["instructions.py<br/>AGENTS.md 注入"]
    end

    subgraph L3["上下文与记忆"]
        direction LR
        CTX["context.py<br/>ContextManager"]
        STATE["state.py<br/>SessionState / TurnLedger"]
        SESSION["session.py<br/>SessionStore"]
    end

    subgraph L4["执行层"]
        direction LR
        LLM["llm.py<br/>LLMClient / LiteLLM"]
        REG["tools/registry.py<br/>注册与执行"]
        TOOLS["fs / search / shell<br/>edit / plan"]
        SAFETY["tools/safety.py<br/>路径边界"]
    end

    subgraph L5["可观测与评估"]
        direction LR
        TRACE["trace.py<br/>RunTrace JSONL"]
        EVAL["breeze_eval/harness.py"]
        SCRIPTS["scripts/eval.py<br/>report.py"]
    end

    CFG["config.py<br/>Settings（全层共享）"]

    L1 --> L2
    L2 --> L3
    L2 --> L4
    L2 --> L5
    CFG -.-> L2
```

> 箭头表示层间依赖方向，具体调用关系见后续时序图与流程图。
> 各层内部的模块对应关系见下方职责表。

### 模块职责速查

| 文件 | 核心对象 | 职责 | 关键约束 |
|---|---|---|---|
| `cli.py` | `ask` / `chat` | 解析参数、切换 `settings.repo_root`、渲染答案与摘要 | 不需要模型的命令不校验 API Key |
| `chat.py` | `run_chat` | 多回合输入循环、每回合 trace、会话恢复与落盘 | 与 CLI 解耦，依赖可注入 |
| `chat_commands.py` | `ChatController` | 运行中改模型/预算/权限/trace，写 config 事件 | 配置变更可被 `--resume` 恢复 |
| `agent.py` | `Agent` | ReAct 主循环、步数提醒、编辑失败上限、空答案恢复 | 默认 25 步、连续编辑失败 3 次 |
| `context.py` | `ContextManager` | 软/硬预算、按工具类别降级、前缀复用统计 | 软预算不动 `read_file` |
| `state.py` | `SessionState` / `TurnLedger` | 从真实工具结果提取事实；跨回合归档问答结论 | 不采信模型自述 |
| `session.py` | `SessionStore` | append-only JSONL，恢复 ledger/state/config | 空会话不落盘 |
| `llm.py` | `LLMClient` | LiteLLM 调用、超时重试、参数不兼容回退、用量提取 | API Key 只来自环境变量 |
| `trace.py` | `RunTrace` | `run_start / llm_call / tool_call / guard / permission / run_summary` | 每回合或固定文件追加 |
| `tools/registry.py` | `TOOLS` | 装饰器注册、schema 导出、按名执行、参数校验 | 名称全局唯一；参数错误抛 `ToolArgumentError` |
| `tools/safety.py` | `safe_path` | 仓库边界；`.env*`/`.git/` 读取保护 | 写操作只能用 `safe_path` |
| `tools/fs.py` | `list_files` / `read_file` | 目录浏览、带行号读取、`start_line/end_line` 定点读 | 忽略缓存/构建目录与 `.gitignore` |
| `tools/search.py` | `grep` | `文件:行号:内容`；rg 优先，Python 回退 | 结果行数与字节双上限 |
| `tools/shell.py` | `run_shell` | `shell=False`、可执行文件/子命令/选项白名单 | 拒绝未加引号的串联/管道/重定向 |
| `tools/edit.py` | 5 个工具 | 单点替换、建文件、删路径、回滚、pytest | 补丁大小、受保护路径、原子写入 |
| `tools/plan.py` | `update_plan` | 计划写入 `SessionState.plan` | 最多 2000 字符 |
| `capabilities.py` | `detect` | 按环境裁剪工具集与提示词 | 写操作后可能重新探测 |
| `instructions.py` | AGENTS.md | 仓库级指引作为独立 system 消息注入 | 最多 8000 字符，不能放宽安全规则 |
| `config.py` | `settings` | 默认值 → 用户 → 仓库 → 环境变量 → CLI 合并 | 安全白名单不可配置 |

## 3. 单轮 `breeze ask` 时序

```mermaid
sequenceDiagram
    autonumber
    actor U as 用户
    participant CLI as cli.ask
    participant AG as Agent
    participant CM as ContextManager
    participant LLM as LLMClient
    participant T as 工具系统

    U->>CLI: ask "问题" + 参数
    CLI->>CLI: 校验仓库、覆盖 repo_root
    CLI->>LLM: 构造 LLMClient
    CLI->>AG: 构造 Agent（已开启 RunTrace）
    AG->>AG: 探测能力、裁剪工具集<br/>生成 system prompt 与独立 AGENTS.md 消息
    CLI->>AG: run(question)

    loop step = 0 .. max_steps-1
        AG->>AG: 注入步数提醒
        AG->>CM: build_with_stats(...)
        CM-->>AG: messages + PromptStats
        AG->>LLM: chat(messages, tools, tool_choice)
        LLM-->>AG: content + tool_calls + usage
        Note over AG: 写 trace：llm_call
        alt 有工具调用
            AG->>T: execute(name, arguments)
            T-->>AG: 结果文本
            Note over AG: 写 trace：tool_call<br/>state.update + 追加 role=tool
        else 无工具调用
            AG->>AG: 空答案恢复 / 编辑声明校验
            AG-->>CLI: 最终答案（归档到 TurnLedger）
        end
    end

    CLI->>U: Markdown 答案
    CLI->>U: Trace Summary
    Note over CLI: trace.close 写 run_summary
```

> `run_shell` 是特例：`Agent._execute_shell` 先按权限模式判定，再调用
> `registry.execute("run_shell", ...)` 或 `shell.run_unlisted(...)`。

## 4. ReAct 主循环

```mermaid
flowchart TD
    A([run question]) --> B[刷新能力与 system prompt]
    B --> C{消息为空?}
    C -- 是 --> D[写入 system + user]
    C -- 否 --> E[替换 system、追加 user]
    D --> F[start_turn：清空读取计数]
    E --> F
    F --> G{step < max_steps?}
    G -- 否 --> Z1[finish_turn：达到最大步数]
    G -- 是 --> H[提醒：接近硬上限 / 半程<br/>/ 剩 1 次 / 最后一步]
    H --> I[取工具 schema<br/>最后一步 tool_choice=none]
    I --> J[选视图：archive 当前回合<br/>append 全部历史]
    J --> K[ContextManager 构建消息]
    K --> L[LLMClient.chat]
    L --> M{调用成功?}
    M -- 否 --> Z2[log_error；返回失败<br/>本回合不归档]
    M -- 是 --> N[log_llm：tokens/缓存/前缀]
    N --> O{最后一步且有 tool_calls?}
    O -- 是 --> P[忽略工具<br/>报告未完成]
    O -- 否 --> Q{有 tool_calls?}
    Q -- 否 --> R{答案为空?}
    R -- 是 --> S[空答案恢复<br/>再请求一次]
    R -- 否 --> T{只是计划 / 声称已编辑<br/>/ 声称验证通过?}
    S --> T
    T -- 命中且未提醒过 --> U[追加对应提醒，continue]
    U --> G
    T -- 否 --> V[补“未完成/验证未通过”说明<br/>finish_turn 归档并返回]
    Q -- 是 --> W["追加 assistant（tool_calls）"]
    W --> X[逐个工具：<br/>写上限检查 / 重复读抑制<br/>/ 重复写抑制 / 参数校验]
    X --> Y{写工具失败<br/>且不是参数错误?}
    Y -- 是 --> Y1[连续失败 +1<br/>达到上限后拒绝后续写]
    Y -- 否 --> Y2[成功写：清零失败计数<br/>编辑成功：edits_this_turn +1]
    Y1 --> G
    Y2 --> G
```

关键计数规则：

- `edits_this_turn`：`apply_patch`、`create_file`、`delete_path`、`git_restore` 成功才 +1，用于半程提醒和“声称已编辑”校验。
- `edit_failures`：只统计三个写工具（`apply_patch`、`create_file`、`delete_path`）的连续失败；成功一次清零，达到 `max_edit_failures` 后拒绝后续写操作。
- **参数错误不计入 `edit_failures`**：漏传必填项、未知参数、类型不符、参数不是合法 JSON 都只返回可修正的说明，模型改对后可直接重试。
- 工具成功与否按结果前缀判断：`Error`、`Tool execution failed`、`Failed` 视为失败。
- `apply_patch` / `create_file` 失败时结果追加 `No changes were written to the target path.`，避免模型为确认而重读；`delete_path` 可能部分删除，不加该断言。
- 同一路径的 `read_file` 在副本仍可见时被连续抑制 3 次，会直接结束回合并报告未完成；同一写调用成功后再原样重复会被抑制。

## 5. 上下文构建（ContextManager）

### 5.1 消息注入顺序

```text
system        默认提示 + 运行环境说明
instructions  AGENTS.md（独立 system 消息，非空时）
archive       TurnLedger 渲染的已完成回合（仅 archive 模式）
user          当前回合问题（append 模式下是整个会话首问）
snapshot      跨回合工作集快照（仅回合第一步且存在历史）
body          assistant ↔ tool 的完整往返
plan          update_plan 的当前计划（非空时）
state         SessionState（降级或回合启动时注入完整状态；
              未降级时只注入本回合重复读取警示）
budget        剩余预算与仍可见的 read_file 工作集（每轮）
continuation  最后一条是 system 时补的短 user 消息
```

`plan` 与 `state` 放在末尾，是为了让计划/索引变化不切断前面的缓存前缀；
`state` 的完整块只在发生降级或回合启动时注入，未压缩回合只保留重复读取提醒，
避免占用预算或把历史读取计数误当成当前事实。`budget` 块随剩余预算变化，
因此放在 `state` 之后、`continuation` 之前。

### 5.2 两级预算与降级流程

```mermaid
flowchart TD
    A([messages 视图]) --> B{context.enable<br/>且消息数 ≥ 2?}
    B -- 否 --> C[原样返回 + 基础统计]
    B -- 是 --> D[head = system + 首条 user<br/>body = 其余消息]
    D --> E[折叠 plan 参数、压缩 reasoning<br/>建立 tool_call_id → 工具名/参数索引]
    E --> F[遍历 tool 结果]
    F --> G{grep / list_files<br/>且超过 1000 字符?}
    G -- 是 --> H[降级为指针 + 最多 1500 字符预览]
    G -- 否 --> I[保留原文]
    H --> J[单条结果超过 16KB：<br/>保留头 8KB + 尾 8KB]
    I --> J
    J --> K{消息 Token 超过软预算<br/>默认 24000?}
    K -- 是 --> L[按体积从大到小降级可再生输出<br/>run_shell / run_tests]
    K -- 否 --> M{消息 Token 超过硬上限<br/>默认 24000?}
    L --> M
    M -- 否 --> Q[补读取指针说明与批次头]
    M -- 是 --> N[降级 read_file：按消息从旧到新<br/>保护每个文件最近一次读取<br/>只处理 ≥ 512 字符的结果]
    N --> O{仍超硬上限?}
    O -- 是 --> P[放开最近读取保护，继续降级 read_file]
    O -- 否 --> Q
    P --> Q
    Q --> R[组装：system → instructions → archive<br/>→ user → snapshot → body → plan<br/>→ state → budget → continuation]
    R --> S[PromptStats：分段 Token + 可复用前缀]
```

五个设计点：

1. **软预算不动文件原文**：`read_file` 是 `apply_patch` 的 `old` 来源，换出后模型只能重读，既多花 Token 又切断缓存。
2. **定位类结果保留预览**：`grep`/`list_files` 整块丢弃会让模型只剩“重跑”一个选择。
3. **大结果优先降级**：小结果换出省不下多少 Token，却迫使模型重读，收益为负。
4. **读取按旧到新降级**：新读到的内容优先留在上下文里，否则模型会陷入“读取→被压缩→立刻重读”的循环。已被降级的旧读取若有更新的可见副本，指针会改为指向该副本而不是要求重读全文件。
5. **降级决策保持稳定**：同一 tool_call_id 一旦降级就不再还原，即使下一轮总量暂时低于预算；软阶段一次降到预算的 85%，减少“每步降一条、每步改一次 body”的抖动。两者都是为了保护 body 中部的前缀缓存。

### 5.3 缓存前缀统计

`ContextManager._stats()` 对每条消息做 `blake2b` 指纹，与上一次 build 逐条比较，
相同前缀的 Token 计入 `cacheable_prefix_tokens`。它统计的是**消息本身**，不含工具 schema；
provider 返回的 `prompt_tokens` 通常更大，两者差额就是 schema 与格式开销。

## 6. 多回合 `breeze chat`

### 6.1 会话主循环

```mermaid
flowchart TD
    A([breeze chat]) --> B{--list?}
    B -- 是 --> B1[列出会话，可按 --search 过滤，退出]
    B -- 否 --> C{--resume?}
    C -- 是 --> C1[SessionStore.resume<br/>saved_config 恢复 CLI/控制器]
    C -- 否 --> C2[新建 SessionStore<br/>首个回合才落盘]
    C1 --> D[构造 LLMClient + ChatController]
    C2 --> D
    D --> E[run_chat 输入循环]
    E --> F{输入类型}
    F -- /exit /quit --> Z[bye]
    F -- /help --> E
    F -- /new --> G[agent.reset + session.record_reset<br/>turns = 0]
    G --> E
    F -- 其他 / 命令 --> H[ChatController.handle<br/>/model /trace /profile /set /config /plan]
    H --> E
    F -- 普通问题 --> I[创建本回合 RunTrace]
    I --> J{agent 已存在?}
    J -- 否 --> K[agent_factory<br/>session.load 恢复 ledger/state<br/>apply_to_agent 同步运行时配置]
    J -- 是 --> L[替换 agent.trace]
    K --> M["agent.run（question）"]
    L --> M
    M --> N{本回合成功归档?}
    N -- 是 --> O[session.record_turn<br/>问答 + touched + state + token 摘要]
    N -- 否/中断 --> P[不落盘，继续会话]
    O --> Q[输出答案 + 本轮 Token]
    P --> Q
    Q --> E
```

### 6.2 会话文件事件模型

```mermaid
flowchart LR
    A[session_start] --> B[config...]
    B --> C[turn 1]
    C --> D[turn 2]
    D --> E[...]
    E --> F[reset]
    F --> G[turn n]
```

| 事件 | 写入时机 | 内容 |
|---|---|---|
| `session_start` | 首个回合落盘前 | session_id、repo_root、model、创建时间 |
| `config` | `/model` `/trace` `/profile` `/set` | 变更键值；`--resume` 时合并最后一份 |
| `turn` | 回合正常结束 | 问题、答案、touched、tokens、trace 路径、`SessionState` 快照 |
| `reset` | `/new` | 恢复时只认最后一个 reset 之后的内容 |

存储选择 append-only JSONL：与 trace 同族、零依赖、人可读、坏行可跳过。
没有跑过回合的会话不会留下空文件，避免污染 `--list` 与 `--resume latest`。

### 6.3 跨回合记忆

| 结构 | 保留什么 | 丢弃什么 | 作用 |
|---|---|---|---|
| `TurnLedger` | 问题（单行折叠）、答案原文（受预算）、触碰文件 | 历史工具调用与中间输出 | 下一回合只看到“结论” |
| `SessionState` | 符号位置、最近读取范围、编辑结果、测试摘要、计划 | 本回合读取计数（不落盘） | 降级后仍能定点重读/编辑 |
| `SessionStore` | 两者的持久化快照 | trace 级细节 | 进程退出后可 `--resume` |

## 7. 工具执行与权限

```mermaid
flowchart TD
    A([LLM 返回 tool_call]) --> B{参数是合法 JSON 对象?}
    B -- 否 --> B1[参数错误 → role=tool<br/>不计入 edit_failures]
    B -- 是 --> B2{schema 校验通过?<br/>必填 / 未知参数 / 类型}
    B2 -- 否 --> B1
    B2 -- 是 --> C{写工具且连续失败<br/>已达 max_edit_failures?}
    C -- 是 --> C1[合成错误结果 → role=tool<br/>不执行]
    C -- 否 --> C2{重复读 / 重复写?}
    C2 -- 是 --> C3[抑制并返回提示<br/>重复读 3 次则结束回合]
    C2 -- 否 --> D{工具名 = run_shell?}
    D -- 否 --> E[registry.execute]
    D -- 是 --> F{permission 模式}
    F -- strict --> G[check_command 白名单校验]
    G -- 通过 --> E
    G -- 不通过 --> G1[拒绝]
    F -- confirm --> H{通过白名单?}
    H -- 是 --> E
    H -- 否 --> I{有交互确认回调?}
    I -- 否 --> I1[拒绝：无交互用户]
    I -- 是 --> J{用户同意?}
    J -- 是 --> K[run_unlisted]
    J -- 否 --> J1[拒绝 + permission 事件]
    F -- open --> L{通过白名单?}
    L -- 是 --> E
    L -- 否 --> M[run_unlisted + permission allowed]
    E --> N[文本结果]
    K --> N
    M --> N
    N --> O[log_tool + tool_end 事件 + state.update]
    O --> P[追加 role=tool]
    O --> Q[写工具：成功清零 / 失败 +1<br/>参数错误不加；编辑工具成功：edits_this_turn +1]
```

参数校验在 `registry.execute` 入口完成：必填项缺失、未知参数、类型不符会抛
`ToolArgumentError`，Agent 转成带 `Expected:` 列表的错误结果，并明确写工具没有
写入目标文件。这样模型能直接修正参数，而不是面对一句 `TypeError` 再盲目重试。

三种权限模式都保留的边界：

- `shell=False`，一次只执行一条命令；未加引号的 `; | & < >` 一律拒绝。
- `cwd` 必须落在仓库内；超时默认 180 秒；stdout/stderr 各按预算截断。
- 可执行文件白名单：`git`、`node`、`rg`、`python`、`python3`、`where`；
  git 子命令、Python 模块、危险选项另有黑名单。
- 路径统一经过 `safe_path`（写）或 `safe_read_path`（读）；`.git/` 与 `.env*`
  禁止读写（`.env.example` / `.env.sample` 例外），写工具还额外保护仓库内的
  trace 目录。

### 7.1 工具集按环境裁剪

```mermaid
flowchart LR
    A[detect 仓库根] --> B{git 可执行且<br/>在 git 工作区?}
    B -- 否 --> C[移除 git_restore<br/>提示词改写为替代做法]
    B -- 是 --> D[保留 git 工具]
    A --> E{检测到 Python 项目?}
    E -- 否 --> F[移除 run_tests<br/>提示词改写为项目自带测试命令]
    E -- 是 --> G[保留 run_tests]
```

`_refresh_capabilities_after_write()` 只在当前缺少 Python 项目时重新探测，
因为写操作可能刚创建 `pyproject.toml` 等标记；能力变化会同步工具集与 system prompt。

## 8. Trace 与评估

```mermaid
flowchart LR
    subgraph RUN["运行时"]
        A[Agent] -- log_llm / log_tool --> T[RunTrace]
        A -- guard / error / permission --> T
        A -- ledger / state --> S[SessionStore]
        T --> F1[traces/时间戳_runid.jsonl]
        T -. capture_messages .-> F2[traces/*.messages.jsonl]
        S --> F3[~/.breeze/sessions/session_id.jsonl]
    end

    subgraph EVAL["评估"]
        E1[scripts/eval.py] --> E2[breeze_eval/harness.py]
        E2 --> E3[git worktree 隔离]
        E3 --> E4[setup：patch / command<br/>然后 commit]
        E4 --> E5[运行 Agent 或 run_chat]
        E5 --> E6{protect 路径被改动?}
        E6 -- 是 --> X[直接判失败<br/>不执行 eval_command]
        E6 -- 否 --> E7{eval_command 退出码 0?}
        E7 -- 是 --> OK[PASS]
        E7 -- 否 --> FAIL[FAIL]
        E5 --> R1[reports/*.json]
        R1 --> R2[scripts/report.py → Markdown]
    end
```

Trace 事件类型：

| 事件 | 关键字段 | 用途 |
|---|---|---|
| `run_start` | run_id、ts | 标记文件起点 |
| `llm_call` | step、model、tokens、cache、cost、latency、prompt_breakdown、cacheable_prefix | Token 与缓存分析 |
| `tool_call` | tool、arguments、result_preview、error、latency | 工具行为复盘；异常和 `Error`/`Failed` 字符串结果都会写入 `error` |
| `guard` | step、message | 自检拦截（空答案、计划式回答、无编辑声明、验证声明不符、上下文收敛、重复读/写抑制、最后一步工具被忽略） |
| `permission` | command、decision、mode | 命令审计 |
| `error` | step、message | 非工具错误（主要是 LLM 调用失败） |
| `run_summary` | 汇总计数 | 与 `trace.summary()` 一致 |

评估的评分只看 `eval_command` 退出码与 `protect` 路径是否干净，不看模型自述；
`--repeat N` 取 Token 中位数并记录按 run 通过率。

## 9. 配置与启动顺序

```mermaid
flowchart TD
    A[代码默认值 DEFAULTS] --> B[用户配置 ~/.breeze/config.toml]
    B --> C[仓库配置 breeze.toml]
    C --> D[环境变量 BREEZE_*]
    D --> E[CLI 参数]
    E --> F[chat 会话内配置]
    F --> G[Settings 单例]
    G --> H[Agent / ContextManager / LLMClient / Shell / Trace]
    I[.env：从 cwd 向上查找最近一份] -. API Key 等环境变量 .-> D
```

- `.env` 只用于环境变量；API Key 永不写入配置文件。
- 压缩档位 `short / medium / long / custom` 是默认值之上的一层：
  显式配置与环境变量仍覆盖档位。
- 安全白名单、受保护路径、补丁/diff/测试输出上限、schema 默认值不开放配置。

## 10. 关键默认值与边界

| 项 | 默认值 | 位置 |
|---|---:|---|
| 最大循环步数 | 25 | `config.DEFAULTS.agent.max_steps` |
| 连续编辑失败上限 | 3 | `config.DEFAULTS.agent.max_edit_failures` |
| 回合软预算 | 24000 tokens | `config.DEFAULTS.context.intra_turn_budget` |
| 回合硬上限 | 24000 tokens | `config.DEFAULTS.context.hard_budget` |
| 单条工具结果上限 | 16000 bytes | `context.tool_result_max_bytes` |
| grep 结果行数 | 100 | `search.DEFAULT_MAX_RESULTS` |
| list_files 条目 / 单目录文件 | 200 / 20 | `fs.DEFAULT_MAX_ENTRIES` / `DEFAULT_MAX_FILES_PER_DIR` |
| 补丁 old/new | 20000 bytes、200 行差 | `edit.MAX_PATCH_BYTES` / `MAX_PATCH_LINES` |
| create_file 内容 | 50000 bytes | `edit.MAX_CREATE_BYTES` |
| run_tests 测试输出 | 8000 bytes | `edit.MAX_TESTS_OUTPUT_BYTES` |
| run_shell 超时 | 180 s | `shell.DEFAULT_TIMEOUT_SECONDS` |
| run_tests 超时 | 240 s | `edit.TESTS_TIMEOUT_SECONDS` |
| LLM 超时 / 连接超时 / 重试 | 600 s / 15 s / 2 | `config.DEFAULTS.llm` |
| 计划长度 | 2000 字符 | `state.PLAN_MAX_CHARS` |
| AGENTS.md 长度 | 8000 字符 | `instructions.MAX_INSTRUCTIONS_CHARS` |

## 11. 扩展点

- **新增工具**：在 `src/breeze/tools/` 下用 `@register(name=..., description=..., parameters=...)`
  注册，函数返回字符串；若依赖环境能力，在 `capabilities.py` 增加探测并在
  `agent._enabled_tool_names()` 裁剪。
- **调整压缩策略**：改 `ContextManager` 的 `POINTER_TOOLS`、`SPILL_TOOLS`、
  `READ_SPILL_MIN_CHARS`，或通过 `[context]` / 压缩档位改预算。
- **新增会话内命令**：在 `ChatController.handle()` 增加分支，配置变更走
  `self._record(...)` 以便 `--resume` 恢复。
- **新增评估任务**：在 `tasks/quality.yaml` 加任务，先跑
  `python scripts/check_tasks.py --suite core` 确认“修复前失败、protect 干净”。

## 12. 已知边界

- 编辑正确性仍受模型能力限制：自述“已完成”与真实写入可能不一致，
  代码层用“无成功编辑则再提醒一次”兜底，但不能替代模型推理。
- 上下文按消息 Token 估算，不含工具 schema；硬上限应配置在模型上下文以内。
- 降级会改写 body 中部消息，被改写位置之后的前缀缓存全部失效；因此压缩回合的
  缓存命中率会低于 append-only，这是用预算换正确性的代价。已降级结果在后续
  build 中保持指针形态，避免预算回落时又还原成原文。
- `reasoning_content` 只保留最新一步全文，更早的会被截断；截断发生在旧消息上，
  会使该位置之后的前缀失效。真实 trace 中这是主要的缓存失分来源，限制为一条
  已把失配范围缩到最近一步。
- 参数校验只覆盖必填项、未知键与 JSON 基础类型，不校验取值范围或语义合法性。
- `append` 模式只用于成本对照，上下文会持续增长。
- 多回合只保留问答结论与结构化索引，不保留完整工具过程；需要精确原文时
  只能重新读取。
