"""Refreshing one container's carrier tracking on demand, from the UI.

This is a thin orchestration over parts that already exist — it adds no transport,
no parser and no second write path. All it does is answer the questions the
scheduled poller never has to ask, and then hand over:

Is this container already tracked?
    A scheduled sync starts from a subscription, which already names its provider.
    A person pressing "Refresh tracking" starts from a container, which may not have
    one yet. When it does, that subscription is a verified tracking source and the
    refresh is an ordinary sync cycle through
    :func:`apps.scm.tracking.sync.sync_tracking_subscription`. No other carrier is
    tried, and a later empty answer or carrier outage never withdraws a subscription
    that has already proved itself — that would throw away a working tracking source
    over a transient blip.

If not, who might know it?
    The user is not asked to name the carrier. Instead:

        unknown container carrier
            → discovery among the team's configured carriers
            → first verified tracking source
            → TrackingSubscription
            → normal tracking sync

    :func:`get_preferred_carrier_codes_for_container` collects the evidence worth
    trying first — the carrier named on the container's shipment, and a carrier
    recorded explicitly for this container number when it was planned — and
    :func:`apps.scm.integrations.carriers.carrier_discovery.discover_carrier_for_container`
    sweeps those and then the team's other connected carriers until one answers with
    data. A strong signal orders the sweep; it does not end it, because a shipment's
    carrier field can be stale or name a forwarder rather than the operator.

Does that carrier actually track this container?
    A candidate carrier is not a tracking source. Asking Maersk about a box does not
    make Maersk its carrier, so an untracked container is *probed* — through the same
    :func:`~apps.scm.integrations.carriers.probe.probe_container_number` that
    planned-container discovery uses — and the ``TrackingSubscription`` is created
    only once a probe comes back with at least one normalised event. No data, an
    outage or a rejected credential all leave the container exactly as it was:
    unassigned, free to be tried against another carrier later.

    Finding tracking data does not touch ``Shipment.carrier`` either. The carrier
    that can tell us where a box is and the carrier a shipment was booked with are
    separate facts, and reconciling them is not this button's job.

Either way the calls run inside :func:`interactive_carrier_requests`, so a slow
carrier cannot hold the web worker for minutes — and because discovery may ask
several carriers, each one is asked exactly once per refresh.

What the user is told is decided here too, from outcomes and error kinds alone.
Carrier error text can carry a response body or an echoed credential, so it is
logged and never rendered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.utils.translation import gettext_lazy as _

from apps.scm.integrations.carriers.http import interactive_carrier_requests

from .models import TrackingSubscription, TrackingSyncRun
from .services import create_sync_run
from .sync import apply_sync_outcome, store_verified_carrier_result, sync_tracking_subscription

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from apps.scm.integrations.carriers.carrier_discovery import CarrierDiscoveryOutcome

logger = logging.getLogger(__name__)

# Message levels, matching django.contrib.messages' function names. Kept as plain
# strings so this module stays independent of the messages framework.
SUCCESS = "success"
INFO = "info"
WARNING = "warning"
ERROR = "error"

# What happened, as a value the panel can branch on without parsing the message.
UPDATED = "updated"
NO_DATA = "no_data"
NOT_CONFIGURED = "not_configured"
# Nothing could even be asked: the team has no carrier able to answer by container
# number. Not "we do not know the carrier" — that is what discovery is for.
CARRIER_UNKNOWN = "carrier_unknown"
UNAVAILABLE = "unavailable"
IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class RefreshResult:
    """What one manual refresh achieved, ready to show to the person who asked."""

    level: str
    # Usually lazily translated, so the locale active when it is rendered wins.
    message: StrOrPromise
    state: str = UPDATED
    carrier_code: str = ""
    carrier_name: str = ""
    events_created: int = 0
    events_updated: int = 0
    sync_run: TrackingSyncRun | None = None
    # Whether the container has a verified tracking source after this refresh.
    # ``carrier_name`` alone cannot say: it names the carrier that was *asked*.
    tracked: bool = False
    # The carriers this refresh actually reached, for a compact "we checked these"
    # line. Empty on the fast path, where only the verified carrier is asked.
    carriers_checked: tuple[str, ...] = field(default_factory=tuple)

    @property
    def events_seen(self) -> int:
        return self.events_created + self.events_updated


def get_preferred_carrier_codes_for_container(team, container) -> list[str]:
    """Return the carriers worth asking about ``container`` first, strongest first.

    These order discovery; they do not limit it. Each is a signal that somebody
    believes a carrier is involved — not evidence that it can tell us where the box
    is — so a NOT_FOUND here is a reason to keep sweeping, not to stop.

    The ISO 6346 owner prefix is deliberately absent: it names who owns the box, not
    who is moving it. Discovery adds it as a tie-breaker below these signals.
    """
    from apps.scm.integrations.carriers.registry import resolve_carrier_code

    codes: list[str] = []

    def add(value: str) -> None:
        code = resolve_carrier_code(value)
        if code and code not in codes:
            codes.append(code)

    # 1. The carrier named on the shipment the container is travelling on.
    from apps.scm.shipments.models import ShipmentContainer

    link = (
        ShipmentContainer.objects.filter(container=container, shipment__team=team)
        .select_related("shipment")
        .order_by("-created_at")
        .first()
    )
    if link is not None:
        add(link.shipment.carrier)

    # 2. A carrier recorded explicitly for this container number when it was planned.
    #    Someone chose it; that outranks anything the system could infer.
    from apps.scm.containers.models import PlannedContainer

    planned = (
        PlannedContainer.objects.filter(team=team, container_number=container.container_id)
        .exclude(carrier="")
        .order_by("-created_at")
        .first()
    )
    if planned is not None:
        add(planned.carrier)

    return codes


def get_verified_container_subscription(*, team, container):
    """Return this container's established subscription, or None.

    "Established" means it exists and has not been cancelled — every subscription is
    created from carrier data, so the presence of one *is* the record that a carrier
    tracks this container, and which. None means no carrier has proved itself yet
    and the container is open to discovery.
    """
    return (
        TrackingSubscription.objects.filter(team=team, container=container)
        .exclude(status=TrackingSubscription.Status.CANCELLED)
        .select_related("provider", "shipment")
        .order_by("-created_at")
        .first()
    )


def get_or_create_container_subscription(*, team, container, carrier_code: str, carrier_name: str = ""):
    """Start tracking this container with this carrier, or return the existing watch.

    Only ever called once the carrier has returned tracking data for the container:
    a subscription is an assertion that this carrier is a verified tracking source,
    not a note of who we intend to ask.

    Matches what container discovery creates, on the same natural key, so a container
    that discovery later finds does not end up with two watches.
    """
    from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider
    from apps.scm.shipments.models import ShipmentContainer

    provider = get_or_create_tracking_provider(carrier_code=carrier_code, carrier_name=carrier_name or carrier_code)
    if provider is None:
        return None

    link = (
        ShipmentContainer.objects.filter(container=container, shipment__team=team)
        .select_related("shipment")
        .order_by("-created_at")
        .first()
    )
    subscription, created = TrackingSubscription.objects.get_or_create(
        team=team,
        provider=provider,
        container=container,
        reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
        defaults={
            "shipment": link.shipment if link else None,
            "tracking_reference": container.container_id,
            "status": TrackingSubscription.Status.ACTIVE,
        },
    )
    if created:
        logger.info(
            "Created TrackingSubscription for container %s: provider %s returned tracking data for it.",
            container.container_id,
            provider.code,
        )
    return subscription


def refresh_container_tracking(*, team, container) -> RefreshResult:
    """Fetch this container's tracking now, and report the result.

    Runs in the caller's thread so the person who pressed the button sees the real
    outcome rather than "queued". No carrier failure escapes as an exception — both
    the sync engine and the probe classify every one of them — so the result is
    always something the UI can show.
    """
    subscription = get_verified_container_subscription(team=team, container=container)
    if subscription is not None:
        return _sync_verified_subscription(subscription)
    return _discover_and_activate(team=team, container=container)


def _sync_verified_subscription(subscription: TrackingSubscription) -> RefreshResult:
    """Refresh a container that already has a proven tracking source.

    No other carrier is tried: the question "who tracks this box" is settled, and
    re-opening it on every refresh would spend the team's rate limits re-proving it.
    """
    from apps.scm.integrations.carriers.registry import (
        UnknownCarrierError,
        get_carrier_definition,
        resolve_carrier_code,
    )

    carrier_code = resolve_carrier_code(subscription.provider.code) or subscription.provider.code
    try:
        carrier_name = get_carrier_definition(carrier_code).name
    except UnknownCarrierError:
        carrier_name = subscription.provider.name or carrier_code

    # Bound the call: someone is waiting for this response.
    with interactive_carrier_requests():
        sync_run = sync_tracking_subscription(subscription)

    if sync_run is None:
        return RefreshResult(
            level=INFO,
            state=IN_PROGRESS,
            carrier_code=carrier_code,
            carrier_name=carrier_name,
            tracked=True,
            message=_("A tracking refresh for this container is already running."),
        )
    return _describe(
        sync_run,
        carrier_name=carrier_name,
        carrier_code=carrier_code,
        reference=subscription.tracking_reference,
        tracked=True,
    )


def _discover_and_activate(*, team, container) -> RefreshResult:
    """Find a carrier that knows this container, and make it its tracking source.

    The subscription is created between the two halves of this function, and only
    there: everything above it can leave the container unassigned, everything below
    it runs because a carrier produced events. A carrier that has nothing, is down
    or rejects our credentials is therefore never recorded as a tracking source, and
    the same container can be tried against a different carrier tomorrow.

    The attempts themselves are not lost — each HTTP call is in IntegrationRequestLog,
    and the sweep is logged by the discovery service against the container number.
    """
    from apps.scm.integrations.carriers.carrier_discovery import discover_carrier_for_container

    reference = container.container_id
    preferred = get_preferred_carrier_codes_for_container(team, container)

    with interactive_carrier_requests():
        outcome = discover_carrier_for_container(
            team=team,
            container_number=reference,
            preferred_carrier_codes=preferred,
        )

    if not outcome.found:
        return _describe_no_match(outcome)

    # Verified: this carrier knows the container, so it becomes its tracking source.
    # Nothing else about the container changes — in particular the shipment keeps
    # whatever carrier it was booked with.
    common: dict[str, Any] = {
        "carrier_code": outcome.carrier_code,
        "carrier_name": outcome.carrier_name,
        "carriers_checked": tuple(outcome.carrier_names(outcome.answered)),
    }
    subscription = get_or_create_container_subscription(
        team=team,
        container=container,
        carrier_code=outcome.carrier_code,
        carrier_name=outcome.carrier_name,
    )
    if subscription is None:
        return RefreshResult(
            level=ERROR,
            state=NOT_CONFIGURED,
            message=_("Could not start tracking %(reference)s.") % {"reference": reference},
            **common,
        )

    sync_run = create_sync_run(team=team, subscription=subscription, provider=subscription.provider)
    sync_outcome = store_verified_carrier_result(
        subscription,
        raw_payload=outcome.raw_payload,
        events=outcome.events,
    )
    apply_sync_outcome(subscription, sync_run, sync_outcome)
    return _describe(
        sync_run,
        carrier_name=outcome.carrier_name,
        carrier_code=outcome.carrier_code,
        reference=reference,
        tracked=True,
        discovered=True,
        carriers_checked=common["carriers_checked"],
    )


def _describe_no_match(outcome: CarrierDiscoveryOutcome) -> RefreshResult:
    """Explain a sweep that found nothing, without exposing carrier internals.

    Three different situations hide behind "no tracking": nobody has the box, nobody
    could be asked, and nobody answered. They need different advice, so they get
    different states even though the message stays to one line.
    """
    answered = outcome.answered
    checked = tuple(outcome.carrier_names(answered))
    common: dict[str, Any] = {"carriers_checked": checked}

    if not outcome.attempts:
        # Not "we cannot work out the carrier" — there is nothing connected that can
        # answer a question about a container number at all.
        return RefreshResult(
            level=ERROR,
            state=CARRIER_UNKNOWN,
            message=_("No carrier integration is connected that can be asked about this container."),
            **common,
        )

    if not answered:
        # Everything was skipped: not connected, or a stub adapter.
        return RefreshResult(
            level=WARNING,
            state=NOT_CONFIGURED,
            message=_not_configured_message(outcome),
            **common,
        )

    if not outcome.not_found and outcome.errored:
        # Every carrier we reached failed technically. That is not "no data".
        return RefreshResult(
            level=ERROR,
            state=UNAVAILABLE,
            message=_unavailable_message(outcome),
            **common,
        )

    return RefreshResult(
        # A carrier we could not reach might have been the one with the data, so a
        # partial sweep is not the clean "nobody has it" an all-answered sweep is.
        level=WARNING if (outcome.errored or outcome.skipped) else INFO,
        state=NO_DATA,
        message=_no_data_message(outcome),
        **common,
    )


def _not_configured_message(outcome: CarrierDiscoveryOutcome) -> StrOrPromise:
    """Nothing could be asked. Name the carrier only when there is exactly one."""
    base = _("Tracking is not configured for this container.")
    names = outcome.carrier_names(outcome.skipped)
    if len(names) == 1:
        return _("%(base)s %(carrier)s is not connected for this team yet.") % {"base": base, "carrier": names[0]}
    return base


def _unavailable_message(outcome: CarrierDiscoveryOutcome) -> StrOrPromise:
    """Every carrier reached failed. Say so plainly; the detail is in the log."""
    names = outcome.carrier_names(outcome.errored)
    if len(names) == 1:
        return _failure_message(outcome.errored[0].error_kind, names[0])
    return _("Tracking is temporarily unavailable. None of the %(count)s carriers checked could be reached.") % {
        "count": len(names)
    }


def _no_data_message(outcome: CarrierDiscoveryOutcome) -> StrOrPromise:
    """No carrier has this container. Say how widely we looked."""
    answered = outcome.answered
    names = outcome.carrier_names(answered)
    if len(names) == 1:
        message = _(
            "No tracking data found at %(carrier)s. "
            "The carrier has not been assigned as a tracking source for this container."
        ) % {"carrier": names[0]}
    else:
        message = _("No tracking data found. Checked %(count)s carriers: %(carriers)s.") % {
            "count": len(names),
            "carriers": _join_names(names),
        }

    unreached = len(outcome.errored) + len(outcome.skipped)
    if unreached:
        message = _("%(message)s %(count)s further carrier(s) could not be checked.") % {
            "message": message,
            "count": unreached,
        }
    return message


def _join_names(names: list[str]) -> str:
    """Join carrier names for one compact line: "Maersk, CMA CGM and COSCO Shipping"."""
    if len(names) < 2:
        return "".join(names)
    return _("%(list)s and %(last)s") % {"list": ", ".join(names[:-1]), "last": names[-1]}


def _describe(
    sync_run: TrackingSyncRun,
    *,
    carrier_name: str,
    carrier_code: str,
    reference: str,
    tracked: bool,
    discovered: bool = False,
    carriers_checked: tuple[str, ...] = (),
) -> RefreshResult:
    """Turn a finished sync run into something worth reading.

    The wording comes from the run's status and error type only. ``error_message``
    can carry a carrier response body — it belongs in the log, not on the page.
    ``discovered`` says the carrier was just found rather than already known, which
    is worth telling the user once.
    """
    statuses = TrackingSyncRun.Status
    common: dict[str, Any] = {
        "carrier_code": carrier_code,
        "carrier_name": carrier_name,
        "events_created": sync_run.events_created,
        "events_updated": sync_run.events_updated,
        "sync_run": sync_run,
        "tracked": tracked,
        "carriers_checked": carriers_checked,
    }

    if sync_run.status in (statuses.SKIPPED, statuses.FAILED):
        _log_technical_failure(sync_run, carrier_code=carrier_code, reference=reference)

    if sync_run.status == statuses.SKIPPED:
        return RefreshResult(
            level=WARNING,
            state=NOT_CONFIGURED,
            message=_("Tracking is not configured for this container."),
            **common,
        )

    if sync_run.status == statuses.FAILED:
        return RefreshResult(
            level=ERROR,
            state=_failure_state(sync_run.error_type),
            message=_failure_message(sync_run.error_type, carrier_name),
            **common,
        )

    total = sync_run.events_created + sync_run.events_updated
    if not total:
        if sync_run.status == statuses.PARTIAL_SUCCESS:
            # Events arrived but none of them could be stored — not "no data".
            return RefreshResult(
                level=ERROR,
                state=UNAVAILABLE,
                message=_("%(carrier)s tracking is temporarily unavailable.") % {"carrier": carrier_name},
                **common,
            )
        # The carrier answered and has nothing for this reference — a real answer.
        # An established watch survives it: a carrier that goes quiet for one call
        # has not stopped being this container's carrier.
        return RefreshResult(
            level=INFO,
            state=NO_DATA,
            message=_("No tracking data found at %(carrier)s for this container.") % {"carrier": carrier_name},
            **common,
        )

    if discovered:
        message = _("Tracking found via %(carrier)s. %(total)s tracking events retrieved.") % {
            "carrier": carrier_name,
            "total": total,
        }
    else:
        message = _("Tracking updated — %(total)s events received · %(created)s new · %(updated)s unchanged.") % {
            "total": total,
            "created": sync_run.events_created,
            "updated": sync_run.events_updated,
        }
    if sync_run.status == statuses.PARTIAL_SUCCESS:
        return RefreshResult(
            level=WARNING,
            state=UPDATED,
            message=_("%(summary)s Some events could not be stored.") % {"summary": message},
            **common,
        )
    return RefreshResult(level=SUCCESS, state=UPDATED, message=message, **common)


def _failure_state(error_type: str) -> str:
    """A configuration problem and an outage need different advice, so keep them apart."""
    errors = TrackingSyncRun.ErrorType
    if error_type in (errors.NOT_CONFIGURED, errors.NOT_IMPLEMENTED, errors.UNSUPPORTED_REFERENCE):
        return NOT_CONFIGURED
    return UNAVAILABLE


def _failure_message(error_type: str, carrier_name: str) -> StrOrPromise:
    errors = TrackingSyncRun.ErrorType
    if _failure_state(error_type) == NOT_CONFIGURED:
        return _("Tracking is not configured for this container.")
    if error_type == errors.RATE_LIMIT:
        return _("%(carrier)s is rate limiting us right now. Try again in a few minutes.") % {"carrier": carrier_name}
    # Authentication failures are deliberately not spelled out to the user: the fix
    # is an admin task, and the distinction only helps someone probing the setup.
    return _("%(carrier)s tracking is temporarily unavailable.") % {"carrier": carrier_name}


def _log_technical_failure(sync_run: TrackingSyncRun, *, carrier_code: str, reference: str) -> None:
    """Keep the detail the user is not shown, where support can find it."""
    logger.warning(
        "Manual tracking refresh %s for %s/%s: error_type=%s detail=%s",
        sync_run.status,
        carrier_code,
        reference,
        sync_run.error_type or "none",
        sync_run.error_message or "(none)",
    )
