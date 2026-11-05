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

## Releasing (maintainers)

1. Make sure CI on `main` is green.
2. Move the **Unreleased** entries in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading, and update the links at the bottom.
3. Set `__version__` in `src/garmin_connect_personal_mcp/__init__.py` to `X.Y.Z`.
4. Check `uv run python scripts/gen_tools_doc.py` leaves no diff.
5. Commit, then tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

The [release workflow](.github/workflows/release.yml) runs the full test matrix, publishes to PyPI with Trusted Publishing, and creates the GitHub release from the changelog section. It stops before publishing if the tag doesn't match the version or the changelog has no section for it.
