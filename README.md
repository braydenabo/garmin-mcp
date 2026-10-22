# garmin-connect-personal-mcp

An MCP server that lets any AI assistant answer questions about **your own** Garmin data: sleep, HRV, stress, body battery, training and activities.

It syncs Garmin's official [Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-api) into a SQLite archive on your machine and serves it to Claude, ChatGPT, Gemini, Cursor, VS Code, Codex and any other MCP client.

> **Unofficial.** This project is not affiliated with, endorsed by, or supported by Garmin. Garmin and Garmin Connect are trademarks of Garmin Ltd. or its subsidiaries.
>
> **Not medical advice.** The data comes from consumer wearables. This server reports numbers and trends; it does not diagnose anything.

## Status

**Pre-alpha. Nothing works yet.** The repository is being set up. Follow the [releases](https://github.com/braydenabo/garmin-mcp/releases) for the first usable version.

## How it will work

1. Create a Personal API key in [Garmin Connect](https://connect.garmin.com/app/settings/personalApiToken).
2. `uvx garmin-connect-personal-mcp init`: stores the key privately and runs a first sync.
3. `uvx garmin-connect-personal-mcp install-client claude`: adds the server to your assistant (or `cursor`, `vscode`, `gemini`, `codex`).
4. Ask: *"How did I sleep last night?"*

## Privacy

- **You run it yourself, with your own key.** There is no hosted service, and your data never passes through this project.
- Your archive and API key stay on your machine, outside this repository.
- Every tool is read-only towards Garmin. The only network call goes to Garmin's own API, to sync.
- Remote (HTTP) mode is opt-in and refuses to listen on a network without authentication.

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest
```

[AGENTS.md](AGENTS.md) has the commands and repo rules, for people and AI coding agents alike. To report a security problem, see [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE)
