# Claude Desktop

## Before you start

Install the package, save your Garmin key and download your data. See the [README quickstart](../../README.md#quickstart). Then:

```bash
garmin-connect-personal-mcp install-client claude-desktop --write
```

Then quit Claude Desktop completely and reopen it. Starting a new chat isn't enough.

### Manual setup

Open **Settings > Developer > Edit Config**, which opens `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the server under `mcpServers`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/full/path/to/garmin-connect-personal-mcp",
      "args": ["serve"]
    }
  }
}
```

Find the full path with `which garmin-connect-personal-mcp` (macOS/Linux) or `where garmin-connect-personal-mcp` (Windows). Apps started from the dock or Start menu often don't see your shell's PATH, so the full path is safer than the bare command name.

## Check it works

In a new chat, open the tools or connectors menu in the message box. The `garmin` server and its tools should be listed. Ask "What Garmin data do you have?"; Claude should call `garmin_describe_data`.

## Things to ask

- "How did I sleep last night?"
- "Summarize my training and recovery this week."
- "Is my resting heart rate lower than three months ago?"
- "List my runs since July and how my pace changed."
- "Anything unusual in my HRV lately?"

## Options

Add these as environment variables in the server's configuration (`env`), or as `serve` flags:

| Setting | Effect |
| --- | --- |
| `GARMIN_MCP_READ_ONLY=1` (`--read-only`) | Never contact Garmin: hides `garmin_sync` and skips the startup sync. |
| `GARMIN_MCP_AUTO_SYNC=0` (`--no-auto-sync`) | Don't sync on startup when the data is over a day old. |
| `GARMIN_MCP_TOOLSETS=daily,activities` (`--toolsets`) | Load fewer tools (`daily`, `activities`, `sync`); helps smaller models. `garmin_describe_data` is always on. |

All tools are listed in [docs/tools.md](../tools.md).

## Troubleshooting

1. Run `garmin-connect-personal-mcp doctor`. It checks your key, Garmin access and the archive, and says how to fix anything wrong. Its output is safe to paste into an issue.
2. If the tools don't appear, re-run `garmin-connect-personal-mcp install-client claude-desktop` and restart the app. If you moved or reinstalled the package, the saved path may be out of date.
3. Claude Desktop writes MCP logs to `~/Library/Logs/Claude/mcp-server-garmin.log` on macOS and `%APPDATA%\Claude\logs\` on Windows.
