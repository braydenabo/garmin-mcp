"""Opt-in, per-user macOS launchd service. No secrets in service definitions or logs."""

import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from garmin_connect_personal_mcp import (
    chatgpt,
    config,
    tunnel_credentials,
    tunnel_install,
    tunnel_process,
)
from garmin_connect_personal_mcp.sync import SyncAlreadyRunning, sync_lock

LABEL_PREFIX = "garmin-connect-personal-mcp.chatgpt."
PROG = chatgpt.PROG


def _supported() -> bool:
    return sys.platform == "darwin"


class BackgroundError(Exception):
    """An actionable error safe to display without upstream output."""


def _command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/launchctl", *args], capture_output=True, text=True, check=False, timeout=15
    )


class LaunchAgent:
    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.label = LABEL_PREFIX + profile
        self.path = Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"
        self.domain = f"gui/{getattr(os, 'getuid', lambda: 0)()}"
        self.target = f"{self.domain}/{self.label}"

    def spec(self) -> dict[str, Any] | None:
        if self.path.is_symlink():
            raise BackgroundError("Refusing a linked service file. Review it manually first.")
        if not self.path.exists():
            return None
        try:
            spec = plistlib.loads(self.path.read_bytes())
            args = spec.get("ProgramArguments", [])
            if (
                spec.get("Label") != self.label
                or len(args) != 9
                or not all(isinstance(arg, str) for arg in args)
                or args[1:5]
                != [
                    "-m",
                    "garmin_connect_personal_mcp",
                    "chatgpt-background",
                    "run",
                ]
                or args[5:8] != ["--profile", self.profile, "--client"]
                or not Path(args[0]).is_absolute()
                or not Path(args[8]).is_absolute()
                or set(spec.get("EnvironmentVariables", {})) != {config.DATA_DIR_ENV_VAR}
                or not Path(spec["EnvironmentVariables"][config.DATA_DIR_ENV_VAR]).is_absolute()
            ):
                raise ValueError("Not our service")
            return dict(spec)
        except (ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
            raise BackgroundError(
                "Unrecognized service file. Review it manually before replacing it."
            ) from None

    def loaded(self) -> subprocess.CompletedProcess[str]:
        result = _command(["print", self.target])
        if result.returncode not in (0, 113):
            raise BackgroundError(
                "Could not inspect the macOS login agent. Check session permissions."
            )
        return result

    def start(self) -> None:
        if self.spec() is None:
            raise BackgroundError(f"Not installed. Run: {PROG} chatgpt-background install")
        if self.loaded().returncode == 0:
            result = _command(["kickstart", self.target])
        else:
            result = _command(["bootstrap", self.domain, str(self.path)])
        if result.returncode:
            raise BackgroundError(
                "macOS could not start the tunnel agent. Check System Settings > General > "
                "Login Items, then retry chatgpt-background start."
            )

    def stop(self) -> None:
        if self.loaded().returncode == 0 and _command(["bootout", self.target]).returncode:
            raise BackgroundError(
                "macOS could not stop the tunnel agent. Retry before uninstalling."
            )

    def install(self, executable: str) -> None:
        if self.spec() is not None:
            raise BackgroundError(
                "Already installed. Use chatgpt-background start, or uninstall to reconfigure."
            )
        paths = config.data_paths()
        config.ensure_data_dir(paths)
        spec: dict[str, Any] = {
            "Label": self.label,
            "ProgramArguments": [
                sys.executable,
                "-m",
                "garmin_connect_personal_mcp",
                "chatgpt-background",
                "run",
                "--profile",
                self.profile,
                "--client",
                executable,
            ],
            "EnvironmentVariables": {config.DATA_DIR_ENV_VAR: str(paths.root.absolute())},
            "WorkingDirectory": str(paths.root.absolute()),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 60,
            "ProcessType": "Background",
            "StandardOutPath": os.devnull,
            "StandardErrorPath": os.devnull,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = plistlib.dumps(spec)
        # Exclusive, owner-only creation protects unrelated files and private local paths.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            self.start()
        except (BackgroundError, OSError, subprocess.SubprocessError):
            self.stop()
            self.path.unlink()
            raise


def _client() -> str:
    found = shutil.which("tunnel-client")
    executable = Path(found) if found else tunnel_install.installed_path()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BackgroundError(f"Tunnel client missing. Run: {PROG} setup-chatgpt --no-start")
    return str(executable.absolute())


def _runtime_env(profile: str) -> dict[str, str]:
    key = tunnel_credentials.load(profile)
    if not key or any(c.isspace() for c in key):
        raise BackgroundError(
            f"No usable saved key. Run: {PROG} setup-chatgpt --profile {profile} --no-start "
            "and accept OS-keychain storage. Background startup does not use shell API keys."
        )
    # The read-only MCP subprocess does not need unrelated OpenAI or Garmin credentials.
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            chatgpt.KEY_ENV,
            "OPENAI_API_KEY",
            "OPENAI_ADMIN_KEY",
            config.API_KEY_ENV_VAR,
        )
    }
    env[chatgpt.KEY_ENV] = key
    return env


def _check(executable: str, profile: str, data_dir: str | None = None) -> dict[str, str]:
    paths = config.DataPaths(Path(data_dir)) if data_dir else config.data_paths()
    if not paths.db.is_file():
        raise BackgroundError(f"Archive missing. Run: {PROG} init, then {PROG} sync")
    if not chatgpt._profile_exists(executable, profile):
        raise BackgroundError(f"Tunnel profile missing. Run: {PROG} setup-chatgpt --no-start")
    env = _runtime_env(profile)
    env[config.DATA_DIR_ENV_VAR] = str(paths.root.absolute())
    if not chatgpt._check_authentication(executable, profile, env):
        raise BackgroundError(
            f"Tunnel access check failed. Check your connection, or run: {PROG} "
            f"setup-chatgpt --profile {profile} --replace-key --no-start"
        )
    return env


def manage(action: str, *, profile: str, check: bool = False, client: str | None = None) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", profile):
        print("Profile must be 1-64 letters, numbers, underscores or hyphens.", file=sys.stderr)
        return 2
    if not _supported():
        print("Background startup currently supports macOS only. Use setup-chatgpt instead.")
        return 1
    try:
        agent = LaunchAgent(profile)
        if action == "run":
            if client is None or not Path(client).is_absolute():
                raise BackgroundError(
                    "The service needs an absolute tunnel-client path; reinstall it."
                )
            return tunnel_process.run(client, profile, _check(client, profile), quiet=True)
        if action == "status":
            spec = agent.spec()
            if spec is None:
                print(f"Not installed. Run: {PROG} chatgpt-background install --profile {profile}")
                return 0
            loaded = agent.loaded()
            if loaded.returncode:
                print("Installed, stopped. Use chatgpt-background start; login also starts it.")
            elif re.search(r"^\s*state = running\s*$", loaded.stdout, re.MULTILINE):
                print("Background process is running; tunnel connectivity is not yet verified.")
            else:
                print(
                    "Agent loaded but not running; macOS retries with a 60-second launch throttle."
                )
                print("Run chatgpt-background status --check for setup/authentication diagnostics.")
            if check:
                executable = spec["ProgramArguments"][-1]
                _check(executable, profile, spec["EnvironmentVariables"][config.DATA_DIR_ENV_VAR])
                print("Profile and metadata access OK; test a tool in ChatGPT to finish.")
            return 0
        if action == "stop":
            if agent.spec() is None:
                print("No background tunnel installed.")
                return 0
            agent.stop()
            print("Background tunnel stopped until you start it again or log in next time.")
            return 0
        if action == "uninstall":
            if agent.spec() is None:
                print("No background tunnel installed.")
                return 0
            agent.stop()
            agent.path.unlink()
            print(
                "Background agent removed. Your keychain key, tunnel profile and archive are kept."
            )
            return 0
        if action not in ("install", "start"):
            raise BackgroundError(
                "Unknown background action. Use install, start, stop, status or uninstall."
            )
        if action == "install" and (
            "archive-v" in sys.executable or not Path(sys.executable).is_absolute()
        ):
            raise BackgroundError(
                "Install a persistent copy with uv tool install before enabling startup."
            )
        executable = (
            _client()
            if action == "install"
            else str((agent.spec() or {}).get("ProgramArguments", [""])[-1])
        )
        if not executable:
            raise BackgroundError("Not installed. Run chatgpt-background install first.")
        spec = agent.spec() if action == "start" else None
        env = _check(
            executable,
            profile,
            spec["EnvironmentVariables"][config.DATA_DIR_ENV_VAR] if spec else None,
        )
        try:
            with sync_lock(tunnel_process.lock_path(profile, env)):
                pass
        except SyncAlreadyRunning:
            if action == "start" and agent.loaded().returncode == 0:
                print("Background tunnel is already running.")
                return 0
            raise BackgroundError(
                "Stop the foreground tunnel with Ctrl-C before enabling background startup."
            ) from None
        if action == "install":
            agent.install(executable)
        else:
            agent.start()
        print("Background startup enabled. macOS will run the tunnel now and at login.")
        print("Check: chatgpt-background status --check. Your computer must stay awake and online.")
        return 0
    except (BackgroundError, tunnel_credentials.CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print(
            "Could not manage the agent. Check installation, keychain and macOS permissions.",
            file=sys.stderr,
        )
        return 1
