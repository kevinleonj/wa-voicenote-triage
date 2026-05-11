"""Tests for Twilio webhook signature validation.

Uses Twilio's canonical example from https://www.twilio.com/docs/usage/security
to validate the HMAC-SHA1 algorithm. The canonical signature value
``RSOYDt4T1cUTdK1PDd93/VVr8B8=`` is the published example output for the
fixed URL/params/AuthToken combination below.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI

from wa_voicenote.twilio_signing import (
    compute_signature,
    is_valid_signature,
    require_valid_twilio_signature,
)

# ----- Canonical Twilio docs example -----------------------------------------
# Source: https://www.twilio.com/docs/usage/security
CANONICAL_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
CANONICAL_PARAMS: dict[str, str] = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}
CANONICAL_TOKEN = "12345"  # noqa: S105 - canonical Twilio docs example token.
CANONICAL_SIGNATURE = "RSOYDt4T1cUTdK1PDd93/VVr8B8="


# ----- Pure function tests ---------------------------------------------------


def test_compute_signature_canonical() -> None:
    """Twilio's canonical example must produce the published signature."""
    assert compute_signature(CANONICAL_TOKEN, CANONICAL_URL, CANONICAL_PARAMS) == (
        CANONICAL_SIGNATURE
    )


def test_is_valid_signature_returns_true_for_canonical() -> None:
    assert is_valid_signature(CANONICAL_TOKEN, CANONICAL_URL, CANONICAL_PARAMS, CANONICAL_SIGNATURE)


def test_is_valid_signature_returns_false_for_tampered_signature() -> None:
    # Mutate the last character (before the '=' padding) so length is preserved
    # but the bytes differ.
    tampered = CANONICAL_SIGNATURE[:-2] + ("A" if CANONICAL_SIGNATURE[-2] != "A" else "B") + "="
    assert tampered != CANONICAL_SIGNATURE
    assert not is_valid_signature(CANONICAL_TOKEN, CANONICAL_URL, CANONICAL_PARAMS, tampered)


def test_is_valid_signature_returns_false_for_tampered_params() -> None:
    tampered_params = dict(CANONICAL_PARAMS)
    tampered_params["Digits"] = "9999"
    assert not is_valid_signature(
        CANONICAL_TOKEN, CANONICAL_URL, tampered_params, CANONICAL_SIGNATURE
    )


def test_is_valid_signature_returns_false_for_wrong_token() -> None:
    assert not is_valid_signature(
        "wrong_token", CANONICAL_URL, CANONICAL_PARAMS, CANONICAL_SIGNATURE
    )


def test_is_valid_signature_handles_unsorted_params() -> None:
    """The function must sort params internally; insertion order should not matter."""
    # Construct a dict in reverse-sorted order.
    unsorted = {k: CANONICAL_PARAMS[k] for k in sorted(CANONICAL_PARAMS, reverse=True)}
    assert list(unsorted.keys()) != sorted(CANONICAL_PARAMS.keys())
    assert is_valid_signature(CANONICAL_TOKEN, CANONICAL_URL, unsorted, CANONICAL_SIGNATURE)


def test_is_valid_signature_uses_constant_time_compare() -> None:
    """Code-level assertion that hmac.compare_digest is used (constant time)."""
    src = inspect.getsource(is_valid_signature)
    assert "compare_digest" in src, (
        "is_valid_signature must use hmac.compare_digest for constant-time comparison"
    )


@pytest.mark.parametrize(
    "bad_signature",
    [
        "",
        "not-base64-!@#",
        "AAAA",  # valid base64 but wrong length
        CANONICAL_SIGNATURE + "extra",
    ],
)
def test_is_valid_signature_rejects_malformed(bad_signature: str) -> None:
    assert not is_valid_signature(CANONICAL_TOKEN, CANONICAL_URL, CANONICAL_PARAMS, bad_signature)


# ----- FastAPI dependency tests ----------------------------------------------


def _build_app(auth_token: str) -> FastAPI:
    """Build a minimal FastAPI app that exposes a single signature-protected route.

    Overrides the ``get_settings`` dependency with a lightweight stub so the
    dependency does not pull from the real env-driven Settings object.
    """
    from wa_voicenote.config import get_settings as real_get_settings

    app = FastAPI()

    class _StubSettings:
        twilio_auth_token: Any

        def __init__(self, token: str) -> None:
            # Mimic pydantic SecretStr.get_secret_value() interface.
            class _Secret:
                def __init__(self, v: str) -> None:
                    self._v = v

                def get_secret_value(self) -> str:
                    return self._v

            self.twilio_auth_token = _Secret(token)

    def _override_get_settings() -> _StubSettings:
        return _StubSettings(auth_token)

    app.dependency_overrides[real_get_settings] = _override_get_settings

    @app.post("/myapp.php", dependencies=[Depends(require_valid_twilio_signature)])
    async def protected() -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.fixture
async def client_canonical() -> AsyncIterator[httpx.AsyncClient]:
    app = _build_app(CANONICAL_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_dependency_passes_with_valid_signature() -> None:
    """A request with the canonical signature reaches the route handler."""
    app = _build_app(CANONICAL_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        # Build a request whose reconstructed URL matches CANONICAL_URL via
        # X-Forwarded-Proto / X-Forwarded-Host. Path comes from request.url.path.
        # We override host+proto headers to make the signed string match.
        response = await ac.post(
            "/myapp.php?foo=1&bar=2",
            data=CANONICAL_PARAMS,
            headers={
                "X-Twilio-Signature": CANONICAL_SIGNATURE,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mycompany.com",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"ok": "yes"}


async def test_dependency_403_on_invalid_signature() -> None:
    app = _build_app(CANONICAL_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        tampered = CANONICAL_SIGNATURE[:-2] + "AA"
        response = await ac.post(
            "/myapp.php?foo=1&bar=2",
            data=CANONICAL_PARAMS,
            headers={
                "X-Twilio-Signature": tampered,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mycompany.com",
            },
        )
    assert response.status_code == 403
    assert "Invalid" in response.json()["detail"]


async def test_dependency_403_on_missing_header() -> None:
    app = _build_app(CANONICAL_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.post(
            "/myapp.php?foo=1&bar=2",
            data=CANONICAL_PARAMS,
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mycompany.com",
            },
        )
    assert response.status_code == 403
    assert "Missing" in response.json()["detail"]


async def test_dependency_uses_forwarded_proto_and_host() -> None:
    """When X-Forwarded-Proto/Host are set, the dependency must sign the public URL.

    The request hits ``http://testserver`` internally but Twilio signs against
    ``https://mycompany.com`` — using forwarded headers must yield a match.
    """
    app = _build_app(CANONICAL_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.post(
            "/myapp.php?foo=1&bar=2",
            data=CANONICAL_PARAMS,
            headers={
                "X-Twilio-Signature": CANONICAL_SIGNATURE,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mycompany.com",
            },
        )
    assert response.status_code == 200


async def test_dependency_403_without_forwarded_proto_uses_request_url() -> None:
    """Without forwarded headers, the dependency falls back to request.url.

    The signature computed against the canonical public URL therefore does NOT
    match the internal ``http://testserver/...`` URL.
    """
    app = _build_app(CANONICAL_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.post(
            "/myapp.php?foo=1&bar=2",
            data=CANONICAL_PARAMS,
            headers={"X-Twilio-Signature": CANONICAL_SIGNATURE},
        )
    assert response.status_code == 403
