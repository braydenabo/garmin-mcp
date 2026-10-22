# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor, Gemini, Copilot, etc.) working in this repository. The same rules apply to human contributors.

## What this is

`garmin-connect-personal-mcp`: an unofficial MCP server for a person's own Garmin Connect data. A sync step pulls from Garmin's official Personal API into a local SQLite archive; MCP tools read only from that archive. Python 3.12+, managed with `uv`. The import package is `garmin_connect_personal_mcp`, the CLI is `garmin-connect-personal-mcp`.

## Commands

```bash
uv sync                      # install deps (including dev group)
uv run pre-commit install    # install git hooks
uv run pytest                # tests (no network; coverage gate 80%)
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict
```

## Hard rules: this is a public repository

1. **Never commit secrets.** No API keys, tokens, `.env` files or token caches. Keys live in `~/.config/garmin/connect-personal-api-key` or `GARMIN_PERSONAL_API_KEY`.
2. **Never commit personal data.** No real health data, FIT/GPX/TCX files, databases, real activity IDs, GPS coordinates, or screenshots of real data. Test fixtures and doc examples use synthetic values only. Never record real API responses as fixtures.
3. **Never commit planning documents.** Plans stay in the gitignored `.plans/` directory.
4. Do not weaken `.gitignore`, `scripts/check_*.py`, `.pre-commit-config.yaml` or the CI guard job to get a commit through.

## Helping a user set it up

These commands are safe for a coding agent to run when the user asks:

- `garmin-connect-personal-mcp sync [--days-back N]` downloads data into the user's local archive.
- `garmin-connect-personal-mcp schedule install [--time HH:MM]` installs a daily sync with the OS scheduler. It's a lasting change to the user's machine, so run it only when the user asks for automatic syncing. `schedule status` and `schedule remove` check or undo it.

Never handle the user's API key yourself. The user saves it with `garmin-connect-personal-mcp init` at the hidden prompt. MCP tools must never install or change schedules; they only point the user to the command.

## Conventions

- Source in `src/garmin_connect_personal_mcp/`, tests in `tests/`, mirroring module names.
- Tests never touch the network or the user's real data directory.
- MCP tools are named `garmin_<verb>_<noun>`, use flat JSON input schemas (no `anyOf`/`oneOf`), put units in field names (`distance_km`, `hr_bpm`), and declare accurate annotations.
- Tool names and arguments are public API: renaming one is a breaking change.
- User-facing errors say what to do next, e.g. "No data for 2026-10-21; last sync was 2026-10-19. Call garmin_sync."
- Update `CHANGELOG.md` under "Unreleased" for user-visible changes.
