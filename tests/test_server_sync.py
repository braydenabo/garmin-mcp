"""garmin_sync, toolsets, read-only mode and the startup auto-sync."""

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters
from test_sync import FakeApi, FakeClient

from garmin_connect_personal_mcp import autosync, cli, config, db, sync
from garmin_connect_personal_mcp.server import TOOLSETS, build_server

ALL_TOOLS = {"garmin_describe_data", *(name for names in TOOLSETS.values() for name in names)}


def tool_names(**kwargs: Any) -> set[str]:
    server = build_server(config.data_paths(), **kwargs)

    async def main() -> set[str]:
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    return anyio.run(main)


def call_sync(args: dict[str, Any], api_factory: Any) -> tuple[bool, Any, str]:
    server = build_server(config.data_paths(), api_factory=api_factory)

    async def main() -> tuple[bool, Any, str]:
        async with Client(server) as client:
            result = await client.call_tool("garmin_sync", args)
            return bool(result.is_error), result.structured_content, result.content[0].text  # type: ignore[union-attr]

    return anyio.run(main)


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)


# --- garmin_sync


def test_sync_tool_resumes_and_reports() -> None:
    client = FakeClient()
    error, result, _ = call_sync({}, lambda: FakeApi(client))
    assert not error and result["ok"]
    assert result["days_saved"]["sleep"] == 7  # first sync: the last week
    assert result["message"].startswith("Synced")


def test_sync_tool_days_back() -> None:
    client = FakeClient()
    _, result, _ = call_sync({"days_back": 30}, lambda: FakeApi(client))
    assert result["days_saved"]["sleep"] == 30


@pytest.mark.parametrize("days", [-1, 91])
def test_sync_tool_limits_backfill(days: int) -> None:
    error, _, text = call_sync({"days_back": days}, lambda: FakeApi(FakeClient()))
    assert error and "--days-back" in text


def test_sync_tool_without_key_says_the_user_must_act() -> None:
    error, _, text = call_sync({}, None)
    assert error and "init" in text and "you can't" in text


def test_sync_tool_when_already_running() -> None:
    paths = config.data_paths()
    with sync.sync_lock(paths.sync_lock):
        error, result, _ = call_sync({}, lambda: FakeApi(FakeClient()))
    assert not error and result["already_running"] is True and result["ok"] is False


def test_sync_tool_reports_rate_limit() -> None:
    client = FakeClient()
    client.fail[("daily_summary", sync_start())] = 429
    _, result, _ = call_sync({}, lambda: FakeApi(client))
    assert not result["ok"] and result["message"].startswith("Stopped early")


def sync_start() -> date:
    return datetime.now().astimezone().date() - timedelta(days=sync.FIRST_SYNC_DAYS - 1)


def test_sync_tool_annotations() -> None:
    server = build_server(config.data_paths())

    async def main() -> Any:
        async with Client(server) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    tools = anyio.run(main)
    sync_hints = tools["garmin_sync"].annotations
    assert sync_hints.open_world_hint is True and sync_hints.read_only_hint is False
    assert sync_hints.destructive_hint is False and sync_hints.idempotent_hint is True
    for name in ALL_TOOLS - {"garmin_sync"}:
        hints = tools[name].annotations
        assert hints.read_only_hint is True and hints.open_world_hint is False, name


# --- toolsets and read-only


def test_all_tools_by_default() -> None:
    assert tool_names() == ALL_TOOLS


def test_toolsets_limit_tools() -> None:
    assert tool_names(toolsets=("daily",)) == {
        "garmin_describe_data",
        "garmin_get_day",
        "garmin_get_trend",
    }
    assert tool_names(toolsets=()) == {"garmin_describe_data"}


def test_read_only_hides_sync() -> None:
    assert tool_names(read_only=True) == ALL_TOOLS - {"garmin_sync"}


def test_unknown_toolset() -> None:
    with pytest.raises(ValueError, match="Choose from"):
        build_server(config.data_paths(), toolsets=("everything",))


def test_cli_serve_rejects_unknown_toolset(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve", "--toolsets", "daily,nope"]) == 2
    assert "nope" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="stdio subprocess test is POSIX-only for now")
def test_serve_flags_over_stdio(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "garmin_connect_personal_mcp", "serve", "--toolsets", "activities"],
        env={
            "HOME": str(isolated_home),
            "GARMIN_MCP_DATA_DIR": os.environ["GARMIN_MCP_DATA_DIR"],
            "GARMIN_MCP_READ_ONLY": "1",
            "PATH": os.environ.get("PATH", ""),
        },
    )

    async def main() -> set[str]:
        async with Client(params, read_timeout_seconds=30) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert anyio.run(main) == {
        "garmin_describe_data",
        "garmin_list_activities",
        "garmin_get_activity",
    }


# --- startup auto-sync


def record_sync_at(paths: config.DataPaths, when: datetime) -> None:
    conn = db.connect(paths.db)
    conn.execute(
        "INSERT INTO sync_log VALUES ('sleep', '2026-03-01', 'ok', NULL, ?)", (when.isoformat(),)
    )
    conn.commit()
    conn.close()


def test_is_stale() -> None:
    paths = config.data_paths()
    now = datetime(2026, 3, 15, 12, tzinfo=UTC)
    assert autosync.is_stale(paths, now)  # no archive
    db.connect(paths.db).close()
    assert autosync.is_stale(paths, now)  # never synced
    record_sync_at(paths, now - timedelta(hours=2))
    assert not autosync.is_stale(paths, now)
    assert autosync.is_stale(paths, now + timedelta(days=2))


def test_start_if_stale() -> None:
    paths = config.data_paths()
    started: list[Any] = []
    assert not autosync.start_if_stale(paths, has_key=lambda: False, start=started.append)
    assert autosync.start_if_stale(paths, has_key=lambda: True, start=started.append)
    assert len(started) == 1
    record_sync_at(paths, datetime.now(UTC))
    assert not autosync.start_if_stale(paths, has_key=lambda: True, start=started.append)


def test_background_sync_never_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Exploding(FakeApi):
        def call(self, request: Any) -> Any:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "garmin_connect_personal_mcp.garmin_client.GarminApi", lambda: Exploding(FakeClient())
    )
    autosync._sync_quietly(config.data_paths())
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout carries the MCP protocol; nothing may be printed there
    assert "background sync failed" in captured.err


def test_background_sync_without_key_is_quiet_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    autosync._sync_quietly(config.data_paths())
    captured = capsys.readouterr()
    assert captured.out == "" and "skipped" in captured.err
