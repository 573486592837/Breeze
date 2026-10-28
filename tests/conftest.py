"""测试通用 fixture。"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把会话目录指向临时路径。

    chat 相关测试会真的写文件；不隔离时会污染用户真实的
    ~/.breeze/sessions，并被 `breeze chat --resume latest` 选中。
    """
    from breeze.config import settings

    monkeypatch.setattr(settings, "sessions_dir", tmp_path / "sessions")
