"""The made-up demo archive: safe to create, deterministic, and usable by every tool."""

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from garmin_connect_personal_mcp import cli, config, demo, fit, server
from garmin_connect_personal_mcp.fit import Sample

END = date(2026, 3, 31)


@pytest.fixture(scope="module")
def paths(tmp_path_factory: pytest.TempPathFactory) -> config.DataPaths:
    made = config.DataPaths(tmp_path_factory.mktemp("demo") / "archive")
    demo.build(made, days=40, end=END)
    return made


def fingerprint(paths: config.DataPaths) -> str:
    conn = sqlite3.connect(paths.db)
    rows = conn.execute(
        "SELECT dataset, calendar_date, raw_json FROM daily_raw ORDER BY 1, 2"
    ).fetchall()
    rows += conn.execute("SELECT activity_id, raw_json FROM activities ORDER BY 1").fetchall()
    conn.close()
    digest = hashlib.sha256(json.dumps(rows).encode())
    for path in sorted(paths.fit_dir.iterdir()):
        digest.update(fit.read_fit_file(path).__repr__().encode())
    return digest.hexdigest()


def test_same_seed_and_end_give_the_same_archive(tmp_path: Path) -> None:
    def made(name: str, seed: int) -> config.DataPaths:
        paths = config.DataPaths(tmp_path / name)
        demo.build(paths, days=14, end=END, seed=seed)
        return paths

    first = fingerprint(made("first", 7))
    assert fingerprint(made("again", 7)) == first
    assert fingerprint(made("other", 8)) != first


@pytest.mark.parametrize("existing", ["archive", "any file"])
def test_refuses_a_folder_that_is_not_empty(tmp_path: Path, existing: str) -> None:
    folder = tmp_path / "busy"
    folder.mkdir()
    (folder / ("garmin.db" if existing == "archive" else "notes.txt")).write_text("x")
    with pytest.raises(demo.DemoError, match="isn't empty"):
        demo.build(config.DataPaths(folder), end=END)
    assert sorted(p.name for p in folder.iterdir()) == [
        "garmin.db" if existing == "archive" else "notes.txt"
    ]


def test_an_empty_folder_is_fine(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    made = demo.build(config.DataPaths(folder), days=14, end=END)
    assert (made.days, made.worn_days) == (14, 13)  # day 9 before the end wasn't worn


@pytest.mark.parametrize("days", [13, 3651])
def test_days_bounds(tmp_path: Path, days: int) -> None:
    with pytest.raises(demo.DemoError, match="between 14 and 3650"):
        demo.build(config.DataPaths(tmp_path / "d"), days=days, end=END)


def test_patterns_worth_asking_about(paths: config.DataPaths) -> None:
    sick = date(2026, 3, 7)  # 24 days before END
    baseline = server.get_baseline(paths, "resting_hr_bpm", sick, 14, END)
    assert baseline.position == "above"
    assert server.get_baseline(paths, "hrv_last_night_ms", sick, 14, END).position == "below"
    assert server.list_activities(paths, sick, sick, "", 5, "").total == 0
    not_worn = date(2026, 3, 22)  # 9 days before END
    assert set(server.get_day(paths, not_worn).status.values()) == {"no_data"}


def test_every_activity_has_a_readable_fit_file_without_gps(paths: config.DataPaths) -> None:
    files = sorted(paths.fit_dir.iterdir())
    assert files
    for path in files:
        data = fit.read_fit_file(path)
        assert data and data[0].heart_rate_bpm is not None
    assert not any("lat" in f or "long" in f for f in Sample.__slots__)  # type: ignore[attr-defined]


def test_fit_files_have_valid_crcs() -> None:
    from datetime import datetime

    data = demo.fit_file(datetime(2026, 3, 1, 7, 0), [{"hr": 140.0}, {"hr": 141.0}])
    assert demo._crc(data[:12]) == int.from_bytes(data[12:14], "little")  # header CRC
    assert demo._crc(data) == 0  # a file CRC check over everything comes out zero
    assert [s.heart_rate_bpm for s in fit.decode(data)] == [140, 141]


def test_marked_as_demo(paths: config.DataPaths) -> None:
    marker = json.loads((paths.root / "DEMO.json").read_text())
    assert marker == {"demo": True, "seed": 7, "days": 40, "end": "2026-03-31"}


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "cli demo"
    assert cli.main(["demo", str(folder), "--days", "14", "--end", "2026-03-31"]) == 0
    out = capsys.readouterr().out
    assert "All of it is made up" in out and "serve --read-only" in out
    assert "GARMIN_MCP_DATA_DIR='" in out  # quoted, since the path has a space
    assert cli.main(["demo", str(folder)]) == 1
    assert "isn't empty" in capsys.readouterr().err
    assert cli.main(["demo", str(tmp_path / "x"), "--end", "soon"]) == 2


# --- real data can never be synced into a demo archive


def test_run_sync_refuses_a_demo_archive(paths: config.DataPaths) -> None:
    from garmin_connect_personal_mcp import sync

    class NoApi:
        def call(self, request: object) -> object:
            raise AssertionError("must not contact Garmin")

    with pytest.raises(sync.DemoArchive, match="demo archive"):
        sync.run_sync(NoApi(), paths)


def test_cli_sync_refuses_a_demo_archive(
    paths: config.DataPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GARMIN_MCP_DATA_DIR", str(paths.root))
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "synthetic-key-for-this-test")
    assert cli.main(["sync"]) == 1
    assert "demo archive" in capsys.readouterr().err


def test_sync_tool_and_startup_sync_refuse_a_demo_archive(paths: config.DataPaths) -> None:
    import anyio
    from mcp import Client

    from garmin_connect_personal_mcp import autosync

    async def call() -> tuple[bool, str]:
        async with Client(server.build_server(paths, api_factory=lambda: None)) as client:
            result = await client.call_tool("garmin_sync", {})
            return result.is_error, result.content[0].text  # type: ignore[union-attr]

    error, text = anyio.run(call)
    assert error and "demo archive" in text
    started: list[object] = []
    assert not autosync.start_if_stale(paths, has_key=lambda: True, start=started.append)
    assert started == []
