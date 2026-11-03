"""trace 模块的单元测试。"""

import json
from pathlib import Path

import pytest

from breeze.trace import RunTrace


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    """返回临时追踪目录。"""
    d = tmp_path / "traces"
    d.mkdir()
    return d


def _read_events(path: Path) -> list[dict]:
    """读取 JSONL 文件的所有事件。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_trace_creates_file(trace_dir: Path) -> None:
    """初始化应创建 JSONL 文件，文件名含 run_id。"""
    with RunTrace(trace_dir=trace_dir, run_id="abcd1234") as trace:
        assert trace.path.exists()
        assert "abcd1234" in trace.path.name

    events = _read_events(trace.path)
    assert events[0]["event"] == "run_start"
    assert events[0]["run_id"] == "abcd1234"


def test_trace_log_llm(trace_dir: Path) -> None:
    """log_llm 应写入完整字段。"""
    with RunTrace(trace_dir=trace_dir, run_id="run1") as trace:
        trace.log_llm(
            step=0,
            model="openai/Pro/deepseek-ai/DeepSeek-V3.2",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            latency_ms=850.5,
            content="我先查看文件结构",
            tool_calls=[{"name": "list_files", "arguments": {"path": "."}}],
        )

    events = _read_events(trace.path)
    llm_events = [e for e in events if e["event"] == "llm_call"]
    assert len(llm_events) == 1

    e = llm_events[0]
    assert e["step"] == 0
    assert e["prompt_tokens"] == 100
    assert e["completion_tokens"] == 20
    assert e["total_tokens"] == 120
    assert e["latency_ms"] == 850.5
    assert e["tool_calls"][0]["name"] == "list_files"


def test_trace_logs_cache_cost_and_prompt_breakdown(trace_dir: Path) -> None:
    """缓存、成本与 prompt 组成应写入事件并计入 summary。"""
    with RunTrace(trace_dir=trace_dir, run_id="run-cache") as trace:
        trace.log_llm(
            step=0,
            model="openai/deepseek-reasoner",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            latency_ms=10.0,
            cache_hit_tokens=60,
            cache_miss_tokens=40,
            cost_usd=0.0012,
            prompt_breakdown={"system": 50, "plan": 0, "archive": 0, "user": 10, "state": 0, "body": 40},
            cacheable_prefix_tokens=80,
        )
        summary = trace.summary()

    event = [e for e in _read_events(trace.path) if e["event"] == "llm_call"][0]
    assert event["cache_hit_tokens"] == 60
    assert event["cache_miss_tokens"] == 40
    assert event["cost_usd"] == pytest.approx(0.0012)
    assert event["cacheable_prefix_tokens"] == 80
    assert event["prompt_breakdown"]["system"] == 50

    assert summary["cache_reported_calls"] == 1
    assert summary["cache_hit_rate"] == 0.6
    assert summary["estimated_prompt_tokens"] == 100
    assert summary["cacheable_prefix_ratio"] == 0.8
    assert summary["cost_usd"] == pytest.approx(0.0012)


def test_trace_summary_cache_unknown_stays_none(trace_dir: Path) -> None:
    """provider 不报告缓存时，命中率与成本为 None 而不是 0。"""
    with RunTrace(trace_dir=trace_dir, run_id="run-nocache") as trace:
        trace.log_llm(
            step=0,
            model="openai/deepseek-reasoner",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            latency_ms=10.0,
        )
        summary = trace.summary()

    assert summary["cache_reported_calls"] == 0
    assert summary["cache_hit_rate"] is None
    assert summary["cost_usd"] is None


def test_trace_log_tool(trace_dir: Path) -> None:
    """log_tool 应写入完整字段。"""
    with RunTrace(trace_dir=trace_dir, run_id="run2") as trace:
        trace.log_tool(
            step=0,
            tool="read_file",
            arguments={"path": "README.md"},
            result="# demo\n",
            latency_ms=3.2,
        )

    events = _read_events(trace.path)
    tool_events = [e for e in events if e["event"] == "tool_call"]
    assert len(tool_events) == 1

    e = tool_events[0]
    assert e["tool"] == "read_file"
    assert e["arguments"] == {"path": "README.md"}
    assert e["result_preview"] == "# demo\n"
    assert e["error"] is None


def test_trace_log_tool_with_error(trace_dir: Path) -> None:
    """log_tool 出错时应记录 error。"""
    with RunTrace(trace_dir=trace_dir, run_id="run3") as trace:
        trace.log_tool(
            step=1,
            tool="run_shell",
            arguments={"command": "git status"},
            result=None,
            latency_ms=1.0,
            error="Executable not allowed: git",
        )

    events = _read_events(trace.path)
    e = [ev for ev in events if ev["event"] == "tool_call"][0]
    assert e["error"] == "Executable not allowed: git"


def test_trace_log_error(trace_dir: Path) -> None:
    """log_error 应写入 error 事件。"""
    with RunTrace(trace_dir=trace_dir, run_id="run4") as trace:
        trace.log_error(step=2, message="LLM API 调用失败")

    events = _read_events(trace.path)
    err = [ev for ev in events if ev["event"] == "error"][0]
    assert err["message"] == "LLM API 调用失败"
    assert err["step"] == 2


def test_trace_summary_counts(trace_dir: Path) -> None:
    """summary 应累加 Token、调用次数与延迟。"""
    with RunTrace(trace_dir=trace_dir, run_id="run5") as trace:
        trace.log_llm(0, "model-a", 100, 20, 120, 500.0)
        trace.log_llm(1, "model-a", 200, 30, 230, 300.0)
        trace.log_tool(0, "read_file", {"path": "a"}, "x", 10.0)
        trace.log_tool(0, "read_file", {"path": "b"}, "y", 15.0)
        trace.log_error(1, "boom")

        s = trace.summary()
        assert s["llm_calls"] == 2
        assert s["tool_calls"] == 2
        assert s["prompt_tokens"] == 300
        assert s["completion_tokens"] == 50
        assert s["total_tokens"] == 350
        assert s["llm_latency_ms"] == 800.0
        assert s["tool_latency_ms"] == 25.0
        assert s["errors"] == 1


def test_trace_summary_written_on_close(trace_dir: Path) -> None:
    """close 应写入 run_summary 事件。"""
    with RunTrace(trace_dir=trace_dir, run_id="run6") as trace:
        trace.log_llm(0, "m", 10, 5, 15, 100.0)

    events = _read_events(trace.path)
    summary = [e for e in events if e["event"] == "run_summary"]
    assert len(summary) == 1
    assert summary[0]["llm_calls"] == 1
    assert summary[0]["total_tokens"] == 15


def test_trace_close_is_idempotent(trace_dir: Path) -> None:
    """重复 close 不应报错或重复写 summary。"""
    trace = RunTrace(trace_dir=trace_dir, run_id="run7")
    trace.log_llm(0, "m", 1, 1, 2, 1.0)
    trace.close()
    trace.close()

    events = _read_events(trace.path)
    summary = [e for e in events if e["event"] == "run_summary"]
    assert len(summary) == 1


def test_trace_result_preview_keeps_head_and_tail(trace_dir: Path) -> None:
    """result 超过 500 字符时截断，但保留头部与尾部（尾部常有失败摘要）。"""
    big_result = "HEAD" + "x" * 1000 + "TAIL"
    with RunTrace(trace_dir=trace_dir, run_id="run8") as trace:
        trace.log_tool(0, "read_file", {"path": "big"}, big_result, 1.0)

    events = _read_events(trace.path)
    e = [ev for ev in events if ev["event"] == "tool_call"][0]
    preview = e["result_preview"]
    assert len(preview) == 500
    assert preview.startswith("HEAD")
    assert preview.endswith("TAIL")
    assert "\n...\n" in preview


def test_trace_content_preview_truncated(trace_dir: Path) -> None:
    """content 超过 500 字符时同样保留头尾。"""
    with RunTrace(trace_dir=trace_dir, run_id="run9") as trace:
        trace.log_llm(0, "m", 1, 1, 2, 1.0, content="y" * 1000)

    events = _read_events(trace.path)
    e = [ev for ev in events if ev["event"] == "llm_call"][0]
    preview = e["content_preview"]
    assert len(preview) == 500
    assert preview.startswith("y")
    assert preview.endswith("y")
    assert "\n...\n" in preview


def test_trace_creates_missing_dir(tmp_path: Path) -> None:
    """trace_dir 不存在时应自动创建。"""
    target = tmp_path / "nested" / "traces"
    assert not target.exists()

    with RunTrace(trace_dir=target, run_id="run10"):
        pass

    assert target.exists()
    assert any(target.iterdir())


def test_trace_events_are_valid_jsonl(trace_dir: Path) -> None:
    """每行应为合法 JSON，且 event 字段存在。"""
    with RunTrace(trace_dir=trace_dir, run_id="run11") as trace:
        trace.log_llm(0, "m", 1, 1, 2, 1.0)
        trace.log_tool(0, "t", {}, "r", 1.0)

    raw = trace.path.read_text(encoding="utf-8").splitlines()
    for line in raw:
        obj = json.loads(line)
        assert "event" in obj
        assert "ts" in obj
        assert obj["run_id"] == "run11"


# ─── 自定义文件名 ───


def test_trace_name_prefix(trace_dir: Path) -> None:
    """name 作为文件名前缀，时间戳与 run_id 仍保证唯一。"""
    with RunTrace(trace_dir=trace_dir, run_id="run12", name="fix-bug") as trace:
        assert trace.name == "fix-bug"
        assert trace.path.name.startswith("fix-bug_")
        assert "run12" in trace.path.name


def test_trace_name_strips_extension_and_unsafe_chars(trace_dir: Path) -> None:
    """名称不能带路径分隔符，也不能把 .jsonl 写进前缀。"""
    with RunTrace(trace_dir=trace_dir, run_id="run13", name="../bad name.jsonl") as trace:
        assert trace.name == "bad_name"
        assert trace.path.parent == trace_dir
        assert trace.path.name.startswith("bad_name_")


def test_trace_name_allows_chinese(trace_dir: Path) -> None:
    """中文名称可直接作为前缀。"""
    with RunTrace(trace_dir=trace_dir, run_id="run15", name="修复登录") as trace:
        assert trace.name == "修复登录"
        assert trace.path.name.startswith("修复登录_")


def test_trace_without_name_keeps_default_format(trace_dir: Path) -> None:
    """不传 name 时文件名格式保持时间戳_run_id.jsonl。"""
    with RunTrace(trace_dir=trace_dir, run_id="run14") as trace:
        assert trace.name is None
        assert trace.path.name.endswith("_run14.jsonl")
        assert trace.path.name.count("_") == 1


def test_trace_log_permission_does_not_count_as_error(trace_dir: Path) -> None:
    """权限判定事件独立记录，不计入 errors。"""
    with RunTrace(trace_dir=trace_dir, run_id="perm1") as trace:
        trace.log_permission(2, "ls -la", "denied", "confirm")
        summary = trace.summary()

    event = next(e for e in _read_events(trace.path) if e["event"] == "permission")
    assert event["step"] == 2
    assert event["command"] == "ls -la"
    assert event["decision"] == "denied"
    assert event["mode"] == "confirm"
    assert summary["errors"] == 0


# ─── 完整 prompt 捕获 ───


def test_capture_messages_off_by_default(trace_dir: Path) -> None:
    """未开启捕获时不创建 messages 文件。"""
    with RunTrace(trace_dir=trace_dir, run_id="cap0") as trace:
        assert trace.messages_path is None
        trace.log_llm(0, "m", 1, 1, 2, 1.0, messages=[{"role": "user", "content": "hi"}])

    assert not list(trace_dir.glob("*.messages.jsonl"))
    assert trace.summary()["messages_path"] is None


def test_capture_messages_writes_full_prompt(trace_dir: Path) -> None:
    """开启捕获后，完整 messages/tools/tool_choice 写入独立文件。"""
    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "q"}]
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

    with RunTrace(trace_dir=trace_dir, run_id="cap1", capture_messages=True) as trace:
        assert trace.messages_path is not None
        trace.log_llm(
            0,
            "m",
            10,
            2,
            12,
            1.0,
            content="y" * 1000,
            messages=messages,
            tools=tools,
            tool_choice="none",
        )
        summary = trace.summary()

    records = _read_events(trace.messages_path)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "llm_messages"
    assert record["run_id"] == "cap1"
    assert record["step"] == 0
    assert record["messages"] == messages
    assert record["tools"] == tools
    assert record["tool_choice"] == "none"

    assert summary["messages_path"] == str(trace.messages_path)
    # 常规 trace 仍只存预览，不重复整份 prompt
    llm_event = next(e for e in _read_events(trace.path) if e["event"] == "llm_call")
    assert "messages" not in llm_event


def test_capture_messages_skips_calls_without_payload(trace_dir: Path) -> None:
    """捕获开启但调用方未传 messages 时，不写空记录。"""
    with RunTrace(trace_dir=trace_dir, run_id="cap2", capture_messages=True) as trace:
        trace.log_llm(0, "m", 1, 1, 2, 1.0)

    assert trace.messages_path is not None
    assert trace.messages_path.read_text(encoding="utf-8") == ""
