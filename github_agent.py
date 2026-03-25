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
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.tools.apply_patch import ApplyPatchTool
from openhands.tools.delegate import DelegateTool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.glob import GlobTool
from openhands.tools.grep import GrepTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool


class GitHubAgent:
    """Main agent class for managing GitHub PRs and Issues"""

    def __init__(self):
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.github_username = os.getenv("GITHUB_USERNAME", "openhands-bot")
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "600"))
        self.github_api = "https://api.github.com"
        self.work_dir = os.getenv("WORK_DIR", "/tmp/openhands-work")
        self.base_dir = Path(__file__).parent.resolve()
        
        # Allowed mentioners - only respond to mentions from these users
        allowed = os.getenv("ALLOWED_MENTIONERS", "")
        self.allowed_mentioners = [u.strip() for u in allowed.split(",") if u.strip()] if allowed else None

        # Setup LLM
        llm = LLM(
            model=os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929"),
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
            disable_vision=True,
        )

        # Create agent with tools and conversation condenser
        self.agent = Agent(
            llm=llm,
            tools=[
                Tool(name=TerminalTool.name),
                Tool(name=FileEditorTool.name),
                Tool(name=TaskTrackerTool.name),
                Tool(name=GlobTool.name),
                Tool(name=GrepTool.name),
                Tool(name=ApplyPatchTool.name),
                Tool(name=DelegateTool.name),
            ],
            system_prompt_filename=str(self.base_dir / "prompts/github_agent_system_prompt.j2"),
            system_prompt_kwargs={"llm_security_analyzer": False},
            condenser=LLMSummarizingCondenser(
                llm=llm,
                max_size=100,
                keep_first=10,
            ),
        )

        self.work_dir = os.getenv("WORK_DIR", "/tmp/openhands-work")
        self.state_dir = Path(os.getenv("STATE_DIR", "/var/lib/openhands-github-agent"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "agent_state.json"

        Path(self.work_dir).mkdir(parents=True, exist_ok=True)

        self._load_state()
        logger.info("GitHub Agent initialized. Using notifications API for all repos")

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

        # Handle full URLs from notifications directly
        if endpoint.startswith("https://"):
            url = endpoint
        else:
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

    def _get_assigned_issues(self) -> List[Dict]:
        """Get issues assigned to the bot across all repos using /user/issues endpoint."""
        all_issues = []
        
        # Use the /user/issues endpoint - finds issues assigned to auth user across all repos
        # This is much faster than iterating through each repo
        endpoint = f"user/issues?assignee={self.github_username}&state=open&per_page=100"
        issues = self._api_request(endpoint)
        
        if issues:
            for issue in issues:
                if "pull_request" not in issue:
                    # Extract repo from repository_url
                    repo_url = issue.get("repository_url", "")
                    parts = repo_url.rstrip("/").split("/")
                    if len(parts) >= 2:
                        repo = f"{parts[-2]}/{parts[-1]}"
                        issue["_repo"] = repo
                        all_issues.append(issue)

        return all_issues

    def _mark_notification_read(self, thread_url: str) -> None:
        """Mark a notification thread as read."""
        try:
            # Extract thread ID from URL and use proper endpoint format
            # URL: https://api.github.com/notifications/threads/{id}
            parts = thread_url.rstrip('/').split('/')
            if len(parts) >= 2:
                thread_id = parts[-1]
                endpoint = f"notifications/threads/{thread_id}"
                self._api_request(endpoint, "PATCH", {"last_read_at": datetime.now().isoformat()})
                logger.debug(f"Marked notification as read: {thread_id}")
        except Exception as e:
            logger.debug(f"Failed to mark notification as read: {e}")

    def _get_mentioned_issues(self) -> List[Dict]:
        """Get issues where the bot is mentioned via GitHub notifications API.
        
        Fetches all notifications across all repos, filters for mentions from
        contributors in watched repos, and returns the referenced issues.
        """
        all_issues = []

        # Fetch all notifications for the authenticated user
        notifications = self._api_request("notifications?participating=false&per_page=100")
        
        if not notifications:
            logger.info("No notifications found")
            return all_issues

        logger.info(f"Total notifications: {len(notifications)}")

        for notification in notifications:
            reason = notification.get("reason", "")
            # Process mention, review_requested, and author notifications
            if reason not in ["mention", "review_requested", "author"]:
                logger.debug(f"Skipping notification (reason={reason}): {notification.get('subject',{}).get('title')}")
                continue

            logger.info(f"Processing notification: {notification.get('subject',{}).get('title')} (reason={reason})")

            subject = notification.get("subject", {})
            subject_type = subject.get("type")
            reason = notification.get("reason", "")
            thread_url = notification.get("url")
            logger.debug(f"  Subject type: {subject_type}, Reason: {reason}")
            # Only process Issue and PullRequest notifications
            if subject_type not in ["Issue", "PullRequest"]:
                if thread_url:
                    self._mark_notification_read(thread_url)
                continue

            # Get the issue/PR URL directly from notification subject
            issue_url = subject.get("url")
            logger.debug(f"  Issue/PR URL: {issue_url}")
            if not issue_url:
                logger.debug(f"  No URL in notification")
                if thread_url:
                    self._mark_notification_read(thread_url)
                continue

            # Extract repo and number from URL
            # URL format: https://api.github.com/repos/{owner}/{repo}/issues/{number}
            # PRs use the same /issues/ endpoint
            try:
                parts = issue_url.rstrip('/').split('/')
                logger.debug(f"  URL parts: {parts}")
                if len(parts) >= 7 and parts[-2] in ['issues', 'pulls']:
                    owner = parts[-4]
                    repo_name = parts[-3]
                    number = int(parts[-1])
                    full_repo = f"{owner}/{repo_name}"
                    logger.info(f"  Parsed: {full_repo}#{number} (reason={reason})")
                else:
                    logger.info(f"  Invalid URL format: {issue_url}")
                    continue
            except (ValueError, IndexError) as e:
                logger.debug(f"  URL parse error: {e}")
                continue

            # Fetch the issue/PR to get the comments and check who mentioned us
            issue_details = self._api_request(issue_url)
            
            if not issue_details:
                logger.debug(f"  Failed to fetch issue details")
                continue

            logger.debug(f"  Issue/PR: {full_repo}#{number}")
            is_pr = parts[-2] == 'pulls'

            # For review_requested/author notifications, fetch the actual review
            if reason in ["review_requested", "author"]:
                logger.info(f"Processing {reason} notification for PR #{number}")
                logger.debug(f"  is_pr detected: {parts[-2] == 'pulls'}")
                is_pr = True  # review_requested and author are always PRs
                
                # Fetch reviews to get the actual review body
                reviews_endpoint = f"repos/{full_repo}/pulls/{number}/reviews?per_page=100"
                reviews = self._api_request(reviews_endpoint)
                
                comment_author = None
                mention_comment_body = ""
                
                if reviews:
                    sorted_reviews = sorted(
                        [r for r in reviews if r.get("submitted_at")],
                        key=lambda x: x.get("submitted_at", ""),
                        reverse=True
                    )
                    for review in sorted_reviews:
                        reviewer = review.get("user", {}).get("login")
                        body = review.get("body") or f"Review by {reviewer}"
                        comment_author = reviewer
                        mention_comment_body = body
                        logger.info(f"Found review from {comment_author}")
                        break
                
                if not comment_author:
                    comment_author = issue_details.get("user", {}).get("login", "user")
                    mention_comment_body = f"Review requested on this PR by {comment_author}"
                
                logger.info(f"  PR author: {comment_author}, will process")
            else:
                # For mentions, fetch comments to find who mentioned us
                comments_endpoint = f"repos/{full_repo}/issues/{number}/comments"
                comments = self._api_request(comments_endpoint)
                
                logger.debug(f"  Got {len(comments) if comments else 0} comments")
                
                # Find the most recent comment that mentions us
                comment_author = None
                mention_comment_body = ""
                
                if comments:
                    # Sort comments by creation date (newest first) and find the first mention
                    sorted_comments = sorted(
                        [c for c in comments if c.get("created_at")],
                        key=lambda x: x.get("created_at", ""),
                        reverse=True
                    )
                    for comment in sorted_comments:
                        body = comment.get("body", "")
                        if f"@{self.github_username}" in body:
                            comment_author = comment.get("user", {}).get("login")
                            mention_comment_body = body
                            logger.info(f"  Found most recent mention from {comment_author}")
                            break

                if not comment_author:
                    logger.debug(f"  Could not find comment with @{self.github_username}")
                    continue

                logger.info(f"Comment author: {comment_author} on {full_repo}")
                
                # For PRs, only respond if commenter is the repo owner or has done a review
                # For issues, check whitelist and contributors
                if is_pr:
                    # Check if commenter is the repo owner
                    repo_owner = issue_details.get("user", {}).get("login")
                    has_review = self._has_reviewed_pr(full_repo, number, comment_author)
                    
                    if comment_author != repo_owner and not has_review:
                        logger.info(f"Mention from {comment_author} on PR {full_repo}#{number} - not owner or reviewer, marking as read")
                        self._mark_notification_read(thread_url)
                        continue
                else:
                    # For issues, check whitelist first
                    if self.allowed_mentioners and comment_author not in self.allowed_mentioners:
                        logger.info(f"Mention from {comment_author} on issue - not in allowed list, marking as read")
                        self._mark_notification_read(thread_url)
                        continue
                    
                    # Then check if contributor
                    if not self._is_contributor(full_repo, comment_author):
                        logger.info(f"Mention from {comment_author} on {full_repo} - not a contributor, marking as read")
                        self._mark_notification_read(thread_url)
                        continue

            logger.info(f"Found mention from {comment_author} on {full_repo}#{number}")

            # Skip if already assigned (for issues) - but mark as read
            if not is_pr and issue_details.get("assignee"):
                logger.info(f"Issue #{number} already assigned, marking notification as read")
                self._mark_notification_read(thread_url)
                continue

            issue_details["_repo"] = full_repo
            issue_details["_mention_comment"] = mention_comment_body
            issue_details["_is_pr"] = is_pr
            issue_details["number"] = number
            issue_details["_thread_url"] = thread_url
            issue_details["_comment_author"] = comment_author

            all_issues.append(issue_details)

        return all_issues

    def _has_reviewed_pr(self, repo: str, pr_number: int, username: str) -> bool:
        """Check if user has submitted a review on the PR"""
        if not username:
            return False
        
        if username == self.github_username:
            return True
        
        try:
            reviews = self._api_request(f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100", silent=True)
            if reviews:
                for review in reviews:
                    if review.get("user", {}).get("login") == username:
                        return True
        except Exception:
            pass
        
        return False

    def _is_contributor(self, repo: str, username: str) -> bool:
        """Check if user is a contributor to the repo"""
        if not username:
            return False
        
        if username == self.github_username:
            return True
        
        # For repos we don't have access to, trust that commenters are allowed
        # We can't check contributors/collaborators on external repos
        try:
            contributors = self._api_request(f"repos/{repo}/contributors?per_page=100", silent=True)
            if contributors:
                for contributor in contributors:
                    if contributor.get("login") == username:
                        return True
        except Exception:
            pass
        
        # If we can't verify contributors, trust the mentioner
        # This allows mentions from external repos where we lack access
        return True

    def _handle_pr_from_notification(self, pr: Dict) -> bool:
        """Handle a PR mentioned in a notification."""
        repo = pr.get("_repo")
        pr_number = pr["number"]
        logger.info(f"Handling PR #{repo}#{pr_number} from notification: {pr['title']}")

        import uuid
        conv_id_str = f"{repo.replace('/', '_')}_pr_{pr_number}"
        conv_id = uuid.uuid5(uuid.NAMESPACE_DNS, conv_id_str)
        conv_state_dir = self.state_dir / "conversations" / conv_id_str
        conv_state_dir.mkdir(parents=True, exist_ok=True)

        conversation = Conversation(
            agent=self.agent,
            workspace=str(self.work_dir),
            persistence_dir=str(conv_state_dir),
            conversation_id=conv_id,
        )

        comment_author = pr.get("_comment_author", pr.get("user", {}).get("login", "user"))
        mention_comment = pr.get("_mention_comment", "")
        is_review_request = "Review requested" in mention_comment
        
        if is_review_request:
            prompt = f"""You have been requested to review this PR.

PR: #{pr_number} - {pr['title']}
Repo: {repo}
https://github.com/{repo}/pull/{pr_number}

Requested by: @{comment_author}

Your task:
1. Review the PR code changes
2. Check if the changes are correct and complete
3. Add review comments if you find issues
4. Approve or request changes based on your review

Use `gh` CLI to checkout the PR: `gh pr checkout {pr_number}`"""
        else:
            prompt = f"""A user tagged you in a comment on this PR. Respond to their request.

PR: #{pr_number} - {pr['title']}
Repo: {repo}
https://github.com/{repo}/pull/{pr_number}

Comment from @{comment_author}:
{mention_comment}

Your task:
1. Understand the user's request in the comment
2. Review the PR and address any feedback
3. Push updates if needed, or comment back with your findings
4. If you need more information, ask the user

Use `gh` CLI to checkout the PR: `gh pr checkout {pr_number}`"""

        conversation.send_message(prompt)
        conversation.run()
        self._save_state()
        return True

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
            comment_author = issue.get("_comment_author", issue.get("user", {}).get("login", "user"))
            prompt = f"""A user tagged you in a comment on this issue. Respond to their request.

Issue: #{issue['number']} - {issue['title']}
Repo: {repo}
https://github.com/{repo}/issues/{issue['number']}

Comment from @{comment_author}:
{issue['_mention_comment']}

Your task:
1. Understand the user's request in the comment
2. Respond appropriately - comment back, ask clarifying questions, or help if possible
3. Only create a PR if the user explicitly asks you to make changes

Respond with a comment on the issue addressing their request. Use `gh issue comment {issue['number']} --body "..."` to respond."""
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
            # 1. Handle assigned issues
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
                    is_pr = "pull_request" in issue or issue.get("_is_pr", False)
                    if is_pr:
                        self._handle_pr_from_notification(issue)
                    else:
                        self._assign_issue(repo, issue["number"])
                        self._handle_issue(issue, is_assigned=True)
                    
                    # Mark notification as read only after successful processing
                    thread_url = issue.get("_thread_url")
                    if thread_url:
                        self._mark_notification_read(thread_url)
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
        logger.info("Using GitHub notifications API for all repos")

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
    required_vars = ["GITHUB_TOKEN", "LLM_API_KEY"]
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    # Create and run agent
    agent = GitHubAgent()
    agent.run()


if __name__ == "__main__":
    main()
