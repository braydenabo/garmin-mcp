"""Install, run and remove the daily sync with the real OS scheduler. CI only.

Usage: schedule_smoke.py EXPECTED_BACKEND   (launchd | systemd | cron | Task Scheduler)

It changes the machine's scheduler, so it refuses to run outside CI. The data
folder path contains spaces to exercise quoting. The job is started once right
away; with no API key saved, sync fails at once without touching the network,
and that error landing in logs/sync.log proves the job definition, its quoting
and its output redirection all work.
"""

import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROG = [sys.executable, "-m", "garmin_connect_personal_mcp"]
EXPECTED_LOG_TEXT = "No Garmin API key found"
WAIT_SECONDS = 90


def run(*args: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        [*PROG, *args], env=env, capture_output=True, text=True, encoding="utf-8", check=False
    )
    print(f"$ {' '.join(args)}\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        raise SystemExit(f"`{' '.join(args)}` exited with {result.returncode}")
    return result.stdout


def trigger(backend: str, task_env: dict[str, str]) -> None:
    """Start the installed job now, the way the scheduler would."""
    from garmin_connect_personal_mcp import schedule

    if backend == "launchd":
        target = f"gui/{os.getuid()}/{schedule.LAUNCHD_LABEL}"
        subprocess.run(["launchctl", "kickstart", target], check=True)
    elif backend == "systemd":
        # A oneshot service reports failure when its command exits non-zero, and this
        # sync is meant to fail (no key). The log check below is what counts.
        service = f"{schedule.NAME}.service"
        subprocess.run(["systemctl", "--user", "start", service], check=False)
        subprocess.run(["systemctl", "--user", "status", "--no-pager", service], check=False)
    elif backend == "cron":
        lines = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
        line = next(x for x in lines if x.endswith(schedule.CRON_MARKER))
        command = line.removesuffix(schedule.CRON_MARKER).split(" ", 5)[5]
        # cron unescapes \% and runs the line with /bin/sh and a minimal environment.
        minimal = {"PATH": "/usr/bin:/bin", "HOME": task_env["HOME"]}
        subprocess.run(["/bin/sh", "-c", command.replace("\\%", "%")], env=minimal, check=False)
        print(f"ran: {shlex.quote(command)}")
    else:
        subprocess.run(["schtasks", "/Run", "/TN", schedule.TaskScheduler.task], check=True)


def main(expected: str) -> int:
    if os.environ.get("CI") != "true":
        print("This changes your OS scheduler; it only runs in CI.", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="schedule smoke "))
    data_dir = root / "data dir with spaces"
    env = {**os.environ, "GARMIN_MCP_DATA_DIR": str(data_dir)}
    env.pop("GARMIN_PERSONAL_API_KEY", None)

    from garmin_connect_personal_mcp import config

    key_file = config.api_key_file()
    if key_file.exists():
        print(f"{key_file} already exists; refusing to touch a real key.", file=sys.stderr)
        return 2
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("synthetic-ci-key-not-real\n", encoding="utf-8")
    try:
        installed = run("schedule", "install", "--time", "03:17", env=env)
    finally:
        key_file.unlink()  # so the triggered sync fails offline, before any request
    status = run("schedule", "status", env=env)
    if f"[{expected}]" not in status or "not installed" in status:
        raise SystemExit(f"expected an installed {expected} job, got:\n{status}")
    if "03:17" not in installed:
        raise SystemExit(f"install didn't report the chosen time:\n{installed}")

    log = data_dir / "logs" / "sync.log"
    trigger(expected, env)
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if log.exists() and EXPECTED_LOG_TEXT in log.read_text(encoding="utf-8", errors="replace"):
            break
        time.sleep(2)
    else:
        found = log.read_text(errors="replace") if log.exists() else "(no log file)"
        raise SystemExit(f"the scheduled job didn't write the expected log line:\n{found}")
    print(f"job ran and logged to {log}")

    run("schedule", "remove", env=env)
    status = run("schedule", "status", env=env)
    if "not installed" not in status:
        raise SystemExit(f"job still installed after remove:\n{status}")
    print(f"OK: {expected} install, run and remove")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
