# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `init` command: saves your Garmin Personal API key with owner-only permissions, in the same place Garmin's own client looks, and creates the local data folder (override with `GARMIN_MCP_DATA_DIR`).
- Project skeleton: package, `--version` CLI, CI on Linux, macOS and Windows.
- Guards against committing secrets and health data.
