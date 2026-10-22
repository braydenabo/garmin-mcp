"""Run one tunnel per profile, without orphaning a background child on shutdown."""

import signal
import subprocess
from pathlib import Path
from types import FrameType

from garmin_connect_personal_mcp import config
from garmin_connect_personal_mcp.sync import SyncAlreadyRunning, sync_lock


def lock_path(profile: str, env: dict[str, str] | None = None) -> Path:
    return config.data_paths(env).root / f"chatgpt-{profile}.lock"


def run(executable: str, profile: str, env: dict[str, str], *, quiet: bool = False) -> int:
    command = [
        executable,
        "run",
        "--profile",
        profile,
        "--control-plane.api-key",
        "env:CONTROL_PLANE_API_KEY",
    ]
    try:
        with sync_lock(lock_path(profile, env)):
            if not quiet:
                return subprocess.run(command, env=env, check=False).returncode  # noqa: S603
            # Never persist the client's raw output: it may contain upstream details.
            with subprocess.Popen(  # noqa: S603
                command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ) as child:

                def stop(signum: int, frame: FrameType | None) -> None:
                    raise KeyboardInterrupt

                previous = signal.signal(signal.SIGTERM, stop)
                try:
                    return child.wait()
                except KeyboardInterrupt:
                    child.terminate()
                    return 130
                finally:
                    signal.signal(signal.SIGTERM, previous)
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
    except SyncAlreadyRunning:
        print("This tunnel profile is already running. Stop the other instance first.", flush=True)
        return 1
