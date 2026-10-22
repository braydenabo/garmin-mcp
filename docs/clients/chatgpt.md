# ChatGPT

ChatGPT cannot launch a local stdio server directly. OpenAI Secure MCP Tunnel provides a private connection to a server running on your computer; it does not make the Garmin server publicly reachable.

## Requirements

- ChatGPT web with permission to add custom MCP servers. Availability depends on your account and workspace; on Business and Enterprise/Edu, workspace settings and admin permissions apply.
- An OpenAI Platform organization and permission to create/use a Secure MCP Tunnel. The tunnel must be associated with the ChatGPT workspace where you will add the app.
- Permission to download OpenAI's `tunnel-client` during guided setup, or a copy already installed.
- Garmin setup completed and data synced. See the [README quickstart](../../README.md#quickstart).

## Guided setup (recommended)

After installing the Garmin package and syncing your archive, run:

```bash
garmin-connect-personal-mcp setup-chatgpt
```

The guide checks for an archive and `tunnel-client`. If the tunnel client is missing, it offers to download the official build for your computer, verifies the published SHA-256 checksum, and installs it in the app's private tools folder. You do not need administrator access or a PATH change. It continues setup immediately after installation.

The guide opens OpenAI Platform in your default browser when it needs you to create a tunnel or runtime key. Return to the terminal to paste the tunnel ID and enter the runtime key at the hidden prompt. The wizard creates a native `garmin-local` profile and automatically reuses it on later runs. Existing profiles are never overwritten.

After the connection check passes, choose **Yes** when asked to save the key in your OS keychain. Future runs reuse it without asking you to paste it again. Supported stores are macOS Keychain, Windows Credential Locker, and supported Linux Secret Service/KWallet backends. The wizard never writes the key into a profile, repository file or plaintext credential file. If no native keychain is available, it continues without saving; you can provide the key through the hidden prompt or `CONTROL_PLANE_API_KEY`. An explicitly supplied environment key takes precedence over the saved key.

The guide checks connectivity, opens ChatGPT's Plugins page, explains the browser steps, and starts the tunnel in your terminal. Finish adding Garmin once the tunnel is running. Keep the terminal open while using ChatGPT; Ctrl-C stops the tunnel. New profiles use `serve --read-only`, so ChatGPT reads the archive without contacting Garmin. Run `garmin-connect-personal-mcp sync` separately to update the archive.

To start again, including after an interrupted setup that already created a profile, use the same command:

```bash
garmin-connect-personal-mcp setup-chatgpt
```

On a restart, the guide skips the account setup pages and starts the existing tunnel after checking connectivity. This command runs in the foreground. On macOS you can opt into background startup below; nothing is enabled automatically.

Other options: `--tunnel-id <id>` skips the tunnel ID prompt; `--profile <name>` chooses a different profile; `--no-start` configures and checks without starting the tunnel; `--no-browser` prints links without opening tabs; `--no-keychain` bypasses saved credentials and disables saving. `--reuse-profile` remains available to explicitly reuse an existing profile. Reused profiles retain their existing server command and settings. If you use `GARMIN_MCP_DATA_DIR`, keep that environment setting when restarting.

To remove a saved key or replace an expired one:

```bash
garmin-connect-personal-mcp setup-chatgpt --forget-key
garmin-connect-personal-mcp setup-chatgpt
```

This removes the key for the selected profile from the OS keychain; it keeps the tunnel profile and your Garmin archive.

If the tunnel reports repeated **401 Unauthorized**, stop it with Ctrl-C, then run:

```bash
unset CONTROL_PLANE_API_KEY
garmin-connect-personal-mcp setup-chatgpt --replace-key
```

`--replace-key` bypasses both environment and saved credentials for this run and prompts privately. Enter a valid OpenAI tunnel runtime key from the organization associated with the tunnel, with Tunnels Read + Use. After authenticated metadata access succeeds, accept saving the replacement in your OS keychain. Removing the stale environment variable prevents it overriding the saved replacement on the next restart. `unset` is for macOS/Linux shells; in PowerShell use `Remove-Item Env:CONTROL_PLANE_API_KEY -ErrorAction SilentlyContinue` instead.

The wizard's local `doctor` check verifies configuration. It separately performs a read-only authenticated tunnel metadata lookup using the runtime key; passing the local check alone does not establish API access. It never uses an admin key to mask a failing runtime key.

If automatic installation fails, the wizard explains the error and opens the official download page as a fallback. On machines without a browser, it prints the exact link instead. Browser login and account/workspace permissions remain user steps.

Account and workspace permissions still need to be set in your browser. A successful local check does not confirm ChatGPT support: select the Garmin plugin and make the tool call below to verify the connection.

## Optional background startup (macOS)

Once the connection works in ChatGPT, you can keep the tunnel running without an open terminal. This is a per-user macOS LaunchAgent, not a cloud deployment. It starts when you log in, and launchd restarts exited processes with a 60-second launch throttle. The tunnel client handles temporary connection failures while it remains running. Your computer must still be awake, online and logged in for ChatGPT to reach it.

First stop any foreground tunnel with Ctrl-C. If you have not saved the runtime key yet, run the command below and accept saving it in your OS keychain:

```bash
garmin-connect-personal-mcp setup-chatgpt --no-start
```

Then explicitly enable startup:

```bash
garmin-connect-personal-mcp chatgpt-background install
garmin-connect-personal-mcp chatgpt-background status --check
```

`install` validates the existing profile and saved key before writing a service file or enabling the agent. It never creates a tunnel, prompts for a key, or saves an environment key. macOS may ask you to allow Python access to the saved key. If the keychain is locked or access is refused, unlock/authorize it and retry. No API key is written into the LaunchAgent or passed on the command line. Raw client output is discarded rather than recorded in background logs; `status --check` provides sanitized profile/key/network diagnostics.

`status` reports whether the agent is installed, loaded and running. `--check` additionally makes a read-only authenticated tunnel metadata request using the saved key. Neither check proves a tool call can complete: confirm by asking ChatGPT what Garmin data is available.

Use these controls later:

```bash
garmin-connect-personal-mcp chatgpt-background stop
garmin-connect-personal-mcp chatgpt-background start
garmin-connect-personal-mcp chatgpt-background uninstall
```

`stop` unloads the agent until you start it again or log in next time. `uninstall` also removes this profile's LaunchAgent, disabling future login startup; it keeps your keychain entry, tunnel profile and Garmin archive. Add `--profile <name>` after any action to manage another profile. Duplicate foreground/background runs for the same profile and archive are refused.

The agent pins the Python installation, tunnel-client executable and archive directory used at installation time. Keep that installation in place. If you move/reinstall Python or change the archive location, uninstall and install the agent again. Do not use a temporary `uvx` environment. In a source checkout, prefix commands with `uv run`; the agent uses that checkout's persistent `.venv`, not `uv` at login.

For a rejected or expired key, stop the agent, run `setup-chatgpt --replace-key --no-start` and save the replacement, then start the agent again. Background mode deliberately ignores shell API keys and uses only the saved keychain entry. Reused native tunnel profiles keep their existing command/settings; new guided profiles are read-only.

Background startup currently supports **macOS only**. Linux and Windows users can continue using the foreground `setup-chatgpt` command.

## Manual setup

1. In [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels), create a tunnel and associate it with the ChatGPT workspace you will use. Copy its tunnel ID.
2. Create a restricted runtime API key for `tunnel-client` in the Platform organization. Enter it locally as `CONTROL_PLANE_API_KEY`; do not paste it into ChatGPT, this repository, or an issue.
3. Configure and check a local stdio profile. Replace `<tunnel_id>` with the ID from step 1:

   ```bash
   tunnel-client init \
     --sample sample_mcp_stdio_local \
     --profile garmin-local \
     --tunnel-id <tunnel_id> \
     --mcp-command "garmin-connect-personal-mcp serve"
   tunnel-client doctor --profile garmin-local --explain
   ```

   Keep the runtime key in the local environment or another secret store supported by `tunnel-client`; don't put its value in shell history or a committed config. If `garmin-connect-personal-mcp` isn't on the executable search path, use its full path for `--mcp-command`.

4. Start the tunnel client and leave it running while adding or using the ChatGPT connection:

   ```bash
   tunnel-client run --profile garmin-local
   ```

5. In ChatGPT on the web, open **Plugins**, select **+**, then **Add custom MCP server**. Choose **Tunnel** as the connection, select the tunnel, choose **No authentication** (the local server does not implement OAuth), review the warning, and create the plugin. The tunnel itself is scoped through your Platform organization and associated ChatGPT workspace. If the tunnel is missing, check its workspace association and your tunnel **Read + Use** permission.
6. Install/select the plugin in a new conversation and ask: **“What Garmin data do you have?”** Then try **“How did I sleep yesterday?”** The first prompt should call `garmin_describe_data`; the second should use the local archive.

The tunnel client must remain running for tool discovery and calls. For setup details and permissions, see OpenAI's [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

## Optional HTTP mode

The server also supports Streamable HTTP at `http://127.0.0.1:8000/mcp`:

```bash
garmin-connect-personal-mcp serve --http --read-only --no-auto-sync
```

This is useful for local MCP Inspector checks or a tunnel profile that forwards to an HTTP MCP server. The server binds to loopback by default. A non-loopback bind is refused unless `GARMIN_MCP_HTTP_BEARER_TOKEN` is set. This static-token mode is for self-managed HTTP clients; it is not a substitute for OAuth on a public ChatGPT endpoint. Keep personal Garmin data behind the local tunnel unless you intentionally configure a separate authenticated deployment.

## Troubleshooting

- If charts still show an old layout after upgrading, restart the tunnel/server, refresh GarminMCP's tools in ChatGPT, and request a new chart in a new conversation. Chart UI addresses include a content version so the refreshed tool list points at fresh HTML; old conversation widgets can retain their cached layout.
- `tunnel-client doctor --profile garmin-local --explain` reports profile and connectivity problems.
- Confirm `tunnel-client run` is still active; ChatGPT cannot discover or call tools while it is stopped.
- If the tunnel is not listed, associate it with the target ChatGPT workspace and confirm the operator has **Tunnels Read + Use**.
- If ChatGPT connects but lists no tools, check that the configured MCP command starts `garmin-connect-personal-mcp serve` and that Garmin setup is complete.
