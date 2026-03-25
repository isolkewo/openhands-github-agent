# OpenHands GitHub Agent - Agent Instructions

## Overview
This is a 24/7 autonomous GitHub agent that manages PRs and Issues using the OpenHands SDK. It runs continuously, checking every 10 minutes for assigned issues, PR reviews, and mentions.

## Build/Lint/Test Commands

### Running the Agent
```bash
# Using the run script (recommended)
./run.sh

# Manual execution
source venv/bin/activate
export GITHUB_TOKEN
export GITHUB_USERNAME
export LLM_API_KEY
export LLM_BASE_URL
python3 github_agent.py
```

### Development Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Activate virtual environment
source venv/bin/activate
```

### Running Tests
This project does not have a formal test suite. To test changes:
```bash
# Run a dry-run by executing the agent with test environment variables
export GITHUB_TOKEN="test_token"
export LLM_API_KEY="test_key"
python3 -c "from github_agent import GitHubAgent; agent = GitHubAgent()"
```

### Code Quality
No linting or formatting tools are configured. When making changes:
- Run `python3 -m py_compile github_agent.py` to check for syntax errors
- Manually review code against the style guidelines below

## Code Style Guidelines

### Imports
- Standard library imports first (sorted alphabetically)
- Third-party imports second (sorted alphabetically)
- Local imports last
- Use absolute imports, not relative

```python
import os
import sys
from pathlib import Path
from typing import Optional, List, Dict

from openhands.sdk import LLM, Agent
```

### File Structure
- Shebang line for executable scripts: `#!/usr/bin/env python3`
- Module docstring at the top describing purpose
- Logging setup before any other imports that might log
- Class definitions followed by functions
- `if __name__ == "__main__":` guard at the bottom

### Naming Conventions
- **Classes**: PascalCase (`GitHubAgent`)
- **Functions/Variables**: snake_case (`_get_assigned_issues`, `github_token`)
- **Constants**: UPPER_SNAKE_CASE (`HEARTBEAT_INTERVAL`)
- **Private methods**: Leading underscore (`_api_request`)
- **Module-level private**: Leading underscore prefix

### Type Hints
- Use type hints for all function parameters and return values
- Use `Optional[T]` for nullable types
- Use `List[T]`, `Dict[K, V]` from `typing` module
- Use `None` for literal None returns

```python
def _api_request(
    endpoint: str, 
    method: str = "GET", 
    data: Optional[Dict] = None
) -> Optional[Dict]:
```

### Error Handling
- Use try/except blocks for API calls and file operations
- Log errors with appropriate level (ERROR for failures, DEBUG for details)
- Return `None` or default values on failure rather than raising
- Never silently swallow errors - always log

```python
try:
    result = self._api_request(endpoint)
except Exception as e:
    logger.error(f"Request failed: {e}")
    return None
```

### Logging
- Create module-specific logger: `logger = logging.getLogger("github-agent")`
- Use structured log levels: DEBUG, INFO, ERROR
- Include context in error messages (repo, issue numbers, etc.)
- Suppress verbose third-party library logs at startup

### API Design
- Prefix private methods with underscore
- Use descriptive method names (`_handle_issue`, `_mark_notification_read`)
- Keep methods focused on single responsibility
- Use data classes/dicts for structured data transfer

### Comments & Documentation
- Module-level docstrings describing purpose
- Class docstrings explaining role
- No inline comments for obvious code
- Docstrings use reStructuredText format

### Environment Variables
Required:
- `GITHUB_TOKEN` - GitHub API token
- `LLM_API_KEY` - LLM provider API key

Optional:
- `GITHUB_USERNAME` - Bot username (default: "openhands-bot")
- `LLM_MODEL` - LLM model (default: "anthropic/claude-sonnet-4-5-20250929")
- `HEARTBEAT_INTERVAL` - Seconds between cycles (default: 600)
- `WORK_DIR` - Working directory
- `STATE_DIR` - State persistence directory

### Security
- Never commit secrets or API keys
- Validate all external input before processing
- Use environment variables for credentials
- Check for `.env` files before committing

### GitHub API Usage
- Use the GitHub REST API via `urllib`
- Always include Authorization header
- Handle 403 errors specially (rate limiting/permissions)
- Use appropriate rate limiting and error handling

## Cursor/Copilot Rules
No Cursor or Copilot rules are configured in this repository.

## Key Files
- `github_agent.py` - Main agent implementation
- `run.sh` - Startup script with environment setup
- `prompts/github_agent_system_prompt.j2` - System prompt for the agent
- `requirements.txt` - Python dependencies

## Important Notes
- The agent runs continuously with a heartbeat loop
- State is persisted to `STATE_DIR/conversations/` for each issue/PR
- Uses the GitHub Notifications API for real-time updates
- Forks repos when lacking write access
- Comments on issues/PRs to communicate progress
