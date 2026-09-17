"""Re-run stored provider responses through the current parser and ingestion.

Every provider response is kept as a :class:`TrackingRawPayload`, which means a parser
improvement is not only for events that arrive from now on: the payloads we already
have can be read again with the better parser and the existing events filled in.
That is what this command is for. It makes no provider calls, so correcting how a
response is read costs nothing and consumes no quota.

It is safe to run repeatedly. Events are written through the same idempotent path a
sync uses — :func:`apps.scm.tracking.ingestion.persist_normalised_events` — so an
event the provider gave an ID for keeps its fingerprint and is updated in place rather
than duplicated.

The one thing to know before running it: an event whose response carried *no* event ID
is fingerprinted from its identifying fields, so a correction to any of those fields —
a filled-in location, a timestamp read in the right timezone — produces a new
fingerprint. Such an event is re-created rather than updated, leaving the old row
behind. ``--prune-superseded`` deletes those, and the command refuses to leave them
silently: without the flag it counts them and says so.

Non-carrier providers work here too. Traqo's responses are stored by the same
mechanism, and a stored one is the only way to re-read a container without spending a
Traqo shipment slot, so the provider is resolved through
:mod:`apps.scm.tracking.sources` when the carrier registry does not know it.

Usage:
    python manage.py reparse_tracking_payloads --provider maersk --container TRDU9258963
    python manage.py reparse_tracking_payloads --provider maersk --dry-run
    python manage.py reparse_tracking_payloads --provider traqo --container CPWU2588297
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.scm.integrations.carriers.factory import build_carrier_parser
from apps.scm.integrations.carriers.registry import UnknownCarrierError
from apps.scm.tracking.ingestion import persist_normalised_events
from apps.scm.tracking.models import TrackingEvent, TrackingRawPayload
from apps.scm.tracking.sources import get_non_carrier_source
from apps.teams.models import Team


class Command(BaseCommand):
    help = "Re-parse stored provider payloads and update the events derived from them."

    def add_arguments(self, parser):
        parser.add_argument("--provider", required=True, help="Tracking provider code, e.g. maersk")
        parser.add_argument("--container", help="Limit to payloads for one container number")
        parser.add_argument("--team", help="Team slug. Optional; defaults to every team.")
        parser.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing")
        parser.add_argument(
            "--prune-superseded",
            action="store_true",
            help="Delete events replaced by a re-parse (only affects events the provider gave no event ID)",
        )

    def handle(self, *args, **options):
        provider_code = options["provider"].strip().lower()
        dry_run = options["dry_run"]

        read_payload = self._reader(provider_code)
        payloads = self._payloads(provider_code, options)
        if not payloads:
            self.stdout.write(self.style.WARNING("No stored payloads match those filters."))
            return

        self.stdout.write(f"Re-parsing {len(payloads)} stored payload(s) for '{provider_code}'…")

        totals = {"created": 0, "updated": 0, "failed": 0, "events": 0, "unreadable": 0}
        touched_subscription_ids = set()
        reread_payload_ids = set()
        written_fingerprints = set()

        for payload in payloads:
            reference = payload.subscription.tracking_reference if payload.subscription else ""
            try:
                events = read_payload(payload.payload_json, reference)
            except Exception as exc:  # noqa: BLE001 — one bad payload must not stop the backfill
                totals["unreadable"] += 1
                self.stderr.write(self.style.WARNING(f"  payload {payload.pk}: could not parse — {exc}"))
                continue

            totals["events"] += len(events)
            if dry_run:
                self._report_dry_run(payload, events)
                continue

            subscription = payload.subscription
            result = persist_normalised_events(
                team=payload.team,
                provider=payload.provider,
                events=events,
                subscription=subscription,
                shipment=subscription.shipment if subscription else None,
                container=subscription.container if subscription else None,
                raw_payload=payload,
            )
            for key in ("created", "updated", "failed"):
                totals[key] += result[key]
            written_fingerprints.update(result["fingerprints"])
            reread_payload_ids.add(payload.pk)
            if subscription is not None:
                touched_subscription_ids.add(subscription.pk)

        self._report(totals, dry_run=dry_run)
        if not dry_run:
            self._handle_superseded(
                provider_code,
                options,
                subscription_ids=touched_subscription_ids,
                reread_payload_ids=reread_payload_ids,
                written_fingerprints=written_fingerprints,
            )

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _reader(self, provider_code: str):
        """Return ``(payload_json, reference) -> events`` for this provider.

        Carriers come from the registry. A provider the registry does not know may still
        be a legitimate non-carrier source with a mapper of its own; only a code that is
        neither is an error.
        """
        try:
            parser = build_carrier_parser(provider_code)
        except UnknownCarrierError as exc:
            source = get_non_carrier_source(provider_code)
            if source is None:
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"'{provider_code}' is {source.name}, not a carrier — reading it with its own mapper.")
            return source.read_payload
        return lambda payload_json, _reference: parser.parse_tracking_events(payload_json)

    def _payloads(self, provider_code: str, options) -> list[TrackingRawPayload]:
        """Return the payloads to re-read, oldest first.

        Oldest first so that when two payloads describe the same event, the newest
        answer is the one that lands last — the same order a sequence of syncs would
        have applied them in.

        Archived payloads are skipped: retention has dropped their body, so there is
        nothing left to re-parse and attempting it would only log noise.
        """
        queryset = (
            TrackingRawPayload.objects.filter(provider__code=provider_code, archived_at__isnull=True)
            .exclude(payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE)
            .select_related("team", "provider", "subscription", "subscription__shipment", "subscription__container")
            .order_by("received_at", "pk")
        )

        team_slug = options.get("team")
        if team_slug:
            try:
                team = Team.objects.get(slug=team_slug)
            except Team.DoesNotExist as exc:
                raise CommandError(f"No team with slug '{team_slug}'.") from exc
            queryset = queryset.filter(team=team)

        container_number = options.get("container")
        if container_number:
            reference = container_number.strip().upper()
            queryset = queryset.filter(
                Q(subscription__tracking_reference__iexact=reference)
                | Q(subscription__container__isnull=False, events__equipment_reference__iexact=reference)
            ).distinct()

        return list(queryset)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _report_dry_run(self, payload: TrackingRawPayload, events: list) -> None:
        reference = payload.subscription.tracking_reference if payload.subscription else "—"
        self.stdout.write(f"  payload {payload.pk} ({reference}): {len(events)} event(s)")
        for event in events:
            place = event.location_unlocode or event.location_name or "—"
            vessel = f" {event.vessel_name} {event.voyage_number}".rstrip() if event.vessel_name else ""
            self.stdout.write(
                f"    {event.event_datetime} {event.event_type}/{event.event_code} "
                f"[{event.event_classifier}] {place}{vessel}"
            )

    def _report(self, totals: dict, *, dry_run: bool) -> None:
        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry run: {totals['events']} event(s) would be written."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done: {totals['created']} created, {totals['updated']} updated, {totals['failed']} failed."
                )
            )
        if totals["unreadable"]:
            self.stdout.write(self.style.WARNING(f"{totals['unreadable']} payload(s) could not be parsed."))

    # ------------------------------------------------------------------
    # Superseded events
    # ------------------------------------------------------------------

    def _handle_superseded(
        self,
        provider_code: str,
        options,
        *,
        subscription_ids: set,
        reread_payload_ids: set,
        written_fingerprints: set,
    ) -> None:
        """Report — and optionally remove — rows a re-parse replaced rather than updated.

        Only events with no provider event ID can end up here, because only their
        fingerprint depends on the fields the parser just read differently. An event
        qualifies when this run did **not** write its fingerprint and it came from a
        payload this run re-read — the payload that should have produced it did not, so
        the corrected row now stands in its place. Events with no payload link at all
        predate payload linking and are treated the same way, as before.

        The payload condition is what keeps the deletion honest: an event derived from
        an archived payload, whose body retention has dropped and which therefore could
        not be regenerated, is never a candidate.
        """
        if not subscription_ids:
            return

        superseded = TrackingEvent.objects.filter(
            Q(raw_payload__isnull=True) | Q(raw_payload_id__in=reread_payload_ids),
            provider__code=provider_code,
            subscription_id__in=subscription_ids,
            source_event_id="",
        ).exclude(event_fingerprint__in=written_fingerprints)
        count = superseded.count()
        if not count:
            return

        if options["prune_superseded"]:
            superseded.delete()
            self.stdout.write(self.style.SUCCESS(f"Removed {count} event(s) superseded by the re-parse."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"{count} event(s) were replaced rather than updated because the carrier gave them no "
                    f"event ID. Re-run with --prune-superseded to remove the old rows."
                )
            )
