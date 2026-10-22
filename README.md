# garmin-connect-personal-mcp

An MCP server that lets any AI assistant answer questions about **your own** Garmin data: sleep, HRV, stress, body battery, training and activities.

It syncs Garmin's official [Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api) into a SQLite archive on your machine and serves it to Claude, ChatGPT, Gemini, Cursor, VS Code, Codex and any other MCP client.

> **Unofficial.** This project is not affiliated with, endorsed by, or supported by Garmin. Garmin and Garmin Connect are trademarks of Garmin Ltd. or its subsidiaries.
>
> **Not medical advice.** The data comes from consumer wearables. This server reports numbers and trends; it does not diagnose anything.

## Status

**Alpha.** Syncing, the local archive and the MCP server work and are tested on macOS, Linux and Windows. The package isn't on PyPI yet, so install it from GitHub (below). ChatGPT support needs the remote server, which is planned.

## Quickstart

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and a Garmin Personal API key.

1. **Install:**

   ```bash
   uv tool install git+https://github.com/braydenabo/garmin-mcp
   ```

2. **Save your key.** Create one in [Garmin Connect](https://connect.garmin.com/app/settings/personalApiToken), then paste it at the hidden prompt:

   ```bash
   garmin-connect-personal-mcp init
   ```

3. **Download your data** (the last 90 days; later runs pick up where they left off):

   ```bash
   garmin-connect-personal-mcp sync --days-back 90
   ```

4. **Keep it up to date** (optional, recommended). Garmin's API won't fill in old days on request, so a daily sync keeps your history complete:

   ```bash
   garmin-connect-personal-mcp schedule install
   ```

5. **Connect your assistant.** Run it without `--write` first to see what it will change:

   ```bash
   garmin-connect-personal-mcp install-client claude-desktop --write
   ```

   Other clients: `claude-code`, `cursor`, `vscode`, `gemini`, `codex`. See [docs/clients](docs/clients/README.md) for each one.

6. **Ask:** *"How did I sleep last night?"*

## What you can ask

- "Summarize my training and recovery this week."
- "Is my resting heart rate lower than three months ago?"
- "How has my weekly running distance built up since July?"
- "Anything unusual in my HRV lately?"
- "Show me my sleep stages for the last two weeks."

Ask to *see* your data ("show me my last 7 days of sleep") and apps that support [MCP Apps](https://modelcontextprotocol.io/) (Claude Desktop, claude.ai, ChatGPT, VS Code) display an interactive chart. Elsewhere you get the same numbers as text.

The assistant uses seven tools: `garmin_describe_data`, `garmin_get_day`, `garmin_get_trend`, `garmin_list_activities`, `garmin_get_activity`, `garmin_show_chart` and `garmin_sync`. See [docs/tools.md](docs/tools.md). Missing data is reported as missing, never as zero.

## Troubleshooting

Run:

```bash
garmin-connect-personal-mcp doctor
```

It checks your API key, Garmin access, the local archive, recent sync errors and the daily schedule, and says how to fix anything wrong. The report contains no API key, health data or home folder path, so you can paste it into an issue as is. Add `--offline` to skip contacting Garmin.

## Privacy

- **You run it yourself, with your own key.** There is no hosted service, and your data never passes through this project.
- Your archive and API key stay on your machine, outside this repository.
- The only network calls go to Garmin's own API, to sync. `serve --read-only` turns even that off.
- Tools never return your name, profile photos or GPS coordinates, even though Garmin's records contain them, so a cloud assistant isn't told where you live.

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest
```

[AGENTS.md](AGENTS.md) has the commands and repo rules, for people and AI coding agents alike. To report a security problem, see [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE)
