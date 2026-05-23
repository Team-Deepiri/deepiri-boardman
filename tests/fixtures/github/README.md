# Synthetic GitHub Webhook Fixtures

These fixtures are acceptance-only payloads used to validate webhook parsing and routing logic.

Rules:

- Use fake IDs, users, SHAs, and URLs only.
- Keep `repository.full_name` fixed to `Team-Deepiri/deepiri-boardman`.
- Do not place credentials, tokens, or production URLs in these files.

Fixture set:

- `ping.json`
- `issues_opened.json`
- `pull_request_opened.json`
- `pull_request_review_submitted.json`
- `issue_comment_created.json`
