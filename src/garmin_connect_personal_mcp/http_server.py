"""Streamable HTTP runner with optional static bearer authentication."""

import hmac

from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    """Protect every HTTP request with a configured bearer token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"authorization"),
            b"",
        )
        if not hmac.compare_digest(supplied, self.expected):
            body = b"Unauthorized"
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain"), (b"content-length", b"12")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def run_http(server: MCPServer, *, host: str, port: int, bearer_token: str) -> None:
    """Run the server with stateless Streamable HTTP at /mcp."""
    import uvicorn

    app: ASGIApp = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=host,
    )
    if bearer_token:
        app = BearerAuthMiddleware(app, bearer_token)
    uvicorn.run(app, host=host, port=port, log_level="info")
