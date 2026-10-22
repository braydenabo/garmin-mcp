# Security policy

This project handles API keys and personal health data, so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Report privately through GitHub: [Security → Report a vulnerability](https://github.com/braydenabo/garmin-mcp/security/advisories/new).

Include what you found, how to reproduce it, and the version affected. You should get a reply within 7 days.

## What never to share

Nobody from this project will ever ask for your Garmin API key, Garmin password, OAuth tokens or database file. Never paste them into issues, discussions or pull requests. If you posted one by mistake, revoke the key in [Garmin Connect](https://connect.garmin.com/app/settings/personalApiToken) right away, then delete the post.

## Supported versions

Only the latest release gets security fixes until 1.0.

## Scope

In scope: this server, its CLI, its sync and storage, and its HTTP mode. Out of scope: Garmin's API and its official client, MCP clients, and how a user chooses to expose their own deployment.
