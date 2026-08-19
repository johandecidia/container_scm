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

Phase 2 adds ``--compare``, which benchmarks Traqo's data against what a direct carrier
integration already stored for the same real container::

    make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live'
    make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live --json --output run.json'

``--compare`` requires an explicit ``--live`` or ``--sandbox``: sandbox returns fixed
demo data for one container whatever you ask about, so comparing real carrier events
against it would measure nothing, and silently choosing it would hide that.
"""

import json
import pathlib

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.carriers.exceptions import CarrierError, CarrierNoDataError
from apps.scm.integrations.traqo import PROVIDER_CODE
from apps.scm.integrations.traqo.benchmark import (
    DEFAULT_TOLERANCE_HOURS,
    REFERENCE_PROVIDER_CODE,
    compare_providers,
    render_json,
    render_text,
)
from apps.scm.integrations.traqo.mapper import map_traqo_container_payload
from apps.scm.integrations.traqo.sealines import resolve_sealine
from apps.scm.integrations.traqo.service import fetch_traqo_container, ingest_traqo_container
from apps.teams.models import Team


class Command(BaseCommand):
    help = "Fetch one container from Traqo, ingest it, and optionally benchmark it against a direct carrier."

    def add_arguments(self, parser):
        parser.add_argument("container_number", help="Container number, e.g. MRSU6859427")
        parser.add_argument(
            "--sealine",
            required=True,
            help="Carrier for the container: a SCAC (MAEU) or a Container SCM carrier code (maersk).",
        )
        parser.add_argument("--team", help="Team slug. Optional when the installation has exactly one team.")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--sandbox", action="store_true", help="Use the Traqo sandbox (default outside --compare).")
        mode.add_argument("--live", action="store_true", help="Use the live Traqo API (needs TRAQO_ENABLED).")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and map only; write nothing to the database.",
        )

        benchmark = parser.add_argument_group("comparison (Phase 2 benchmark)")
        benchmark.add_argument(
            "--compare",
            action="store_true",
            help="Benchmark Traqo against the carrier data already stored for this container.",
        )
        benchmark.add_argument(
            "--reference-provider",
            default=REFERENCE_PROVIDER_CODE,
            help=f"Provider to benchmark against (default: {REFERENCE_PROVIDER_CODE}).",
        )
        benchmark.add_argument(
            "--tolerance-hours",
            type=int,
            default=DEFAULT_TOLERANCE_HOURS,
            help=f"How far apart two reports of one event may be and still match (default: {DEFAULT_TOLERANCE_HOURS}).",
        )
        benchmark.add_argument(
            "--refresh-reference",
            action="store_true",
            help="Refresh the reference provider through its own existing sync first (extra carrier call).",
        )
        benchmark.add_argument(
            "--no-fetch",
            action="store_true",
            help="Compare what is already stored without spending another Traqo request.",
        )
        benchmark.add_argument("--json", action="store_true", help="Print the benchmark result as JSON.")
        benchmark.add_argument("--output", help="Write the benchmark JSON to this path.")
        benchmark.add_argument("--verbose-events", action="store_true", help="Show every field difference per event.")

    def handle(self, *args, **options):
        container_number = options["container_number"].strip().upper()
        try:
            sealine = resolve_sealine(options["sealine"])
        except CarrierError as exc:
            raise CommandError(str(exc)) from exc

        if options["compare"]:
            self._compare(container_number=container_number, sealine=sealine, options=options)
            return

        sandbox = not options["live"]
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

    def _compare(self, *, container_number: str, sealine: str, options: dict) -> None:
        """Benchmark Traqo against a provider that already tracks this real container."""
        if not (options["live"] or options["sandbox"]):
            raise CommandError(
                "--compare needs an explicit mode. Pass --live to benchmark real Traqo data, or "
                "--sandbox to exercise the benchmark machinery on fixed demo data. There is no "
                "default, because sandbox returns the same demo shipment whatever container you ask "
                "about, and comparing real carrier events against it would measure nothing."
            )
        sandbox = options["sandbox"]

        team = self._resolve_team(options.get("team"))
        container = self._require_container(team, container_number)
        reference_provider = options["reference_provider"].strip().lower()
        self._require_reference_data(team, container, reference_provider, options)
        self._announce_comparison(
            container_number=container_number,
            sealine=sealine,
            sandbox=sandbox,
            reference_provider=reference_provider,
            options=options,
        )

        try:
            result = compare_providers(
                team=team,
                container=container,
                sealine=sealine,
                reference_provider_code=reference_provider,
                sandbox=sandbox,
                ingest_candidate=not options["no_fetch"],
                refresh_reference=options["refresh_reference"],
                tolerance_hours=options["tolerance_hours"],
            )
        except CarrierNoDataError:
            raise CommandError(
                f"Traqo has no shipment for {container_number} with sealine {sealine}. "
                "Nothing was written, and no comparison is possible."
            ) from None
        except CarrierError as exc:
            # Already sanitised by the client — no key, no headers, no body.
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

        payload = render_json(result)
        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            self.stdout.write(render_text(result, verbose=options["verbose_events"]))

        if options["output"]:
            path = pathlib.Path(options["output"]).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2))
            self.stdout.write(self.style.SUCCESS(f"Benchmark JSON written to {path}"))

    def _announce_comparison(self, *, container_number, sealine, sandbox, reference_provider, options) -> None:
        """State the side effects before causing any of them."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Benchmark run"))
        self.stdout.write(f"  Container:          {container_number}")
        self.stdout.write(f"  Carrier / sealine:  {sealine}")
        self.stdout.write(f"  Reference provider: {reference_provider}")
        self.stdout.write(f"  Mode:               {'sandbox' if sandbox else 'PRODUCTION'}")
        self.stdout.write(f"  Match tolerance:    ±{options['tolerance_hours']}h")

        if options["no_fetch"]:
            self.stdout.write("  Traqo request:      none (--no-fetch; comparing stored data)")
        elif sandbox:
            self.stdout.write("  Traqo request:      1 sandbox request (fixed demo data, no slot consumed)")
        else:
            self.stdout.write(
                self.style.WARNING("  Traqo request:      1 PRODUCTION request — a Traqo shipment slot may be consumed")
            )
        if options["refresh_reference"]:
            self.stdout.write(f"  Reference refresh:  yes — one extra {reference_provider} carrier call")
        self.stdout.write("")

    def _require_container(self, team: Team, container_number: str):
        """Return the team's existing container, or explain that the benchmark needs one.

        The benchmark measures providers against a real journey, so it never creates a
        container — an empty one would compare nothing against nothing.
        """
        from django.core.exceptions import ValidationError

        from apps.scm.containers.models import Container
        from apps.scm.containers.utils import parse_container_id

        try:
            parts = parse_container_id(container_number)
        except (ValidationError, ValueError) as exc:
            raise CommandError(f"{container_number} is not a valid container number: {exc}") from exc

        container = Container.objects.filter(
            team=team,
            owner_code=parts["owner_code"],
            category_id=parts["category_id"],
            serial_number=parts["serial_number"],
        ).first()
        if container is None:
            raise CommandError(
                f"Team '{team.slug}' has no container {container_number}. The benchmark compares providers "
                "on a container Container SCM already tracks; it does not create one."
            )
        return container

    def _require_reference_data(self, team, container, reference_provider: str, options: dict) -> None:
        """Refuse to spend a production request when there is nothing to compare against."""
        from apps.scm.tracking.models import TrackingEvent

        count = TrackingEvent.objects.filter(team=team, container=container, provider__code=reference_provider).count()
        if count or options["refresh_reference"] or options["no_fetch"]:
            return
        raise CommandError(
            f"No {reference_provider} events are stored for {container.container_id}, so there is nothing to "
            f"benchmark against. Pick a container with {reference_provider} tracking, or pass "
            "--refresh-reference to fetch it first."
        )

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
