# Risk Intel Alert Automation (AIthon)

Claude Code skills for the Risk Intel Alert pipeline: daily precision calculation, incremental value analysis, rule candidate generation, and Slack notifications for `#risk_fraud_squad`.

## Skills

| Skill | Description |
|-------|-------------|
| `risk-alert-precision` | Downloads the Risk Intel Alerts sheet, fetches Redash SQL, joins with Snowflake fraud data, outputs precision per alert per time window |
| `risk-alert-incremental-value` | For alerts above 10% precision, calculates incremental value over the payment ecosystem via Snowflake |
| `risk-alert-to-rules` | Converts alert SQL into candidate json-rules-engine rules using Chalk features |
| `risk-alert-slack-notifier` | Posts per-alert recommendation threads to `#risk_fraud_squad` |
| `risk-alert-e2e` | Orchestrates the full pipeline end-to-end; run this to trigger everything |

## Installation

### 1. Add this repo as a marketplace

Add the following to your `~/.claude/settings.json` under `extraKnownMarketplaces`:

```json
"extraKnownMarketplaces": {
  "aithon": {
    "source": {
      "source": "github",
      "repo": "amit-genish/fruad-aithon-automate-risk-intel-alerts"
    }
  }
}
```

### 2. Install the plugin

```bash
claude plugin install risk-alert@aithon
```

### 3. Verify

Open a new Claude Code session and run `/risk-alert-e2e` — the skill should load.

## Prerequisites

The pipeline requires:
- **uv** (Python package manager): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Node.js ≥ 18**: via `nvm` or `brew install node`
- **Snowflake env vars** in `~/.zshrc`: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`
- **Google Drive MCP connector** connected in Claude Code (for sheet download)
- **Snowflake MCP connector** connected in Claude Code (for queries)
- **Slack MCP connector** connected in Claude Code (for notifications)

The `risk-alert-e2e` skill checks all prerequisites before running.

## Running the pipeline

```
/risk-alert-e2e
```

Or trigger the full scheduled run directly. The pipeline prompts whether to send Slack messages or write a local report only.

## Scheduling

The pipeline is configured to run Mon–Sat at 05:00 TLV time (02:00 UTC).
Use the `schedule` skill to set this up:

```
Schedule: 0 2 * * 1-6
Prompt: Run the risk-alert-e2e skill
```
