import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """每个测试用独立的用户数据目录，不读写真实的全局配置和作品。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NYASUB_HOME", str(home))
    return home


@pytest.fixture
def sample(tmp_path):
    """复制一份测试样片（40 秒：配音 + 硬字幕 + 只覆盖一部分的外挂字幕）。"""
    d = tmp_path / "work"
    d.mkdir()
    for name in ("sample.mp4", "sample.srt"):
        shutil.copy(FIXTURES / name, d / name)
    return d / "sample.mp4"


@pytest.fixture
def api_config(tmp_path):
    p = tmp_path / "api.toml"
    p.write_text('[llm]\nmode = "api"\nmodel = "fake"\napi_key_env = "NYASUB_TEST_KEY"\n', encoding="utf-8")
    return p
