"""打包边界测试：release 产物只含运行时需要的代码。"""

import tarfile
import zipfile
from pathlib import Path

import pytest
from setuptools import build_meta

from breeze import __version__


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """构建 wheel 并返回其成员路径集合。"""
    outdir = tmp_path_factory.mktemp("dist")
    name = build_meta.build_wheel(str(outdir))
    with zipfile.ZipFile(outdir / name) as zf:
        return set(zf.namelist())


@pytest.fixture(scope="module")
def sdist_tops(tmp_path_factory: pytest.TempPathFactory) -> set[str]:
    """构建 sdist 并返回其顶层条目名。"""
    outdir = tmp_path_factory.mktemp("sdist")
    name = build_meta.build_sdist(str(outdir))
    with tarfile.open(outdir / name) as tf:
        members = [m.name.split("/", 1)[1] for m in tf.getmembers() if "/" in m.name]
    return {member.split("/")[0] for member in members}


def test_wheel_ships_runtime_modules(wheel_names: set[str]) -> None:
    """Agent、CLI、工具与持久化都应在包里。"""
    for member in ("breeze/cli.py", "breeze/agent.py", "breeze/chat.py", "breeze/session.py", "breeze/tools/shell.py"):
        assert member in wheel_names, member


def test_wheel_excludes_eval_harness(wheel_names: set[str]) -> None:
    """评估框架属于开发面，不进发行包。"""
    assert not any(name.endswith("eval.py") for name in wheel_names)
    assert not any("breeze_eval" in name for name in wheel_names)


def test_wheel_excludes_repo_only_dirs(wheel_names: set[str]) -> None:
    """tests/scripts/tasks/fixtures 都不该出现在 wheel 里。"""
    assert not any(
        name.startswith(("tests/", "scripts/", "tasks/", "fixtures/", "breeze_eval/")) for name in wheel_names
    )


def test_wheel_version_matches_package_version(wheel_names: set[str]) -> None:
    """发行包版本与 breeze.__version__ 同源，不再各自漂移。"""
    assert f"breeze-{__version__}.dist-info/METADATA" in wheel_names


def test_harness_importable_from_repo_root() -> None:
    """包外模块仍可从仓库根导入（tests 与 scripts/eval.py 依赖它）。"""
    from breeze_eval import harness

    assert callable(harness.run_eval)
    assert Path(harness.__file__).name == "harness.py"


def test_sdist_keeps_reproducible_sources(sdist_tops: set[str]) -> None:
    """sdist 保留 tests 与其依赖的评估框架，可从发行包跑测试。"""
    for entry in ("src", "tests", "breeze_eval", "tasks", "fixtures", "samples", "scripts"):
        assert entry in sdist_tops, entry


def test_sdist_excludes_run_artifacts(sdist_tops: set[str]) -> None:
    """trace 与报告是本机运行产物，不进发行包。"""
    for entry in ("traces", "reports", "dist", "build"):
        assert entry not in sdist_tops, entry


def test_litellm_bound_avoids_the_mcp_requiring_range() -> None:
    """litellm 上界要挡住 1.106：它带 tools 的调用会 import 未声明的 mcp。

    litellm 1.106.0 起 ``completion(tools=...)`` 无条件
    ``from litellm.responses.mcp.chat_completions_handler import ...``，
    而 mcp 只声明在 proxy/mcp extra 里；Breeze 每轮都带工具 schema，
    于是新鲜安装直接 ``ModuleNotFoundError: No module named 'mcp'``。
    """
    import tomllib

    from packaging.requirements import Requirement

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    litellm_req = next(
        (Requirement(dep) for dep in pyproject["project"]["dependencies"] if dep.startswith("litellm")), None
    )
    assert litellm_req is not None, "pyproject 里应声明 litellm 依赖"

    assert not litellm_req.specifier.contains("1.106.6"), litellm_req
    assert litellm_req.specifier.contains("1.105.7"), litellm_req
