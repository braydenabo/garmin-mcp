"""garmin_show_chart and its MCP Apps wiring."""

from datetime import date, timedelta
from typing import Any

import anyio
from mcp import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

from garmin_connect_personal_mcp import config, db
from garmin_connect_personal_mcp.server import CHART_HTML, CHART_URI, LEGACY_CHART_URI, build_server

TODAY = date(2026, 3, 15)


def with_client(fn: Any, *, ui: bool = False, **kwargs: Any) -> Any:
    server = build_server(config.data_paths(), today=lambda: TODAY, **kwargs)

    async def main() -> Any:
        extensions = [advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})] if ui else []
        async with Client(server, extensions=extensions) as client:
            return await fn(client)

    return anyio.run(main)


def seed_sleep() -> None:
    conn = db.connect(config.data_paths().db)
    for i in range(7):
        when = TODAY - timedelta(days=i)
        db.upsert_daily(
            conn,
            "sleep",
            when,
            {"calendarDate": when.isoformat(), "sleepTimeSeconds": 27000, "deepSleepSeconds": 5400},
        )
    conn.commit()
    conn.close()


def test_chart_tool_points_to_the_chart_view() -> None:
    async def go(client: Client) -> Any:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        resources = (await client.list_resources()).resources
        page = await client.read_resource(CHART_URI)
        return tools["garmin_show_chart"], resources, page

    tool, resources, page = with_client(go)
    assert tool.meta == {"ui": {"resourceUri": CHART_URI}}
    assert tool.annotations.read_only_hint is True
    assert {(r.uri, r.mime_type) for r in resources} == {
        (CHART_URI, "text/html;profile=mcp-app"),
        (LEGACY_CHART_URI, "text/html;profile=mcp-app"),
    }
    assert "ui/notifications/tool-result" in page.contents[0].text
    assert "already displayed" in page.contents[0].meta["openai/widgetDescription"]


def test_chart_resource_cache_key_matches_html_and_legacy_uri_still_resolves() -> None:
    import hashlib

    expected_uri = f"ui://garmin/chart-{hashlib.sha256(CHART_HTML.encode()).hexdigest()[:16]}.html"
    assert expected_uri == CHART_URI

    async def go(client: Client) -> Any:
        return await client.read_resource(LEGACY_CHART_URI)

    result = with_client(go)
    assert result.contents[0].text == CHART_HTML


def test_chart_returns_data_the_model_can_read() -> None:
    seed_sleep()

    async def go(client: Client) -> Any:
        return await client.call_tool(
            "garmin_show_chart", {"metric": "sleep_stages", "start": "2026-03-09"}
        )

    result = with_client(go)
    assert not result.is_error
    data = result.structured_content
    assert data["kind"] == "stacked" and data["labels"][-1] == TODAY.isoformat()
    assert data["series"][0]["values"][-1] == 1.5
    assert '"Deep"' in result.content[0].text  # text copy for clients without MCP Apps


def test_ui_chart_keeps_points_out_of_model_visible_content() -> None:
    seed_sleep()

    async def go(client: Client) -> Any:
        return await client.call_tool(
            "garmin_show_chart", {"metric": "sleep_stages", "start": "2026-03-09"}
        )

    result = with_client(go, ui=True)
    assert not result.is_error
    summary = result.structured_content
    assert summary["metric"] == "sleep_stages" and summary["days_with_data"] == 7
    assert summary["average"] is not None and summary["start"] == "2026-03-09"
    assert "labels" not in summary and "series" not in summary
    assert "displayed" in result.content[0].text
    assert "2026-03-09" not in result.content[0].text
    full = result.meta["garmin/chart"]
    assert full["labels"][-1] == TODAY.isoformat()
    assert full["series"][0]["values"][-1] == 1.5


def test_ui_empty_chart_preserves_missing_data_and_summary() -> None:
    seed_sleep()

    async def go(client: Client) -> Any:
        return await client.call_tool("garmin_show_chart", {"metric": "steps"})

    result = with_client(go, ui=True)
    assert not result.is_error
    assert result.structured_content["average"] is None
    assert result.structured_content["days_with_data"] == 0
    assert result.meta["garmin/chart"]["series"][0]["values"] == [None] * 30


def test_chart_defaults_to_30_days_and_guides_errors() -> None:
    seed_sleep()

    async def go(client: Client) -> Any:
        ok = await client.call_tool("garmin_show_chart", {"metric": "sleep_hours"})
        typo = await client.call_tool("garmin_show_chart", {"metric": "sleep_stage"})
        huge = await client.call_tool(
            "garmin_show_chart", {"metric": "steps", "start": "2024-01-01"}
        )
        return ok, typo, huge

    ok, typo, huge = with_client(go)
    assert len(ok.structured_content["labels"]) == 30
    assert typo.is_error and "sleep_stages" in typo.content[0].text
    assert huge.is_error and "bucket='week'" in huge.content[0].text


def test_chart_without_archive_suggests_sync() -> None:
    async def go(client: Client) -> Any:
        return await client.call_tool("garmin_show_chart", {"metric": "steps"})

    result = with_client(go)
    assert result.is_error and "sync" in result.content[0].text


def test_charts_toolset_can_be_turned_off() -> None:
    async def go(client: Client) -> Any:
        names = {t.name for t in (await client.list_tools()).tools}
        resources = (await client.list_resources()).resources
        return names, resources

    names, resources = with_client(go, toolsets=("daily", "activities", "sync"))
    assert "garmin_show_chart" not in names and resources == []
    names, _ = with_client(go, toolsets=("charts",))
    assert names == {"garmin_describe_data", "garmin_show_chart"}


def test_serve_includes_charts_by_default(monkeypatch: Any) -> None:
    from garmin_connect_personal_mcp import cli

    monkeypatch.delenv("GARMIN_MCP_TOOLSETS", raising=False)
    args = cli.build_parser().parse_args(["serve"])
    assert set(args.toolsets.split(",")) == {"daily", "activities", "sync", "charts"}
