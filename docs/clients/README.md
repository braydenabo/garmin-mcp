# Connecting an MCP client

| Client | Setup | Guide |
| --- | --- | --- |
| Claude Desktop | `garmin-connect-personal-mcp install-client claude-desktop --write` | [claude-desktop.md](claude-desktop.md) |
| Claude Code | `garmin-connect-personal-mcp install-client claude-code --write` | [claude-code.md](claude-code.md) |
| Cursor | `garmin-connect-personal-mcp install-client cursor --write` | [cursor.md](cursor.md) |
| VS Code | `garmin-connect-personal-mcp install-client vscode` (paste the snippet) | [vscode.md](vscode.md) |
| Gemini CLI | `garmin-connect-personal-mcp install-client gemini --write` | [gemini-cli.md](gemini-cli.md) |
| Codex CLI | `garmin-connect-personal-mcp install-client codex --write` | [codex.md](codex.md) |
| ChatGPT | Not yet: it only connects to remote (HTTPS) servers, which are planned. | |

Without `--write`, `install-client` only shows what it would change. With it, JSON configs are backed up before editing and only the `garmin` entry is touched.

Any other MCP client that can launch a local (stdio) server works too: point it at `garmin-connect-personal-mcp serve`, using the full path from `which garmin-connect-personal-mcp`.
