"""verify_signature_any accepts the org webhook secret OR the GitHub App webhook secret."""

from __future__ import annotations

import hashlib
import hmac

from boardman.github.webhooks import verify_signature_any

BODY = b'{"zen": "Keep it logically awesome."}'


def _sig(secret: str, body: bytes = BODY) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_matches_first_secret():
    assert verify_signature_any(BODY, _sig("org-secret"), ["org-secret", "app-secret"])


def test_matches_second_secret():
    assert verify_signature_any(BODY, _sig("app-secret"), ["org-secret", "app-secret"])


def test_rejects_when_no_secret_matches():
    assert not verify_signature_any(BODY, _sig("wrong"), ["org-secret", "app-secret"])


def test_empty_secrets_disables_verification():
    # Same escape hatch as a single empty GITHUB_WEBHOOK_SECRET today (local dev).
    assert verify_signature_any(BODY, "sha256=whatever", ["", ""])
    assert verify_signature_any(BODY, "sha256=whatever", [])


def test_ignores_blank_secret_in_list():
    assert verify_signature_any(BODY, _sig("app-secret"), ["", "app-secret"])
    assert not verify_signature_any(BODY, _sig("wrong"), ["", "app-secret"])
