"""Twilio webhook signature validation.

Implements the canonical X-Twilio-Signature HMAC-SHA1 algorithm per
https://www.twilio.com/docs/usage/security.

Algorithm:
    1. Take the full request URL (including query string).
    2. Sort POST params alphabetically by key (case-sensitive, Unix sort).
    3. Concatenate the URL with ``k + v`` for each sorted (k, v) pair, with
       no separator between pairs or between key and value.
    4. HMAC-SHA1 with the Twilio Auth Token as the key.
    5. Base64-encode the digest.
    6. Compare to the ``X-Twilio-Signature`` header in constant time.

The dependency ``require_valid_twilio_signature`` is applied per-route via
``Depends`` so that public endpoints (``/health``, ``/diag``) are not gated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from wa_voicenote.config import Settings, get_settings

_TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"
_FORWARDED_PROTO_HEADER = "X-Forwarded-Proto"
_FORWARDED_HOST_HEADER = "X-Forwarded-Host"


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """Compute the canonical Twilio webhook signature.

    Pure function; no I/O. Sorts params alphabetically by key, concatenates
    ``url + "".join(k + v for k, v in sorted_items)``, HMAC-SHA1 with the
    auth token as key, then Base64-encodes the digest.
    """
    sorted_items = sorted(params.items())
    signed_string = url + "".join(k + v for k, v in sorted_items)
    digest = hmac.new(
        auth_token.encode("utf-8"),
        signed_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def is_valid_signature(
    auth_token: str,
    url: str,
    params: Mapping[str, str],
    signature: str,
) -> bool:
    """Return True iff ``signature`` matches the canonical Twilio signature.

    Uses ``hmac.compare_digest`` for constant-time comparison to defeat
    timing-side-channel attacks.
    """
    expected = compute_signature(auth_token, url, params)
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii"))


def _reconstruct_public_url(request: Request) -> str:
    """Reconstruct the URL Twilio used when signing the request.

    Azure Container Apps terminates TLS at the ingress, so the FastAPI app
    sees ``http://`` internally. Twilio signed the *public* URL, so:

    1. If ``X-Forwarded-Proto`` is set (Container Apps and most proxies set
       this), use it for the scheme. The standard ``Host`` header (or the
       optional ``X-Forwarded-Host``) supplies the public hostname — Container
       Apps does NOT set ``X-Forwarded-Host``, so the regular ``Host`` header
       is the source of truth for the public host.
    2. Otherwise (local dev, direct HTTP) fall back to ``request.url``.
    """
    forwarded_proto = request.headers.get(_FORWARDED_PROTO_HEADER)
    if not forwarded_proto:
        return str(request.url)

    forwarded_host = request.headers.get(_FORWARDED_HOST_HEADER)
    host = forwarded_host or request.headers.get("host") or request.url.netloc

    path = request.url.path
    query = request.url.query
    suffix = f"?{query}" if query else ""
    return f"{forwarded_proto}://{host}{path}{suffix}"


async def require_valid_twilio_signature(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """FastAPI dependency: enforce a valid X-Twilio-Signature on the request.

    On success returns ``None``. On any failure raises
    ``HTTPException(status_code=403)``.
    """
    signature = request.headers.get(_TWILIO_SIGNATURE_HEADER)
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing X-Twilio-Signature header",
        )

    form = await request.form()
    # All Twilio webhook POST params are strings; coerce defensively.
    params: dict[str, str] = {key: str(value) for key, value in form.items()}

    url = _reconstruct_public_url(request)
    auth_token = settings.twilio_auth_token.get_secret_value()

    if not is_valid_signature(auth_token, url, params, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Twilio signature",
        )
