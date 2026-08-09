"""Re-run stored carrier responses through the current parser and ingestion.

Every carrier response is kept as a :class:`TrackingRawPayload`, which means a parser
improvement is not only for events that arrive from now on: the payloads we already
have can be read again with the better parser and the existing events filled in.
That is what this command is for. It makes no carrier calls.

It is safe to run repeatedly. Events are written through the same idempotent path a
sync uses — :func:`apps.scm.tracking.ingestion.persist_normalised_events` — so an
event the carrier gave an ID for keeps its fingerprint and is updated in place rather
than duplicated.

The one thing to know before running it: an event whose carrier response carried *no*
event ID is fingerprinted from its identifying fields, and those fields now include
location and vessel where they used to be blank. Such an event gets a new fingerprint
and is therefore re-created rather than updated, leaving the old, emptier row behind.
``--prune-superseded`` deletes those, and the command refuses to leave them silently:
without the flag it counts them and says so.

Usage:
    python manage.py reparse_tracking_payloads --provider maersk --container TRDU9258963
    python manage.py reparse_tracking_payloads --provider maersk --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.scm.integrations.carriers.factory import build_carrier_parser
from apps.scm.integrations.carriers.registry import UnknownCarrierError
from apps.scm.tracking.ingestion import persist_normalised_events
from apps.scm.tracking.models import TrackingEvent, TrackingRawPayload
from apps.teams.models import Team


class Command(BaseCommand):
    help = "Re-parse stored carrier payloads and update the events derived from them."

    def add_arguments(self, parser):
        parser.add_argument("--provider", required=True, help="Tracking provider code, e.g. maersk")
        parser.add_argument("--container", help="Limit to payloads for one container number")
        parser.add_argument("--team", help="Team slug. Optional; defaults to every team.")
        parser.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing")
        parser.add_argument(
            "--prune-superseded",
            action="store_true",
            help="Delete events replaced by a re-parse (only affects carrier events with no event ID)",
        )

    def handle(self, *args, **options):
        provider_code = options["provider"].strip().lower()
        dry_run = options["dry_run"]

        try:
            parser = build_carrier_parser(provider_code)
        except UnknownCarrierError as exc:
            raise CommandError(str(exc)) from exc

        payloads = self._payloads(provider_code, options)
        if not payloads:
            self.stdout.write(self.style.WARNING("No stored payloads match those filters."))
            return

        self.stdout.write(f"Re-parsing {len(payloads)} stored payload(s) for '{provider_code}'…")

        totals = {"created": 0, "updated": 0, "failed": 0, "events": 0, "unreadable": 0}
        touched_subscription_ids = set()

        for payload in payloads:
            try:
                events = parser.parse_tracking_events(payload.payload_json)
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
            if subscription is not None:
                touched_subscription_ids.add(subscription.pk)

        self._report(totals, dry_run=dry_run)
        if not dry_run:
            self._handle_superseded(provider_code, options, touched_subscription_ids)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

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
        reference = payload.subscription.tracking_reference if payload.subscription_id else "—"
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

    def _handle_superseded(self, provider_code: str, options, subscription_ids: set) -> None:
        """Report — and optionally remove — rows a re-parse replaced rather than updated.

        Only events with no carrier event ID can end up here, because only their
        fingerprint depends on the fields the parser just started filling in. They are
        recognised by having been created by an earlier ingestion and not re-linked to
        any payload this run touched.
        """
        if not subscription_ids:
            return

        superseded = TrackingEvent.objects.filter(
            provider__code=provider_code,
            subscription_id__in=subscription_ids,
            source_event_id="",
            raw_payload__isnull=True,
        )
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
