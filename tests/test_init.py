import io
from pathlib import Path

import pytest

from garmin_connect_personal_mcp import cli, config


def key_file() -> Path:
    return config.api_key_file()


def test_init_saves_key_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("fake-key-123\n"))
    assert cli.main(["init", "--key-stdin"]) == 0
    assert key_file().read_text(encoding="utf-8") == "fake-key-123\n"
    out = capsys.readouterr().out
    assert "fake-key-123" not in out
    assert config.data_paths().fit_dir.is_dir()


def test_init_prompts_with_hidden_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt: "fake-key-123")
    assert cli.main(["init"]) == 0
    assert key_file().read_text(encoding="utf-8") == "fake-key-123\n"


def test_init_keeps_existing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config.save_api_key("original-key")
    monkeypatch.setattr("sys.stdin", io.StringIO("new-key"))
    assert cli.main(["init", "--key-stdin"]) == 0
    assert key_file().read_text(encoding="utf-8") == "original-key\n"
    assert "--force" in capsys.readouterr().out


def test_init_force_replaces_key(monkeypatch: pytest.MonkeyPatch) -> None:
    config.save_api_key("original-key")
    monkeypatch.setattr("sys.stdin", io.StringIO("new-key"))
    assert cli.main(["init", "--key-stdin", "--force"]) == 0
    assert key_file().read_text(encoding="utf-8") == "new-key\n"


def test_init_uses_env_key_without_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "fake-key-123")
    assert cli.main(["init"]) == 0
    assert not key_file().exists()
    assert "GARMIN_PERSONAL_API_KEY" in capsys.readouterr().out


def test_init_rejects_empty_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["init", "--key-stdin"]) == 1
    assert not key_file().exists()
    assert "empty" in capsys.readouterr().err
