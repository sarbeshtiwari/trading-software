"""Read-only diagnostics never expose credentials or invoke order endpoints."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.brokers.groww.client import GrowwClient
from app.brokers.groww.errors import GrowwAuthError
from app.config import get_settings
from scripts.verify_groww_readonly import verify


@pytest.mark.parametrize(
    "value,expected", [(25000, "VERIFIED"), (None, "UNAVAILABLE"), ("NaN", "UNAVAILABLE")]
)
async def test_only_authentication_and_read_only_ltp_are_used(value, expected):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/live-data/ltp"
        return httpx.Response(200, json={"status": "SUCCESS", "payload": {"NSE_NIFTY": value}})

    client = GrowwClient(
        settings=get_settings(),
        transport=httpx.MockTransport(handle),
        authenticator=AsyncMock(get_token=AsyncMock(return_value="isolated-test-token")),
    )
    try:
        report = await verify(get_settings(), client=client)
        assert report["authentication"] == "VERIFIED"
        assert report["market_data"] == expected
        assert report["order_requests"] == 0 and len(requests) == 1
        assert report["live_execution"] == "UNVERIFIED"
        assert report["freshness_for_execution"] == "NOT_VERIFIED"
        assert "isolated-test-token" not in str(report)
    finally:
        await client.aclose()


async def test_authorization_failure_is_distinguished_from_token_minting():
    client = AsyncMock()
    client.authenticator.get_token.return_value = "isolated-private-token"
    client.get.side_effect = GrowwAuthError(
        "secret-response-must-not-be-displayed", broker_code="GA005", http_status=403
    )
    report = await verify(get_settings(), client=client)
    assert report["authentication"] == "VERIFIED" and report["market_data"] == "FAILED"
    assert report["broker_code"] == "GA005" and report["http_status"] == 403
    assert "secret-response" not in str(report) and "isolated-private" not in str(report)
    settings = get_settings().model_copy(update={"groww_base_url": "https://untrusted.invalid"})
    client.reset_mock()
    assert (await verify(settings, client=client))["error_type"] == "NONSTANDARD_HOST_REFUSED"
    client.authenticator.get_token.assert_not_awaited()


@pytest.mark.parametrize(
    "body,content_type,expected",
    [
        ("", "text/plain", "EMPTY"),
        ("<html>private-reflected-secret</html>", "text/html", "HTML"),
        ("private-reflected-secret", "text/plain", "NON_JSON"),
    ],
)
async def test_non_json_diagnostics_never_retain_response_body(body, content_type, expected):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(403, text=body, headers={"content-type": content_type})

    client = GrowwClient(
        settings=get_settings(),
        transport=httpx.MockTransport(handle),
        authenticator=AsyncMock(get_token=AsyncMock(return_value="isolated-test-token")),
    )
    try:
        report = await verify(get_settings(), client=client)
        assert report["response_class"] == expected
        assert report["http_status"] == 403
        assert report["subscription_state"] == "UNOBSERVED"
        assert "private-reflected-secret" not in str(report)
        assert len(requests) == 1
        with pytest.raises(GrowwAuthError) as raised:
            await client.get("/live-data/ltp", allow_retry=False)
        assert "private-reflected-secret" not in str(raised.value.context)
    finally:
        await client.aclose()


async def test_json_permission_failure_has_bounded_retry_and_safe_classification():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            403, json={"status": "FAILURE", "error": {"message": "private-message"}}
        )

    auth = AsyncMock(get_token=AsyncMock(return_value="isolated-test-token"))
    auth.invalidate = lambda: None
    client = GrowwClient(
        settings=get_settings(), transport=httpx.MockTransport(handle), authenticator=auth
    )
    try:
        report = await verify(get_settings(), client=client)
        assert report["response_class"] == "JSON_BROKER_FAILURE"
        assert report["authentication"] == "VERIFIED"
        assert len(requests) == 2
        assert "private-message" not in str(report)
    finally:
        await client.aclose()
