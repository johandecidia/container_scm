"""OAuth2 client-credentials tokens for carrier APIs.

Carriers that use the client-credentials grant share this helper: a token is
requested with the client id/secret, cached in memory until shortly before it
expires, and re-requested when needed. Client-credentials tokens have no refresh
token — expiry means a new request.

The token and the client secret are never logged, and a failed token response is
reported by status and error code only, because the body can echo the request.
"""

from __future__ import annotations

import logging
import time

import requests

from .exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierTimeoutError,
)

logger = logging.getLogger(__name__)

# Refresh a little before the real expiry to avoid races near the boundary.
_EXPIRY_SKEW_SECONDS = 60
_DEFAULT_EXPIRES_IN = 3600


class ClientCredentialsAuth:
    """Acquires and caches an OAuth2 client-credentials access token."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
        provider_code: str = "",
        timeout_seconds: int = 30,
        audience: str = "",
    ) -> None:
        if not token_url:
            raise CarrierConfigurationError(
                "token_url is required for OAuth2 authentication.", provider_code=provider_code
            )
        if not client_id or not client_secret:
            raise CarrierConfigurationError(
                "client_id and client_secret are required for OAuth2 authentication.",
                provider_code=provider_code,
            )

        self.token_url = token_url
        self.client_id = client_id
        self._client_secret = client_secret
        self.scope = scope
        self.audience = audience
        self.provider_code = provider_code
        self.timeout_seconds = timeout_seconds

        self._token: str | None = None
        # Monotonic deadline (seconds); immune to wall-clock changes.
        self._expires_at: float = 0.0

    def get_access_token(self) -> str:
        """Return a valid access token, requesting a new one only when needed."""
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        return self._request_token()

    def invalidate_token(self) -> None:
        """Drop the cached token so the next call fetches a fresh one."""
        self._token = None
        self._expires_at = 0.0

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_access_token()}"}

    def _request_token(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self._client_secret,
        }
        if self.scope:
            data["scope"] = self.scope
        if self.audience:
            data["audience"] = self.audience

        try:
            response = requests.post(self.token_url, data=data, timeout=self.timeout_seconds)
        except requests.Timeout as exc:
            raise CarrierTimeoutError(
                "Timed out requesting carrier access token.", provider_code=self.provider_code
            ) from exc
        except requests.RequestException as exc:
            raise CarrierTimeoutError(
                "Network error requesting carrier access token.", provider_code=self.provider_code
            ) from exc

        if response.status_code != 200:
            error_code = ""
            try:
                error_code = response.json().get("error", "")
            except ValueError:
                error_code = ""
            raise CarrierAuthenticationError(
                f"Token request failed (HTTP {response.status_code}"
                + (f", error={error_code}" if error_code else "")
                + ")",
                provider_code=self.provider_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CarrierAuthenticationError(
                "Token response was not valid JSON.", provider_code=self.provider_code
            ) from exc

        token = payload.get("access_token")
        if not token:
            raise CarrierAuthenticationError(
                "Token response did not contain an access_token.", provider_code=self.provider_code
            )

        try:
            expires_in = int(payload.get("expires_in", _DEFAULT_EXPIRES_IN))
        except TypeError, ValueError:
            expires_in = _DEFAULT_EXPIRES_IN

        self._token = token
        self._expires_at = time.monotonic() + max(expires_in - _EXPIRY_SKEW_SECONDS, 0)
        logger.info("Acquired %s access token (expires_in=%ss)", self.provider_code or "carrier", expires_in)
        return token


class ApiKeyAuth:
    """Sends a static API key in a configured header."""

    def __init__(self, *, header_name: str, api_key: str, provider_code: str = "") -> None:
        if not header_name:
            raise CarrierConfigurationError(
                "api_key_header_name is required for API key authentication.", provider_code=provider_code
            )
        if not api_key:
            raise CarrierConfigurationError(
                "An api_key credential is required for API key authentication.", provider_code=provider_code
            )
        self.header_name = header_name
        self._api_key = api_key
        self.provider_code = provider_code

    def get_access_token(self) -> str:
        return self._api_key

    def invalidate_token(self) -> None:
        """No-op: a static API key cannot be refreshed."""

    def auth_headers(self) -> dict:
        return {self.header_name: self._api_key}


class ClientIdSecretHeaderAuth:
    """Sends a client id and client secret as two static headers.

    The gateway style rather than the grant style. Carriers behind an IBM API Connect
    gateway — Hapag-Lloyd's Track & Trace among them — issue a client id and secret
    from their developer portal and expect *both* on every request
    (``X-IBM-Client-Id`` / ``X-IBM-Client-Secret``); there is no token endpoint and
    nothing to exchange.

    That is neither :class:`ApiKeyAuth`, which can only send one header, nor
    :class:`ClientCredentialsAuth`, which spends a round trip acquiring a token the
    gateway would not accept. Both header names are configuration because the prefix
    varies by gateway deployment, and the secret is a credential rather than a token:
    it is never logged and there is nothing to refresh.
    """

    def __init__(
        self,
        *,
        client_id_header_name: str,
        client_secret_header_name: str,
        client_id: str,
        client_secret: str,
        provider_code: str = "",
    ) -> None:
        missing = [
            name
            for name, value in (
                ("client_id_header_name", client_id_header_name),
                ("client_secret_header_name", client_secret_header_name),
            )
            if not value
        ]
        if missing:
            raise CarrierConfigurationError(
                f"{', '.join(missing)} is required for client id/secret header authentication.",
                provider_code=provider_code,
            )
        if not client_id or not client_secret:
            raise CarrierConfigurationError(
                "client_id and client_secret credentials are required for client id/secret header authentication.",
                provider_code=provider_code,
            )

        self.client_id_header_name = client_id_header_name
        self.client_secret_header_name = client_secret_header_name
        self.client_id = client_id
        self._client_secret = client_secret
        self.provider_code = provider_code

    def get_access_token(self) -> str:
        """The client id — the non-secret half, for callers that want an identifier.

        Deliberately not the secret: nothing about this style has an access token, and
        returning the secret here would put it wherever a token is considered safe.
        """
        return self.client_id

    def invalidate_token(self) -> None:
        """No-op: a portal-issued client secret cannot be refreshed in-band."""

    def auth_headers(self) -> dict:
        return {
            self.client_id_header_name: self.client_id,
            self.client_secret_header_name: self._client_secret,
        }
