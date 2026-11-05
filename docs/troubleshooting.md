# Troubleshooting

Find your symptom below. Most problems are fixed by what `doctor` tells you, so start there.

## Start here: run doctor

```bash
garmin-connect-personal-mcp doctor
```

It checks your API key, Garmin access, the archive, recent sync errors and the daily schedule, and prints a fix under anything wrong. Add `--offline` to skip contacting Garmin, or `--json` for output an AI agent can read. The report contains no API key, health data or home folder path, so it's safe to paste into an issue.

If you installed with `uv tool install`, the commands below work as written. In a source checkout, put `uv run` in front of each one.

## API key

**"No Garmin API key found"** or doctor's **"no key saved"**: no key is saved yet. Create one in [Garmin Connect](https://connect.garmin.com/app/settings/personalApiToken), then run `garmin-connect-personal-mcp init` and paste it at the hidden prompt.

**"Garmin rejected the API key (HTTP 401)"**: the key was revoked, expired or mistyped. Create a new one and save it with `garmin-connect-personal-mcp init --force`.

**"... is empty"**: the key file exists but holds nothing. Run `garmin-connect-personal-mcp init --force`.

**"other users can read it"** (macOS and Linux): run the `chmod 600 ...` command doctor prints, so only you can read the key.

**"your API key is only in $GARMIN_PERSONAL_API_KEY"** when installing the schedule: a scheduled job can't see variables from your shell. Save the key to a file with `garmin-connect-personal-mcp init`.

Never paste your key into an issue, a chat with an assistant, or a pull request. If you did, revoke it in Garmin Connect straight away.

## Can't reach Garmin

**"Couldn't reach Garmin"**: check your internet connection, VPN or firewall, then run `doctor` again.

**"Garmin's servers returned an error (HTTP 5xx)"**: a problem on Garmin's side. Sync already retries a few times. Try again later; everything already saved is kept.

## Rate limited

**"Garmin's rate limit was reached (HTTP 429)"**: Garmin limits how many requests a Personal API key can make. The limit isn't published, and once reached it can take a while to clear, so wait before trying again rather than retrying straight away.

Sync stops cleanly when this happens and keeps everything saved so far. The next `sync` resumes where it stopped. Large backfills (`--days-back` in the hundreds) are the most likely to hit the limit; split them across a few runs if needed.

## No data, or data is missing

Work through these in order:

1. **Has your watch synced to Garmin Connect?** This project reads Garmin's servers, not your watch. Open the Garmin Connect app and let it sync first.
2. **Sync again.** `garmin-connect-personal-mcp sync` fetches anything new and re-checks the last two days, because watches often upload late. An assistant can do the same with `garmin_sync`, unless the server runs with `--read-only`.
3. **Was the watch worn?** Days without wear come back from Garmin as empty records. The tools report those days as missing (`no_data`), never as zeros.
4. **Is the date before your first sync?** The first sync fetches 7 days. For older history run `garmin-connect-personal-mcp sync --days-back 90` (up to 3650). Garmin's Personal API returns at least 90 days; how much further it goes isn't documented.

`garmin_get_day` gives each dataset a status that tells these cases apart: `data`, `no_data` (checked, nothing recorded), `not_synced` (sync hasn't reached that date) and `sync_failed` (sync tried and got an error).

## Sync errors

doctor's **Sync errors** check lists days that failed. Each `sync` retries them automatically, so a single failure usually clears itself. If the same days keep failing, run `garmin-connect-personal-mcp sync` and read its output. If that doesn't explain it, [open an issue](#reporting-a-problem).

**"Another sync is already running"**: only one sync runs at a time, whether from a terminal, the daily schedule or the MCP server. Wait for it to finish. The lock is released automatically even if a sync crashes.

## Daily sync isn't running

Check its state and the time of the last successful sync:

```bash
garmin-connect-personal-mcp schedule status
```

- **Not installed**: run `garmin-connect-personal-mcp schedule install` (add `--time HH:MM` to choose when).
- **Installed but not syncing**: read `logs/sync.log` in the data folder (`schedule status` prints its path). Each run appends its output there, errors included.
- **You moved or reinstalled Python or the package**: the job runs the exact Python that installed it. Reinstall the job with `schedule remove`, then `schedule install`.
- **You installed it from `uvx`**: uvx's cache can be cleared at any time, which breaks the job. Install for good with `uv tool install garmin-connect-personal-mcp`, then reinstall the schedule.
- **The computer was off or asleep at the scheduled time**: the systemd timer catches up when the computer is back, and launchd runs a sync missed during sleep when the Mac wakes. Cron and Windows Task Scheduler skip missed runs. Either way, the MCP server syncs when it starts if the data is over a day old, and the next daily run fills the gap.

## The archive

**"no archive yet"**: run `garmin-connect-personal-mcp sync` to create it.

**"schema version N is newer than this package supports"**: the archive was written by a newer release. Upgrade with `uv tool upgrade garmin-connect-personal-mcp`.

**"can't open ..." or "can't read ..."**: the database file is damaged or unreadable. Restore `garmin.db` from a backup if you have one. Otherwise move it aside and run `sync --days-back 3650` to rebuild it. Only what Garmin still returns can be fetched again (at least 90 days), so back up the data folder if your history matters to you.

## The assistant doesn't see the tools

1. **Restart the app** (Claude Desktop, Cursor, VS Code and so on). Most apps only start MCP servers when they launch.
2. **Re-run the setup** with `garmin-connect-personal-mcp install-client <app>`. The saved command points at a specific Python, so moving or reinstalling the package can break it. See [docs/clients](clients/README.md) for your app.
3. **Check toolsets.** `--toolsets` or `GARMIN_MCP_TOOLSETS` limits which tools load (`daily`, `activities`, `query`, `sync`, `charts`). Leave it unset for all of them. `--read-only` or `GARMIN_MCP_READ_ONLY=1` removes `garmin_sync`.
4. **Read the app's MCP log.** Each client guide says where it is. A Python traceback there usually names the problem.

## A new tool or fix doesn't show up

Apps remember a server's tool list. After upgrading:

1. **Make sure the new version is running.** Run `garmin-connect-personal-mcp --version`, and upgrade with `uv tool upgrade garmin-connect-personal-mcp`.
2. **Restart the app**, or reload its MCP servers.
3. **ChatGPT:** open GarminMCP at [chatgpt.com/plugins](https://chatgpt.com/plugins), click **Refresh**, and start a **new conversation**. Existing conversations keep the old tool list. If the tunnel runs in the background, restart it first with `chatgpt-background stop`, then `start`.

## Activity streams

**"The FIT file for activity N isn't downloaded yet"**: sync downloads each activity's file after listing it. Run `sync` (or `garmin_sync`), then ask again.

**"... is missing from the archive folder"**: the file was deleted or moved. `garmin_get_activity` still has the summary.

**A field is always null**, such as power or altitude: the watch didn't record it for that activity. Power needs a power meter or a watch that estimates running power; altitude needs a barometric altimeter or GPS.

## garmin_query errors

- **"Only SELECT statements ... are allowed"**: the tool only reads. It can't change data.
- **"no such table"**: the only tables are `daily` and `activities`. Their columns, with units, are in the `garmin://schema` resource and in [data.md](data.md#daily-metrics).
- **"took over 2 seconds and was stopped"**: narrow the query with a `WHERE` on `date`, or aggregate.
- **"Only the first 500 rows are shown"**: add `GROUP BY` or `LIMIT`.

## ChatGPT

The [ChatGPT guide](clients/chatgpt.md#troubleshooting) covers the tunnel, workspace permissions, the runtime key and charts.

## Reporting a problem

[Open an issue](https://github.com/braydenabo/garmin-mcp/issues/new/choose) with:

- the output of `garmin-connect-personal-mcp doctor`
- your operating system and the AI app you use
- what you did, what you expected, and what happened instead

Leave out your API key, health data, activity IDs and screenshots of your data. Report security problems privately, as described in [SECURITY.md](../SECURITY.md).
