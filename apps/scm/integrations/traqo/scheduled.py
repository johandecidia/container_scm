"""Refreshing an established Traqo watch on a schedule, as an ordinary tracking provider.

Discovery and ongoing tracking are different problems, and this module only does the
second. By the time a Traqo ``TrackingSubscription`` exists, the question "who is moving
this box" has been answered and recorded on the watch itself::

    provider.code       traqo    who supplies the data
    carrier_code        one      who is moving the box
    carrier_source      ...      how that was established
    provider_reference  ONEY     what this fetch needs

So a poll is one request. It does **not** re-run carrier resolution: no free lookup, no
candidate probe, no direct sweep, and above all no Vizion identification. Re-deriving the
carrier on every cycle would spend real money to be told what the watch already says, and
would let a transient aggregator answer overwrite a carrier that has already proved itself.

What this is *not* is a second Traqo pipeline. It supplies one thing the carrier registry
cannot — a fetch that takes a sealine — and hands the result straight back to the tracking
sync engine as a :class:`~apps.scm.tracking.sync.SyncOutcome`::

    sync.sync_tracking_subscription      the lock
    sync._run_sync                       the run, and the unexpected-error guard
    sources.get_scheduled_provider_sync  → sync_traqo_subscription (here)
    fetch_and_map_traqo_container        one request, the one Traqo mapper
    write_traqo_response                 raw payload, events, deferred ETA observation
    sync.apply_sync_outcome              state, cadence, completion

Everything after the fetch is the code a Maersk poll runs. The run history, the typed
error classification, the failure backoff, the state machine and the polling cadence are
all the engine's, which is why Traqo needs no retry policy, no status and no interval of
its own — and why a manual refresh of the same watch now converges on this exact path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apps.scm.integrations.carriers.exceptions import CarrierError, CarrierNoDataError

from . import PROVIDER_CODE

if TYPE_CHECKING:
    from apps.scm.tracking.models import TrackingSubscription
    from apps.scm.tracking.sync import SyncOutcome

logger = logging.getLogger(__name__)


def sync_traqo_subscription(
    subscription: TrackingSubscription,
    *,
    client=None,
    sandbox: bool = False,
) -> SyncOutcome:
    """Run one Traqo fetch for an existing watch and report the outcome.

    Never raises a carrier error: every one is classified into the same ``SyncOutcome``
    vocabulary a carrier fetch produces, so an expired Traqo key and an expired Maersk key
    back off identically. ``client`` and ``sandbox`` exist for tests and sandbox runs; a
    scheduled poll passes neither and goes live.
    """
    from apps.scm.tracking.models import TrackingSubscription, TrackingSyncRun
    from apps.scm.tracking.sync import SyncOutcome, outcome_for_carrier_error

    statuses = TrackingSyncRun.Status
    error_types = TrackingSyncRun.ErrorType

    container = subscription.container
    if container is None or subscription.reference_type != TrackingSubscription.ReferenceType.CONTAINER_NUMBER:
        # Traqo's container endpoint is the only one this watch could be refreshed from,
        # and it needs a container. Nothing to retry, so this is a configuration gap.
        return SyncOutcome(
            status=statuses.SKIPPED,
            error_type=error_types.NOT_CONFIGURED,
            error_message="A Traqo watch can only be refreshed by container number.",
        )

    sealine = resolve_watch_sealine(subscription)
    if not sealine:
        return SyncOutcome(
            status=statuses.SKIPPED,
            error_type=error_types.NOT_CONFIGURED,
            error_message=(
                "This Traqo watch records no sealine, and none can be derived from its carrier. "
                "Traqo cannot be asked about the container without one."
            ),
        )

    container_number = subscription.tracking_reference.strip().upper()
    if not container_number:
        return SyncOutcome(
            status=statuses.FAILED,
            error_type=error_types.UNSUPPORTED_REFERENCE,
            error_message="Subscription has no tracking reference.",
        )

    from .service import fetch_and_map_traqo_container, write_traqo_response

    try:
        response = fetch_and_map_traqo_container(
            container_number=container_number,
            sealine=sealine,
            sandbox=sandbox,
            client=client,
        )
    except CarrierNoDataError as exc:
        # A real answer: Traqo has no shipment for this container under this sealine right
        # now. It is not grounds for withdrawing the watch, forgetting the carrier or
        # touching the events already stored — the engine treats it as a success with no
        # events, exactly as it does a carrier's 404.
        logger.info(
            "Traqo has no data for %s (%s) — watch %s left as it is.", container_number, sealine, subscription.pk
        )
        return SyncOutcome(
            status=statuses.SUCCESS,
            metadata={"no_data": True, "sealine": sealine, "provider_message": str(exc)},
        )
    except CarrierError as exc:
        logger.warning(
            "Scheduled Traqo sync for %s (%s) failed: %s (%s).",
            container_number,
            sealine,
            type(exc).__name__,
            exc,
        )
        return outcome_for_carrier_error(exc)

    if not response.is_for_requested_container:
        # Traqo answered about a different container. Not "no data" — a payload that
        # cannot be trusted to be about the right box, and must never be mapped onto this
        # one. The same rule the candidate probe applies.
        logger.error(
            "Traqo answered about %s when asked about %s (%s); the payload was not stored.",
            response.reported_reference,
            container_number,
            sealine,
        )
        return SyncOutcome(
            status=statuses.FAILED,
            error_type=error_types.INVALID_RESPONSE,
            error_message=f"Traqo answered about {response.reported_reference}, not {container_number}.",
        )

    write = write_traqo_response(subscription=subscription, container=container, response=response)
    write.outcome.metadata = {
        **write.outcome.metadata,
        "sealine": sealine,
        "events_mapped": len(response.events),
    }
    return write.outcome


def resolve_watch_sealine(subscription: TrackingSubscription) -> str:
    """Return the sealine to ask Traqo with for this watch, or "" when there is none.

    Two sources, in this order, and no third:

    1. ``provider_reference`` — what the watch recorded, and for a probed container the
       sealine Traqo itself *answered* under. Always preferred.
    2. the canonical carrier → sealine mapping, for a watch created before the sealine was
       persisted. Deterministic, and only ever applied to a ``carrier_code`` that is
       already this container's recorded carrier — so it recovers a handle rather than
       deciding anything. Recovered values are written back, once, because a watch that
       needs deriving on every cycle is a watch nobody has actually fixed.

    Deliberately absent: guessing from the container prefix. That is carrier discovery's
    question, and answering it here would make a poll capable of silently re-assigning the
    carrier. A watch with no sealine and no carrier is a configuration problem to report.
    """
    from .sealines import sealine_for_carrier_code

    recorded = (subscription.provider_reference or "").strip().upper()
    if recorded:
        return recorded

    derived = sealine_for_carrier_code(subscription.carrier_code)
    if not derived:
        logger.warning(
            "Traqo watch %s has no sealine and none can be derived from carrier %r.",
            subscription.pk,
            subscription.carrier_code,
        )
        return ""

    from apps.scm.tracking.manual_refresh import record_subscription_carrier

    record_subscription_carrier(subscription, provider_reference=derived)
    logger.info(
        "Recovered Traqo sealine %s for watch %s from its recorded carrier %s.",
        derived,
        subscription.pk,
        subscription.carrier_code,
    )
    return derived


__all__ = ["PROVIDER_CODE", "resolve_watch_sealine", "sync_traqo_subscription"]
