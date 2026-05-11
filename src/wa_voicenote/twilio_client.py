"""Twilio Programmable Messaging REST client.

Sends outbound WhatsApp messages from the configured `whatsapp:+14155238886`
sandbox sender. Uses direct httpx calls (no Twilio SDK) for consistency with
the AOAI client and to avoid transitive dependency bloat.

Auth: HTTP Basic with TWILIO_ACCOUNT_SID:TWILIO_AUTH_TOKEN.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

_HTTP_ERROR_THRESHOLD = 400
_HTTP_RATE_LIMITED = 429
_HTTP_SERVER_ERROR_FLOOR = 500


@dataclass(frozen=True)
class TwilioMessage:
    """Result of a successful Twilio message-create call."""

    sid: str
    status: str  # queued | sending | sent | delivered | failed | undelivered
    to: str
    from_: str


class TwilioError(Exception):
    """Base Twilio failure."""


class TwilioHttpError(TwilioError):
    """HTTP non-2xx from Twilio. Carries status_code and response body."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Twilio HTTP {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class TwilioClient:
    """Typed async wrapper around POST /Accounts/{AccountSid}/Messages.json.

    The class is auth-agnostic in shape but only supports HTTP Basic auth
    with (AccountSid, AuthToken). Retries on 429 and 5xx with linear
    backoff; never retries on other 4xx (those indicate caller error).
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: SecretStr,
        from_number: str,  # e.g. whatsapp:+14155238886
        http_timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from = from_number
        self._timeout = http_timeout_seconds
        self._http = http_client  # if None, create a transient client per call

    def _url(self) -> str:
        return f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/Messages.json"

    async def _post(
        self,
        data: dict[str, str],
        auth: tuple[str, str],
    ) -> httpx.Response:
        url = self._url()
        if self._http is not None:
            return await self._http.post(url, data=data, auth=auth)
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
            return await client.post(url, data=data, auth=auth)

    async def send_text(
        self,
        to: str,
        body: str,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
    ) -> TwilioMessage:
        """Send a text WhatsApp message.

        Retries on 429 and 5xx with linear backoff (``backoff_seconds * attempt``
        where ``attempt`` is 1-indexed). Non-retryable 4xx errors raise
        ``TwilioHttpError`` immediately.

        The auth token is constructed into the Basic-auth tuple only at the
        call site and is never logged, stringified, or placed in the URL or
        request body.
        """
        data = {"From": self._from, "To": to, "Body": body}
        # Construct auth tuple at the call site; do not retain.
        auth = (self._account_sid, self._auth_token.get_secret_value())

        attempt = 0
        last_status = 0
        last_body = ""
        while attempt <= max_retries:
            response = await self._post(data, auth)
            status = response.status_code

            if status < _HTTP_ERROR_THRESHOLD:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TwilioHttpError(status, response.text)
                return TwilioMessage(
                    sid=str(payload.get("sid", "")),
                    status=str(payload.get("status", "")),
                    to=str(payload.get("to", to)),
                    from_=str(payload.get("from", self._from)),
                )

            last_status = status
            last_body = response.text

            # Retry on 429 and 5xx; otherwise raise immediately.
            if status == _HTTP_RATE_LIMITED or status >= _HTTP_SERVER_ERROR_FLOOR:
                if attempt == max_retries:
                    break
                await asyncio.sleep(backoff_seconds * (attempt + 1))
                attempt += 1
                continue
            raise TwilioHttpError(status, last_body)

        raise TwilioHttpError(last_status, last_body)
