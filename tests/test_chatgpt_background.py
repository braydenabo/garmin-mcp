"""All service management, credential access and network checks are replaced with fakes."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from garmin_connect_personal_mcp import chatgpt, cli, config, tunnel_credentials, tunnel_process
from garmin_connect_personal_mcp import chatgpt_background as background
from garmin_connect_personal_mcp.sync import sync_lock

MACOS = sys.platform == "darwin"


class FakeLaunchctl:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded = False
        self.state = "running"
        self.fail = ""

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        code, text = 0, ""
        if args[0] == self.fail:
            code, text = 1, "synthetic-sensitive-upstream-output"
        elif args[0] == "print":
            code = 0 if self.loaded else 113
            text = f"state = {self.state}\n"
        elif args[0] in ("bootstrap", "kickstart"):
            self.loaded = True
        elif args[0] == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(args, code, text, text)


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeLaunchctl:
    paths = config.data_paths()
    config.ensure_data_dir(paths)
    paths.db.touch()
    executable = tmp_path / "tools with spaces" / "tunnel-client"
    executable.parent.mkdir()
    executable.touch()
    executable.chmod(0o700)
    monkeypatch.setattr(background, "_supported", lambda: True)
    monkeypatch.setattr(background.shutil, "which", lambda name: str(executable))
    monkeypatch.setattr(chatgpt, "_profile_exists", lambda executable, profile: True)
    monkeypatch.setattr(chatgpt, "_check_authentication", lambda *a, **kw: True)
    monkeypatch.setattr(tunnel_credentials, "load", lambda profile: "synthetic-saved-key")
    # Use a persistent-looking path without relying on the CI host's Python location.
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python env" / "python"))
    runner = FakeLaunchctl()
    monkeypatch.setattr(background, "_command", runner)
    return runner


def agent(profile: str = "garmin-local") -> background.LaunchAgent:
    return background.LaunchAgent(profile)


def test_install_status_stop_start_uninstall(
    prepared: FakeLaunchctl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["chatgpt-background", "status"]) == 0
    assert "Not installed" in capsys.readouterr().out
    assert cli.main(["chatgpt-background", "install"]) == 0
    assert agent().path.is_file()
    spec = plistlib.loads(agent().path.read_bytes())
    assert spec["RunAtLoad"] is True and spec["KeepAlive"] is True
    assert spec["ThrottleInterval"] == 60
    assert spec["ProgramArguments"][0] == sys.executable
    assert spec["ProgramArguments"][-2] == "--client"
    assert "tools with spaces" in spec["ProgramArguments"][-1]
    assert spec["EnvironmentVariables"] == {config.DATA_DIR_ENV_VAR: str(config.data_paths().root)}
    assert spec["StandardOutPath"] == os.devnull
    assert "synthetic-saved-key" not in agent().path.read_text()
    if os.name == "posix":
        assert agent().path.stat().st_mode & 0o777 == 0o600
    assert cli.main(["chatgpt-background", "status", "--check"]) == 0
    out = capsys.readouterr().out
    assert "running" in out and "metadata access OK" in out
    assert cli.main(["chatgpt-background", "stop"]) == 0
    assert not prepared.loaded and agent().path.exists()
    assert cli.main(["chatgpt-background", "status"]) == 0
    assert "stopped" in capsys.readouterr().out
    assert cli.main(["chatgpt-background", "start"]) == 0
    assert prepared.loaded
    assert cli.main(["chatgpt-background", "uninstall"]) == 0
    assert not agent().path.exists() and not prepared.loaded
    assert config.data_paths().db.exists()
    assert "archive are kept" in capsys.readouterr().out
    assert cli.main(["chatgpt-background", "uninstall"]) == 0
    assert cli.main(["chatgpt-background", "stop"]) == 0


def test_existing_install_is_preserved(prepared: FakeLaunchctl) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    original = agent().path.read_bytes()
    assert cli.main(["chatgpt-background", "install"]) == 1
    assert agent().path.read_bytes() == original


@pytest.mark.parametrize("missing", ["key", "profile", "archive", "client", "authentication"])
def test_failed_preflight_never_installs(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    if missing == "key":
        monkeypatch.setattr(tunnel_credentials, "load", lambda profile: None)
    elif missing == "profile":
        monkeypatch.setattr(chatgpt, "_profile_exists", lambda *a: False)
    elif missing == "archive":
        config.data_paths().db.unlink()
    elif missing == "client":
        monkeypatch.setattr(background.shutil, "which", lambda name: None)
        monkeypatch.setattr(background.tunnel_install, "installed_path", lambda: Path("missing"))
    else:
        monkeypatch.setattr(chatgpt, "_check_authentication", lambda *a, **kw: False)
    assert cli.main(["chatgpt-background", "install"]) == 1
    assert not agent().path.exists()
    assert not prepared.calls


def test_uses_saved_key_not_shell_keys(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (chatgpt.KEY_ENV, "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", config.API_KEY_ENV_VAR):
        monkeypatch.setenv(name, "synthetic-stale-environment-key")
    env = background._runtime_env("garmin-local")
    assert env[chatgpt.KEY_ENV].endswith("saved-key")
    assert not {"OPENAI_API_KEY", "OPENAI_ADMIN_KEY", config.API_KEY_ENV_VAR} & env.keys()
    assert cli.main(["chatgpt-background", "install"]) == 0
    assert "synthetic-" not in agent().path.read_text()
    assert "synthetic-" not in str(capsys.readouterr())


def test_locked_keychain_is_actionable_and_private(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def locked(profile: str) -> None:
        raise tunnel_credentials.CredentialError("Unlock your OS keychain and retry.")

    monkeypatch.setattr(tunnel_credentials, "load", locked)
    assert cli.main(["chatgpt-background", "install"]) == 1
    assert "keychain" in capsys.readouterr().err
    assert not agent().path.exists()


def test_failed_bootstrap_rolls_back_only_new_definition(
    prepared: FakeLaunchctl, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared.fail = "bootstrap"
    assert cli.main(["chatgpt-background", "install"]) == 1
    assert not agent().path.exists()
    assert "synthetic-sensitive" not in str(capsys.readouterr())


def test_failed_stop_keeps_definition(prepared: FakeLaunchctl) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    prepared.fail = "bootout"
    assert cli.main(["chatgpt-background", "uninstall"]) == 1
    assert agent().path.exists()


@pytest.mark.parametrize("contents", [b"not a plist", plistlib.dumps({"Label": "other"})])
def test_unrecognized_files_are_preserved(prepared: FakeLaunchctl, contents: bytes) -> None:
    target = agent().path
    target.parent.mkdir(parents=True)
    target.write_bytes(contents)
    assert cli.main(["chatgpt-background", "uninstall"]) == 1
    assert target.read_bytes() == contents
    assert not prepared.calls


def test_start_missing_install_is_actionable(prepared: FakeLaunchctl) -> None:
    assert cli.main(["chatgpt-background", "start"]) == 1
    assert not prepared.calls


def test_status_does_not_claim_connectivity(
    prepared: FakeLaunchctl, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    prepared.state = "waiting"
    assert cli.main(["chatgpt-background", "status"]) == 0
    assert "not running" in capsys.readouterr().out


def test_service_keeps_installed_data_directory(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saved = config.data_paths().root
    assert cli.main(["chatgpt-background", "install"]) == 0
    monkeypatch.setenv(config.DATA_DIR_ENV_VAR, str(tmp_path / "different archive"))
    check = Mock(return_value=True)
    monkeypatch.setattr(chatgpt, "_check_authentication", check)
    assert cli.main(["chatgpt-background", "status", "--check"]) == 0
    assert check.call_args.args[2][config.DATA_DIR_ENV_VAR] == str(saved)


def test_foreground_instance_blocks_install(prepared: FakeLaunchctl) -> None:
    with sync_lock(tunnel_process.lock_path("garmin-local")):
        assert cli.main(["chatgpt-background", "install"]) == 1
    assert not agent().path.exists()


def test_start_is_idempotent_for_running_instance(prepared: FakeLaunchctl) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    with sync_lock(tunnel_process.lock_path("garmin-local")):
        assert cli.main(["chatgpt-background", "start"]) == 0
    assert prepared.loaded


def test_internal_runner_passes_only_saved_key(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = Mock(return_value=7)
    monkeypatch.setattr(tunnel_process, "run", run)
    assert cli.main(["chatgpt-background", "run", "--client", str(tmp_path / "client")]) == 7
    assert run.call_args.kwargs == {"quiet": True}
    assert run.call_args.args[2][chatgpt.KEY_ENV].endswith("saved-key")
    assert cli.main(["chatgpt-background", "run", "--client", "relative"]) == 1


def test_unsupported_platform_and_invalid_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(background, "_supported", lambda: False)
    assert cli.main(["chatgpt-background", "install"]) == 1
    assert "macOS only" in capsys.readouterr().out
    assert cli.main(["chatgpt-background", "install", "--profile", "../unsafe"]) == 2


def test_temporary_python_is_rejected(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "archive-v0" / "python"))
    assert cli.main(["chatgpt-background", "install"]) == 1
    assert not agent().path.exists()


def test_command_captures_output_and_has_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(background.subprocess, "run", run)
    background._command(["print", "gui/123/synthetic"])
    assert run.call_args.args[0] == ["/bin/launchctl", "print", "gui/123/synthetic"]
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["timeout"] == 15


@pytest.mark.skipif(not MACOS, reason="macOS plist validator")
def test_launchagent_passes_native_plist_validation(prepared: FakeLaunchctl) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    checked = subprocess.run(
        ["/usr/bin/plutil", "-lint", "--", str(agent().path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr


@pytest.mark.skipif(os.name == "nt", reason="symlinks may require Windows developer mode")
def test_linked_definition_is_never_removed(prepared: FakeLaunchctl, tmp_path: Path) -> None:
    target = tmp_path / "unrelated.txt"
    target.write_text("unrelated configuration")
    definition = agent().path
    definition.parent.mkdir(parents=True)
    definition.symlink_to(target)
    assert cli.main(["chatgpt-background", "uninstall"]) == 1
    assert definition.is_symlink()
    assert target.read_text() == "unrelated configuration"
    assert not prepared.calls


def test_inspection_failure_does_not_remove_service(prepared: FakeLaunchctl) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    prepared.fail = "print"
    assert cli.main(["chatgpt-background", "uninstall"]) == 1
    assert agent().path.exists()


def test_os_error_has_safe_actionable_output(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    monkeypatch.setattr(background, "_command", Mock(side_effect=OSError("sensitive details")))
    assert cli.main(["chatgpt-background", "status"]) == 1
    output = capsys.readouterr()
    assert "macOS permissions" in output.err
    assert "sensitive details" not in output.err


@pytest.mark.parametrize("state,running", [("running", True), ("waiting", False)])
def test_live_status_checks_do_not_claim_the_daemons_health_port(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, state: str, running: bool
) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    prepared.state = state
    check = Mock(return_value=True)
    monkeypatch.setattr(chatgpt, "_check_authentication", check)
    assert cli.main(["chatgpt-background", "status", "--check"]) == 0
    assert check.call_args.kwargs == {"running": running}
    assert not any(call[0] in ("bootout", "kickstart") for call in prepared.calls)


def test_starting_running_agent_uses_nonconflicting_probe(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    check = Mock(return_value=True)
    monkeypatch.setattr(chatgpt, "_check_authentication", check)
    assert cli.main(["chatgpt-background", "start"]) == 0
    assert check.call_args.kwargs == {"running": True}


def test_failed_check_does_not_automatically_recommend_key_replacement(
    prepared: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["chatgpt-background", "install"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(chatgpt, "_check_authentication", Mock(return_value=False))
    assert cli.main(["chatgpt-background", "status", "--check"]) == 1
    output = capsys.readouterr()
    assert "does not mean your API key was rejected" in output.err
    assert "--replace-key" not in output.err
