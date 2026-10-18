#!/usr/bin/env python3
"""Trace one task through the GitHub lifecycle against a REAL Boardman and a REAL ClickUp.

Sends HMAC-signed synthetic GitHub webhooks (issue opened, PR opened, QA comment, QA approval,
PR merged) to a running Boardman, then reads the task back from the ClickUp API after each step
and checks its status. This is the live version of tests/test_clickup_lifecycle_trace.py.

It writes real data: it creates a task and comments on it. Use a throwaway ClickUp list and a
test repo mapped to that list in repos.yml (`clickup_list_id`). Read docs/CLICKUP_LIVE_CHECKLIST.md
first. Boardman itself may call the real GitHub API for the PR number you give it (comments,
review requests), so use a real PR in the test repo or expect those side effects to be reported
as non-fatal warnings in Boardman's logs.

    CLICKUP_API_TOKEN=pk_... GITHUB_WEBHOOK_SECRET=... \\
      python scripts/clickup_live_trace.py --boardman-url https://boardman.deepiri.com \\
        --repo Team-Deepiri/boardman-sandbox --issue 1 --pr 2 \\
        --dev-login <dev github login> --qa-login <qa github login>

Expected statuses default to the CLICKUP_STATUS_* values Boardman is configured with; pass the
same env vars here. A step with no configured status is reported as SKIPPED, not failed.
Exit code 0 only if every checked step matched.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx

CLICKUP = os.environ.get("CLICKUP_API_BASE", "https://api.clickup.com/api/v2").rstrip("/")


def _sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _expected() -> dict[str, str]:
    env = os.environ.get
    return {
        "issue": env("CLICKUP_STATUS_ASSIGNED", "to do"),
        "pr_opened": env("CLICKUP_STATUS_NEEDS_QA", ""),
        "qa_comment": env("CLICKUP_STATUS_IN_QA", ""),
        "qa_approve": env("CLICKUP_STATUS_APPROVED", ""),
        "merged": env("CLICKUP_STATUS_COMPLETED", "complete"),
    }


class Trace:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.http = httpx.Client(timeout=30)
        self.repo_name = a.repo.split("/", 1)[1]
        self.results: list[tuple[str, str, str, str]] = []

    # -- GitHub -> Boardman ------------------------------------------------------------------
    def deliver(self, event: str, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        r = self.http.post(
            f"{self.a.boardman_url.rstrip('/')}/api/v1/webhooks/github",
            content=raw,
            headers={
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": f"live-trace-{uuid.uuid4()}",
                "X-Hub-Signature-256": _sign(self.a.secret, raw),
                "Content-Type": "application/json",
            },
        )
        if r.status_code not in (200, 202):
            sys.exit(f"Boardman rejected the {event} webhook: {r.status_code} {r.text[:300]}")

    def repo(self) -> dict[str, str]:
        return {"full_name": self.a.repo, "name": self.repo_name}

    def pr(self, action: str, *, merged: bool = False) -> dict[str, Any]:
        n, repo = self.a.pr, self.a.repo
        return {
            "action": action,
            "pull_request": {
                "number": n,
                "title": "Boardman live trace",
                "body": f"Fixes #{self.a.issue}",
                "html_url": f"https://github.com/{repo}/pull/{n}",
                "state": "closed" if action == "closed" else "open",
                "merged": merged,
                "draft": False,
                "user": {"login": self.a.dev_login},
                "head": {"ref": "live-trace"},
                "base": {"ref": "main"},
                "labels": [],
                "commits": 1,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "repository": self.repo(),
        }

    def review(self, state: str, rid: int) -> dict[str, Any]:
        n, repo = self.a.pr, self.a.repo
        return {
            "action": "submitted",
            "review": {
                "id": rid,
                "user": {"login": self.a.qa_login},
                "state": state,
                "body": "",
                "html_url": f"https://github.com/{repo}/pull/{n}#pullrequestreview-{rid}",
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "pull_request": {
                "number": n,
                "title": "Boardman live trace",
                "body": f"Fixes #{self.a.issue}",
            },
            "repository": self.repo(),
        }

    # -- ClickUp (read-only) -----------------------------------------------------------------
    def find_task(self) -> dict[str, Any] | None:
        """The task Boardman made for the issue: look it up through Boardman's own mapping."""
        r = self.http.get(
            f"{self.a.boardman_url.rstrip('/')}/api/v1/mappings", headers=self._boardman_auth()
        )
        r.raise_for_status()
        rows = r.json() if isinstance(r.json(), list) else r.json().get("mappings", [])
        for row in rows:
            if str(row.get("github_issue_number")) == str(self.a.issue) and (
                self.repo_name == row.get("github_repo") or self.a.repo == row.get("github_repo")
            ):
                return row
        return None

    def _boardman_auth(self) -> dict[str, str]:
        tok = os.environ.get("BOARDMAN_API_TOKEN", "")
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def status_of(self, task_id: str) -> str:
        r = self.http.get(
            f"{CLICKUP}/task/{task_id}", headers={"Authorization": self.a.clickup_token}
        )
        r.raise_for_status()
        return str((r.json().get("status") or {}).get("status", "")).strip()

    def check(self, step: str, expected: str, task_id: str) -> None:
        deadline = time.time() + self.a.wait
        got = ""
        while time.time() < deadline:
            got = self.status_of(task_id)
            if not expected or got.casefold() == expected.casefold():
                break
            time.sleep(1.5)
        if not expected:
            self.results.append((step, "SKIPPED", "(no status configured)", got))
        else:
            ok = got.casefold() == expected.casefold()
            self.results.append((step, "PASS" if ok else "FAIL", expected, got))

    def run(self) -> int:
        exp = _expected()
        a = self.a
        self.deliver(
            "issues",
            {
                "action": "opened",
                "issue": {
                    "number": a.issue,
                    "title": "Boardman live trace issue",
                    "body": "created by scripts/clickup_live_trace.py",
                    "html_url": f"https://github.com/{a.repo}/issues/{a.issue}",
                    "state": "open",
                    "labels": [],
                    "assignees": [{"login": a.dev_login}],
                },
                "repository": self.repo(),
            },
        )
        time.sleep(2)
        row = self.find_task()
        if not row:
            sys.exit(
                "No ClickUp task was mapped to the issue. Check repos.yml clickup_list_id, "
                "TASK_PROVIDER=clickup and Boardman's logs."
            )
        tid = str(row.get("plaky_task_id") or row.get("task_id"))
        print(f"ClickUp task: {tid}  {row.get('plaky_task_url') or ''}")
        self.check("issue opened + assigned", exp["issue"], tid)
        self.deliver("pull_request", self.pr("opened"))
        self.check("PR opened", exp["pr_opened"], tid)
        self.deliver("pull_request_review", self.review("commented", 1))
        self.check("QA comments", exp["qa_comment"], tid)
        self.deliver("pull_request_review", self.review("approved", 2))
        self.check("QA approves", exp["qa_approve"], tid)
        self.deliver("pull_request", self.pr("closed", merged=True))
        self.check("PR merged", exp["merged"], tid)

        print(f"\n{'step':<26}{'result':<9}{'expected':<18}actual")
        for step, res, want, got in self.results:
            print(f"{step:<26}{res:<9}{want:<18}{got}")
        failed = [r for r in self.results if r[1] == "FAIL"]
        print(
            "\n" + ("ALL CHECKED STEPS PASSED" if not failed else f"{len(failed)} STEP(S) FAILED")
        )
        return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--boardman-url", required=True)
    p.add_argument("--repo", required=True, help="owner/name of a test repo mapped in repos.yml")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--dev-login", required=True)
    p.add_argument("--qa-login", required=True)
    p.add_argument("--wait", type=float, default=20.0, help="seconds to wait for each status")
    a = p.parse_args()
    a.secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    a.clickup_token = os.environ.get("CLICKUP_API_TOKEN", "")
    if not a.secret or not a.clickup_token:
        sys.exit("Set GITHUB_WEBHOOK_SECRET and CLICKUP_API_TOKEN in the environment.")
    return Trace(a).run()


if __name__ == "__main__":
    sys.exit(main())
