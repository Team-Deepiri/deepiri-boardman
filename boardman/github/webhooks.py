"""Parse raw GitHub webhook payloads into typed event models."""

import hashlib
import hmac
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    if not secret:
        return True
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.strip()
    if provided.startswith("sha256="):
        provided = provided.split("=", 1)[1]
    return hmac.compare_digest(provided, expected)


def verify_signature_any(raw_body: bytes, signature_header: str, secrets: list[str]) -> bool:
    """True if the signature matches ANY configured secret.

    During the GitHub App cutover two delivery paths coexist: the org-level webhook
    (signed with GITHUB_WEBHOOK_SECRET) and the App's own webhook (signed with
    GITHUB_APP_WEBHOOK_SECRET). A delivery is authentic if it verifies against either.
    An empty secret list, or a list whose only entries are empty, disables verification
    exactly as a single empty secret does today.
    """
    candidates = [s for s in secrets if s]
    if not candidates:
        return True
    return any(verify_signature(raw_body, signature_header, s) for s in candidates)


class GitHubIssue(BaseModel):
    number: int
    title: str
    body: str | None = None
    html_url: str
    state: str = "open"
    user: Any | None = None
    labels: list[Any] = Field(default_factory=list)
    pull_request: Any | None = None
    # GitHub's native issue Type (org feature): {"name": "Feature", ...} or None.
    # PRs never have one — for them, labels are the only typing signal.
    type: Any | None = None
    priority: Any | None = None
    # Sidebar issue fields (org feature): rows like {"issue_field_name": "Priority",
    # "single_select_option": {"name": "High"}}. Undeclared keys are DROPPED by
    # pydantic, which silently ate the team's actual priority signal.
    issue_field_values: list[Any] = Field(default_factory=list)
    assignee: Any | None = None
    assignees: list[Any] = Field(default_factory=list)


class GitHubPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    # title/html_url/state default to "" because GitHub's *events feed* embeds a slimmer
    # pull_request object inside review events than webhooks do (it omits these). Only `number`
    # is guaranteed everywhere; the review handlers need the number + review state, not these.
    title: str = ""
    body: str | None = None
    html_url: str = ""
    state: str = ""
    merged: bool = False
    # GitHub stamps these once and leaves them stamped, which makes them the one part of
    # the payload that does not depend on delivery order: `state` is a snapshot taken when
    # the delivery was built, so a redelivery or a queued retry from before the close still
    # says "open". Anything that would revive a closed PR's links checks these instead.
    closed_at: str | None = None
    merged_at: str | None = None
    # When this snapshot was built. The one field that orders two deliveries against each
    # other, which is what tells a genuine reopen from a retry of an event that predates
    # the close -- both of those say state "open" with closed_at null.
    updated_at: str | None = None
    draft: bool = False
    user: Any | None = None
    base: Any | None = None
    head: Any | None = None
    labels: list[Any] = Field(default_factory=list)
    assignees: list[Any] = Field(default_factory=list)
    requested_reviewers: list[Any] = Field(default_factory=list)
    # Cumulative commit count on the PR at the time of this delivery. GitHub stamps this
    # on every pull_request.* payload; used to count commits pushed SINCE the last QA
    # review verdict (see PullRequestTaskLink.commits_at_last_review) without needing a
    # separate commit-listing API call.
    commits: int = 0


class GitHubRepository(BaseModel):
    full_name: str
    name: str


class IssueEventPayload(BaseModel):
    action: str
    issue: GitHubIssue
    repository: GitHubRepository


class PullRequestEventPayload(BaseModel):
    action: str
    pull_request: GitHubPullRequest
    repository: GitHubRepository
    label: dict | None = None


class GitHubReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user: dict | None = None
    state: str = ""
    body: str | None = None
    id: int | str | None = None
    node_id: str | None = None
    html_url: str | None = None
    submitted_at: str | None = None
    updated_at: str | None = None


class PullRequestReviewEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    review: GitHubReview
    pull_request: GitHubPullRequest
    repository: GitHubRepository


class IssueCommentIssuePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    pull_request: dict | None = None


class IssueCommentEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    issue: IssueCommentIssuePayload
    comment: dict
    repository: GitHubRepository
    # What the `edited` event actually changed. GitHub includes `body` here only when the
    # TEXT changed, so its absence is how a no-op edit -- saving without a change, or an
    # edit to something other than the text -- is told from a real correction. Without it
    # every `edited` delivery looks like new wording, because updated_at moved.
    changes: dict | None = None


class PullRequestReviewCommentEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    comment: dict | None = None
    pull_request: GitHubPullRequest | None = None
    repository: GitHubRepository
    # See IssueCommentEventPayload.changes.
    changes: dict | None = None


class PingEventPayload(BaseModel):
    hook: Any | None = None
    repository: GitHubRepository | None = None


class GitHubDeployment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str = ""
    ref: str = ""
    environment: str = ""


class GitHubDeploymentStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: str = ""  # success | failure | error | pending | in_progress | queued


class DeploymentStatusEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = ""
    deployment: GitHubDeployment
    deployment_status: GitHubDeploymentStatus
    repository: GitHubRepository


def parse_webhook_payload(event_type: str, payload_dict: dict) -> Any:
    if event_type == "issues":
        return IssueEventPayload(**payload_dict)
    if event_type == "pull_request":
        return PullRequestEventPayload(**payload_dict)
    if event_type == "pull_request_review":
        return PullRequestReviewEventPayload(**payload_dict)
    if event_type == "pull_request_review_comment":
        return PullRequestReviewCommentEventPayload(**payload_dict)
    if event_type == "issue_comment":
        return IssueCommentEventPayload(**payload_dict)
    if event_type == "ping":
        return PingEventPayload(**payload_dict)
    if event_type == "deployment_status":
        return DeploymentStatusEventPayload(**payload_dict)
    return None
