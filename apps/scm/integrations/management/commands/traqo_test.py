"""Prove the Traqo integration end to end against the sandbox, from one command.

Fetches a container from Traqo, stores the response, normalises its events through the
ordinary tracking ingestion path, and prints what reached TrackingEvent — so the whole
chain can be inspected without any UI.

    make manage ARGS='traqo_test MRSU6859427 --sealine MAEU --sandbox'
    make manage ARGS='traqo_test MRSU6859427 --sealine maersk --sandbox --dry-run'
    make manage ARGS='traqo_test MRKU1234567 --sealine MAEU --live'

Sandbox is the default because it is fixed demo data behind no credential. ``--live``
requires TRAQO_ENABLED and TRAQO_API_KEY; the key is never printed.

``--dry-run`` fetches and maps without writing anything, which is the quickest way to
see how a payload would be interpreted.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.carriers.exceptions import CarrierError, CarrierNoDataError
from apps.scm.integrations.traqo import PROVIDER_CODE
from apps.scm.integrations.traqo.mapper import map_traqo_container_payload
from apps.scm.integrations.traqo.sealines import resolve_sealine
from apps.scm.integrations.traqo.service import fetch_traqo_container, ingest_traqo_container
from apps.teams.models import Team


class Command(BaseCommand):
    help = "Fetch one container from Traqo and run it through the tracking ingestion pipeline."

    def add_arguments(self, parser):
        parser.add_argument("container_number", help="Container number, e.g. MRSU6859427")
        parser.add_argument(
            "--sealine",
            required=True,
            help="Carrier for the container: a SCAC (MAEU) or a Container SCM carrier code (maersk).",
        )
        parser.add_argument("--team", help="Team slug. Optional when the installation has exactly one team.")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--sandbox", action="store_true", help="Use the Traqo sandbox (default).")
        mode.add_argument("--live", action="store_true", help="Use the live Traqo API (needs TRAQO_ENABLED).")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and map only; write nothing to the database.",
        )

    def handle(self, *args, **options):
        container_number = options["container_number"].strip().upper()
        sandbox = not options["live"]
        try:
            sealine = resolve_sealine(options["sealine"])
        except CarrierError as exc:
            raise CommandError(str(exc)) from exc

        mode = "sandbox" if sandbox else "live"
        self.stdout.write(f"Asking Traqo ({mode}) about {container_number}, sealine {sealine}…")

        if options["dry_run"]:
            self._dry_run(container_number=container_number, sealine=sealine, sandbox=sandbox)
            return

        team = self._resolve_team(options.get("team"))
        container = self._resolve_container(team, container_number)
        self._ingest(team=team, container=container, sealine=sealine, sandbox=sandbox)

    # ------------------------------------------------------------------
    # Modes
    # ------------------------------------------------------------------

    def _dry_run(self, *, container_number: str, sealine: str, sandbox: bool) -> None:
        payload = self._fetch(
            lambda: fetch_traqo_container(container_number=container_number, sealine=sealine, sandbox=sandbox),
            container_number,
        )
        if payload is None:
            return

        events = map_traqo_container_payload(payload, container_number=container_number)
        data = payload.get("data") or {}
        self.stdout.write(self.style.SUCCESS(f"Traqo returned {len(events)} event(s) — nothing written (--dry-run)."))
        self._write_shipment_summary(data)
        for event in events:
            self._write_normalised_event(event)

    def _ingest(self, *, team, container, sealine: str, sandbox: bool) -> None:
        result = self._fetch(
            lambda: ingest_traqo_container(
                team=team,
                container=container,
                sealine=sealine,
                sandbox=sandbox,
            ),
            container.container_id,
        )
        if result is None:
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Traqo returned {result.events_mapped} event(s): "
                f"{result.events_created} created, {result.events_updated} updated, "
                f"{result.events_failed} failed."
            )
        )
        self.stdout.write(f"Raw payloads stored this run: {result.raw_payloads_created}")
        self.stdout.write(
            f"Subscription #{result.subscription.pk} "
            f"(provider={result.subscription.provider.code}, status={result.subscription.status}, "
            f"tracking_status={result.subscription.tracking_status})"
        )
        self._write_shipment_summary(result.payload.get("data") or {})
        self._write_stored_events(team, container)
        self._write_phase1_note()

    def _fetch(self, action, reference: str):
        """Run a Traqo call, reporting a typed failure instead of a traceback."""
        try:
            return action()
        except CarrierNoDataError:
            self.stdout.write(self.style.WARNING(f"Traqo has no shipment for {reference}."))
            return None
        except CarrierError as exc:
            # Traqo's messages describe the problem and never echo the credential.
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write_shipment_summary(self, data: dict) -> None:
        """Print the shipment-level values Phase 1 keeps in the raw payload only."""
        self.stdout.write("Shipment level (preserved in TrackingRawPayload, not modelled in Phase 1):")
        for label, key in (
            ("reference", "reference_number"),
            ("sealine", "sealine"),
            ("origin", "origin"),
            ("destination", "destination"),
            ("status", "status"),
            ("eta", "eta"),
            ("is_delayed", "is_delayed"),
            ("latitude", "latitude"),
            ("longitude", "longitude"),
            ("last_updated_at", "last_updated_at"),
        ):
            value = data.get(key)
            if value not in (None, ""):
                self.stdout.write(f"  {label}: {value}")

    def _write_normalised_event(self, event) -> None:
        parts = [
            str(event.event_datetime),
            f"{event.event_type}/{event.event_code}",
            event.event_classifier or "unclassified",
            event.location_name or "(no place)",
        ]
        self.stdout.write("  " + " · ".join(parts) + f" — {event.description}")

    def _write_stored_events(self, team, container) -> None:
        """Read the events back through the ordinary selector the timeline uses."""
        from apps.scm.tracking.selectors import get_tracking_events_for_container

        events = list(get_tracking_events_for_container(team, container))
        self.stdout.write(f"TrackingEvents now stored for {container.container_id}: {len(events)}")
        for event in events:
            self.stdout.write(
                f"  [{event.provider.code}] {event.event_datetime} · {event.event_type}"
                f" ({event.event_time_type}) · {event.location_name or '(no place)'}"
                f" · {event.carrier_reference or '—'} — {event.description}"
            )

    def _write_phase1_note(self) -> None:
        self.stdout.write(
            self.style.WARNING(
                "Phase 1: Traqo is not in the carrier registry, so the scheduled poller and the "
                "container refresh button cannot drive this subscription — re-run this command to "
                "refresh it. See apps/scm/integrations/traqo/README.md."
            )
        )

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve_team(self, slug: str | None) -> Team:
        if slug:
            try:
                return Team.objects.get(slug=slug)
            except Team.DoesNotExist as exc:
                raise CommandError(f"No team with slug '{slug}'.") from exc

        teams = list(Team.objects.all()[:2])
        if len(teams) == 1:
            return teams[0]
        if not teams:
            raise CommandError("There are no teams to run this against.")
        raise CommandError("More than one team exists — pass --team <team-slug>.")

    def _resolve_container(self, team: Team, container_number: str):
        """Return the team's container, creating it through the existing linker if new."""
        from django.core.exceptions import ValidationError

        from apps.scm.containers.models import Container
        from apps.scm.containers.utils import parse_container_id
        from apps.scm.integrations.carriers.auto_link import create_or_link_discovered_container
        from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult

        try:
            parts = parse_container_id(container_number)
        except (ValidationError, ValueError) as exc:
            raise CommandError(f"{container_number} is not a valid container number: {exc}") from exc

        existing = Container.objects.filter(
            team=team,
            owner_code=parts["owner_code"],
            category_id=parts["category_id"],
            serial_number=parts["serial_number"],
        ).first()
        if existing is not None:
            return existing

        summary = create_or_link_discovered_container(
            team=team,
            shipment=None,
            result=ContainerDiscoveryResult(
                container_number=container_number,
                carrier_code=PROVIDER_CODE,
                carrier_name="Traqo Ocean",
            ),
        )
        if summary["container"] is None:
            raise CommandError(f"Could not create container {container_number}. An EquipmentType must exist first.")
        self.stdout.write(f"Created container {container_number} for team '{team.slug}'.")
        return summary["container"]
