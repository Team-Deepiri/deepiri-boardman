import hmac
import hashlib
from typing import Optional, List, Any
from pydantic import BaseModel, Field


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


class GitHubPullRequest(BaseModel):
    number: int
    title: str
    body: Optional[str] = None
    html_url: str
    state: str
    merged: bool = False
    user: Optional[Any] = None
    base: Optional[Any] = None
    head: Optional[Any] = None


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


class PingEventPayload(BaseModel):
    hook: Optional[Any] = None
    repository: Optional[GitHubRepository] = None


def parse_webhook_payload(event_type: str, payload_dict: dict) -> Any:
    if event_type == "issues":
        return IssueEventPayload(**payload_dict)
    elif event_type == "pull_request":
        return PullRequestEventPayload(**payload_dict)
    elif event_type == "ping":
        return PingEventPayload(**payload_dict)
    return None