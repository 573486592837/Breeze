"""CLI 的单元测试。"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from breeze import cli as cli_module
from breeze.cli import app
from breeze.llm import LLMResponse

runner = CliRunner()


class FakeLLMClient:
    """假 LLM，避免真实网络调用。"""

    def __init__(self, *args, **kwargs) -> None:
        self.model = kwargs.get("model") or "fake-model"

    def chat(self, messages, tools=None, temperature=0.0):
        return LLMResponse(
            content="fake answer",
            tool_calls=[],
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


@pytest.fixture(autouse=True)
def patch_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局替换 LLMClient，避免真实 API 调用。"""
    monkeypatch.setattr(cli_module, "LLMClient", FakeLLMClient)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """构造一个空仓库目录。"""
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


# ─── 正常路径 ───


def test_ask_basic(repo: Path, tmp_path: Path) -> None:
    """breeze ask 应返回 Agent 答案。"""
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert result.exit_code == 0
    assert "fake answer" in result.stdout


def test_ask_quiet_mode(repo: Path, tmp_path: Path) -> None:
    """--quiet 只输出答案，不显示 trace 摘要。"""
    result = runner.invoke(
        app,
        [
            "ask",
            "hello",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "traces"),
            "--quiet",
        ],
    )
    assert result.exit_code == 0
    assert "fake answer" in result.stdout
    assert "Trace Summary" not in result.stdout


def test_ask_shows_summary(repo: Path, tmp_path: Path) -> None:
    """默认应显示 trace 摘要。"""
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert result.exit_code == 0
    assert "Trace Summary" in result.stdout
    assert "total_tokens" in result.stdout


def test_ask_writes_trace_file(repo: Path, tmp_path: Path) -> None:
    """运行后应在 trace_dir 下生成 JSONL 文件。"""
    trace_dir = tmp_path / "traces"
    runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(trace_dir)],
    )
    files = list(trace_dir.glob("*.jsonl"))
    assert len(files) == 1


def test_ask_trace_name_prefix(repo: Path, tmp_path: Path) -> None:
    """--trace-name 让 trace 文件带上可辨认的前缀。"""
    trace_dir = tmp_path / "traces"
    runner.invoke(
        app,
        [
            "ask",
            "hello",
            "--repo",
            str(repo),
            "--trace-dir",
            str(trace_dir),
            "--trace-name",
            "explain-registry",
        ],
    )
    files = list(trace_dir.glob("*.jsonl"))

    assert len(files) == 1
    assert files[0].name.startswith("explain-registry_")


def test_ask_renders_markdown(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Markdown 答案应渲染，而不是把 ** 等标记原样打到控制台。"""

    class MarkdownLLM(FakeLLMClient):
        def chat(self, messages, tools=None, temperature=0.0):
            return LLMResponse(
                content="**important** answer\n\n```python\nx = 1\n```",
                tool_calls=[],
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            )

    monkeypatch.setattr(cli_module, "LLMClient", MarkdownLLM)
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )

    assert result.exit_code == 0
    assert "important" in result.stdout
    assert "**important**" not in result.stdout
    assert "```" not in result.stdout


def test_help_lists_current_options() -> None:
    """帮助文案应覆盖持久化与 trace 命名等新参数。"""
    top = runner.invoke(app, ["--help"])
    ask_help = runner.invoke(app, ["ask", "--help"])
    chat_help = runner.invoke(app, ["chat", "--help"])

    assert top.exit_code == 0
    assert "breeze ask --help" in top.stdout
    assert "--trace-name" in ask_help.stdout
    assert "--trace-name" in chat_help.stdout
    assert "--resume" in chat_help.stdout
    assert "--list" in chat_help.stdout


def test_ask_custom_max_steps(repo: Path, tmp_path: Path) -> None:
    """--max-steps 应传递给 Agent。"""
    captured = {}

    original_agent = cli_module.Agent

    class CapturingAgent:
        def __init__(self, llm, trace, max_steps):
            captured["max_steps"] = max_steps
            self._inner = original_agent(llm=llm, trace=trace, max_steps=max_steps)

        def run(self, question):
            return self._inner.run(question)

    cli_module.Agent = CapturingAgent
    try:
        result = runner.invoke(
            app,
            [
                "ask",
                "hello",
                "--repo",
                str(repo),
                "--trace-dir",
                str(tmp_path / "traces"),
                "--max-steps",
                "3",
            ],
        )
    finally:
        cli_module.Agent = original_agent

    assert result.exit_code == 0
    assert captured["max_steps"] == 3


def test_ask_custom_model(repo: Path, tmp_path: Path) -> None:
    """--model 应覆盖默认模型。"""
    result = runner.invoke(
        app,
        [
            "ask",
            "hello",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "traces"),
            "--model",
            "custom-model",
        ],
    )
    assert result.exit_code == 0
    assert "custom-model" in result.stdout


# ─── 错误路径 ───


def test_ask_invalid_repo(tmp_path: Path) -> None:
    """仓库路径无效时退出码为 1。"""
    bad = tmp_path / "not-exist"
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(bad)],
    )
    assert result.exit_code == 1
    assert "invalid" in result.stdout or "invalid" in (result.stderr or "")


def test_ask_missing_api_key(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """API key 缺失时退出码为 1。"""

    class FailingLLM:
        def __init__(self, *args, **kwargs):
            raise ValueError("SILICONFLOW_API_KEY 未设置")

    monkeypatch.setattr(cli_module, "LLMClient", FailingLLM)
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert result.exit_code == 1


def test_ask_runtime_error(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行时异常退出码为 2。"""

    class ExplodingAgent:
        def __init__(self, **kwargs):
            pass

        def run(self, question):
            raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "Agent", ExplodingAgent)
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert result.exit_code == 2


def test_ask_restores_repo_root(repo: Path, tmp_path: Path) -> None:
    """运行后 settings.repo_root 应恢复为原值。"""
    original = cli_module.settings.repo_root
    runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert cli_module.settings.repo_root == original


def test_ask_uses_settings_max_steps_when_not_specified(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 --max-steps 时 Agent 使用 settings 的值。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "agent_max_steps", 4)

    captured: dict[str, Any] = {}
    original = cli_module.Agent

    class CapturingAgent:
        def __init__(self, llm, trace, max_steps=None, **kwargs):
            self._inner = original(llm=llm, trace=trace, max_steps=max_steps, **kwargs)
            captured["resolved_max_steps"] = self._inner.max_steps

        def run(self, question):
            return self._inner.run(question)

    monkeypatch.setattr(cli_module, "Agent", CapturingAgent)

    result = runner.invoke(
        app,
        [
            "ask",
            "hello",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "traces"),
        ],
    )

    assert result.exit_code == 0
    assert captured["resolved_max_steps"] == 4


# ─── chat 命令 ───


def test_chat_runs_turns_until_exit(repo: Path, tmp_path: Path) -> None:
    """chat 逐回合执行并退出。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="hello\nagain\n/exit\n",
    )
    assert result.exit_code == 0
    assert result.stdout.count("fake answer") == 2
    assert "bye." in result.stdout


def test_chat_writes_one_trace_per_turn(repo: Path, tmp_path: Path) -> None:
    """每回合生成独立 trace 文件。"""
    trace_dir = tmp_path / "traces"
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(trace_dir)],
        input="hello\nagain\n/exit\n",
    )
    assert len(list(trace_dir.glob("*.jsonl"))) == 2


def test_chat_traces_use_session_id_prefix(repo: Path, tmp_path: Path) -> None:
    """默认用 session id 做 trace 前缀，便于把 trace 与会话对上。"""
    session_dir = tmp_path / "sessions"
    trace_dir = tmp_path / "traces"
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(trace_dir), "--session-dir", str(session_dir)],
        input="hello\nagain\n/exit\n",
    )
    session_file = next(session_dir.glob("*.jsonl"))
    traces = list(trace_dir.glob("*.jsonl"))

    assert len(traces) == 2
    assert all(t.name.startswith(f"{session_file.stem}_") for t in traces)


def test_chat_trace_name_override(repo: Path, tmp_path: Path) -> None:
    """--trace-name 覆盖默认的 session id 前缀。"""
    trace_dir = tmp_path / "traces"
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(trace_dir), "--trace-name", "refactor-run"],
        input="hello\n/exit\n",
    )
    traces = list(trace_dir.glob("*.jsonl"))

    assert len(traces) == 1
    assert traces[0].name.startswith("refactor-run_")


def test_chat_shows_turn_tokens_then_final_summary(repo: Path, tmp_path: Path) -> None:
    """每回合只报本轮 Token，退出时再汇总整场对话。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="hello\nagain\n/exit\n",
    )

    assert result.exit_code == 0
    assert result.stdout.count("本轮 tokens：15") == 2
    assert result.stdout.count("Trace Summary") == 1
    assert "turns" in result.stdout
    assert "30" in result.stdout


def test_chat_exit_without_turn_prints_no_summary(repo: Path, tmp_path: Path) -> None:
    """没有跑过回合就退出时不显示汇总。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/help\n/exit\n",
    )

    assert result.exit_code == 0
    assert "Trace Summary" not in result.stdout


def test_chat_renders_markdown(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """chat 答案同样应渲染 Markdown。"""

    class MarkdownLLM(FakeLLMClient):
        def chat(self, messages, tools=None, temperature=0.0):
            return LLMResponse(
                content="**bold** answer",
                tool_calls=[],
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            )

    monkeypatch.setattr(cli_module, "LLMClient", MarkdownLLM)
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="hello\n/exit\n",
    )

    assert result.exit_code == 0
    assert "bold" in result.stdout
    assert "**bold**" not in result.stdout


def test_chat_help_command(repo: Path, tmp_path: Path) -> None:
    """/help 输出命令说明且不进入回合。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/help\n/exit\n",
    )
    assert result.exit_code == 0
    assert "/new" in result.stdout
    assert "fake answer" not in result.stdout


def test_chat_invalid_repo(tmp_path: Path) -> None:
    """仓库路径无效时退出码为 1。"""
    result = runner.invoke(app, ["chat", "--repo", str(tmp_path / "not-exist")], input="/exit\n")
    assert result.exit_code == 1


def test_chat_runtime_error(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回合内异常退出码为 2。"""

    class ExplodingAgent:
        def __init__(self, **kwargs):
            pass

        def reset(self) -> None:
            pass

        def run(self, question):
            raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "Agent", ExplodingAgent)
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="hello\n",
    )
    assert result.exit_code == 2


def test_chat_restores_repo_root(repo: Path, tmp_path: Path) -> None:
    """退出后 settings.repo_root 应恢复为原值。"""
    original = cli_module.settings.repo_root
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/exit\n",
    )
    assert cli_module.settings.repo_root == original


# ─── chat 会话持久化 ───


def test_chat_writes_session_file_and_prints_id(repo: Path, tmp_path: Path) -> None:
    """每回合落盘到新会话文件，并在头部打印 session id。"""
    session_dir = tmp_path / "sessions"
    result = runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "traces"),
            "--session-dir",
            str(session_dir),
        ],
        input="hello\nagain\n/exit\n",
    )
    files = list(session_dir.glob("*.jsonl"))

    assert result.exit_code == 0
    assert len(files) == 1
    assert f"session: {files[0].stem}" in result.stdout
    text = files[0].read_text(encoding="utf-8")
    assert text.count('"event": "turn"') == 2


def test_chat_list_sessions_without_llm(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--list 只读目录，不需要 LLM 配置也能用。"""

    class NoLLM:
        def __init__(self, *args, **kwargs):
            raise AssertionError("--list 不应构造 LLMClient")

    monkeypatch.setattr(cli_module, "LLMClient", NoLLM)

    from breeze.session import SessionStore
    from breeze.state import SessionState, TurnLedger

    session_dir = tmp_path / "sessions"
    store = SessionStore(session_dir, "20260101T000000_aaa", repo_root=repo, model="fake-model")
    ledger = TurnLedger()
    ledger.record("第一个问题", "答案是 x", ["src/a.py"])
    store.record_turn(ledger.records[0], SessionState(), {"trace_path": "t", "llm_calls": 1, "total_tokens": 123})

    result = runner.invoke(app, ["chat", "--list", "--session-dir", str(session_dir)])

    assert result.exit_code == 0
    assert "20260101T000000_aaa" in result.stdout
    assert "Saved sessions" in result.stdout
    assert "第一个问题" in result.stdout


def test_chat_list_sessions_empty(tmp_path: Path) -> None:
    """没有会话时给出提示而非报错。"""
    result = runner.invoke(app, ["chat", "--list", "--session-dir", str(tmp_path / "sessions")])

    assert result.exit_code == 0
    assert "没有保存的会话" in result.stdout


def test_chat_list_search_filters_by_keyword(repo: Path, tmp_path: Path) -> None:
    """--list --search 只显示命中提问或回答的会话。"""
    from breeze.session import SessionStore
    from breeze.state import SessionState, TurnLedger

    session_dir = tmp_path / "sessions"
    hit = SessionStore(session_dir, "20260101T000000_aaa", repo_root=repo, model="m")
    ledger = TurnLedger()
    ledger.record("修复 safe_path 越界", "改好了", [])
    hit.record_turn(ledger.records[0], SessionState(), {"trace_path": "t", "llm_calls": 1, "total_tokens": 1})

    miss = SessionStore(session_dir, "20260201T000000_bbb", repo_root=repo, model="m")
    ledger = TurnLedger()
    ledger.record("解释 registry", "工具注册表", [])
    miss.record_turn(ledger.records[0], SessionState(), {"trace_path": "t", "llm_calls": 1, "total_tokens": 1})

    result = runner.invoke(
        app,
        ["chat", "--list", "--session-dir", str(session_dir), "--search", "SAFE_PATH"],
    )

    assert result.exit_code == 0
    assert "20260101T000000_aaa" in result.stdout
    assert "safe_path" in result.stdout
    assert "registry" not in result.stdout


def test_chat_list_search_without_match(tmp_path: Path) -> None:
    """关键词无命中时给出提示而非列表。"""
    result = runner.invoke(
        app,
        ["chat", "--list", "--session-dir", str(tmp_path / "sessions"), "--search", "foo"],
    )

    assert result.exit_code == 0
    assert "没有匹配 foo 的会话" in result.stdout


def test_chat_search_requires_list(repo: Path, tmp_path: Path) -> None:
    """--search 单独使用应报错并提示 --list。"""
    result = runner.invoke(app, ["chat", "--search", "foo", "--repo", str(repo)])

    assert result.exit_code == 1
    assert "--list" in result.stdout + (result.stderr or "")


def test_chat_resume_continues_same_session(repo: Path, tmp_path: Path) -> None:
    """--resume latest 沿用同一会话文件并接上历史归档。"""
    session_dir = tmp_path / "sessions"
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "t1"), "--session-dir", str(session_dir)],
        input="first question\n/exit\n",
    )
    first_file = next(session_dir.glob("*.jsonl"))

    result = runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "t2"),
            "--session-dir",
            str(session_dir),
            "--resume",
            "latest",
        ],
        input="second question\n/exit\n",
    )
    text = first_file.read_text(encoding="utf-8")

    assert result.exit_code == 0
    assert first_file.stem in result.stdout
    assert "Trace Summary" in result.stdout
    assert "turn 1" in result.stdout
    assert text.count('"event": "session_start"') == 1
    assert text.count('"event": "turn"') == 2
    assert "first question" in text


def test_chat_resume_unknown_id(repo: Path, tmp_path: Path) -> None:
    """不存在的 session id 退出码 1 并提示 --list。"""
    (tmp_path / "sessions").mkdir()
    result = runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--session-dir",
            str(tmp_path / "sessions"),
            "--resume",
            "20990101T000000_deadbeef",
        ],
        input="/exit\n",
    )

    assert result.exit_code == 1
    assert "--list" in result.stdout + (result.stderr or "")


def test_chat_resume_warns_on_repo_mismatch(repo: Path, tmp_path: Path) -> None:
    """会话原仓库与当前 --repo 不一致时给出提醒。"""
    session_dir = tmp_path / "sessions"
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "t1"), "--session-dir", str(session_dir)],
        input="hello\n/exit\n",
    )

    other = tmp_path / "other"
    other.mkdir()
    result = runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(other),
            "--trace-dir",
            str(tmp_path / "t2"),
            "--session-dir",
            str(session_dir),
            "--resume",
            "latest",
        ],
        input="/exit\n",
    )

    assert result.exit_code == 0
    assert "原先在" in result.stdout


def test_chat_resume_injects_archive_into_first_view(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume 后的首个 LLM 视图应含 system + 归档 + 当前问题。"""
    views: list[list[dict]] = []

    class RecordingLLM:
        model = "rec"

        def __init__(self, *args, **kwargs):
            pass

        def chat(self, messages, tools=None, temperature=0.0):
            views.append(list(messages))
            return LLMResponse(
                content="prior answer",
                tool_calls=[],
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            )

    monkeypatch.setattr(cli_module, "LLMClient", RecordingLLM)
    session_dir = tmp_path / "sessions"
    trace_dir = tmp_path / "traces"
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(trace_dir), "--session-dir", str(session_dir)],
        input="previous question\n/exit\n",
    )
    runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--trace-dir",
            str(trace_dir),
            "--session-dir",
            str(session_dir),
            "--resume",
            "latest",
        ],
        input="next question\n/exit\n",
    )

    resumed_view = views[-1]
    assert [m["role"] for m in resumed_view] == ["system", "user", "user"]
    assert "[Completed turns" in resumed_view[1]["content"]
    assert "previous question" in resumed_view[1]["content"]
    assert "prior answer" in resumed_view[1]["content"]
    assert resumed_view[2]["content"] == "next question"


def test_chat_without_turns_leaves_no_session_file(repo: Path, tmp_path: Path) -> None:
    """没跑过回合的会话不留下文件，避免污染 --list 与 --resume latest。"""
    session_dir = tmp_path / "sessions"
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces"), "--session-dir", str(session_dir)],
        input="/help\n/exit\n",
    )

    assert result.exit_code == 0
    assert not session_dir.exists() or list(session_dir.glob("*.jsonl")) == []


def test_chat_without_session_dir_uses_configured_dir(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未传 --session-dir 时写入 sessions.dir 配置值（测试里由 conftest 指到临时目录）。"""
    from breeze.config import settings

    configured = tmp_path / "configured-sessions"
    monkeypatch.setattr(settings, "sessions_dir", configured)

    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="hello\n/exit\n",
    )

    assert result.exit_code == 0
    assert len(list(configured.glob("*.jsonl"))) == 1


# ─── chat 运行时命令 ───


def test_chat_model_command_switches_model(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """/model 切换后，后续回合使用新模型。"""

    class RecordingLLM(FakeLLMClient):
        seen: list[str] = []

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            RecordingLLM.seen.append(self.model)

    monkeypatch.setattr(cli_module, "LLMClient", RecordingLLM)
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/model alt-model\nhello\n/exit\n",
    )

    assert result.exit_code == 0
    assert "模型已切换" in result.stdout
    assert RecordingLLM.seen == ["fake-model", "alt-model"]
    assert "fake answer" in result.stdout


def test_chat_trace_command_changes_prefix(repo: Path, tmp_path: Path) -> None:
    """/trace 设置后的回合使用新前缀。"""
    trace_dir = tmp_path / "traces"
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(trace_dir)],
        input="/trace my-run\nhello\n/exit\n",
    )

    assert result.exit_code == 0
    traces = list(trace_dir.glob("*.jsonl"))
    assert len(traces) == 1
    assert traces[0].name.startswith("my-run_")


def test_chat_profile_command(repo: Path, tmp_path: Path) -> None:
    """/profile 切换压缩档位并回报预算。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/profile short\nhello\n/exit\n",
    )

    assert result.exit_code == 0
    assert "压缩档位已切换：short" in result.stdout
    assert "intra_turn_budget=12000" in result.stdout


def test_chat_config_command(repo: Path, tmp_path: Path) -> None:
    """/config 展示当前运行配置，不进入回合。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/config\n/exit\n",
    )

    assert result.exit_code == 0
    assert "运行配置：" in result.stdout
    assert "model: fake-model" in result.stdout
    assert "trace 前缀:" in result.stdout
    assert "fake answer" not in result.stdout


def test_chat_unknown_command(repo: Path, tmp_path: Path) -> None:
    """未知命令提示 /help，且不触发回合。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/nope\n/exit\n",
    )

    assert result.exit_code == 0
    assert "未知命令" in result.stdout
    assert "fake answer" not in result.stdout


def test_chat_set_command(repo: Path, tmp_path: Path) -> None:
    """/set 调整参数并回报。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/set max_steps 3\nhello\n/exit\n",
    )

    assert result.exit_code == 0
    assert "max_steps 已设为：3" in result.stdout


def test_chat_only_commands_leaves_no_session_file(repo: Path, tmp_path: Path) -> None:
    """只调整命令、没有回合时同样不落空会话文件。"""
    session_dir = tmp_path / "sessions"
    result = runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "traces"),
            "--session-dir",
            str(session_dir),
        ],
        input="/model other-model\n/config\n/exit\n",
    )

    assert result.exit_code == 0
    assert not session_dir.exists() or list(session_dir.glob("*.jsonl")) == []


def test_chat_resume_restores_saved_config(repo: Path, tmp_path: Path) -> None:
    """--resume 恢复会话里记录的模型、trace 前缀与运行时参数。"""
    session_dir = tmp_path / "sessions"
    first_traces = tmp_path / "t1"
    second_traces = tmp_path / "t2"
    runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--trace-dir",
            str(first_traces),
            "--session-dir",
            str(session_dir),
        ],
        input="/model resumed-model\n/trace resumed-trace\n/set temperature 0.5\n/set max_steps 3\nhello\n/exit\n",
    )

    result = runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--trace-dir",
            str(second_traces),
            "--session-dir",
            str(session_dir),
            "--resume",
            "latest",
        ],
        input="/config\nagain\n/exit\n",
    )

    assert result.exit_code == 0
    assert "model: resumed-model" in result.stdout
    assert "trace 前缀: resumed-trace" in result.stdout
    assert "max_steps: 3" in result.stdout
    assert "temperature: 0.5" in result.stdout
    traces = list(second_traces.glob("*.jsonl"))
    assert len(traces) == 1
    assert traces[0].name.startswith("resumed-trace_")


def test_chat_cli_model_overrides_saved(repo: Path, tmp_path: Path) -> None:
    """--model 优先于会话里保存的模型。"""
    session_dir = tmp_path / "sessions"
    runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "t1"),
            "--session-dir",
            str(session_dir),
        ],
        input="/model saved-model\nhello\n/exit\n",
    )

    result = runner.invoke(
        app,
        [
            "chat",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "t2"),
            "--session-dir",
            str(session_dir),
            "--resume",
            "latest",
            "--model",
            "cli-model",
        ],
        input="/config\n/exit\n",
    )

    assert result.exit_code == 0
    assert "model: cli-model" in result.stdout


# ─── 版本 ───


def test_version_flag() -> None:
    """breeze --version 输出与 breeze.__version__ 一致。"""
    from breeze import __version__

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_flag_without_api_key(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--version 不该依赖密钥：LLMClient 构造会抛错时也要能输出版本。"""
    from breeze import __version__

    class ExplodingLLM:
        def __init__(self, *args, **kwargs):
            raise ValueError("SILICONFLOW_API_KEY 未设置，请检查 .env 文件或环境变量")

    monkeypatch.setattr(cli_module, "LLMClient", ExplodingLLM)
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout
