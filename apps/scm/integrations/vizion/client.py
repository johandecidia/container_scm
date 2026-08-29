"""HTTP client for the Vizion container tracking API.

Transport, retries, backoff, Retry-After and timeout classification all come from the
shared :class:`~apps.scm.integrations.carriers.http.CarrierHttpClient`; the only things
this class owns are Vizion's URL shape, its ``X-API-Key`` authentication and its status
semantics (see :mod:`.errors`).

Demo versus production is a different host and nothing else::

    production   POST https://prod.vizionapi.com/references   + X-API-Key
    demo         POST https://demo.vizionapi.com/references   + X-API-Key

Both need a key — unlike Traqo's sandbox, Vizion's demo environment is metered per key
— and references do not cross between the two: a reference created in demo cannot be
read from production. So the environment is fixed at construction and never branched on
afterwards.

Configuration is environment-based (``VIZION_API_KEY``, ``VIZION_BASE_URL``,
``VIZION_DEMO_BASE_URL``, ``VIZION_ENABLED``) rather than per-team like the carrier
adapters, because a Vizion account is one aggregator subscription for the installation,
not a customer agreement each team holds with a carrier. The key is read once into the
auth object, never logged, and never placed in a query string or an error message.

Three calls, because the POC needs exactly three:

    create_reference()   start tracking — with **no carrier**, this is ACI
    get_reference()      the reference's current state, including the ACI outcome
    list_updates()       the tracking payloads Vizion has built for it

There is no webhook handling. Vizion can push the same update body to a callback URL,
and :func:`~.mapper.map_vizion_update` reads that body directly, so a later phase can
add a webhook endpoint that feeds the identical normalisation pipeline. Polling is
enough for Phase 1 and needs no endpoint to be exposed.
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
from .errors import classify_vizion_error

logger = logging.getLogger(__name__)

PRODUCTION_BASE_URL = "https://prod.vizionapi.com"
DEMO_BASE_URL = "https://demo.vizionapi.com"

API_KEY_HEADER = "X-API-Key"

_CONTAINER_NUMBER_RE = re.compile(r"^[A-Z]{4}\d{7}$")


class VizionClient:
    """Reads container tracking and carrier identification from Vizion."""

    provider_code = PROVIDER_CODE

    def __init__(
        self,
        *,
        base_url: str = PRODUCTION_BASE_URL,
        api_key: str = "",
        demo: bool = False,
        session=None,
        http_config: HttpConfig | None = None,
    ) -> None:
        self.base_url = (base_url or PRODUCTION_BASE_URL).rstrip("/")
        self.demo = bool(demo)
        self._api_key = api_key or ""
        self._session = session
        self._http_config = http_config or HttpConfig.from_config({})
        self._http: CarrierHttpClient | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, *, demo: bool = False, session=None) -> VizionClient:
        """Build a client from the installation's VIZION_* settings.

        ``VIZION_ENABLED`` gates both environments, not production alone. Vizion's demo
        is metered against the same account key rather than being free fixed data, so
        there is no equivalent of Traqo's always-reachable sandbox and pretending
        otherwise would spend somebody's quota by default.
        """
        if not getattr(settings, "VIZION_ENABLED", False):
            raise CarrierConfigurationError(
                "Vizion is not enabled. Set VIZION_ENABLED=true and VIZION_API_KEY to make calls.",
                provider_code=PROVIDER_CODE,
            )

        default = DEMO_BASE_URL if demo else PRODUCTION_BASE_URL
        setting_name = "VIZION_DEMO_BASE_URL" if demo else "VIZION_BASE_URL"
        base_url = str(getattr(settings, setting_name, "") or default)
        api_key = str(getattr(settings, "VIZION_API_KEY", "") or "")

        return cls(base_url=base_url, api_key=api_key, demo=demo, session=session)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _build_auth(self) -> ApiKeyAuth:
        if not self._api_key:
            raise CarrierConfigurationError(
                "VIZION_API_KEY is required for Vizion calls, including the demo environment.",
                provider_code=PROVIDER_CODE,
            )
        return ApiKeyAuth(header_name=API_KEY_HEADER, api_key=self._api_key, provider_code=PROVIDER_CODE)

    @property
    def http(self) -> CarrierHttpClient:
        if self._http is None:
            self._http = CarrierHttpClient(
                provider_code=PROVIDER_CODE,
                config=self._http_config,
                auth=self._build_auth(),
                # No Integration record backs Vizion, so there is no team-scoped
                # IntegrationRequestLog row to write; the shared client skips logging.
                integration=None,
                session=self._session,
                error_classifier=classify_vizion_error,
            )
        return self._http

    def references_url(self) -> str:
        return f"{self.base_url}/references"

    def reference_url(self, reference_id: str) -> str:
        return f"{self.base_url}/references/{reference_id}"

    def updates_url(self, reference_id: str) -> str:
        return f"{self.base_url}/references/{reference_id}/updates"

    # ------------------------------------------------------------------
    # Vizion contract
    # ------------------------------------------------------------------

    def create_reference(self, container_number: str, *, carrier_code: str = "") -> dict:
        """Create a Vizion reference for a container and return the response envelope.

        **Omitting ``carrier_code`` is what invokes Auto Carrier Identification.** That
        is Vizion's documented contract: there is no ``carrier_code: "AUTO"`` sentinel,
        the field is simply absent, and Vizion then searches its supported lines for one
        that has data. So the ACI path is not a special mode of this method — it is this
        method called the way the POC calls it, with a container number and nothing else.

        ``carrier_code`` exists for the comparison case only: asking Vizion the same
        question *with* a carrier, to see what ACI cost or gained. It is never derived
        from the container number here. Which carrier holds a box is decided by carrier
        resolution, and a transport must not also be a policy.

        The whole envelope is returned, not just ``reference``, because that is the
        original response and what belongs in TrackingRawPayload.
        """
        number = self._require_container_number(container_number)

        body: dict = {"container_id": number}
        code = (carrier_code or "").strip().upper()
        if code:
            body["carrier_code"] = code

        payload = self.http.post(self.references_url(), json_body=body)
        if not isinstance(payload, dict):
            raise CarrierInvalidResponseError(
                "Vizion reference creation did not return a JSON object.",
                provider_code=PROVIDER_CODE,
                status_code=200,
            )

        logger.info(
            "Vizion reference created for %s (%s, carrier %s).",
            number,
            "demo" if self.demo else "production",
            code or "ACI — none supplied",
        )
        return payload

    def get_reference(self, reference_id: str) -> dict:
        """Return one reference's current state.

        This is where an ACI outcome is read from: ``last_update_status`` carries
        ``auto_carrier_completed``, ``auto_carrier_not_found`` or ``auto_carrier_failed``,
        and ``carrier_scac`` is populated once identification succeeds.
        """
        identifier = (reference_id or "").strip()
        if not identifier:
            raise CarrierUnsupportedReferenceError("A Vizion reference id is required.", provider_code=PROVIDER_CODE)

        payload = self.http.get(self.reference_url(identifier))
        if not isinstance(payload, dict):
            raise CarrierInvalidResponseError(
                "Vizion reference lookup did not return a JSON object.",
                provider_code=PROVIDER_CODE,
                status_code=200,
            )
        return payload

    def list_updates(self, reference_id: str) -> list[dict]:
        """Return the update envelopes Vizion has built for one reference.

        Vizion answers either a bare array or a paginated ``{"data": [...]}`` object
        depending on the query, so both are accepted and normalised to a list here —
        that shape difference is a transport detail and must not reach the mapper.

        An empty list is a valid answer: a reference can exist, and ACI can still be
        searching, with no update built yet.
        """
        identifier = (reference_id or "").strip()
        if not identifier:
            raise CarrierUnsupportedReferenceError("A Vizion reference id is required.", provider_code=PROVIDER_CODE)

        payload = self.http.get(self.updates_url(identifier))
        updates = self._as_update_list(payload)
        logger.info("Vizion returned %d update(s) for reference %s.", len(updates), identifier)
        return updates

    # ------------------------------------------------------------------
    # Response shape
    # ------------------------------------------------------------------

    @staticmethod
    def _as_update_list(payload) -> list[dict]:
        """Coerce Vizion's two documented update-list shapes into one list of objects."""
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
            entries = payload["data"]
        else:
            raise CarrierInvalidResponseError(
                "Vizion update list was neither an array nor an object with a data array.",
                provider_code=PROVIDER_CODE,
                status_code=200,
            )
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _require_container_number(container_number: str) -> str:
        number = (container_number or "").strip().upper()
        if not _CONTAINER_NUMBER_RE.match(number):
            raise CarrierUnsupportedReferenceError(
                f"'{container_number}' is not a container number (expected 4 letters and 7 digits).",
                provider_code=PROVIDER_CODE,
            )
        return number
