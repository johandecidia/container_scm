"""HTTP client for the Traqo Ocean container tracking API.

Transport, retries, backoff, Retry-After and timeout classification all come from the
shared :class:`~apps.scm.integrations.carriers.http.CarrierHttpClient`; the only
things this class owns are Traqo's URL shape, its bearer authentication and its
status semantics (see :mod:`.errors`).

Sandbox versus production is a single path segment and nothing else::

    production   GET {base}/container/MRSU6859427?sealine=MAEU     + Authorization
    sandbox      GET {base}/sandbox/container/MRSU6859427?sealine=MAEU

So no branch on "am I in sandbox" leaks past the constructor: the sandbox client
builds a different URL and carries no credential, because the sandbox needs none and
sending one would be a key on the wire for no reason.

Configuration is environment-based (``TRAQO_API_KEY``, ``TRAQO_BASE_URL``,
``TRAQO_ENABLED``) rather than per-team like the carrier adapters, because a Traqo
account is one aggregator subscription for the installation, not a customer agreement
each team holds with a carrier. The key is read once into the auth object, never
logged, and never placed in a query string or an error message.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings

from apps.scm.integrations.carriers.exceptions import (
    CarrierConfigurationError,
    CarrierInvalidResponseError,
    CarrierUnsupportedReferenceError,
)
from apps.scm.integrations.carriers.http import CarrierHttpClient, HttpConfig
from apps.scm.integrations.carriers.oauth import ApiKeyAuth

from . import PROVIDER_CODE
from .errors import classify_traqo_error
from .sealines import resolve_sealine

logger = logging.getLogger(__name__)

PRODUCTION_BASE_URL = "https://traqocontainer.com/api/v1"
SANDBOX_SEGMENT = "sandbox"

_CONTAINER_NUMBER_RE = re.compile(r"^[A-Z]{4}\d{7}$")

# Traqo's carrier-lookup endpoint. Kept apart from ``container`` because the two spend
# different budgets: a lookup draws on its own daily quota and creates no shipment,
# while ``container`` may consume one of the account's shipment slots.
CARRIER_LOOKUP_PATH = "carriers/lookup"

# The account's own plan, allowance and billing cycle. Free — it consumes no shipment
# slot and makes no upstream carrier call — which is what makes it safe to read for a
# status page rather than discovering the limit by hitting it.
ACCOUNT_USAGE_PATH = "account/usage"


class TraqoClient:
    """Reads container tracking from Traqo, in sandbox or production mode."""

    provider_code = PROVIDER_CODE

    def __init__(
        self,
        *,
        base_url: str = PRODUCTION_BASE_URL,
        api_key: str = "",
        sandbox: bool = False,
        session=None,
        http_config: HttpConfig | None = None,
    ) -> None:
        self.base_url = (base_url or PRODUCTION_BASE_URL).rstrip("/")
        self.sandbox = bool(sandbox)
        self._api_key = api_key or ""
        self._session = session
        self._http_config = http_config or HttpConfig.from_config({})
        self._http: CarrierHttpClient | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, *, sandbox: bool = False, session=None) -> TraqoClient:
        """Build a client from the installation's TRAQO_* settings.

        ``TRAQO_ENABLED`` gates production only. The sandbox is fixed demo data behind
        no credential and no billing, so a developer can always reach it — which is
        the whole point of having it as the first integration target.
        """
        base_url = str(getattr(settings, "TRAQO_BASE_URL", "") or PRODUCTION_BASE_URL)
        api_key = str(getattr(settings, "TRAQO_API_KEY", "") or "")

        if not sandbox and not getattr(settings, "TRAQO_ENABLED", False):
            raise CarrierConfigurationError(
                "Traqo is not enabled. Set TRAQO_ENABLED=true and TRAQO_API_KEY to make live calls, "
                "or use the sandbox.",
                provider_code=PROVIDER_CODE,
            )

        return cls(base_url=base_url, api_key=api_key, sandbox=sandbox, session=session)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _build_auth(self):
        """Return the bearer auth for production, or None for the sandbox.

        The bearer prefix is part of the header value, so the existing static-key auth
        carries it — there is no second authentication style to introduce.
        """
        if self.sandbox:
            return None
        if not self._api_key:
            raise CarrierConfigurationError(
                "TRAQO_API_KEY is required for live Traqo calls.",
                provider_code=PROVIDER_CODE,
            )
        return ApiKeyAuth(
            header_name="Authorization",
            api_key=f"Bearer {self._api_key}",
            provider_code=PROVIDER_CODE,
        )

    @property
    def http(self) -> CarrierHttpClient:
        if self._http is None:
            self._http = CarrierHttpClient(
                provider_code=PROVIDER_CODE,
                config=self._http_config,
                auth=self._build_auth(),
                # No Integration record backs Traqo, so there is no team-scoped
                # IntegrationRequestLog row to write; the shared client skips logging.
                integration=None,
                session=self._session,
                error_classifier=classify_traqo_error,
            )
        return self._http

    def container_url(self, container_number: str) -> str:
        """Return the endpoint URL for a container, sandbox or production."""
        segments = [self.base_url]
        if self.sandbox:
            segments.append(SANDBOX_SEGMENT)
        segments.append("container")
        segments.append(container_number)
        return "/".join(segments)

    def carrier_lookup_url(self) -> str:
        """Return the carrier-lookup endpoint URL, sandbox or production."""
        return self._url(CARRIER_LOOKUP_PATH)

    def account_usage_url(self) -> str:
        """Return the account-usage endpoint URL, sandbox or production."""
        return self._url(ACCOUNT_USAGE_PATH)

    def _url(self, path: str) -> str:
        """Join ``path`` onto the base URL, inserting the sandbox segment when in sandbox.

        The sandbox is a path segment and nothing else, so every endpoint composes its
        URL the same way and none of them has to remember the branch.
        """
        segments = [self.base_url]
        if self.sandbox:
            segments.append(SANDBOX_SEGMENT)
        segments.append(path)
        return "/".join(segments)

    # ------------------------------------------------------------------
    # Traqo contract
    # ------------------------------------------------------------------

    def get_container(self, container_number: str, sealine: str) -> dict:
        """Return Traqo's response envelope for one container.

        ``sealine`` is mandatory at Traqo and accepts either a Container SCM carrier
        code or the SCAC itself — see :func:`.sealines.resolve_sealine`. It is never
        derived from the container number here: which carrier holds a box is decided
        by Container SCM's own carrier discovery, and Traqo must not become a second
        opinion on it.

        The whole envelope is returned, not just ``data``, because that is the
        original response and what belongs in TrackingRawPayload.

        Raises a typed carrier error on failure, and CarrierNoDataError when Traqo has
        no shipment for the container (HTTP 404).
        """
        number = (container_number or "").strip().upper()
        if not _CONTAINER_NUMBER_RE.match(number):
            raise CarrierUnsupportedReferenceError(
                f"'{container_number}' is not a container number (expected 4 letters and 7 digits).",
                provider_code=PROVIDER_CODE,
            )

        scac = resolve_sealine(sealine)
        payload = self.http.get(self.container_url(number), params={"sealine": scac})
        return self._as_shipment_payload(payload, container_number=number)

    def lookup_carrier(self, reference: str) -> dict:
        """Return Traqo's carrier-lookup envelope for one reference.

        Asks "which carrier is likely to know this number" **without** starting
        tracking: no shipment is created and no shipment slot is spent. That is the
        whole reason it is a separate call from :meth:`get_container` — the two draw on
        different budgets, and conflating them would spend a scarce slot to answer a
        question the free endpoint answers.

        The full envelope is returned unparsed. What the answer *means* — how much
        confidence it deserves, whether it agrees with what Container SCM already
        believes — is decided in :mod:`.carrier_lookup`, not here, because a transport
        must not also be a policy.

        Raises the same typed carrier errors as any other Traqo call.
        """
        number = (reference or "").strip().upper()
        if not number:
            raise CarrierUnsupportedReferenceError(
                "A reference is required to look a carrier up.",
                provider_code=PROVIDER_CODE,
            )

        payload = self.http.get(self.carrier_lookup_url(), params={"number": number})
        if not isinstance(payload, dict):
            raise CarrierInvalidResponseError(
                "Traqo carrier lookup did not return a JSON object.",
                provider_code=PROVIDER_CODE,
                status_code=200,
            )
        if payload.get("success") is False:
            message = str(payload.get("message") or "").strip() or "Traqo reported the lookup unsuccessful."
            raise CarrierInvalidResponseError(message, provider_code=PROVIDER_CODE, status_code=200)

        logger.info("Traqo carrier lookup for %s returned.", number)
        return payload

    def _as_shipment_payload(self, payload, *, container_number: str) -> dict:
        """Check the envelope is one the mapper can read, and return it unchanged.

        A body that is not an object, or that reports ``success: false`` on a 200, is
        rejected rather than allowed through as a shipment with no events — the two
        are indistinguishable downstream, and one of them is a silent data loss.
        """
        if not isinstance(payload, dict):
            raise CarrierInvalidResponseError(
                "Traqo response was not a JSON object.",
                provider_code=PROVIDER_CODE,
                status_code=200,
            )
        if payload.get("success") is False:
            message = str(payload.get("message") or "").strip() or "Traqo reported the request unsuccessful."
            raise CarrierInvalidResponseError(message, provider_code=PROVIDER_CODE, status_code=200)
        if not isinstance(payload.get("data"), dict):
            raise CarrierInvalidResponseError(
                "Traqo response contained no shipment data object.",
                provider_code=PROVIDER_CODE,
                status_code=200,
            )

        logger.info(
            "Traqo %s response for %s: %d event(s).",
            SANDBOX_SEGMENT if self.sandbox else "live",
            container_number,
            len(payload["data"].get("events_table") or []),
        )
        return payload

    def get_account_usage(self) -> dict:
        """Return Traqo's account usage envelope — plan, allowance, cycle, rate limit.

        Free at Traqo's end: it spends no shipment slot and makes no upstream carrier
        call, drawing only on the per-minute request budget. That is what makes it
        readable for a status page — the alternative is discovering the limit by hitting
        it, which costs a failed tracking attempt to learn.

        The whole envelope is returned rather than a parsed model, for the same reason
        :meth:`get_container` returns one: a transport must not also be a schema. What
        the numbers *mean* is :mod:`.usage`.

        Everything in here describes our own account, so the caller is responsible for
        keeping it away from customers — see ``tracking/platform_views.py``, which is the
        only thing that renders it.
        """
        payload = self.http.get(self.account_usage_url())
        if not isinstance(payload, dict):
            raise CarrierInvalidResponseError(
                "Traqo account usage did not return a JSON object.",
                provider_code=PROVIDER_CODE,
                status_code=200,
            )
        if payload.get("success") is False:
            message = str(payload.get("message") or "").strip() or "Traqo reported the usage request unsuccessful."
            raise CarrierInvalidResponseError(message, provider_code=PROVIDER_CODE, status_code=200)
        return payload
