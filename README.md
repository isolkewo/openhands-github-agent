# OpenHands GitHub Agent

A 24/7 automated GitHub PR and Issue manager that works on your repositories.

## Features

- Monitors PRs created by the bot account for feedback and merge conflicts
- Handles assigned issues automatically
- Picks up priority unassigned issues (high/critical, good first issue, help wanted)
- Creates branches, implements fixes, and opens PRs
- Persists state across restarts

## Setup

### 1. Clone and Install

```bash
cd /home/debian/openhands-github-agent
source venv/bin/activate
```

### 2. Configure Environment

Create `/etc/openhands-github-agent/env` with your settings:

```bash
sudo mkdir -p /etc/openhands-github-agent
sudo nano /etc/openhands-github-agent/env
```

Required variables:
- `LLM_MODEL` - Model name with provider prefix (e.g., `openai/qwen3.5:122b`)
- `LLM_API_KEY` - Your API key (or `not-needed` for local models)
- `LLM_BASE_URL` - LLM endpoint URL
- `GITHUB_TOKEN` - GitHub personal access token
- `GITHUB_USERNAME` - Bot username
- `GITHUB_REPOSITORIES` - Comma-separated account names to monitor

### 3. Start Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable openhands-github-agent
sudo systemctl start openhands-github-agent
```

### 4. Monitor

```bash
sudo journalctl -u openhands-github-agent -f
```

## Logs

Logs are written to `/var/log/openhands-github-agent/agent.log`

## State

Agent state persists in `/var/lib/openhands-github-agent/agent_state.json`

## Requirements

- Python 3.11+
- Virtual environment with dependencies installed
- Systemd for service management
