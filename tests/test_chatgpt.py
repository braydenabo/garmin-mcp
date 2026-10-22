import subprocess
import sys
from typing import Any

import pytest

from garmin_connect_personal_mcp import chatgpt, cli, config


@pytest.fixture
def tunnel_setup(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, Any]]]:
    paths = config.data_paths()
    paths.root.mkdir(parents=True)
    paths.db.touch()  # The wizard checks existence only; it must never read health data.
    monkeypatch.setattr(chatgpt.shutil, "which", lambda name: "/tools/tunnel-client")
    monkeypatch.setattr(chatgpt, "_profile_exists", lambda executable, profile: False)
    monkeypatch.setattr(
        chatgpt,
        "_check_authentication",
        lambda executable, profile, env: chatgpt._step(
            [executable, "doctor", "--profile", profile, "--explain"], env
        ),
    )
    monkeypatch.setattr(chatgpt.tunnel_credentials, "load", lambda profile: None)
    monkeypatch.setattr(chatgpt.tunnel_credentials, "save", lambda profile, key: None)
    monkeypatch.setattr(chatgpt.tunnel_credentials, "forget", lambda profile: None)
    monkeypatch.setattr(chatgpt.webbrowser, "open", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        chatgpt.tunnel_install, "installed_path", lambda: paths.root / "tunnel-client"
    )
    monkeypatch.setattr(sys, "executable", "/a path/python")
    monkeypatch.setenv(chatgpt.KEY_ENV, "synthetic-runtime-key")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(chatgpt.subprocess, "run", run)
    return calls


def test_guided_setup_creates_checks_and_runs_without_persisting_secrets(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "synthetic-garmin-key")
    assert cli.main(["setup-chatgpt", "--tunnel-id", "tunnel_synthetic"]) == 0
    assert [argv[1] for argv, _ in tunnel_setup] == ["init", "doctor", "run"]
    init, doctor, runtime = tunnel_setup
    assert "--force" not in init[0]
    assert chatgpt.KEY_ENV not in init[1]["env"]
    assert "GARMIN_PERSONAL_API_KEY" not in init[1]["env"]
    command = init[0][init[0].index("--mcp-command") + 1]
    assert "--read-only" in command
    assert "garmin_connect_personal_mcp" in command
    assert doctor[1]["env"][chatgpt.KEY_ENV] == "synthetic-runtime-key"
    assert runtime[1]["env"][config.DATA_DIR_ENV_VAR] == str(config.data_paths().root)
    assert doctor[1]["timeout"] == 60
    output = capsys.readouterr()
    assert "synthetic-runtime-key" not in output.out + output.err
    assert "synthetic-garmin-key" not in output.out + output.err
    assert "still needs to be verified" in output.out


def test_reuse_does_not_rewrite_profile(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
) -> None:
    assert cli.main(["setup-chatgpt", "--reuse-profile", "--no-start"]) == 0
    assert [argv[1] for argv, _ in tunnel_setup] == ["doctor"]


def test_hidden_key_and_tunnel_id_prompts(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(chatgpt.KEY_ENV)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "tunnel_synthetic")
    monkeypatch.setattr(chatgpt.getpass, "getpass", lambda prompt: "synthetic-hidden-key")
    assert cli.main(["setup-chatgpt", "--no-start"]) == 0
    assert tunnel_setup[-1][1]["env"][chatgpt.KEY_ENV] == "synthetic-hidden-key"


@pytest.mark.parametrize(
    "args",
    [
        ["--profile", "../unsafe"],
        ["--tunnel-id", "tunnel_synthetic", "--reuse-profile"],
        ["--tunnel-id", "not-a-tunnel"],
    ],
)
def test_invalid_arguments_do_not_launch_anything(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], args: list[str]
) -> None:
    assert cli.main(["setup-chatgpt", *args]) == 2
    assert not tunnel_setup


@pytest.mark.parametrize("failure_step", ["init", "doctor", "run"])
def test_failures_stop_setup_without_echoing_subprocess_output(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_step: str,
) -> None:
    steps: list[str] = []

    def fail(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        steps.append(argv[1])
        return subprocess.CompletedProcess(
            argv, 1 if argv[1] == failure_step else 0, "secret", "secret"
        )

    monkeypatch.setattr(chatgpt.subprocess, "run", fail)
    assert cli.main(["setup-chatgpt", "--tunnel-id", "tunnel_synthetic"]) == 1
    assert steps[-1] == failure_step
    output = capsys.readouterr()
    assert "secret" not in output.err


def test_missing_archive_never_launches_child(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(chatgpt.shutil, "which", lambda name: pytest.fail("launched too early"))
    assert cli.main(["setup-chatgpt"]) == 1
    assert "sync" in capsys.readouterr().out


def test_missing_tunnel_client_explains_installation(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(chatgpt.shutil, "which", lambda name: None)
    assert cli.main(["setup-chatgpt"]) == 1
    assert chatgpt.DOWNLOAD_URL in capsys.readouterr().out
    assert not tunnel_setup


def test_temporary_install_is_rejected(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", "/cache/archive-v0/example/python")
    assert cli.main(["setup-chatgpt"]) == 1
    assert not tunnel_setup


@pytest.mark.parametrize("has_id", [False, True])
def test_noninteractive_input_requires_explicit_values(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    has_id: bool,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv(chatgpt.KEY_ENV)
    args = ["--tunnel-id", "tunnel_synthetic"] if has_id else []
    assert cli.main(["setup-chatgpt", *args]) == 2
    assert all(argv[1] == "init" for argv, _ in tunnel_setup)


@pytest.mark.parametrize("key", ["", "bad key"])
def test_invalid_runtime_key_does_not_start_tunnel(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    monkeypatch.delenv(chatgpt.KEY_ENV)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chatgpt.getpass, "getpass", lambda prompt: key)
    assert cli.main(["setup-chatgpt", "--reuse-profile"]) == 2
    assert not tunnel_setup


@pytest.mark.parametrize("error", [OSError("private"), subprocess.TimeoutExpired("private", 60)])
def test_launch_error_is_actionable_and_redacted(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(chatgpt.subprocess, "run", fail)
    assert cli.main(["setup-chatgpt", "--reuse-profile"]) == 1
    assert "private" not in capsys.readouterr().err


def test_interruption_is_clean(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(chatgpt.subprocess, "run", interrupt)
    assert cli.main(["setup-chatgpt", "--reuse-profile"]) == 130


def test_installs_missing_client_and_continues_same_setup(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chatgpt.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    installed = chatgpt.tunnel_install.installed_path()
    monkeypatch.setattr(chatgpt.tunnel_install, "install", lambda: installed)
    assert cli.main(["setup-chatgpt", "--tunnel-id", "tunnel_synthetic", "--no-start"]) == 0
    assert [argv[1] for argv, _ in tunnel_setup] == ["init", "doctor"]
    assert all(argv[0] == str(installed) for argv, _ in tunnel_setup)


def test_uses_private_client_without_path_changes(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chatgpt.shutil, "which", lambda name: None)
    installed = chatgpt.tunnel_install.installed_path()
    installed.touch()
    assert cli.main(["setup-chatgpt", "--reuse-profile", "--no-start"]) == 0
    assert tunnel_setup[0][0][0] == str(installed)


def test_opens_browser_pages_through_full_interactive_flow(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(chatgpt.webbrowser, "open", lambda url, **kw: opened.append(url) or True)
    monkeypatch.delenv(chatgpt.KEY_ENV)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "tunnel_synthetic")
    monkeypatch.setattr(chatgpt.getpass, "getpass", lambda prompt: "synthetic-runtime-key")
    assert cli.main(["setup-chatgpt"]) == 0
    assert opened == [chatgpt.TUNNELS_URL, chatgpt.KEYS_URL, chatgpt.CHATGPT_URL]


def test_no_browser_flag_and_browser_failure_are_actionable(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chatgpt.webbrowser, "open", lambda *a, **kw: pytest.fail("browser opened"))
    assert cli.main(["setup-chatgpt", "--reuse-profile", "--no-browser"]) == 0
    monkeypatch.setattr(chatgpt.webbrowser, "open", lambda *a, **kw: False)
    chatgpt._open_page(chatgpt.CHATGPT_URL, no_browser=False)
    assert "could not open" in capsys.readouterr().out


def test_default_restart_reuses_profile_and_saved_key_without_prompts(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(chatgpt.KEY_ENV)
    monkeypatch.setattr(chatgpt, "_profile_exists", lambda executable, profile: True)
    monkeypatch.setattr(chatgpt.tunnel_credentials, "load", lambda profile: "synthetic-saved-key")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected prompt"))
    monkeypatch.setattr(chatgpt.getpass, "getpass", lambda prompt: pytest.fail("key prompt"))
    monkeypatch.setattr(chatgpt.webbrowser, "open", lambda *a, **kw: pytest.fail("browser opened"))
    assert cli.main(["setup-chatgpt"]) == 0
    assert [argv[1] for argv, _ in tunnel_setup] == ["doctor", "run"]
    assert tunnel_setup[-1][1]["env"][chatgpt.KEY_ENV] == "synthetic-saved-key"
    output = capsys.readouterr().out
    assert "Reusing your saved tunnel profile" in output
    assert "synthetic-saved-key" not in output


def test_key_saved_only_with_consent_after_successful_validation(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    monkeypatch.setattr(chatgpt.tunnel_credentials, "save", lambda p, k: saved.append((p, k)))
    assert cli.main(["setup-chatgpt", "--reuse-profile", "--no-start"]) == 0
    assert [argv[1] for argv, _ in tunnel_setup] == ["doctor"]
    assert saved == [("garmin-local", "synthetic-runtime-key")]


def test_no_keychain_flag_never_accesses_credential_store(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chatgpt.tunnel_credentials, "load", lambda p: pytest.fail("keychain read"))
    monkeypatch.setattr(
        chatgpt.tunnel_credentials, "save", lambda p, k: pytest.fail("keychain write")
    )
    assert cli.main(["setup-chatgpt", "--reuse-profile", "--no-keychain", "--no-start"]) == 0


def test_forget_key_does_not_require_archive_or_start_tunnel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(chatgpt.tunnel_credentials, "forget", lambda p: deleted.append(p))
    assert cli.main(["setup-chatgpt", "--profile", "example", "--forget-key"]) == 0
    assert deleted == ["example"]
    assert "profile is kept" in capsys.readouterr().out


def test_keychain_unavailable_does_not_prevent_foreground_use(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: Any) -> None:
        raise chatgpt.tunnel_credentials.CredentialError("OS keychain unavailable")

    monkeypatch.delenv(chatgpt.KEY_ENV)
    monkeypatch.setattr(chatgpt.tunnel_credentials, "load", unavailable)
    monkeypatch.setattr(chatgpt.tunnel_credentials, "save", unavailable)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    monkeypatch.setattr(chatgpt.getpass, "getpass", lambda prompt: "synthetic-runtime-key")
    assert cli.main(["setup-chatgpt", "--reuse-profile", "--no-start"]) == 0


@pytest.mark.parametrize("result,expected", [('[{"name":"garmin-local"}]', True), ("[]", False)])
def test_native_profile_detection(
    monkeypatch: pytest.MonkeyPatch, result: str, expected: bool
) -> None:
    monkeypatch.setattr(
        chatgpt.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, result, ""),
    )
    assert chatgpt._profile_exists("/tools/tunnel-client", "garmin-local") is expected


def test_replace_key_bypasses_environment_and_saved_credentials(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chatgpt.tunnel_credentials, "load", lambda p: pytest.fail("saved key read"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chatgpt.getpass, "getpass", lambda p: "synthetic-replacement-key")
    assert cli.main(["setup-chatgpt", "--reuse-profile", "--replace-key", "--no-start"]) == 0
    assert tunnel_setup[-1][1]["env"][chatgpt.KEY_ENV] == "synthetic-replacement-key"


@pytest.mark.parametrize("status", [0, 401, 403])
def test_authenticated_check_uses_runtime_key_and_redacts_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[1] == "doctor":
            return subprocess.CompletedProcess(
                argv, 0, '{"checks":[{"id":"tunnel_id","summary":"tunnel_synthetic"}]}', ""
            )
        return subprocess.CompletedProcess(
            argv, 0 if status == 0 else 1, "", f"status {status}: sensitive underlying error"
        )

    monkeypatch.setattr(chatgpt.subprocess, "run", run)
    env = {
        chatgpt.KEY_ENV: "synthetic-runtime-key",
        "OPENAI_ADMIN_KEY": "synthetic-admin-key",
        "OPENAI_API_KEY": "synthetic-other-key",
    }
    assert chatgpt._check_authentication("/tools/tunnel-client", "example", env) is (status == 0)
    assert calls[1][0][-2:] == ["get", "tunnel_synthetic"]
    assert calls[1][1]["env"] == {chatgpt.KEY_ENV: "synthetic-runtime-key"}
    output = capsys.readouterr().out
    assert "sensitive underlying" not in output
    assert "synthetic-runtime-key" not in output
    if status:
        assert str(status) in output


def test_local_doctor_pass_does_not_imply_valid_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chatgpt.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, "not valid JSON", ""),
    )
    assert not chatgpt._check_authentication("/tools/tunnel-client", "example", {})


def test_declining_installation_opens_manual_download_page(
    tunnel_setup: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(chatgpt.webbrowser, "open", lambda url, **kw: opened.append(url) or True)
    monkeypatch.setattr(chatgpt.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["setup-chatgpt"]) == 1
    assert opened == [chatgpt.DOWNLOAD_URL]
    assert not tunnel_setup
