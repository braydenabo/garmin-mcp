# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
