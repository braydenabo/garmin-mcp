"""Client config writers, against a temporary home folder."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from garmin_connect_personal_mcp import cli, clients, config

ENTRY = clients.Entry("/opt/py/bin/python", ["-m", "garmin_connect_personal_mcp", "serve"])


def test_entry_uses_this_python_and_passes_only_the_data_dir() -> None:
    entry = clients.server_entry(
        {"GARMIN_MCP_DATA_DIR": "/d", "GARMIN_PERSONAL_API_KEY": "fake-key-123"}
    )
    assert entry.command == sys.executable
    assert entry.args == ["-m", "garmin_connect_personal_mcp", "serve"]
    assert entry.env == {"GARMIN_MCP_DATA_DIR": "/d"}
    assert clients.server_entry({}).env == {}


# --- JSON-file clients


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", "Library/Application Support/Claude/claude_desktop_config.json"),
        ("linux", ".config/Claude/claude_desktop_config.json"),
    ],
)
def test_claude_desktop_paths(tmp_path: Path, platform: str, expected: str) -> None:
    assert clients.ClaudeDesktop(tmp_path, platform).path() == tmp_path / expected


def test_claude_desktop_windows_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    path = clients.ClaudeDesktop(tmp_path, "win32").path()
    assert path == tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"


@pytest.mark.parametrize("key", ["claude-desktop", "cursor", "gemini"])
def test_json_write_creates_file(tmp_path: Path, key: str) -> None:
    client = clients.get_client(key, tmp_path, "darwin")
    assert isinstance(client, clients.JsonFileClient)
    result = client.write(ENTRY)
    assert result.changed
    config = json.loads(client.path().read_text())
    assert config == {"mcpServers": {"garmin": ENTRY.json()}}
    assert not list(client.path().parent.glob("*.bak-*"))  # nothing to back up


def test_json_write_keeps_other_servers_and_settings_and_backs_up(tmp_path: Path) -> None:
    client = clients.Cursor(tmp_path)
    client.path().parent.mkdir(parents=True)
    original = {"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}
    client.path().write_text(json.dumps(original))
    result = client.write(ENTRY)
    config = json.loads(client.path().read_text())
    assert config["mcpServers"]["other"] == {"command": "x"}
    assert config["theme"] == "dark"
    assert config["mcpServers"]["garmin"] == ENTRY.json()
    backups = list(client.path().parent.glob("mcp.json.bak-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original
    assert "Backup:" in result.message


def test_json_write_is_idempotent_and_updates(tmp_path: Path) -> None:
    client = clients.GeminiCli(tmp_path)
    client.write(ENTRY)
    again = client.write(ENTRY)
    assert not again.changed and "already set up" in again.message
    moved = clients.Entry("/new/python", ENTRY.args)
    updated = client.write(moved)
    assert updated.changed and "Updated the existing" in updated.message
    assert json.loads(client.path().read_text())["mcpServers"]["garmin"]["command"] == "/new/python"


def test_json_write_includes_env(tmp_path: Path) -> None:
    client = clients.Cursor(tmp_path)
    client.write(clients.Entry("/py", ["-m", "x"], {"GARMIN_MCP_DATA_DIR": "/d"}))
    spec = json.loads(client.path().read_text())["mcpServers"]["garmin"]
    assert spec["env"] == {"GARMIN_MCP_DATA_DIR": "/d"}


@pytest.mark.parametrize(
    "content", ['{ // a comment\n "mcpServers": {} }', "[1, 2]", '{"mcpServers": []}']
)
def test_json_write_refuses_files_it_cant_safely_edit(tmp_path: Path, content: str) -> None:
    client = clients.GeminiCli(tmp_path)
    client.path().parent.mkdir(parents=True)
    client.path().write_text(content)
    with pytest.raises(clients.ClientError, match="left unchanged"):
        client.write(ENTRY)
    assert client.path().read_text() == content


def test_json_write_handles_empty_file(tmp_path: Path) -> None:
    client = clients.Cursor(tmp_path)
    client.path().parent.mkdir(parents=True)
    client.path().write_text("")
    assert client.write(ENTRY).changed


def test_json_preview_shows_path_and_snippet(tmp_path: Path) -> None:
    preview = clients.Cursor(tmp_path).preview(ENTRY)
    assert str(tmp_path / ".cursor" / "mcp.json") in preview
    assert '"mcpServers"' in preview and '"garmin"' in preview
    assert not (tmp_path / ".cursor").exists()  # preview never writes


# --- CLI-configured clients


class FakeRun:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.code, "", "boom" if self.code else "")


def test_claude_code_command_puts_name_before_env(tmp_path: Path) -> None:
    entry = clients.Entry("/py", ["-m", "x", "serve"], {"GARMIN_MCP_DATA_DIR": "/d"})
    assert clients.ClaudeCode(tmp_path).command(entry) == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "garmin",
        "-e",
        "GARMIN_MCP_DATA_DIR=/d",
        "--",
        "/py",
        "-m",
        "x",
        "serve",
    ]


def test_codex_command(tmp_path: Path) -> None:
    entry = clients.Entry("/py", ["-m", "x", "serve"], {"GARMIN_MCP_DATA_DIR": "/d"})
    assert clients.Codex(tmp_path).command(entry) == [
        "codex",
        "mcp",
        "add",
        "garmin",
        "--env",
        "GARMIN_MCP_DATA_DIR=/d",
        "--",
        "/py",
        "-m",
        "x",
        "serve",
    ]


def test_cli_client_runs_its_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clients.shutil, "which", lambda name: f"/usr/bin/{name}")
    run = FakeRun()
    result = clients.ClaudeCode(tmp_path).write(ENTRY, run)
    assert result.changed and run.calls[0][:3] == ["claude", "mcp", "add"]


def test_cli_client_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clients.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(clients.ClientError, match="boom"):
        clients.Codex(tmp_path).write(ENTRY, FakeRun(code=1))


def test_cli_client_missing_tool_prints_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    with pytest.raises(clients.ClientError, match="codex mcp add garmin"):
        clients.Codex(tmp_path).write(ENTRY, FakeRun())


# --- VS Code and lookup


def test_vscode_snippet_only(tmp_path: Path) -> None:
    client = clients.VsCode(tmp_path)
    snippet = json.loads(client.snippet(ENTRY))
    assert snippet == {"servers": {"garmin": {"type": "stdio", **ENTRY.json()}}}
    assert "MCP: Open User Configuration" in client.preview(ENTRY)
    with pytest.raises(clients.ClientError, match="no fixed location"):
        client.write(ENTRY)


def test_get_client() -> None:
    assert set(clients.CLIENTS) == {
        "claude-desktop",
        "claude-code",
        "cursor",
        "vscode",
        "gemini",
        "codex",
    }
    with pytest.raises(clients.ClientError, match="Choose from"):
        clients.get_client("chatgpt")


# --- CLI


def test_cli_lists_clients(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install-client"]) == 0
    out = capsys.readouterr().out
    assert all(key in out for key in clients.CLIENTS)
    assert "ChatGPT" in out


def test_cli_preview_changes_nothing(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["install-client", "cursor"]) == 0
    captured = capsys.readouterr()
    assert '"mcpServers"' in captured.out and "--write" in captured.out
    assert "no API key saved" in captured.err
    assert not (isolated_home / ".cursor").exists()


def test_cli_write(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config.save_api_key("fake-key-123456")
    assert cli.main(["install-client", "gemini", "--write"]) == 0
    captured = capsys.readouterr()
    assert "Added the 'garmin' server" in captured.out and captured.err == ""
    saved = json.loads((isolated_home / ".gemini" / "settings.json").read_text())
    assert saved["mcpServers"]["garmin"]["command"] == sys.executable
    assert "fake-key-123456" not in json.dumps(saved)


def test_cli_write_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["install-client", "vscode", "--write"]) == 1
    assert "MCP: Open User Configuration" in capsys.readouterr().err


def test_cli_warns_about_uvx_cache(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.sys, "executable", "/Users/x/.cache/uv/archive-v0/abc/bin/python")
    assert cli.main(["install-client", "cursor"]) == 0
    assert "uv tool install" in capsys.readouterr().err


def test_cli_rejects_unknown_client(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["install-client", "chatgpt"])
    assert "invalid choice" in capsys.readouterr().err
