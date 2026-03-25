#!/usr/bin/env python3
"""
OpenHands GitHub Agent - 24/7 GitHub PR and Issue Manager

This agent runs continuously and checks every 10 minutes for:
1. Active PRs needing attention (review comments, rebase, etc.)
2. Issues assigned to self
3. Important unassigned issues to work on

Always assigns issues to self or comments when starting work.
"""

import os
import sys
import json
import time
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
from typing import Optional, List, Dict, Any

logging.basicConfig(
    level=logging.ERROR,
    format="%(message)s",
)

for name in ["openhands.sdk", "litellm", "httpx", "httpcore"]:
    logging.getLogger(name).setLevel(logging.ERROR)

logger = logging.getLogger("github-agent")
logger.setLevel(logging.DEBUG)
logger.propagate = False
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
logger.addHandler(handler)

from openhands.sdk import LLM, Agent, Conversation, Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool


class GitHubAgent:
    """Main agent class for managing GitHub PRs and Issues"""

    def __init__(self):
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.github_username = os.getenv("GITHUB_USERNAME", "openhands-bot")
        repos = os.getenv("GITHUB_REPOSITORIES", "v0l,LNVPS").split(",")
        if self.github_username and self.github_username not in repos:
            repos.append(self.github_username)
        self.github_repos = repos
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "600"))
        self.github_api = "https://api.github.com"
        self.work_dir = os.getenv("WORK_DIR", "/tmp/openhands-work")
        self.active_repos = []
        self.base_dir = Path(__file__).parent.resolve()

        # Setup LLM
        llm = LLM(
            model=os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929"),
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            disable_vision=True,
        )

        # Create agent with tools
        self.agent = Agent(
            llm=llm,
            tools=[
                Tool(name=TerminalTool.name),
                Tool(name=FileEditorTool.name),
                Tool(name=TaskTrackerTool.name),
            ],
            system_prompt_filename=str(self.base_dir / "prompts/github_agent_system_prompt.j2"),
            system_prompt_kwargs={"llm_security_analyzer": False},
        )

        self.work_dir = os.getenv("WORK_DIR", "/tmp/openhands-work")
        self.state_dir = Path(os.getenv("STATE_DIR", "/var/lib/openhands-github-agent"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "agent_state.json"

        Path(self.work_dir).mkdir(parents=True, exist_ok=True)

        self._load_state()
        self._discover_active_repos()
        logger.info(
            f"GitHub Agent initialized. Monitoring {len(self.active_repos)} active repos"
        )

    def _discover_active_repos(self) -> None:
        """Discover active repos from configured accounts"""
        self.active_repos = []

        for account in self.github_repos:
            try:
                endpoint = f"/users/{account}/repos?type=all&sort=updated&per_page=50"
                logger.info(f"Discovering repos for {account}...")
                repos = self._api_request(endpoint)

                logger.info(f"Got {len(repos) if repos else 0} repos from API")
                if repos:
                    for repo in repos:
                        full_name = repo.get("full_name")
                        owner = repo.get("owner", {}).get("login", "")
                        logger.info(f"  Repo: {full_name}, Owner: {owner}, Match: {owner == account}")
                        if full_name and owner == account:
                            self.active_repos.append(full_name)

                org_endpoint = (
                    f"/orgs/{account}/repos?type=member&sort=updated&per_page=50"
                )
                org_repos = self._api_request(org_endpoint)

                if org_repos:
                    for repo in org_repos:
                        full_name = repo.get("full_name")
                        logger.info(f"  Org Repo: {full_name}")
                        if full_name:
                            self.active_repos.append(full_name)
            except Exception as e:
                logger.error(f"Failed to discover repos for {account}: {e}")

        self.active_repos = list(set(self.active_repos))
        logger.info(f"Discovered {len(self.active_repos)} repos")

        if len(self.active_repos) > 10:
            logger.info(f"First 10: {', '.join(self.active_repos[:10])}")

    def _load_state(self) -> None:
        """Load agent state for persistence"""
        try:
            if self.state_file.exists():
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                self.last_processed_prs = state.get("last_processed_prs", {})
                self.last_processed_issues = state.get("last_processed_issues", {})
                self.last_heartbeat = state.get("last_heartbeat")
                self.conversation_history = state.get("conversation_history", {})
                logger.info(f"Loaded state from {self.state_file}")
            else:
                self.last_processed_prs = {}
                self.last_processed_issues = {}
                self.last_heartbeat = None
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            self.last_processed_prs = {}
            self.last_processed_issues = {}
            self.last_heartbeat = None

    def _save_state(self) -> None:
        """Save agent state for persistence"""
        try:
            state = {
                "last_processed_prs": self.last_processed_prs,
                "last_processed_issues": self.last_processed_issues,
                "last_heartbeat": datetime.now().isoformat(),
            }
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _api_request(
        self, endpoint: str, method: str = "GET", data: Optional[Dict] = None, silent: bool = False
    ) -> Optional[Dict]:
        """Make authenticated GitHub API request"""
        import urllib.request
        import urllib.error

        url = f"{self.github_api}/{endpoint.lstrip("/")}"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "OpenHands-GitHub-Agent",
        }

        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            if data:
                req.data = json.dumps(data).encode("utf-8")

            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if hasattr(e, 'read') else ""
            if not silent:
                logger.error(f"API error: {e.code} - {e.reason} on {endpoint}")
                if e.code == 403:
                    logger.error(f"403 details: {error_body[:500]}")
            return None
        except Exception as e:
            if not silent:
                logger.error(f"Request failed: {e}")
            return None

    def _get_active_prs(self) -> List[Dict]:
        """Get open PRs needing attention across all active repos"""
        all_prs = []

        for repo in self.active_repos:
            endpoint = f"repos/{repo}/pulls?state=open&per_page=20"
            prs = self._api_request(endpoint)

            if prs:
                for pr in prs:
                    pr["_repo"] = repo
                    all_prs.append(pr)

        attention_prs = []
        for pr in all_prs:
            if self._needs_attention(pr):
                attention_prs.append(pr)

        return attention_prs

    def _needs_attention(self, pr: Dict) -> bool:
        """Check if a PR needs attention"""
        repo = pr.get("_repo")
        if not repo:
            return False

        author = pr.get("user", {}).get("login")
        if author != self.github_username:
            logger.debug(f"PR #{pr['number']} author {author} != {self.github_username}, skipping")
            return False

        if not self._is_contributor(repo, author):
            logger.debug(f"PR #{pr['number']} author {author} is not a contributor, skipping")
            return False

        comments_endpoint = f"repos/{repo}/pulls/{pr['number']}/reviews"
        reviews = self._api_request(comments_endpoint)

        if reviews:
            # Check latest review state
            latest_review = reviews[-1]
            if latest_review.get("state") == "APPROVED":
                logger.debug(f"PR #{pr['number']} has APPROVED review, skipping")
                return False
            if latest_review.get("state") in ["COMMENTED", "CHANGES_REQUESTED"]:
                logger.info(f"PR #{pr['number']} has {latest_review.get('state')} review")
                return True

        if pr.get("mergeable") == False:
            logger.info(f"PR #{pr['number']} has merge conflicts")
            return True

        comments_endpoint = f"repos/{repo}/issues/{pr['number']}/comments"
        comments = self._api_request(comments_endpoint)

        if comments:
            for comment in comments:
                if f"@{self.github_username}" in comment.get("body", ""):
                    logger.info(f"PR #{pr['number']} has @{self.github_username} mentioned")
                    return True

        return False

    def _get_assigned_issues(self) -> List[Dict]:
        """Get issues assigned to the bot across all repos"""
        all_issues = []

        for repo in self.active_repos:
            endpoint = f"repos/{repo}/issues?state=open&assignee={self.github_username}&per_page=20"
            issues = self._api_request(endpoint)

            if issues:
                for issue in issues:
                    if "pull_request" not in issue:
                        author = issue.get("user", {}).get("login")
                        if not self._is_contributor(repo, author):
                            logger.info(f"Assigned issue #{issue['number']} author {author} is not a contributor, skipping")
                            continue
                        issue["_repo"] = repo
                        all_issues.append(issue)

        return all_issues

    def _get_mentioned_issues(self) -> List[Dict]:
        """Get issues where the bot is mentioned - looks for issue reference in comment"""
        all_issues = []

        for repo in self.active_repos:
            endpoint = f"repos/{repo}/issues?state=open&per_page=50"
            issues = self._api_request(endpoint)

            if issues:
                for issue in issues:
                    if issue.get("assignee") or "pull_request" in issue:
                        continue

                    comments_endpoint = f"repos/{repo}/issues/{issue['number']}/comments"
                    comments = self._api_request(comments_endpoint)

                    if comments:
                        for comment in comments:
                            body = comment.get("body", "")
                            if f"@{self.github_username}" in body:
                                match = re.search(r'#(\d+)|github\.com/.+?/(?:issues|pull)/(\d+)', body)
                                if match:
                                    ref_num = int(match.group(1) or match.group(2))
                                    if ref_num != issue["number"]:
                                        ref_issue = self._api_request(f"repos/{repo}/issues/{ref_num}")
                                        if ref_issue and "pull_request" not in ref_issue:
                                            issue = ref_issue
                                issue["_repo"] = repo
                                issue["_mention_comment"] = body
                                full_issue = self._api_request(f"repos/{repo}/issues/{issue['number']}", silent=True)
                                if full_issue:
                                    issue["title"] = full_issue.get("title", issue["title"])
                                    issue["body"] = full_issue.get("body", issue.get("body", ""))
                                    issue["labels"] = full_issue.get("labels", issue.get("labels", []))
                                    issue["user"] = full_issue.get("user", issue.get("user", {}))
                                
                                author = issue.get("user", {}).get("login")
                                if not self._is_contributor(repo, author):
                                    logger.info(f"Issue #{issue['number']} author {author} is not a contributor, skipping")
                                    continue
                                
                                all_issues.append(issue)
                                break

        return all_issues

    def _is_contributor(self, repo: str, username: str) -> bool:
        """Check if user is a contributor to the repo"""
        if not username:
            return False
        
        if username == self.github_username:
            return True
        
        try:
            contributors = self._api_request(f"repos/{repo}/contributors?per_page=100")
            if contributors:
                for contributor in contributors:
                    if contributor.get("login") == username:
                        return True
            
            members = self._api_request(f"repos/{repo}/collaborators?per_page=100")
            if members:
                for member in members:
                    if member.get("login") == username:
                        return True
        except Exception:
            pass
        
        return True

    def _handle_pr(self, pr: Dict) -> bool:
        """Handle a PR that needs attention"""
        repo = pr.get("_repo")
        pr_number = pr["number"]
        logger.info(f"Handling PR #{repo}#{pr_number}: {pr['title']}")

        import uuid
        conv_id_str = f"{repo.replace('/', '_')}_pr_{pr_number}"
        conv_id = uuid.uuid5(uuid.NAMESPACE_DNS, conv_id_str)
        conv_state_dir = self.state_dir / "conversations" / conv_id_str
        conv_state_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Starting conversation for PR #{pr_number} (id: {conv_id})")

        conversation = Conversation(
            agent=self.agent,
            workspace=str(self.work_dir),
            persistence_dir=str(conv_state_dir),
            conversation_id=conv_id,
        )

        prompt = self._build_pr_prompt(pr)
        conversation.send_message(prompt)
        conversation.run()

        self._save_state()
        return True

    def _build_pr_prompt(self, pr: Dict) -> str:
        """Build prompt for PR handling"""
        repo = pr.get("_repo")
        prompt = f"""
        Work on PR #{pr["number"]}: {pr["title"]}
        Repo: {repo}
        https://github.com/{repo}/pull/{pr["number"]}

        Use `gh` CLI for GitHub operations:
        - gh api repos/{repo}/pulls/{pr["number"]}/comments | jq -r '.[] | "\(.path):\(.line) = \(.body)"' - get review comments as file:line = comment
        - gh pr checkout {pr["number"]} - checkout PR branch
        - gh repo clone {repo} - clone EXACTLY this repo: {repo}
        - git push origin <branch> - push to your fork
        - gh pr create - create PR after pushing

        This PR has CHANGES_REQUESTED review. Address all feedback and push updates.
        
        IMPORTANT: Clone ONLY the repo specified above: {repo}
        IMPORTANT: When commenting on PR, use actual line breaks not \\n characters
        """
        return prompt

    def _comment_on_pr(self, repo: str, pr_number: int, body: str) -> None:
        """Add comment to PR"""
        endpoint = f"repos/{repo}/issues/{pr_number}/comments"
        self._api_request(endpoint, "POST", {"body": body})
        logger.info(f"Commented on PR #{repo}#{pr_number}")

    def _comment_on_issue(self, repo: str, issue_number: int, body: str) -> None:
        """Add comment to issue"""
        endpoint = f"repos/{repo}/issues/{issue_number}/comments"
        self._api_request(endpoint, "POST", {"body": body})
        logger.info(f"Commented on issue #{repo}#{issue_number}")

    def _assign_issue(self, repo: str, issue_number: int) -> bool:
        """Assign issue to self"""
        endpoint = f"repos/{repo}/issues/{issue_number}"
        data = {"assignees": [self.github_username]}
        result = self._api_request(endpoint, "PATCH", data)
        if result:
            logger.info(
                f"Assigned issue #{repo}#{issue_number} to {self.github_username}"
            )
        return result is not None

    def _handle_issue(self, issue: Dict, is_assigned: bool = False) -> bool:
        """Handle an issue - fork, work, create PR"""
        repo = issue.get("_repo")
        issue_number = issue["number"]
        issue_key = f"{repo}#{issue_number}"

        # Check if PR already exists for this issue (don't duplicate work)
        # but allow resuming interrupted sessions
        existing_prs = self._api_request(f"repos/{repo}/pulls?state=open&per_page=100")
        if existing_prs:
            for pr in existing_prs:
                if f"Fix #{issue_number}:" in pr.get("title", "") or f"#{issue_number}" in pr.get("body", ""):
                    logger.info(f"PR already exists for issue {issue_key}, skipping")
                    return True

        issue_type = "Assigned" if is_assigned else "Mentioned"
        logger.info(f"Processing {issue_type} issue #{issue_key}: {issue['title']}")

        import uuid
        conv_id_str = f"{repo.replace('/', '_')}_{issue_number}"
        conv_id = uuid.uuid5(uuid.NAMESPACE_DNS, conv_id_str)
        conv_state_dir = self.state_dir / "conversations" / conv_id_str
        conv_state_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Starting conversation for issue #{issue_number} (id: {conv_id})")

        conversation = Conversation(
            agent=self.agent,
            workspace=str(self.work_dir),
            persistence_dir=str(conv_state_dir),
            conversation_id=conv_id,
        )

        if issue.get("_mention_comment"):
            prompt = f"""Issue: {issue['title']}
Description: {issue.get('body', 'No description provided')}

User request: {issue['_mention_comment']}

Please work on this issue."""
        else:
            prompt = self._build_issue_prompt(issue, is_assigned)

        conversation.send_message(prompt)
        conversation.run()

        self._save_state()
        return True

    def _build_issue_prompt(self, issue: Dict, is_assigned: bool) -> str:
        """Build prompt for issue handling"""
        repo = issue.get("_repo")
        repo_owner = repo.split('/')[0]
        prompt = f"""
        Work on issue #{issue["number"]}: {issue["title"]}
        Repo: {repo}
        https://github.com/{repo}/issues/{issue["number"]}

        Description: {issue.get("body", "No description provided")}

        Working dir: {self.work_dir}
        Clone repo to: {self.work_dir}/{repo}
        Create workspace for your changes: {self.work_dir}/{issue["number"]}_{repo.split('/')[1]}

        Use `gh` CLI for GitHub operations:
        - gh issue view {issue["number"]} - to see issue details
        - gh issue comment {issue["number"]} --body "..." - to comment
        - gh repo clone {repo} - clone this repo: {repo}
        - If you don't have write access to {repo}, fork first: gh repo fork {repo} --clone=true
        - git push origin <branch> - push to your fork or the repo if you have access
        - gh pr create --title "Fix #{issue["number"]}: {issue.get('title', 'Issue')}" --body "PR description" - to create a PR

        When creating a PR, use the following body format:
        ```
        ## Summary
        [Brief description of what was changed and why]

        ## Changes Made
        - [Change 1]: [Explanation of why this change was made]
        - [Change 2]: [Explanation of why this change was made]

        ## Testing
        [Notes on how changes were tested, if applicable]

        Closes #{issue["number"]}
        ```

        IMPORTANT: Clone ONLY this repo: {repo} - do not clone any other repo mentioned in the issue description
        IMPORTANT: The PR description MUST include "Closes #{issue["number"]}" to link the PR to the issue
        Please work on this issue and create a PR when done.
        """
        return prompt

    def _run_heartbeat(self) -> None:
        """Run one heartbeat cycle"""
        logger.info("Starting heartbeat cycle...")
        timestamp = datetime.now().isoformat()

        try:
            # 1. Handle PRs needing attention
            logger.info("Checking PRs needing attention...")
            prs = self._get_active_prs()
            logger.info(f"Found {len(prs)} PRs needing attention")

            for pr in prs:
                try:
                    self._handle_pr(pr)
                except Exception as e:
                    logger.error(f"Failed to handle PR #{pr['number']}: {e}")

            # 2. Handle assigned issues
            logger.info("Checking assigned issues...")
            assigned_issues = self._get_assigned_issues()
            logger.info(f"Found {len(assigned_issues)} assigned issues")

            for issue in assigned_issues:
                try:
                    self._handle_issue(issue, is_assigned=True)
                except Exception as e:
                    logger.error(
                        f"Failed to handle assigned issue #{issue['number']}: {e}"
                    )

            # 3. Handle issues mentioned in comments
            logger.info("Checking issues with bot mentions...")
            mentioned_issues = self._get_mentioned_issues()
            logger.info(f"Found {len(mentioned_issues)} issues with bot mentions")

            for issue in mentioned_issues:
                try:
                    repo = issue.get("_repo")
                    self._assign_issue(repo, issue["number"])
                    self._handle_issue(issue, is_assigned=True)
                except Exception as e:
                    logger.error(
                        f"Failed to handle mentioned issue #{issue['number']}: {e}"
                    )

            self._save_state()
            logger.info(f"Heartbeat cycle completed at {datetime.now().isoformat()}")

        except Exception as e:
            logger.error(f"Heartbeat cycle failed: {e}")

    def run(self) -> None:
        """Run the agent continuously with persistence"""
        logger.info(
            f"Starting OpenHands GitHub Agent (heartbeat every {self.heartbeat_interval}s)"
        )
        logger.info(f"Username: {self.github_username}")
        logger.info(f"Watching accounts: {', '.join(self.github_repos)}")
        logger.info(f"Active repos: {len(self.active_repos)}")

        self._run_heartbeat()

        while True:
            try:
                time.sleep(self.heartbeat_interval)
                self._run_heartbeat()
            except KeyboardInterrupt:
                logger.info("Agent stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(60)


def main():
    """Main entry point"""
    required_vars = ["GITHUB_TOKEN", "GITHUB_REPOSITORIES", "LLM_API_KEY"]
    missing = [var for var in required_vars if not os.getenv(var)]

    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    # Create and run agent
    agent = GitHubAgent()
    agent.run()


if __name__ == "__main__":
    main()
