# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Background status checks no longer fail by trying to reserve the running tunnel's health-listener port. Live-agent checks use a temporary loopback port without changing or restarting the daemon. Local configuration failures no longer suggest replacing the API key, and status hints show the complete CLI command.
- Tunnel runners now reject duplicate instances for the same profile and archive using a crash-released OS lock. Background child processes are stopped with their runner rather than left orphaned.
- Chart guidance requests one chart for general sleep prompts and discourages redrawing the same data with other plotting tools. Chart resources describe their rendered UI to ChatGPT, and height reporting also uses its intrinsic-height bridge when available.
- Chart HTML uses content-versioned resource addresses so clients can fetch layout fixes after refreshing their tool metadata. The original resource remains available for compatibility. Crowded month-end date labels no longer overlap.
- ChatGPT setup checks authenticated tunnel metadata access before saving a runtime key or starting the client. It identifies environment-key overrides and provides `--replace-key` to bypass a rejected environment or saved key; the local configuration check alone no longer implies successful authentication.
- Charts no longer leave a blank scroll area beneath longer date ranges. The screen-reader table is contained in a clipped wrapper, and chart height is reported after the host finishes initializing, including when results shrink.

### Added

- Optional macOS `chatgpt-background install | start | stop | status | uninstall` controls run the tunnel at login with launchd restart throttling. They require a saved native-keychain key, keep credentials out of service files and logs, preserve the configured archive location, and leave the key/profile/archive intact on uninstall. `status --check` checks authenticated metadata access without claiming end-to-end ChatGPT connectivity.
- Charts now show metric-specific colors, rounded bars, duration formatting, average/selected-day headings, keyboard selection and a sleep-stage legend.
- `setup-chatgpt`: guided local Secure MCP Tunnel setup, with prerequisite checks, native tunnel profile creation/reuse, hidden runtime-key input, connectivity checks and foreground startup. New profiles expose read-only tools.
- ChatGPT setup automatically reuses existing tunnel profiles and can remember a validated runtime key in the native OS keychain with consent. The same setup command restarts the tunnel without repeated account prompts. `--no-keychain` bypasses credential storage; `--forget-key` removes a saved key without deleting the profile or archive.
- ChatGPT setup now offers to download and checksum-verify the official tunnel client for macOS, Linux or Windows, installs it privately without PATH changes, and opens the account and ChatGPT setup pages in your browser. `--no-browser` supports headless setups.
- Optional Streamable HTTP transport at `/mcp` for MCP clients and Secure MCP Tunnel. It binds to loopback by default and requires `GARMIN_MCP_HTTP_BEARER_TOKEN` before accepting non-loopback binds. Authentication and bind validation are covered by offline tests; invalid settings are rejected before any startup sync.
- `garmin_show_chart`: when you ask to see your data, apps that support MCP Apps (Claude Desktop, claude.ai, ChatGPT, VS Code) show an interactive chart: bars for totals, lines for levels, stacked sleep stages, with gaps for days without data. The chart page is self-contained and works offline; other apps get the same data as text. It can be turned off with the `charts` toolset.
- `install-client`: shows or applies the setup for Claude Desktop, Claude Code, Cursor, VS Code, Gemini CLI and Codex CLI, plus a setup guide for each in `docs/clients/`. The README quickstart now matches what works today.
- `docs/tools.md`, a tools reference generated from the server, and a contract snapshot test so tool names, schemas and annotations can't change by accident.
- MCP tool `garmin_sync` (incremental by default, backfill up to 90 days), a background sync when the server starts with data over a day old, and `serve --read-only`, `--no-auto-sync` and `--toolsets` (also settable through `GARMIN_MCP_READ_ONLY`, `GARMIN_MCP_AUTO_SYNC` and `GARMIN_MCP_TOOLSETS`).
- MCP tools `garmin_list_activities` (newest first, filtered by date and type, paged) and `garmin_get_activity` (heart-rate zones, power, cadence, training effect, splits). Units are converted (km, minutes, km/h, min/km), and the owner's name, profile photos and GPS coordinates are never returned.
- MCP tools `garmin_get_day` (sleep, HRV and daily summary for one date, with a per-dataset status: data, no_data, not_synced or sync_failed) and `garmin_get_trend` (one metric per day or per week, with summary stats). Dates accept `today` and `yesterday`.
- MCP server (`serve`, stdio) with its first tool, `garmin_describe_data`: what's in the archive, each dataset's coverage and last sync, and every metric with its unit. Built on the official MCP Python SDK 2.x (protocol 2026-07-28).
- `doctor` command: checks the API key, Garmin access, the archive, sync errors, a running sync, the log and the daily schedule, with a fix for each problem. The report is safe to paste publicly (no API key, health data or home folder path). `--offline` skips contacting Garmin; `--json` is for agents.
- `schedule install | status | remove`: run `sync` once a day with the OS scheduler (launchd on macOS, a systemd user timer or cron on Linux, Task Scheduler on Windows). Output goes to `logs/sync.log`; the API key is never written into a scheduler file.
- `sync --days-back` accepts 1 to 3650 days (10 years), to catch typos early.
- Ctrl-C during `sync` stops cleanly instead of printing a traceback, keeps everything saved so far, and prints the command to continue.
- Only one sync runs at a time: a second one (another terminal, or a scheduled run) exits with a clear message. The lock is released by the OS even if a sync crashes.
- Activity sync keeps paging until Garmin returns nothing new, so it no longer stops early if Garmin sends smaller pages than requested.
- `sync` command: downloads daily summaries, sleep, HRV, stress, steps, body battery, weight, activities and original FIT files into the archive. Uses Garmin's date-range endpoints (a 90-day sync is about 20 requests), shows progress, resumes where it left off, re-checks the last two days, retries failed days, and stops cleanly on rate limits.
- Garmin API layer on top of Garmin's official client: retries with backoff on rate limits, server errors and network failures, one token refresh on HTTP 401, and error messages that say how to fix the problem.
- Local SQLite archive that stores each Garmin response unchanged, with versioned migrations and a read-only mode for the server.
- `init` command: saves your Garmin Personal API key with owner-only permissions, in the same place Garmin's own client looks, and creates the local data folder (override with `GARMIN_MCP_DATA_DIR`).
- Project skeleton: package, `--version` CLI, CI on Linux, macOS and Windows.
- Guards against committing secrets and health data.
