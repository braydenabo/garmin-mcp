"""Where things live on disk, and how the Garmin API key is stored.

The API key file and env var are the ones Garmin's official client already
uses, so a key saved here also works with Garmin's own `gc-personal` CLI.
Personal data lives in the OS user data dir, never inside a git checkout.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import platformdirs
from garmin_personal_api.constants import DEFAULT_API_KEY_ENV_VAR, DEFAULT_API_KEY_FILE

APP_NAME = "garmin-connect-personal-mcp"
DATA_DIR_ENV_VAR = "GARMIN_MCP_DATA_DIR"
API_KEY_ENV_VAR = DEFAULT_API_KEY_ENV_VAR
API_KEY_URL = "https://connect.garmin.com/app/settings/personalApiToken"


@dataclass(frozen=True)
class DataPaths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "garmin.db"

    @property
    def fit_dir(self) -> Path:
        return self.root / "fit"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"


def data_paths(env: Mapping[str, str] | None = None) -> DataPaths:
    """`GARMIN_MCP_DATA_DIR` if set, else the OS user data dir."""
    environment = os.environ if env is None else env
    override = environment.get(DATA_DIR_ENV_VAR, "").strip()
    if override:
        return DataPaths(Path(override).expanduser())
    return DataPaths(platformdirs.user_data_path(APP_NAME, appauthor=False))


def ensure_data_dir(paths: DataPaths) -> None:
    """Create the data dirs, private to the current user on POSIX."""
    for path in (paths.root, paths.fit_dir, paths.log_dir):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)


def api_key_file() -> Path:
    return DEFAULT_API_KEY_FILE.expanduser()


def api_key_from_env(env: Mapping[str, str] | None = None) -> bool:
    environment = os.environ if env is None else env
    return bool(environment.get(API_KEY_ENV_VAR, "").strip())


def save_api_key(key: str, path: Path | None = None) -> Path:
    """Write the key with mode 600, replacing any existing file."""
    key = key.strip()
    if not key:
        raise ValueError("The API key is empty.")
    if any(char.isspace() for char in key):
        raise ValueError("The API key contains spaces or line breaks; paste only the key.")
    target = api_key_file() if path is None else path
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    # os.open only applies the mode to new files; fix an existing one too.
    target.chmod(0o600)
    return target


def readable_by_others(path: Path) -> bool:
    """True if group or other users can read the file (POSIX only)."""
    return os.name == "posix" and bool(path.stat().st_mode & 0o077)
