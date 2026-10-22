# Gemini CLI

Google has reportedly moved individual users from Gemini CLI to a new "Antigravity CLI" (June 2026). If that applies to you, check its docs for MCP setup; the manual configuration below is the usual format.

## Before you start

Install the package, save your Garmin key and download your data. See the [README quickstart](../../README.md#quickstart). Then:

```bash
garmin-connect-personal-mcp install-client gemini --write
```

This edits `~/.gemini/settings.json`, keeping your other settings and backing up the old file.

### Manual setup

Add to `~/.gemini/settings.json`:

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

Or use Gemini CLI's own command:

```bash
gemini mcp add -s user garmin garmin-connect-personal-mcp serve
```

## Check it works

Start `gemini` and run `/mcp`. `garmin` should be CONNECTED and list its tools.

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
2. If the tools don't appear, re-run `garmin-connect-personal-mcp install-client gemini` and restart the app. If you moved or reinstalled the package, the saved path may be out of date.
3. `gemini --debug` prints MCP connection errors.
