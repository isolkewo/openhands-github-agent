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
logger.setLevel(logging.INFO)
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
        self.github_repos = os.getenv("GITHUB_REPOSITORIES", "v0l,LNVPS").split(",")
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
        self, endpoint: str, method: str = "GET", data: Optional[Dict] = None
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
            logger.error(f"API error: {e.code} - {e.reason}")
            return None
        except Exception as e:
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

        if pr.get("user", {}).get("login") != self.github_username:
            return False

        comments_endpoint = f"repos/{repo}/pulls/{pr['number']}/reviews"
        reviews = self._api_request(comments_endpoint)

        if reviews:
            for review in reviews:
                if review.get("state") in ["COMMENTED", "CHANGES_REQUESTED"]:
                    return True

        if pr.get("mergeable") == False:
            return True

        comments_endpoint = f"repos/{repo}/issues/{pr['number']}/comments"
        comments = self._api_request(comments_endpoint)

        if comments:
            for comment in comments:
                if f"@{self.github_username}" in comment.get("body", ""):
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
                                full_issue = self._api_request(f"repos/{repo}/issues/{issue['number']}")
                                if full_issue:
                                    issue["title"] = full_issue.get("title", issue["title"])
                                    issue["body"] = full_issue.get("body", issue.get("body", ""))
                                    issue["labels"] = full_issue.get("labels", issue.get("labels", []))
                                    issue["user"] = full_issue.get("user", issue.get("user", {}))
                                all_issues.append(issue)
                                break

        return all_issues

    def _handle_pr(self, pr: Dict) -> bool:
        """Handle a PR that needs attention"""
        repo = pr.get("_repo")
        pr_number = pr["number"]
        logger.info(f"Handling PR #{repo}#{pr_number}: {pr['title']}")

        work_dir = Path(self.work_dir) / repo / f"pr-{pr_number}"
        subprocess.run(["rm", "-rf", str(work_dir)], check=False)
        work_dir.mkdir(parents=True, exist_ok=True)

        repo_url = f"https://x-access-token:{self.github_token}@github.com/{repo}.git"
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(work_dir)], check=True
        )

        subprocess.run(["git", "checkout", pr["head"]["ref"]], cwd=work_dir, check=True)

        conv_id = f"{repo.replace('/', '_')}_pr_{pr_number}"
        conv_state_dir = self.state_dir / "conversations" / conv_id
        conv_state_dir.mkdir(parents=True, exist_ok=True)

        conversation = Conversation(
            agent=self.agent,
            workspace=str(work_dir),
            persistence_dir=str(conv_state_dir),
        )

        prompt = self._build_pr_prompt(pr)

        # Send message to agent
        conversation.send_message(prompt)
        conversation.run()

        # Check if changes were made
        result = subprocess.run(
            ["git", "diff", "--quiet"], cwd=work_dir, capture_output=True
        )

        if result.returncode != 0:
            # Commit and push changes
            subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
            subprocess.run(
                ["git", "config", "user.name", self.github_username],
                cwd=work_dir,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "config",
                    "user.email",
                    f"{self.github_username}@users.noreply.github.com",
                ],
                cwd=work_dir,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Address PR #{pr['number']} feedback"],
                cwd=work_dir,
                check=True,
            )
            subprocess.run(["git", "push"], cwd=work_dir, check=True)

            repo = pr.get("_repo")
            self._comment_on_pr(
                repo,
                pr["number"],
                "I've addressed the feedback. Please review again when convenient.",
            )

        return True

    def _build_pr_prompt(self, pr: Dict) -> str:
        """Build prompt for PR handling"""
        repo = pr.get("_repo")
        prompt = f"""
        You are working on PR #{repo}#{pr["number"]}: {pr["title"]}

        PR Details:
        - Repository: {repo}
        - Branch: {pr["head"]["ref"]}
        - Base: {pr["base"]["ref"]}
        - Author: {pr["user"]["login"]}

        Check the following and address accordingly:
        1. Review comments that need responses
        2. Merge conflicts that need resolving (rebase if necessary)
        3. Any CI/CD failures that need fixing
        4. Code quality issues mentioned in reviews

        Please:
        - Address all feedback professionally
        - Make necessary code changes
        - Update documentation if needed
        - Push your changes to the PR branch
        - Comment on the PR explaining what you've done

        Start by reviewing the PR and determining what needs to be done.
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

        if issue_key in self.last_processed_issues:
            logger.info(f"Issue {issue_key} already processed, skipping")
            return True

        issue_type = "Assigned" if is_assigned else "Mentioned"
        logger.info(f"Processing {issue_type} issue #{issue_key}: {issue['title']}")

        comment = f"I'm working on this issue now."
        self._comment_on_issue(repo, issue_number, comment)

        work_dir = Path(self.work_dir) / repo / f"issue-{issue_number}"
        subprocess.run(["rm", "-rf", str(work_dir)], check=False)
        work_dir.mkdir(parents=True, exist_ok=True)

        fork_repo = f"{self.github_username}/{repo.split('/')[1]}"
        fork_endpoint = f"repos/{repo}/forks"
        fork_result = self._api_request(fork_endpoint, "POST")

        if fork_result and "full_name" in fork_result:
            fork_repo = fork_result["full_name"]
            logger.info(f"Created fork: {fork_repo}")
        else:
            fork_repo = self._api_request(f"repos/{fork_repo}")
            if fork_repo:
                logger.info(f"Using existing fork: {fork_repo}")
            else:
                logger.error(f"Could not create or find fork for {repo}")
                return False

        repo_url = f"https://x-access-token:{self.github_token}@github.com/{fork_repo}.git"
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(work_dir)], check=True
        )

        branch_name = f"openhands/issue-{issue_number}-{issue['title'].lower()[:30].replace(' ', '-')}"

        default_branch_result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=work_dir, capture_output=True, text=True
        )
        if default_branch_result.returncode == 0:
            default_branch = default_branch_result.stdout.strip().split("/")[-1]
        else:
            default_branch = "main"
        subprocess.run(["git", "checkout", default_branch], cwd=work_dir, check=True)
        subprocess.run(["git", "checkout", "-b", branch_name], cwd=work_dir, check=True)

        conv_id = f"{repo.replace('/', '_')}_{issue_number}"
        conv_state_dir = self.state_dir / "conversations" / conv_id
        conv_state_dir.mkdir(parents=True, exist_ok=True)

        conversation = Conversation(
            agent=self.agent,
            workspace=str(work_dir),
            persistence_dir=str(conv_state_dir),
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

        result = subprocess.run(
            ["git", "diff", "--quiet"], cwd=work_dir, capture_output=True
        )

        if result.returncode != 0:
            subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
            subprocess.run(
                ["git", "config", "user.name", self.github_username],
                cwd=work_dir,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "config",
                    "user.email",
                    f"{self.github_username}@users.noreply.github.com",
                ],
                cwd=work_dir,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Fix #{issue_number}: {issue['title']}"],
                cwd=work_dir,
                check=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", branch_name], cwd=work_dir, check=True
            )

            pr_body = f"""## Automated Implementation

I've started working on this issue and created a PR with the implementation.

### What was done:
- Analyzed the issue requirements
- Implemented the necessary changes
- Added tests where appropriate

Please review and let me know if any adjustments are needed.

---
*Automatically created by OpenHands GitHub Agent*
"""
            pr_endpoint = f"repos/{repo}/pulls"
            pr_data = {
                "title": f"Fix #{issue_number}: {issue['title']}",
                "body": pr_body,
                "head": f"{self.github_username}:{branch_name}",
                "base": default_branch,
            }
            self._api_request(pr_endpoint, "POST", pr_data)

            comment = f"I've completed the work on this issue and created a PR."
            self._comment_on_issue(repo, issue_number, comment)

        self.last_processed_issues[issue_key] = datetime.now().isoformat()

        return True

    def _build_issue_prompt(self, issue: Dict, is_assigned: bool) -> str:
        """Build prompt for issue handling"""
        repo = issue.get("_repo")
        prompt = f"""
        You are working on issue #{repo}#{issue["number"]}: {issue["title"]}

        Issue Details:
        - Repository: {repo}
        - Labels: {", ".join([l["name"] for l in issue.get("labels", [])])}
        - Author: {issue["user"]["login"]}

        Description:
        {issue.get("body", "No description provided")}

        Your Tasks:
        1. Analyze the issue and determine what needs to be done
        2. Implement the necessary changes
        3. Write tests if applicable
        4. Commit your changes
        5. Create a pull request
        6. Comment on the issue to let others know you're working on it

        Make sure to:
        - Follow the repository's coding standards
        - Write clean, maintainable code
        - Add appropriate error handling
        - Update documentation if needed
        """

        if is_assigned:
            prompt += """
        Since this issue is assigned to you, you should:
        - Start working on it immediately
        - Comment on the issue stating you're working on it
        - Keep the issue updated as you progress
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
