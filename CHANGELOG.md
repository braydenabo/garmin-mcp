# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Garmin API layer on top of Garmin's official client: retries with backoff on rate limits, server errors and network failures, one token refresh on HTTP 401, and error messages that say how to fix the problem.
- Local SQLite archive that stores each Garmin response unchanged, with versioned migrations and a read-only mode for the server.
- `init` command: saves your Garmin Personal API key with owner-only permissions, in the same place Garmin's own client looks, and creates the local data folder (override with `GARMIN_MCP_DATA_DIR`).
- Project skeleton: package, `--version` CLI, CI on Linux, macOS and Windows.
- Guards against committing secrets and health data.
