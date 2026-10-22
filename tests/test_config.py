import os
from pathlib import Path

import pytest

from garmin_connect_personal_mcp import config

posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")


def test_data_dir_from_env(tmp_path: Path) -> None:
    paths = config.data_paths({"GARMIN_MCP_DATA_DIR": str(tmp_path / "x")})
    assert paths.root == tmp_path / "x"
    assert paths.db == tmp_path / "x" / "garmin.db"
    assert paths.fit_dir == tmp_path / "x" / "fit"


def test_data_dir_default_is_user_data_dir() -> None:
    paths = config.data_paths({})
    assert config.APP_NAME in str(paths.root)
    assert Path.cwd() not in paths.root.parents


def test_ensure_data_dir_creates_folders(tmp_path: Path) -> None:
    paths = config.DataPaths(tmp_path / "data")
    config.ensure_data_dir(paths)
    assert paths.fit_dir.is_dir()
    assert paths.log_dir.is_dir()


def test_key_file_matches_garmin_client(isolated_home: Path) -> None:
    assert config.api_key_file() == isolated_home / ".config/garmin/connect-personal-api-key"


def test_api_key_from_env() -> None:
    assert config.api_key_from_env({"GARMIN_PERSONAL_API_KEY": "abc"})
    assert not config.api_key_from_env({"GARMIN_PERSONAL_API_KEY": "  "})
    assert not config.api_key_from_env({})


def test_save_api_key_strips_and_writes(tmp_path: Path) -> None:
    path = config.save_api_key("  fake-key-123\n", tmp_path / "k")
    assert path.read_text(encoding="utf-8") == "fake-key-123\n"


@posix_only
def test_save_api_key_is_owner_only(tmp_path: Path) -> None:
    path = config.save_api_key("fake-key-123", tmp_path / "k")
    assert path.stat().st_mode & 0o777 == 0o600
    assert not config.readable_by_others(path)


@posix_only
def test_save_api_key_fixes_existing_loose_permissions(tmp_path: Path) -> None:
    path = tmp_path / "k"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    assert config.readable_by_others(path)
    config.save_api_key("fake-key-123", path)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bad", ["", "   \n", "two words", "line\nbreak"])
def test_save_api_key_rejects_bad_input(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        config.save_api_key(bad, tmp_path / "k")
    assert not (tmp_path / "k").exists()


def test_saved_key_is_found_by_garmin_client(isolated_home: Path) -> None:
    from garmin_personal_api import resolve_api_key

    config.save_api_key("fake-key-123")
    assert resolve_api_key() == "fake-key-123"
