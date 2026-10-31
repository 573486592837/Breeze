"""config 模块的单元测试。

覆盖：DEFAULTS 结构、分层加载、环境变量覆盖、优先级、
类型转换、错误处理、未知字段忽略、Settings 类。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from breeze import config as config_module
from breeze.config import DEFAULTS, Settings, find_dotenv_path, load_config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离每个测试的配置环境。

    - 清除所有 BREEZE_* 环境变量
    - 把用户配置路径重定向到 tmp_path，避免读到开发者本机配置
    - 设置假的 API key，让 Settings 可以实例化

    Returns:
        临时用户配置路径，供需要显式写入的测试使用。
    """
    for key in list(os.environ):
        if key.startswith("BREEZE_"):
            monkeypatch.delenv(key, raising=False)

    user_config = tmp_path / "_user_config.toml"
    monkeypatch.setattr("breeze.config.USER_CONFIG_PATH", user_config, raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    return user_config


def test_defaults_has_expected_sections() -> None:
    """DEFAULTS 应包含六个 section。"""
    assert set(DEFAULTS.keys()) == {"llm", "agent", "context", "trace", "sessions", "chat"}


def test_defaults_llm_fields() -> None:
    """llm section 的字段齐全。"""
    llm = DEFAULTS["llm"]
    assert "model" in llm
    assert "api_base" in llm
    assert "temperature" in llm
    assert isinstance(llm["temperature"], float)


def test_defaults_agent_fields() -> None:
    """agent section 的字段齐全且为 int。"""
    agent = DEFAULTS["agent"]
    assert agent["max_steps"] == 10
    assert agent["max_edit_attempts"] == 5
    assert isinstance(agent["max_steps"], int)


def test_defaults_context_fields() -> None:
    """context section 的字段齐全。"""
    ctx = DEFAULTS["context"]
    for key in (
        "enable",
        "intra_turn_budget",
        "state_max_entries",
        "tool_result_max_bytes",
        "tool_result_head_bytes",
        "tool_result_tail_bytes",
    ):
        assert key in ctx, key
    assert isinstance(ctx["enable"], bool)


def test_defaults_context_v2_values() -> None:
    """回合制上下文策略的默认值存在且类型正确。"""
    ctx = DEFAULTS["context"]
    assert ctx["intra_turn_budget"] == 24000
    assert ctx["state_max_entries"] == 8


def test_defaults_chat_and_sessions_fields() -> None:
    """会话持久化相关的默认值存在且类型正确。"""
    chat = DEFAULTS["chat"]
    assert chat["answer_preview"] == 1500
    assert chat["max_records"] == 8
    assert chat["max_chars"] == 6000
    assert DEFAULTS["sessions"]["dir"] == "~/.breeze/sessions"


def test_toml_example_covers_all_defaults() -> None:
    """breeze.toml.example 的字段须与 DEFAULTS 完全一致，防止文档漂移。"""
    import tomllib

    example = Path(__file__).resolve().parents[1] / "breeze.toml.example"
    with example.open("rb") as f:
        data = tomllib.load(f)
    assert set(data.keys()) == set(DEFAULTS.keys())
    for section, values in DEFAULTS.items():
        assert set(data[section].keys()) == set(values.keys()), section


def test_load_defaults_only(tmp_path: Path) -> None:
    """无配置文件、无环境变量时返回 DEFAULTS 的拷贝。"""
    cfg = load_config(tmp_path)
    assert cfg == DEFAULTS
    # 必须是拷贝，不是引用
    assert cfg is not DEFAULTS
    cfg["agent"]["max_steps"] = 999
    assert DEFAULTS["agent"]["max_steps"] == 10


def test_repo_config_overrides_defaults(tmp_path: Path) -> None:
    """仓库根 breeze.toml 覆盖默认值。"""
    (tmp_path / "breeze.toml").write_text("[agent]\nmax_steps = 3\n", encoding="utf-8")

    cfg = load_config(tmp_path)

    assert cfg["agent"]["max_steps"] == 3
    # 其他字段保持默认
    assert cfg["agent"]["max_edit_attempts"] == 5


def test_user_config_overrides_defaults(tmp_path: Path, isolated_config: Path) -> None:
    """用户级配置覆盖默认值。"""
    isolated_config.write_text("[context]\nintra_turn_budget = 1234\n", encoding="utf-8")

    cfg = load_config(tmp_path)

    assert cfg["context"]["intra_turn_budget"] == 1234


def test_repo_overrides_user(tmp_path: Path, isolated_config: Path) -> None:
    """仓库配置优先于用户配置。"""
    isolated_config.write_text("[agent]\nmax_steps = 3\n", encoding="utf-8")
    (tmp_path / "breeze.toml").write_text("[agent]\nmax_steps = 7\n", encoding="utf-8")

    cfg = load_config(tmp_path)

    assert cfg["agent"]["max_steps"] == 7


def test_env_overrides_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量优先于仓库配置。"""
    (tmp_path / "breeze.toml").write_text("[agent]\nmax_steps = 3\n", encoding="utf-8")
    monkeypatch.setenv("BREEZE_MAX_STEPS", "9")

    cfg = load_config(tmp_path)

    assert cfg["agent"]["max_steps"] == 9


def test_env_model_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BREEZE_MODEL 覆盖 llm.model。"""
    monkeypatch.setenv("BREEZE_MODEL", "openai/custom-model")

    cfg = load_config(tmp_path)

    assert cfg["llm"]["model"] == "openai/custom-model"


def test_env_int_conversion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BREEZE_MAX_STEPS 从 str 转为 int。"""
    monkeypatch.setenv("BREEZE_MAX_STEPS", "42")

    cfg = load_config(tmp_path)

    assert cfg["agent"]["max_steps"] == 42
    assert isinstance(cfg["agent"]["max_steps"], int)


def test_env_float_conversion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BREEZE_TEMPERATURE 从 str 转为 float。"""
    monkeypatch.setenv("BREEZE_TEMPERATURE", "0.7")

    cfg = load_config(tmp_path)

    assert cfg["llm"]["temperature"] == 0.7
    assert isinstance(cfg["llm"]["temperature"], float)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
        ("yes", True),
        ("on", True),
    ],
)
def test_env_bool_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: bool,
) -> None:
    """BREEZE_CONTEXT_ENABLE 支持多种 bool 写法。"""
    monkeypatch.setenv("BREEZE_CONTEXT_ENABLE", raw)

    cfg = load_config(tmp_path)

    assert cfg["context"]["enable"] is expected


def test_env_invalid_int_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BREEZE_MAX_STEPS 非数字时报错。"""
    monkeypatch.setenv("BREEZE_MAX_STEPS", "abc")

    with pytest.raises(ValueError, match="invalid value for BREEZE_MAX_STEPS"):
        load_config(tmp_path)


def test_env_invalid_float_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BREEZE_TEMPERATURE 非浮点时报错。"""
    monkeypatch.setenv("BREEZE_TEMPERATURE", "hot")

    with pytest.raises(ValueError, match="BREEZE_TEMPERATURE"):
        load_config(tmp_path)


def test_invalid_toml_raises(tmp_path: Path) -> None:
    """TOML 语法错误应抛 ValueError。"""
    (tmp_path / "breeze.toml").write_text("[agent\nmax_steps = 3\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid TOML"):
        load_config(tmp_path)


def test_invalid_user_toml_raises(tmp_path: Path, isolated_config: Path) -> None:
    """用户级 TOML 语法错误同样应抛 ValueError。"""
    isolated_config.write_text("not toml", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid TOML"):
        load_config(tmp_path)


def test_unknown_section_ignored(tmp_path: Path) -> None:
    """未知 section 不应污染结果。"""
    (tmp_path / "breeze.toml").write_text(
        "[unknown]\nfoo = 1\n[agent]\nmax_steps = 4\n",
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)

    assert "unknown" not in cfg
    assert cfg["agent"]["max_steps"] == 4


def test_unknown_key_ignored(tmp_path: Path) -> None:
    """已知 section 中的未知 key 应被忽略。"""
    (tmp_path / "breeze.toml").write_text(
        "[agent]\nmax_steps = 4\nnot_a_real_key = 99\n",
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)

    assert cfg["agent"]["max_steps"] == 4
    assert "not_a_real_key" not in cfg["agent"]


def test_unknown_type_mismatch_silently_ignored(tmp_path: Path) -> None:
    """section 的值不是 table 时忽略，不抛异常。"""
    (tmp_path / "breeze.toml").write_text('agent = "should be a table"\n', encoding="utf-8")

    cfg = load_config(tmp_path)

    # 默认为完整表结构
    assert cfg["agent"]["max_steps"] == 10


def test_settings_reads_repo_config(tmp_path: Path) -> None:
    """Settings 从 repo_root 下的 breeze.toml 读值。"""
    (tmp_path / "breeze.toml").write_text(
        "[agent]\nmax_steps = 3\nmax_edit_attempts = 2\n"
        "[context]\nintra_turn_budget = 500\n"
        '[llm]\nmodel = "openai/test-model"\ntemperature = 0.5\n',
        encoding="utf-8",
    )

    s = Settings(repo_root=tmp_path)

    assert s.agent_max_steps == 3
    assert s.agent_max_edit_attempts == 2
    assert s.context_intra_turn_budget == 500
    assert s.model == "openai/test-model"
    assert s.llm_temperature == 0.5


def test_settings_uses_defaults_without_config(tmp_path: Path) -> None:
    """没有配置文件时用 DEFAULTS。"""
    s = Settings(repo_root=tmp_path)

    assert s.agent_max_steps == DEFAULTS["agent"]["max_steps"]
    assert s.context_intra_turn_budget == DEFAULTS["context"]["intra_turn_budget"]
    assert s.model == DEFAULTS["llm"]["model"]


def test_settings_env_overrides_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量优先于仓库配置。"""
    (tmp_path / "breeze.toml").write_text("[agent]\nmax_steps = 3\n", encoding="utf-8")
    monkeypatch.setenv("BREEZE_MAX_STEPS", "7")

    s = Settings(repo_root=tmp_path)

    assert s.agent_max_steps == 7


def test_settings_repo_root_resolved(tmp_path: Path) -> None:
    """repo_root 被解析为绝对路径。"""
    s = Settings(repo_root=tmp_path)
    assert s.repo_root.is_absolute()
    assert s.repo_root == tmp_path.resolve()


def test_settings_missing_api_key_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 key 时配置仍可用：校验属于 LLMClient，import 期不能抛。"""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    s = Settings(repo_root=tmp_path)

    assert s.api_key == ""
    assert s.model == DEFAULTS["llm"]["model"]


def test_config_import_without_api_key(tmp_path: Path) -> None:
    """干净环境里 `from breeze.config import settings` 不报错。

    fresh 安装（无 .env、无环境变量）时 CLI 的 --version、chat --list
    都因为模块级 settings = Settings() 抛错而不可用，这里锁住该行为。
    """
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "SILICONFLOW_API_KEY"}
    proc = subprocess.run(
        [sys.executable, "-c", "from breeze.config import settings; print(repr(settings.api_key))"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "''"


def test_find_dotenv_path_prefers_nearest(tmp_path: Path) -> None:
    """逐级向上时最近的一份 .env 生效。"""
    (tmp_path / ".env").write_text("SILICONFLOW_API_KEY=far\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    near = nested / ".env"
    near.write_text("SILICONFLOW_API_KEY=near\n", encoding="utf-8")

    assert find_dotenv_path(nested) == near.resolve()


def test_dotenv_is_loaded_from_cwd_when_installed(tmp_path: Path) -> None:
    """模拟 site-packages 安装：.env 由运行目录决定，与包位置无关。

    v1.3.0 的 wheel 安装里 `breeze ask` 报缺 key —— python-dotenv 的
    `find_dotenv()` 以调用方文件（site-packages/breeze/config.py）为起点
    向上找，项目目录不在这条链上。editable 安装时包目录恰好就在项目里，
    所以本地开发跑不出来。这里把包复制到独立目录、用脚本文件（非 -c，
    避免 python-dotenv 的交互模式回退到 cwd）从项目目录运行，复现该路径。
    """
    import shutil
    import subprocess
    import sys

    site_dir = tmp_path / "site"
    site_dir.mkdir()
    shutil.copytree(
        Path(config_module.__file__).resolve().parent,
        site_dir / "breeze",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    (work_dir / ".env").write_text("SILICONFLOW_API_KEY=dotenv-from-cwd\n", encoding="utf-8")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import breeze.config as c\nprint(c.settings.api_key)\nprint(c.__file__)\n",
        encoding="utf-8",
    )

    env = {k: v for k, v in os.environ.items() if k != "SILICONFLOW_API_KEY" and not k.startswith("BREEZE_")}
    env["PYTHONPATH"] = str(site_dir)
    proc = subprocess.run(
        [sys.executable, str(probe)],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    api_key, module_file = proc.stdout.strip().splitlines()
    # 确认子进程加载的是复制出去的"已安装"副本，而不是仓库里的 editable 包
    assert Path(module_file).resolve().is_relative_to(site_dir.resolve()), module_file
    assert api_key == "dotenv-from-cwd"


def test_settings_reads_chat_and_sessions_config(tmp_path: Path) -> None:
    """chat/sessions 配置可覆盖，且 dir 展开 ~。"""
    (tmp_path / "breeze.toml").write_text(
        '[chat]\nanswer_preview = 300\nmax_records = 2\nmax_chars = 900\n[sessions]\ndir = "my/sessions"\n',
        encoding="utf-8",
    )

    s = Settings(repo_root=tmp_path)

    assert s.chat_answer_preview == 300
    assert s.chat_max_records == 2
    assert s.chat_max_chars == 900
    # 相对路径与 trace.dir 一样保持原样，由使用方按当前工作目录解析
    assert s.sessions_dir == Path("my/sessions")


def test_settings_sessions_dir_expands_home(tmp_path: Path) -> None:
    """默认会话目录展开 ~，落在用户主目录下。"""
    s = Settings(repo_root=tmp_path)

    assert s.sessions_dir.is_absolute()
    assert s.sessions_dir.name == "sessions"
    assert "~" not in str(s.sessions_dir)


def test_settings_type_coercion(tmp_path: Path) -> None:
    """Settings 字段类型正确。"""
    s = Settings(repo_root=tmp_path)

    assert isinstance(s.agent_max_steps, int)
    assert isinstance(s.agent_max_edit_attempts, int)
    assert isinstance(s.context_enable, bool)
    assert isinstance(s.context_intra_turn_budget, int)
    assert isinstance(s.llm_temperature, float)
    assert isinstance(s.trace_dir, str)
    assert isinstance(s.chat_answer_preview, int)
    assert isinstance(s.sessions_dir, Path)
