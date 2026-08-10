"""OAuth2 authentication for Microsoft Business Central.

Uses the Microsoft Entra ID (Azure AD) OAuth2 *client credentials* flow. A single
access token is requested with the app registration's client id/secret and reused
in memory until shortly before it expires. Client credentials tokens are not
refreshed with a refresh token — a new one is requested when the old one expires.

The access token is never logged.
"""

from __future__ import annotations

import logging
import time

import requests

from .exceptions import (
    BusinessCentralAuthenticationError,
    BusinessCentralConfigurationError,
    BusinessCentralConnectionError,
)

logger = logging.getLogger(__name__)

# Business Central resource scope for client-credentials tokens.
BC_DEFAULT_SCOPE = "https://api.businesscentral.dynamics.com/.default"

# Refresh a little before the real expiry to avoid races near the boundary.
_EXPIRY_SKEW_SECONDS = 60


class BusinessCentralAuth:
    """Acquires and caches an Entra ID access token for Business Central."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str = BC_DEFAULT_SCOPE,
        timeout_seconds: int = 30,
    ) -> None:
        if not tenant_id:
            raise BusinessCentralConfigurationError("Business Central tenant_id is required")
        if not client_id or not client_secret:
            raise BusinessCentralConfigurationError("Business Central client_id and client_secret are required")

        self.tenant_id = tenant_id
        self.client_id = client_id
        self._client_secret = client_secret
        self.scope = scope
        self.timeout_seconds = timeout_seconds

        self._token: str | None = None
        # Monotonic deadline (seconds); immune to wall-clock changes.
        self._expires_at: float = 0.0

    @property
    def token_endpoint(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"

    def get_access_token(self) -> str:
        """Return a valid access token, requesting a new one only when needed."""
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        return self._request_token()

    def invalidate_token(self) -> None:
        """Drop the cached token so the next call fetches a fresh one."""
        self._token = None
        self._expires_at = 0.0

    def _request_token(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self._client_secret,
            "scope": self.scope,
        }
        try:
            response = requests.post(self.token_endpoint, data=data, timeout=self.timeout_seconds)
        except requests.Timeout as exc:
            raise BusinessCentralConnectionError("Timed out requesting Business Central access token") from exc
        except requests.RequestException as exc:
            raise BusinessCentralConnectionError("Network error requesting Business Central access token") from exc

        if response.status_code != 200:
            # Do not include the response body verbatim — it can echo request
            # parameters. Surface only the status and the AAD error code.
            error_code = ""
            try:
                error_code = response.json().get("error", "")
            except ValueError:
                error_code = ""
            raise BusinessCentralAuthenticationError(
                f"Token request failed (HTTP {response.status_code}"
                + (f", error={error_code}" if error_code else "")
                + ")"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise BusinessCentralAuthenticationError("Token response was not valid JSON") from exc

        token = payload.get("access_token")
        if not token:
            raise BusinessCentralAuthenticationError("Token response did not contain an access_token")

        expires_in = payload.get("expires_in", 3600)
        try:
            expires_in = int(expires_in)
        except TypeError, ValueError:
            expires_in = 3600

        self._token = token
        self._expires_at = time.monotonic() + max(expires_in - _EXPIRY_SKEW_SECONDS, 0)
        logger.info("Acquired Business Central access token (expires_in=%ss)", expires_in)
        return token
