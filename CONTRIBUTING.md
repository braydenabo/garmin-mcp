# Contributing

Thanks for helping. Bug reports, setup questions, docs fixes and code are all welcome.

## Before you post anything

This project handles personal health data. In issues, pull requests, discussions and commits, **never include**:

- your Garmin API key, password or tokens
- real health data, FIT/GPX/TCX files, database files, activity IDs or GPS coordinates
- screenshots that show your real data

For a bug, paste the output of `garmin-connect-personal-mcp doctor` instead. It is redacted and safe to share. Report security problems privately, as described in [SECURITY.md](SECURITY.md).

## Development setup

You need [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv sync
uv run pre-commit install
```

Checks that CI runs on every pull request:

```bash
uv run pytest                                        # no network, coverage gate 80%
uv run ruff check . && uv run ruff format --check .
uv run mypy                                          # strict
```

The pre-commit hooks scan for secrets and block database, FIT and archive files. Please don't bypass or weaken them; if one blocks something it shouldn't, open an issue.

[AGENTS.md](AGENTS.md) has the layout and conventions, for people and AI coding agents alike.

## Pull requests

- Keep each pull request to one change, with tests.
- Tests use synthetic data only and never touch the network or your real data folder.
- Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
- MCP tool names, arguments and annotations are public API. If you change one on purpose, update the snapshot and docs with `uv run python scripts/gen_tools_doc.py` and explain why in the pull request.

## Stability

From 1.0 the project follows [Semantic Versioning](https://semver.org/). People's prompts, scripts and saved setups depend on the server, so within a major version its public API only grows.

**Public, and kept stable until the next major version:**

- tool names, and their argument names and types
- output field names and types
- metric names and their units
- `garmin_query`'s tables and columns, and the `garmin://schema` resource
- CLI commands and options, and environment variables

**Fine in any minor release:**

- new tools, metrics, output fields, query columns or CLI options
- new *optional* arguments, and new values for an argument that takes a fixed set
- better tool descriptions, titles, notes and error messages
- anything inside the archive, which only this project reads

**Breaking, so only in a new major version:** renaming or removing anything public, changing its type or unit, making an optional argument required, or adding a required one. To replace something, add the new name and keep the old one working until the next major version. Say in the changelog that it's deprecated.

`tests/snapshots/public-api-v1.json` records the 1.x tools, metrics and query tables, and a test fails if a change would break them. CLI commands and environment variables are checked in review. After deliberately adding something you want to promise, record it with `uv run python scripts/freeze_api.py`. The script refuses to drop anything. Starting 2.0 means bumping the version first; the script then writes `public-api-v2.json`.

The chart widget's helper tool, `garmin_get_chart_data`, isn't public: only the widget that ships with the server calls it.

## Releasing (maintainers)

1. Make sure CI on `main` is green.
2. Move the **Unreleased** entries in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading, and update the links at the bottom.
3. Set `__version__` in `src/garmin_connect_personal_mcp/__init__.py` to `X.Y.Z`.
4. Check `uv run python scripts/gen_tools_doc.py` leaves no diff.
   If tools or their descriptions changed, run the [evals](evals/README.md) and add the results to the release notes.
5. Commit, then tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

The [release workflow](.github/workflows/release.yml) runs the full test matrix, publishes to PyPI with Trusted Publishing, and creates the GitHub release from the changelog section. It stops before publishing if the tag doesn't match the version or the changelog has no section for it.
