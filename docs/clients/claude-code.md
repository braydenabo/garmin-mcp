# Claude Code

## Before you start

Install the package, save your Garmin key and download your data. See the [README quickstart](../../README.md#quickstart). Then:

```bash
garmin-connect-personal-mcp install-client claude-code --write
```

This runs `claude mcp add --scope user ...`, so the server is available in every project.

### Manual setup

```bash
claude mcp add --scope user garmin -- garmin-connect-personal-mcp serve
```

## Check it works

Start a new session and run `/mcp`. `garmin` should be listed as connected. Then ask "What Garmin data do you have?".

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
| `GARMIN_MCP_TOOLSETS=daily,activities` (`--toolsets`) | Load fewer tools (`daily`, `activities`, `sync`, `charts`); helps smaller models. `garmin_describe_data` is always on. |

All tools are listed in [docs/tools.md](../tools.md).

## Troubleshooting

1. Run `garmin-connect-personal-mcp doctor`. It checks your key, Garmin access and the archive, and says how to fix anything wrong. Its output is safe to paste into an issue.
2. If the tools don't appear, re-run `garmin-connect-personal-mcp install-client claude-code` and restart the app. If you moved or reinstalled the package, the saved path may be out of date.
3. `claude mcp get garmin` shows the saved command. Running `claude --debug` prints MCP connection errors.
