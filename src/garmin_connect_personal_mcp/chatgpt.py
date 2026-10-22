"""Guide a local Secure MCP Tunnel setup with optional native keychain storage."""

import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import webbrowser

from garmin_connect_personal_mcp import config, tunnel_credentials, tunnel_install

PROG = "garmin-connect-personal-mcp"
TUNNELS_URL = "https://platform.openai.com/settings/organization/tunnels"
KEYS_URL = "https://platform.openai.com/settings/organization/api-keys"
GUIDE_URL = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
DOWNLOAD_URL = "https://github.com/openai/tunnel-client/releases/latest"
KEY_ENV = "CONTROL_PLANE_API_KEY"
CHATGPT_URL = "https://chatgpt.com/plugins"


def _open_page(url: str, *, no_browser: bool) -> None:
    print(f"Setup page: {url}", flush=True)
    if no_browser or not sys.stdin.isatty():
        return
    try:
        opened = webbrowser.open(url, new=2)
    except (OSError, webbrowser.Error):
        opened = False
    if opened:
        print("Opened in your browser. Return to this terminal when ready.", flush=True)
    else:
        print("The browser could not open automatically. Open the setup page above to continue.")


def _find_client(*, no_browser: bool) -> str | None:
    executable = shutil.which("tunnel-client")
    if executable:
        return executable
    installed = tunnel_install.installed_path()
    if installed.is_file():
        return str(installed)
    print("ChatGPT needs OpenAI's tunnel client to reach your local Garmin server.")
    print(f"I can download it from OpenAI's GitHub release and install it here:\n{installed}")
    print("No administrator access or PATH changes are needed.")
    if not sys.stdin.isatty():
        print("Run setup-chatgpt in a terminal to accept installation, or install it from:")
        print(DOWNLOAD_URL)
        return None
    if input("Download and install the tunnel client now? [Y/n]: ").strip().lower() not in (
        "",
        "y",
        "yes",
    ):
        _open_page(DOWNLOAD_URL, no_browser=no_browser)
        print("Install the client from that page, then rerun setup-chatgpt.")
        return None
    try:
        return str(tunnel_install.install())
    except tunnel_install.InstallError as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        _open_page(DOWNLOAD_URL, no_browser=no_browser)
        print("Retry setup, or use the download page for manual installation.")
        return None


def _step(command: list[str], env: dict[str, str]) -> bool:
    """Do not echo subprocess output, which could include credentials or local data."""
    result = subprocess.run(  # noqa: S603
        command, env=env, capture_output=True, text=True, check=False, timeout=60
    )
    return result.returncode == 0


def _profile_exists(executable: str, profile: str) -> bool:
    result = subprocess.run(  # noqa: S603
        [executable, "profiles", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise OSError("Could not list tunnel-client profiles.")
    try:
        profiles = json.loads(result.stdout)
        if not isinstance(profiles, list):
            raise ValueError("Unexpected profile list")
        return any(isinstance(p, dict) and p.get("name") == profile for p in profiles)
    except ValueError:
        raise OSError("Could not read tunnel-client profile list.") from None


def _check_authentication(executable: str, profile: str, env: dict[str, str]) -> bool:
    """Check metadata access with the exact runtime key, without exposing subprocess output."""
    checked = subprocess.run(  # noqa: S603
        [
            executable,
            "doctor",
            "--profile",
            profile,
            "--json",
            "--control-plane.api-key",
            f"env:{KEY_ENV}",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if checked.returncode:
        print("Local tunnel configuration check failed. Check the profile and client installation.")
        return False
    try:
        report = json.loads(checked.stdout)
        tunnel_id = next(c["summary"] for c in report["checks"] if c.get("id") == "tunnel_id")
        if not isinstance(tunnel_id, str) or not re.fullmatch(r"tunnel_[A-Za-z0-9_]+", tunnel_id):
            raise ValueError("Invalid tunnel ID")
    except (ValueError, KeyError, TypeError, StopIteration):
        print("Could not read the tunnel ID from the client's configuration report.")
        return False
    # Admin credentials must not mask a bad runtime credential during this read-only lookup.
    runtime_env = {k: v for k, v in env.items() if k not in ("OPENAI_ADMIN_KEY", "OPENAI_API_KEY")}
    result = subprocess.run(  # noqa: S603
        [executable, "admin", "--json", "tunnels", "get", tunnel_id],
        env=runtime_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode == 0:
        return True
    if "401" in result.stdout + result.stderr:
        print("OpenAI rejected the runtime key (401 Unauthorized).")
    elif "403" in result.stdout + result.stderr:
        print("OpenAI denied access (403). Check Tunnels Read + Use and the tunnel's organization.")
    else:
        print("Could not authenticate access to this tunnel. Check network access and permissions.")
    print("No key was saved and the tunnel was not started.")
    return False


def setup(
    *,
    tunnel_id: str | None,
    profile: str,
    reuse_profile: bool,
    no_start: bool,
    no_browser: bool = False,
    no_keychain: bool = False,
    forget_key: bool = False,
    replace_key: bool = False,
) -> int:
    """Create or reuse a native tunnel-client profile and optionally run it."""
    try:
        return _setup(
            tunnel_id=tunnel_id,
            profile=profile,
            reuse_profile=reuse_profile,
            no_start=no_start,
            no_browser=no_browser,
            no_keychain=no_keychain,
            forget_key=forget_key,
            replace_key=replace_key,
        )
    except (EOFError, KeyboardInterrupt):
        print("\nTunnel stopped. Your profile and any key you chose to save are kept.")
        print("Run setup-chatgpt again to restart using the same profile.")
        return 130
    except (OSError, subprocess.SubprocessError):
        print(
            "Could not run tunnel-client. Check its installation and run "
            "`tunnel-client help quickstart`, then retry.",
            file=sys.stderr,
        )
        return 1


def _setup(
    *,
    tunnel_id: str | None,
    profile: str,
    reuse_profile: bool,
    no_start: bool,
    no_browser: bool,
    no_keychain: bool,
    forget_key: bool,
    replace_key: bool,
) -> int:
    print("Connect Garmin to ChatGPT with a private Secure MCP Tunnel.")
    print("Your archive stays local; requested tool results are sent to ChatGPT.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", profile):
        print("Profile must be 1-64 letters, numbers, underscores or hyphens.", file=sys.stderr)
        return 2
    if forget_key:
        try:
            tunnel_credentials.forget(profile)
        except tunnel_credentials.CredentialError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Removed any saved runtime key for '{profile}'. The tunnel profile is kept.")
        return 0
    if reuse_profile and tunnel_id:
        print("Use either --reuse-profile or --tunnel-id, not both.", file=sys.stderr)
        return 2
    if not config.data_paths().db.is_file():
        print(f"First prepare your archive: run `{PROG} init`, then `{PROG} sync`.")
        return 1
    if "archive-v" in sys.executable:
        print(
            "This Python installation is temporary. Install a persistent copy first with "
            "`uv tool install git+https://github.com/braydenabo/garmin-mcp`, then run "
            f"`{PROG} setup-chatgpt`."
        )
        return 1
    executable = _find_client(no_browser=no_browser)
    if executable is None:
        return 1

    if not reuse_profile and tunnel_id is None:
        reuse_profile = _profile_exists(executable, profile)
    if reuse_profile:
        print(f"Reusing your saved tunnel profile: {profile}.")
    if not reuse_profile:
        print(f"Account step: open {TUNNELS_URL}")
        print("Create a tunnel and associate it with your intended ChatGPT workspace.")
        print("You need Tunnels Read + Manage to create it, and Read + Use to run it.")
        print("Your ChatGPT account/workspace must also allow custom MCP connections.")
        if tunnel_id is None:
            if not sys.stdin.isatty():
                print("Use --tunnel-id <id> or run this command in a terminal.", file=sys.stderr)
                return 2
            _open_page(TUNNELS_URL, no_browser=no_browser)
            tunnel_id = input("Paste the tunnel ID (not an API key): ").strip()
        if not re.fullmatch(r"tunnel_[A-Za-z0-9]+", tunnel_id):
            print(
                "Invalid tunnel ID. Copy the tunnel_... ID from Platform settings.", file=sys.stderr
            )
            return 2

        argv = [sys.executable, "-m", "garmin_connect_personal_mcp", "serve", "--read-only"]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        # init only needs non-secret settings. Never give it a key to write into its profile.
        init_env = {
            name: value
            for name, value in os.environ.items()
            if name not in (KEY_ENV, "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", config.API_KEY_ENV_VAR)
        }
        created = _step(
            [
                executable,
                "init",
                "--sample",
                "sample_mcp_stdio_local",
                "--profile",
                profile,
                "--tunnel-id",
                tunnel_id,
                "--mcp-command",
                command,
                "--control-plane-api-key-ref",
                f"env:{KEY_ENV}",
            ],
            init_env,
        )
        if not created:
            print(
                "Could not create the tunnel profile. Existing profiles are not forcibly "
                "replaced. If this profile is already configured, retry with --reuse-profile; "
                "otherwise choose another --profile or check `tunnel-client help quickstart`.",
                file=sys.stderr,
            )
            if not sys.stdin.isatty() or input(
                f"Reuse the existing '{profile}' profile instead? [y/N]: "
            ).strip().lower() not in ("y", "yes"):
                return 1
        else:
            print("Created the local tunnel profile. No API key was supplied to profile creation.")

    env = dict(os.environ)
    key = "" if replace_key else env.get(KEY_ENV, "").strip()
    if key:
        print(f"Using the runtime key from {KEY_ENV}; it overrides the saved key.")
    key_saved = False
    if not key and not no_keychain and not replace_key:
        try:
            key = tunnel_credentials.load(profile) or ""
            key_saved = bool(key)
            if key_saved:
                print("Using the runtime key saved in your OS keychain.")
        except tunnel_credentials.CredentialError as exc:
            print(str(exc))
    if not key:
        if not sys.stdin.isatty():
            print(
                f"Set {KEY_ENV} locally or rerun in a terminal for a hidden prompt.",
                file=sys.stderr,
            )
            return 2
        print(f"Enter your existing runtime key, or create one with Read + Use at {KEYS_URL}")
        _open_page(KEYS_URL, no_browser=no_browser)
        key = getpass.getpass("OpenAI tunnel runtime key (hidden): ").strip()
    if not key or any(char.isspace() for char in key):
        print("The runtime key is empty or contains whitespace. Try again.", file=sys.stderr)
        return 2
    env[KEY_ENV] = key
    print("Checking tunnel configuration and authenticating the runtime key with OpenAI...")
    if not _check_authentication(executable, profile, env):
        print(
            "Tunnel check failed. Check the workspace association, runtime key permissions, "
            "and network access. For local diagnostics run "
            f"`tunnel-client doctor --profile {profile} --explain` with {KEY_ENV} set. "
            "Do not paste credentials or health data into a support report.",
            file=sys.stderr,
        )
        print("To enter a replacement key, run setup-chatgpt --replace-key.")
        print(f"If {KEY_ENV} is set in your shell, unset it to stop overriding the saved key.")
        return 1

    if (
        not no_keychain
        and not key_saved
        and sys.stdin.isatty()
        and input("Save this key in your OS keychain so you won't need to paste it again? [Y/n]: ")
        .strip()
        .lower()
        in ("", "y", "yes")
    ):
        try:
            tunnel_credentials.save(profile, key)
            key_saved = True
            print("Runtime key saved securely in your OS keychain.")
        except tunnel_credentials.CredentialError as exc:
            print(str(exc))
            print("Continuing without saving the key; it will be needed again next time.")

    print("Tunnel checks passed. The ChatGPT connection still needs to be verified.")
    print("In ChatGPT web: Plugins > + > Add custom MCP server > Connection: Tunnel.")
    print("Select your tunnel and No authentication, then create and install the plugin.")
    print('In a new chat, select Garmin and ask: "What Garmin data do you have?"')
    print("New profiles expose read-only tools; reused profiles keep their existing settings.")
    print("Run the Garmin sync command for fresh data.")
    if no_start:
        print(f"To start later: {PROG} setup-chatgpt --profile {profile}")
        if not key_saved:
            print("Provide the runtime key again when starting, or save it in your OS keychain.")
        return 0
    print("Starting the tunnel. Keep this terminal open and your computer awake; Ctrl-C stops it.")
    print(f"Next time: {PROG} setup-chatgpt --profile {profile}")
    if not reuse_profile:
        _open_page(CHATGPT_URL, no_browser=no_browser)
    return subprocess.run(  # noqa: S603
        [executable, "run", "--profile", profile, "--control-plane.api-key", f"env:{KEY_ENV}"],
        env=env,
        check=False,
    ).returncode
