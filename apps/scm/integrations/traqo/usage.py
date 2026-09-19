"""What Traqo's account usage response means.

The transport returns an envelope; this turns it into the read model a platform status
page renders. Kept apart from :mod:`.client` for the reason every other parser here is:
a transport must not also be a schema.

The shape, recorded from a live response rather than from prose::

    {"success": true, "mode": "sandbox",
     "data": {"plan": "professional", "period_days": 30,
              "cycle": {"start": "2026-08-01T00:00:00.000Z", "end": "2026-09-01T00:00:00.000Z"},
              "shipments": {"limit": 20, "addon_slots": 5, "effective_limit": 25,
                            "used": 12, "active": 9, "remaining": 13},
              "rate_limit": {"per_minute": 60, "remaining": 60, "reset": 1789800882}}}

**``used`` and ``active`` are different numbers and neither is derivable from the
other.** ``used`` counts distinct references *added* during the cycle and is what
enforcement compares against ``effective_limit``; ``active`` counts the ones still on
the dashboard. Untracking a shipment lowers ``active`` and leaves ``used`` where it is,
because the allowance is an intake budget rather than a ceiling on how many are held at
once. So

    used 25, active 10, remaining 0

is a correct and entirely ordinary state: twenty-five references were added this cycle,
fifteen have since been untracked, and not one slot came back. Anything that treated
``active`` as the spend would report capacity this account does not have.

``carried_slots`` is documented but absent from a monthly-plan response, so it is read
as optional. ``effective_limit`` is taken from the payload rather than recomputed —
Traqo says it is "what enforcement compares against", and deriving our own sum would be
a second opinion on somebody else's billing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# How close to the limit is worth saying something about. Ours, not Traqo's: it is a
# presentation threshold for a status page, and no decision anywhere depends on it.
WARNING_REMAINING_FRACTION = 0.2


def parse_traqo_account_usage(payload: dict):
    """Read a Traqo usage envelope into a :class:`ProviderUsage`.

    Returns None when the payload is not a usage response at all, rather than raising:
    the caller is a status page, and "Traqo answered with something unexpected" is a
    state to render, not an exception to propagate into a superuser's dashboard.
    """
    from apps.scm.tracking.sources import ProviderUsage

    from . import PROVIDER_CODE, PROVIDER_NAME

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        logger.warning("Traqo usage response carried no data object.")
        return None

    shipments = data.get("shipments")
    if not isinstance(shipments, dict):
        logger.warning("Traqo usage response carried no shipments object.")
        return None

    cycle = data.get("cycle") if isinstance(data.get("cycle"), dict) else {}
    return ProviderUsage(
        provider_code=PROVIDER_CODE,
        provider_name=PROVIDER_NAME,
        plan=str(data.get("plan") or ""),
        period_days=_int(data.get("period_days")),
        cycle_start=_timestamp(cycle.get("start")),
        cycle_end=_timestamp(cycle.get("end")),
        limit=_int(shipments.get("limit")),
        addon_slots=_int(shipments.get("addon_slots")),
        # Absent on a monthly plan, and zero there by definition.
        carried_slots=_int(shipments.get("carried_slots")),
        effective_limit=_int(shipments.get("effective_limit")),
        used=_int(shipments.get("used")),
        active=_int(shipments.get("active")),
        remaining=_int(shipments.get("remaining")),
        # Whether these are real numbers or the sandbox's fixed demo ones. Shown, because
        # a status page that cannot tell them apart is worse than no status page.
        sandbox=str(data.get("mode") or payload.get("mode") or "").lower() == "sandbox",
    )


def _int(value) -> int:
    """Read a count, treating anything unreadable as 0 rather than failing the page."""
    try:
        return int(value)
    except TypeError, ValueError:
        return 0


def _timestamp(value):
    """Parse one of Traqo's ISO-8601 cycle bounds, or None.

    Traqo sends ``Z``; :func:`datetime.fromisoformat` handles it from Python 3.11, and
    an unparseable bound leaves the cycle unlabelled rather than breaking the page.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Traqo usage cycle bound %r is not an ISO-8601 datetime.", text)
        return None


@dataclass(frozen=True)
class TraqoUsageFetch:
    """The outcome of asking Traqo about our account, for a page that must always render.

    Three states rather than an exception: the numbers, "Traqo is not configured here",
    and "Traqo was asked and could not answer". A status page has to distinguish those —
    the middle one is a deployment fact and the last one is an incident.
    """

    provider_code: str = ""
    provider_name: str = ""
    usage: object | None = None
    configured: bool = True
    error: str = ""

    @property
    def available(self) -> bool:
        return self.usage is not None


def fetch_traqo_account_usage(*, client=None, sandbox: bool = False) -> TraqoUsageFetch:
    """Ask Traqo about our account, and never raise.

    ``sandbox`` exists so a developer with no key can still see the page working against
    Traqo's fixed demo numbers — which the result marks as such, so they are not mistaken
    for production.
    """
    from apps.scm.integrations.carriers.exceptions import CarrierError

    from . import PROVIDER_CODE, PROVIDER_NAME
    from .client import TraqoClient
    from .discovery import is_traqo_configured

    identity = {"provider_code": PROVIDER_CODE, "provider_name": PROVIDER_NAME}

    if client is None and not sandbox and not is_traqo_configured():
        return TraqoUsageFetch(configured=False, **identity)

    try:
        client = client or TraqoClient.from_settings(sandbox=sandbox)
        payload = client.get_account_usage()
    except CarrierError as exc:
        logger.warning("Traqo account usage could not be read: %s (%s).", type(exc).__name__, exc)
        # The provider's own message. Safe here and only here: this result is rendered
        # on the superuser page and reaches no tenant response.
        return TraqoUsageFetch(error=f"{type(exc).__name__}: {exc}", **identity)

    usage = parse_traqo_account_usage(payload)
    if usage is None:
        return TraqoUsageFetch(error="Traqo returned a usage response this version cannot read.", **identity)
    return TraqoUsageFetch(usage=usage, **identity)
