import hmac
import hashlib
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    if not secret:
        return True
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.strip()
    if provided.startswith("sha256="):
        provided = provided.split("=", 1)[1]
    return hmac.compare_digest(provided, expected)


class GitHubIssue(BaseModel):
    number: int
    title: str
    body: Optional[str] = None
    html_url: str
    state: str = "open"
    user: Optional[Any] = None
    labels: list[Any] = Field(default_factory=list)
    pull_request: Optional[Any] = None


class GitHubPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    # title/html_url/state default to "" because GitHub's *events feed* embeds a slimmer
    # pull_request object inside review events than webhooks do (it omits these). Only `number`
    # is guaranteed everywhere; the review handlers need the number + review state, not these.
    title: str = ""
    body: Optional[str] = None
    html_url: str = ""
    state: str = ""
    merged: bool = False
    draft: bool = False
    user: Optional[Any] = None
    base: Optional[Any] = None
    head: Optional[Any] = None
    labels: list[Any] = Field(default_factory=list)
    assignees: list[Any] = Field(default_factory=list)
    requested_reviewers: list[Any] = Field(default_factory=list)


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


class GitHubReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user: Optional[dict] = None
    state: str = ""
    body: Optional[str] = None


class PullRequestReviewEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    review: GitHubReview
    pull_request: GitHubPullRequest
    repository: GitHubRepository


class IssueCommentIssuePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    pull_request: Optional[dict] = None


class IssueCommentEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    issue: IssueCommentIssuePayload
    comment: dict
    repository: GitHubRepository


class PullRequestReviewCommentEventPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    comment: Optional[dict] = None
    pull_request: Optional[GitHubPullRequest] = None
    repository: GitHubRepository


class PingEventPayload(BaseModel):
    hook: Optional[Any] = None
    repository: Optional[GitHubRepository] = None


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
    return None
