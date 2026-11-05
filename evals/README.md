# Evals

Questions to ask an AI assistant connected to this server, with what a good answer contains. They check the whole loop: whether a model picks the right tools, reads the results correctly, and stays honest about missing data and medical limits. Run them before a release that changes tools or their descriptions, and record the results in the release notes, not raw transcripts.

They use the made-up `demo` archive, never real data, so they're safe to run with any assistant and the answers are known.

## Running them

1. **Make a demo archive today.** The questions say "last night" and "the last 30 days", which count back from the demo's last day:

   ```bash
   garmin-connect-personal-mcp demo ~/garmin-eval
   ```

2. **Print the answer key:**

   ```bash
   uv run python scripts/eval_key.py ~/garmin-eval
   ```

   The numbers come from the server's own tools, so they're what a model should find if it uses them well.

3. **Connect the assistant to the demo archive.** For example, for Claude Code:

   ```bash
   GARMIN_MCP_DATA_DIR=~/garmin-eval garmin-connect-personal-mcp install-client claude-code --write
   ```

   This replaces the app's `garmin` server entry, so the assistant only sees the demo. Use the [client guide](../docs/clients/README.md) for other apps, with `GARMIN_MCP_DATA_DIR` set the same way. Afterwards, run the same command without `GARMIN_MCP_DATA_DIR` to switch back to your real archive. Sync is switched off for demo archives, so `garmin_sync` can't pull your real data into the demo.

4. **Ask each question in [questions.md](questions.md) in a new conversation**, exactly as written, then grade the answer against the key and the question's criteria.

## Grading

Each question gets one grade:

- **Pass**: every "must" criterion is met, and nothing is made up.
- **Partial**: the answer is right but misses a "must" criterion, such as a date range or a unit.
- **Fail**: a wrong number, invented data, a diagnosis, or no tool call when one was needed.

Numbers within rounding of the key (a tenth of an hour, one bpm, a few steps) count as right. Calling different tools from the ones listed is fine if the answer is right; the tools column says what a good path looks like.

## Recording results

For each assistant and model: the date, the package version, and the grade per question id, with one line on each partial or fail. For example:

| Question | Claude Sonnet 5.5 | GPT-... | Gemini ... |
| --- | --- | --- | --- |
| sleep-last-night | Pass | | |
| sick-spell | Partial: found the dates, didn't compare with before | | |
