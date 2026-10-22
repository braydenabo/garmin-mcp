import os
import signal
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from garmin_connect_personal_mcp import config, tunnel_process
from garmin_connect_personal_mcp.sync import sync_lock


def test_foreground_keeps_key_out_of_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    launch = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(tunnel_process.subprocess, "run", launch)
    env = {
        config.DATA_DIR_ENV_VAR: str(config.data_paths().root),
        "CONTROL_PLANE_API_KEY": "synthetic-key",
    }
    assert tunnel_process.run("/synthetic/client", "example", env) == 0
    assert "synthetic-key" not in str(launch.call_args.args)
    assert launch.call_args.kwargs["env"] is env


def test_duplicate_instance_does_not_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    launch = Mock()
    monkeypatch.setattr(tunnel_process.subprocess, "run", launch)
    with sync_lock(tunnel_process.lock_path("example")):
        assert tunnel_process.run("/synthetic/client", "example", dict(os.environ)) == 1
    launch.assert_not_called()


@pytest.mark.parametrize("interrupt", [False, True])
def test_background_discards_output_and_cleans_up_child(
    monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    child = Mock()
    child.__enter__ = Mock(return_value=child)
    child.__exit__ = Mock(return_value=False)
    child.poll.return_value = None if interrupt else 0
    child.wait.side_effect = (
        [KeyboardInterrupt(), subprocess.TimeoutExpired("client", 5), 0] if interrupt else [0]
    )
    popen = Mock(return_value=child)
    monkeypatch.setattr(tunnel_process.subprocess, "Popen", popen)
    register = Mock(return_value=signal.SIG_DFL)
    monkeypatch.setattr(tunnel_process.signal, "signal", register)
    env = {config.DATA_DIR_ENV_VAR: str(config.data_paths().root)}
    assert tunnel_process.run("/synthetic/client", "example", env, quiet=True) == (
        130 if interrupt else 0
    )
    assert popen.call_args.kwargs["stdout"] == subprocess.DEVNULL
    assert popen.call_args.kwargs["stderr"] == subprocess.DEVNULL
    assert register.call_count == 2
    if interrupt:
        child.kill.assert_called_once()
    else:
        child.terminate.assert_not_called()
    handler: Any = register.call_args_list[0].args[1]
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable script smoke test")
def test_background_runner_with_real_synthetic_child(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    executable = tmp_path / "synthetic-client"
    executable.write_text("#!/bin/sh\nprintf 'synthetic private output\\n'\nexit 7\n")
    executable.chmod(0o700)
    env = {config.DATA_DIR_ENV_VAR: str(config.data_paths().root)}
    assert tunnel_process.run(str(executable), "synthetic", env, quiet=True) == 7
    assert "synthetic private output" not in str(capsys.readouterr())
    # Exiting must release the lock for the service manager's next launch.
    with sync_lock(tunnel_process.lock_path("synthetic", env)):
        pass
