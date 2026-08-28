"""A 429 from OpenAI means two unrelated things: "slow down" (transient, re-send works)
or "you have no credits" (billing; re-sending is useless). Boardman used to answer both
with "the quota resets within a minute; just re-send" - confidently wrong advice for a
drained account, caught live when every prompt failed with credit_balance_exhausted.
"""

from __future__ import annotations

import httpx

from boardman.agent.service import _classify_llm_error, _provider_hint

# The exact body from the live failure, verbatim.
_NO_CREDITS = (
    "Error code: 429 - {'error': {'message': 'You have no credits remaining. Add "
    "credits to continue using the API at "
    "https://platform.openai.com/settings/organization/billing/.', 'type': "
    "'insufficient_quota', 'param': None, 'code': 'credit_balance_exhausted'}}"
)


class FakeSDK429(Exception):
    """Provider SDK errors carry status_code but are not httpx errors."""

    status_code = 429


def test_sdk_429_no_credits_is_billing_not_rate_limit() -> None:
    cat = _classify_llm_error(FakeSDK429(_NO_CREDITS), provider="openai")
    assert cat == "insufficient_quota"


def test_sdk_429_tpm_is_still_a_rate_limit() -> None:
    exc = FakeSDK429(
        "Error code: 429 - Rate limit reached for gpt-5.1: Limit 30000, "
        "Requested 142900 tokens per min. Please try again in 226ms."
    )
    assert _classify_llm_error(exc, provider="openai") == "rate_limited"


def _httpx_error(status: int, body: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    resp = httpx.Response(status, request=req, text=body)
    return httpx.HTTPStatusError(str(status), request=req, response=resp)


def test_httpx_429_with_exhausted_body() -> None:
    exc = _httpx_error(429, _NO_CREDITS)
    assert _classify_llm_error(exc, provider="openai") == "insufficient_quota"


def test_httpx_429_tpm_stays_rate_limited() -> None:
    exc = _httpx_error(429, "Rate limit reached for gpt-5.1: Limit 30000 tokens per min.")
    assert _classify_llm_error(exc, provider="openai") == "rate_limited"


def test_httpx_400_anthropic_low_balance_is_billing() -> None:
    exc = _httpx_error(
        400,
        "{'type': 'invalid_request_error', 'message': 'Your credit balance is too low "
        "to access the Anthropic API.'}",
    )
    assert _classify_llm_error(exc, provider="anthropic") == "insufficient_quota"


def test_httpx_plain_400_stays_bad_request() -> None:
    exc = _httpx_error(400, "{'error': {'message': 'max_tokens must be a positive integer'}}")
    assert _classify_llm_error(exc, provider="openai") == "bad_request"


def test_gemini_transient_429_is_not_a_billing_problem() -> None:
    """Gemini reuses OpenAI's billing sentence for TRANSIENT free-tier per-minute
    throttles (RetryInfo says retry in seconds). Telling that user they are out of
    money would be the same confidently-wrong-advice bug in the other direction."""
    exc = _httpx_error(
        429,
        "{'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'message': 'You "
        "exceeded your current quota, please check your plan and billing details.', "
        "'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
        "'retryDelay': '7s'}]}}",
    )
    assert _classify_llm_error(exc, provider="gemini") == "rate_limited"


def test_openrouter_402_is_billing() -> None:
    class FakeSDK402(Exception):
        status_code = 402

    exc = FakeSDK402("Insufficient credits. Add more using https://openrouter.ai/settings/credits")
    assert _classify_llm_error(exc, provider="openrouter") == "insufficient_quota"
    assert _classify_llm_error(
        _httpx_error(402, "Insufficient credits"), provider="openrouter"
    ) == ("insufficient_quota")
    hint = _provider_hint("openrouter", "insufficient_quota", "some/model")
    assert "openrouter.ai/settings/credits" in hint


def test_openai_billing_hard_limit_400_is_billing() -> None:
    class FakeSDK400(Exception):
        status_code = 400

    exc = FakeSDK400(
        "Error code: 400 - {'error': {'code': 'billing_hard_limit_reached', "
        "'message': 'Billing hard limit has been reached'}}"
    )
    assert _classify_llm_error(exc, provider="openai") == "insufficient_quota"


def test_string_fallback_catches_quota_wording() -> None:
    exc = Exception("You exceeded your current quota, please check your plan and billing details.")
    assert _classify_llm_error(exc, provider="openai") == "insufficient_quota"
    # The identical sentence from Gemini means a throttle, not billing.
    assert _classify_llm_error(exc, provider="gemini") != "insufficient_quota"


def test_anthropic_400_low_balance_is_billing_not_bad_request() -> None:
    class FakeSDK400(Exception):
        status_code = 400

    exc = FakeSDK400(
        "Error code: 400 - {'type': 'invalid_request_error', 'message': 'Your credit "
        "balance is too low to access the Anthropic API.'}"
    )
    assert _classify_llm_error(exc, provider="anthropic") == "insufficient_quota"


def test_hint_says_billing_and_never_just_resend() -> None:
    hint = _provider_hint("openai", "insufficient_quota", "gpt-5.1")
    assert "billing" in hint.lower()
    assert "platform.openai.com" in hint
    assert "re-sending will not help" in hint


def test_rate_limit_hint_unchanged() -> None:
    hint = _provider_hint("openai", "rate_limited", "gpt-5.1")
    assert "re-send" in hint
