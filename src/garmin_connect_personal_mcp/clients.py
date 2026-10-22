"""Add this server to an MCP client's configuration.

Each client keeps its server list somewhere different. Clients configured
through a JSON file (Claude Desktop, Cursor, Gemini CLI) get the file edited:
only the `garmin` entry is touched, the old file is backed up first, and a
file that isn't plain JSON is left alone. Clients with their own CLI (Claude
Code, Codex) are configured through that CLI. VS Code's user file has no
stable path, so it gets a snippet to paste.

The server entry runs the same Python that runs this command, so it works for
any install method and needs no PATH lookup.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from garmin_connect_personal_mcp.config import DATA_DIR_ENV_VAR

SERVER_NAME = "garmin"


@dataclass(frozen=True)
class Entry:
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)

    def json(self, **extra: str) -> dict[str, Any]:
        spec: dict[str, Any] = {**extra, "command": self.command, "args": self.args}
        if self.env:
            spec["env"] = self.env
        return spec

    def argv(self) -> list[str]:
        return [self.command, *self.args]


def server_entry(env: dict[str, str] | None = None) -> Entry:
    environment = dict(os.environ) if env is None else env
    passed = (
        {DATA_DIR_ENV_VAR: environment[DATA_DIR_ENV_VAR]}
        if environment.get(DATA_DIR_ENV_VAR)
        else {}
    )
    return Entry(sys.executable, ["-m", "garmin_connect_personal_mcp", "serve"], passed)


class ClientError(Exception):
    """Configuration couldn't be written; the message says what to do instead."""


@dataclass(frozen=True)
class Result:
    changed: bool
    message: str


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603


class Client:
    key = ""
    title = ""
    doc = ""
    restart_hint = ""

    def __init__(self, home: Path, platform: str = sys.platform) -> None:
        self.home = home
        self.platform = platform

    def preview(self, entry: Entry) -> str:
        """What will be added, and where, as text for the user."""
        raise NotImplementedError

    def write(self, entry: Entry, run: Runner = _run) -> Result:
        raise NotImplementedError


class JsonFileClient(Client):
    """A client whose servers live under a key in a JSON file."""

    servers_key = "mcpServers"

    def path(self) -> Path:
        raise NotImplementedError

    def spec(self, entry: Entry) -> dict[str, Any]:
        return entry.json()

    def snippet(self, entry: Entry) -> str:
        return json.dumps({self.servers_key: {SERVER_NAME: self.spec(entry)}}, indent=2)

    def preview(self, entry: Entry) -> str:
        return f"Add this to {self.path()}:\n\n{self.snippet(entry)}\n"

    def write(self, entry: Entry, run: Runner = _run) -> Result:
        path = self.path()
        config: dict[str, Any] = {}
        if path.exists():
            text = path.read_text(encoding="utf-8")
            try:
                config = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                raise ClientError(
                    f"{path} isn't plain JSON (it may contain comments), so it was left "
                    f"unchanged. Add this yourself:\n\n{self.snippet(entry)}"
                ) from None
            if not isinstance(config, dict) or not isinstance(
                config.get(self.servers_key, {}), dict
            ):
                raise ClientError(f"{path} has an unexpected layout, so it was left unchanged.")
        servers = config.setdefault(self.servers_key, {})
        new = self.spec(entry)
        if servers.get(SERVER_NAME) == new:
            return Result(False, f"{self.title} is already set up ({path}).")
        replacing = SERVER_NAME in servers
        backup = None
        if path.exists():
            backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
            shutil.copy2(path, backup)
        servers[SERVER_NAME] = new
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        verb = "Updated the existing" if replacing else "Added the"
        saved = f" Backup: {backup}." if backup else ""
        return Result(True, f"{verb} '{SERVER_NAME}' server in {path}.{saved} {self.restart_hint}")


class ClaudeDesktop(JsonFileClient):
    key = "claude-desktop"
    title = "Claude Desktop"
    doc = "docs/clients/claude-desktop.md"
    restart_hint = "Quit and reopen Claude Desktop to load it."

    def path(self) -> Path:
        if self.platform == "darwin":
            return self.home / "Library/Application Support/Claude/claude_desktop_config.json"
        if self.platform == "win32":
            appdata = os.environ.get("APPDATA") or str(self.home / "AppData/Roaming")
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
        return self.home / ".config/Claude/claude_desktop_config.json"


class Cursor(JsonFileClient):
    key = "cursor"
    title = "Cursor"
    doc = "docs/clients/cursor.md"
    restart_hint = "Restart Cursor, then check Settings > MCP."

    def path(self) -> Path:
        return self.home / ".cursor" / "mcp.json"


class GeminiCli(JsonFileClient):
    key = "gemini"
    title = "Gemini CLI"
    doc = "docs/clients/gemini-cli.md"
    restart_hint = "Start a new gemini session and run /mcp to check it."

    def path(self) -> Path:
        return self.home / ".gemini" / "settings.json"


class CliClient(Client):
    """A client configured through its own command-line tool."""

    tool = ""

    def command(self, entry: Entry) -> list[str]:
        raise NotImplementedError

    def preview(self, entry: Entry) -> str:
        return f"Run:\n\n{shlex.join(self.command(entry))}\n"

    def write(self, entry: Entry, run: Runner = _run) -> Result:
        if shutil.which(self.tool) is None:
            raise ClientError(
                f"`{self.tool}` isn't on your PATH. Install {self.title}, or run this yourself:\n\n"
                f"{shlex.join(self.command(entry))}"
            )
        result = run(self.command(entry))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ClientError(f"`{self.tool} mcp add` failed: {detail or result.returncode}")
        return Result(
            True, f"Added the '{SERVER_NAME}' server to {self.title}. {self.restart_hint}"
        )


class ClaudeCode(CliClient):
    key = "claude-code"
    title = "Claude Code"
    tool = "claude"
    doc = "docs/clients/claude-code.md"
    restart_hint = "Start a new session and run /mcp to check it."

    def command(self, entry: Entry) -> list[str]:
        # -e takes several values, so it must come after the name or it would swallow it.
        env = [arg for k, v in entry.env.items() for arg in ("-e", f"{k}={v}")]
        return ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, *env, "--", *entry.argv()]


class Codex(CliClient):
    key = "codex"
    title = "Codex CLI"
    tool = "codex"
    doc = "docs/clients/codex.md"
    restart_hint = "Start a new codex session and run /mcp to check it."

    def command(self, entry: Entry) -> list[str]:
        env = [arg for k, v in entry.env.items() for arg in ("--env", f"{k}={v}")]
        return ["codex", "mcp", "add", SERVER_NAME, *env, "--", *entry.argv()]


class VsCode(Client):
    key = "vscode"
    title = "VS Code"
    doc = "docs/clients/vscode.md"

    def snippet(self, entry: Entry) -> str:
        return json.dumps({"servers": {SERVER_NAME: entry.json(type="stdio")}}, indent=2)

    def preview(self, entry: Entry) -> str:
        return (
            "In VS Code, run the command **MCP: Open User Configuration** and add:\n\n"
            f"{self.snippet(entry)}\n"
        )

    def write(self, entry: Entry, run: Runner = _run) -> Result:
        raise ClientError(
            "VS Code's user MCP file has no fixed location, so it can't be edited automatically. "
            f"{self.preview(entry)}"
        )


CLIENTS: dict[str, type[Client]] = {
    c.key: c for c in (ClaudeDesktop, ClaudeCode, Cursor, VsCode, GeminiCli, Codex)
}


def get_client(key: str, home: Path | None = None, platform: str = sys.platform) -> Client:
    try:
        cls = CLIENTS[key]
    except KeyError:
        raise ClientError(f"Unknown client {key!r}. Choose from: {', '.join(CLIENTS)}.") from None
    return cls(home or Path.home(), platform)
