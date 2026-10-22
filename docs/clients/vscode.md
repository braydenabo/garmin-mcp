# VS Code

## Before you start

Install the package, save your Garmin key and download your data. See the [README quickstart](../../README.md#quickstart). Then:

VS Code's user-level MCP file has no fixed location, so it can't be edited automatically. Print the snippet:

```bash
garmin-connect-personal-mcp install-client vscode
```

Then run the command **MCP: Open User Configuration** from the Command Palette and paste it in. It looks like this, with your real path:

```json
{
  "servers": {
    "garmin": {
      "type": "stdio",
      "command": "/full/path/to/garmin-connect-personal-mcp",
      "args": ["serve"]
    }
  }
}
```

Find the full path with `which garmin-connect-personal-mcp` (macOS/Linux) or `where garmin-connect-personal-mcp` (Windows). Apps started from the dock or Start menu often don't see your shell's PATH, so the full path is safer than the bare command name.

To use it in one project only, put the same snippet in `.vscode/mcp.json` in that project instead.

## Check it works

Run **MCP: List Servers** from the Command Palette. `garmin` should be running. In Copilot Chat's agent mode, the Garmin tools appear in the tools picker.

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
2. If the tools don't appear, re-run `garmin-connect-personal-mcp install-client vscode` and restart the app. If you moved or reinstalled the package, the saved path may be out of date.
3. **MCP: List Servers > garmin > Show Output** shows the server's log.
