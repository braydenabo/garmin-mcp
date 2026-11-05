# FAQ

## What do I need?

A Garmin device that syncs to Garmin Connect, a Garmin Connect account, and a Personal API key, which you create in [Garmin Connect's settings](https://connect.garmin.com/app/settings/personalApiToken). On your computer you need [uv](https://docs.astral.sh/uv/getting-started/installation/); it installs Python for you if needed. The [README quickstart](../README.md#quickstart) takes about five minutes.

## Can I try it without a Garmin account?

Yes. `garmin-connect-personal-mcp demo ~/garmin-demo` writes made-up data into a new folder; run the server with `GARMIN_MCP_DATA_DIR=~/garmin-demo` to use it. See [the README](../README.md#try-it-without-a-garmin-account).

## Which devices work?

Any device that syncs to Garmin Connect. This project reads Garmin's servers, not the watch, so what you get depends on what your device records: HRV needs a watch that measures it overnight, power needs a power meter or running-power support, and so on. Missing data is reported as missing, never as zero.

## Which AI apps work?

Any app that supports MCP. There are setup guides for Claude Desktop, Claude Code, ChatGPT, Cursor, VS Code, Gemini CLI and Codex CLI in [docs/clients](clients/README.md). Interactive charts appear in apps that support MCP Apps (Claude Desktop, claude.ai, ChatGPT, VS Code); others get the same numbers as text.

## How far back does my history go?

Garmin's Personal API returns at least 90 days; how much further it goes isn't documented. The first sync fetches 7 days. To fetch more:

```bash
garmin-connect-personal-mcp sync --days-back 365
```

Up to 3650 days can be requested. Days Garmin no longer returns can't be fetched later, which is why the [daily sync](troubleshooting.md#daily-sync-isnt-running) is worth turning on: your archive keeps everything it has ever downloaded.

## Is there a rate limit?

Yes, but Garmin doesn't publish it. Sync uses Garmin's date-range requests, so a 90-day sync takes about 20 requests. If Garmin does refuse requests, sync stops cleanly and resumes from there next time; see [Rate limited](troubleshooting.md#rate-limited).

## Who sees my data?

- **Your archive and API key stay on your computer.** There's no hosted service, and nothing passes through this project.
- **The only network calls go to Garmin**, to sync. `serve --read-only` turns even that off.
- **Your AI app sees what its tools return.** When you ask a cloud assistant about your sleep, the numbers it needs are sent to that assistant's provider, under that provider's terms. Tools never return your name, profile photos or GPS coordinates. [data.md](data.md#what-the-tools-never-return) has the details.

## Does my API key expire?

Garmin doesn't say. If it stops working, sync reports "Garmin rejected the API key (HTTP 401)". Create a new key and save it with `garmin-connect-personal-mcp init --force`.

## Can it change anything in Garmin Connect?

No. Garmin's Personal API is read-only, and this project only ever reads from it. `garmin_sync` writes only to your local archive.

## Why are distances in kilometers?

Every value is metric, with the unit in its name (`distance_km`, `pace_min_per_km`), so an assistant always knows what a number means. Ask for miles or pounds and the assistant converts them. A built-in imperial option may come later.

## Why is today's data incomplete?

Your watch uploads to Garmin through the Garmin Connect app every so often, and totals like steps keep counting until midnight. Sync re-checks the last two days, so late uploads are picked up the next time it runs.

## Does it work offline?

The tools read only your local archive, so questions about data you've already synced work without internet. Only syncing needs a connection.

## Can several people use one install?

One install is one person: one API key, one archive. Each person should run their own copy with their own key. To keep separate archives on one computer, give each one its own `GARMIN_MCP_DATA_DIR`.

## Is it medical advice?

No. The data comes from consumer wearables, and the tools report numbers and trends without diagnosing anything. Talk to a doctor about health concerns.

## Is this made by Garmin?

No. It's an independent open-source project, not affiliated with, endorsed by or supported by Garmin. It uses Garmin's official Personal API and Garmin's official Python client.

## How do I uninstall it?

```bash
garmin-connect-personal-mcp schedule remove
garmin-connect-personal-mcp chatgpt-background uninstall   # only if you set it up (macOS)
uv tool uninstall garmin-connect-personal-mcp
```

Then remove the server from your AI app's settings, and delete the data folder if you don't want the archive (`doctor` shows where it is). To revoke the key, delete it in [Garmin Connect](https://connect.garmin.com/app/settings/personalApiToken).
