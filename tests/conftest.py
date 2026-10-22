from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME and the data dir at a temp folder so tests never touch real keys or data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY", raising=False)
    return home
