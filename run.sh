#!/bin/bash
cd /home/debian/openhands-github-agent
source venv/bin/activate

# Set correct git identity for commits
git config user.name "isolkewo"
git config user.email "isolkewo@users.noreply.github.com"

export GITHUB_TOKEN
export GITHUB_USERNAME
export GITHUB_REPOSITORIES
export HEARTBEAT_INTERVAL
export WORK_DIR
export STATE_DIR
export LLM_MODEL
export LLM_API_KEY
export LLM_BASE_URL
python3 github_agent.py
