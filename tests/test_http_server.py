"""Transport checks use only in-process ASGI requests and synthetic credentials."""

from typing import Any
from unittest.mock import Mock

import anyio
import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from garmin_connect_personal_mcp import autosync, cli, config, http_server


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": "Bearer synthetic-token"}, 204),
    ],
)
def test_authentication(headers: dict[str, str], status: int) -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    client = TestClient(http_server.BearerAuthMiddleware(app, "synthetic-token"))
    response = client.get("/mcp", headers=headers)
    client.close()
    assert response.status_code == status
    assert "synthetic-token" not in response.text


def test_lifespan_passes_through() -> None:
    seen: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope)

    async def check() -> None:
        await http_server.BearerAuthMiddleware(app, "synthetic")(
            {"type": "lifespan"}, Mock(), Mock()
        )

    anyio.run(check)
    assert seen == [{"type": "lifespan"}]


@pytest.mark.parametrize("token", ["", "synthetic-token"])
def test_runner_configuration(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    server = Mock(spec=MCPServer)
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    http_server.run_http(server, host="127.0.0.1", port=9876, bearer_token=token)
    server.streamable_http_app.assert_called_once_with(
        streamable_http_path="/mcp", json_response=True, stateless_http=True, host="127.0.0.1"
    )
    app = run.call_args.args[0]
    assert isinstance(app, http_server.BearerAuthMiddleware) is bool(token)
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9876, "log_level": "info"}


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "192.0.2.1"])
def test_cli_http_binding(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.delenv("GARMIN_MCP_HTTP_BEARER_TOKEN", raising=False)
    run = Mock()
    monkeypatch.setattr(http_server, "run_http", run)
    assert cli.main(["serve", "--http", "--read-only", "--host", host]) == (
        2 if host == "192.0.2.1" else 0
    )
    assert run.call_count == (0 if host == "192.0.2.1" else 1)


def test_remote_binding_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARMIN_MCP_HTTP_BEARER_TOKEN", "synthetic-token")
    run = Mock()
    monkeypatch.setattr(http_server, "run_http", run)
    assert cli.main(["serve", "--http", "--read-only", "--host", "192.0.2.1"]) == 0
    assert run.call_args.kwargs["bearer_token"].startswith("synthetic-")


@pytest.mark.parametrize("args", [["--port", "0"], ["--port", "65536"], ["--host", "192.0.2.1"]])
def test_invalid_options_do_not_sync_or_create_data(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setenv("GARMIN_MCP_HTTP_BEARER_TOKEN", " ")
    monkeypatch.setattr(autosync, "start_if_stale", lambda *a: pytest.fail("unexpected sync"))
    assert cli.main(["serve", "--http", *args]) == 2
    assert not config.data_paths().root.exists()
