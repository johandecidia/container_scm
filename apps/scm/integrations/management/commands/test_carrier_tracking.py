"""Make one live carrier Track & Trace call and report what came back.

Read-only, and carrier-neutral: it uses the team's real integration and credentials
for whichever provider is named, prints the event count and the most recent event, and
writes nothing but the ordinary sanitised IntegrationRequestLog row. Credentials are
never printed.

Usage:
    python manage.py test_carrier_tracking CMAU1234567 --provider cma_cgm --team <team-slug>
    python manage.py test_carrier_tracking MAEU-BL-1 --provider maersk --by bl --team <team-slug>
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.carriers.exceptions import CarrierError, CarrierNoDataError
from apps.scm.integrations.carriers.factory import build_carrier_client, build_carrier_parser
from apps.scm.integrations.carriers.registry import UnknownCarrierError, get_carrier_definition
from apps.teams.models import Team

# Which fetch_tracking keyword each --by choice maps to.
REFERENCE_KINDS = {
    "container": "container_number",
    "bl": "bill_of_lading_number",
    "booking": "booking_number",
}


def _latest(events: list):
    """Return the most recent event, falling back to the last one the carrier listed.

    Carrier timestamps are not guaranteed to be present, nor uniformly offset-aware,
    so an unorderable set is reported in carrier order rather than crashing a
    diagnostic command.
    """
    dated = [event for event in events if event.event_datetime is not None]
    try:
        return max(dated, key=lambda event: event.event_datetime) if dated else events[-1]
    except TypeError:
        return events[-1]


class Command(BaseCommand):
    help = "Make one live carrier Track & Trace call for a reference (read-only)."

    # Subclasses may fix the carrier, so a per-carrier alias needs no logic of its own.
    provider_code: str = ""

    def add_arguments(self, parser):
        parser.add_argument("reference", help="Reference to look up, e.g. a container number")
        if not self.provider_code:
            parser.add_argument("--provider", required=True, help="Carrier provider code, e.g. cma_cgm")
        parser.add_argument("--team", help="Team slug. Optional when the installation has exactly one team.")
        parser.add_argument(
            "--by",
            choices=sorted(REFERENCE_KINDS),
            default="container",
            help="Which kind of reference was given (default: container).",
        )

    def handle(self, *args, **options):
        provider_code = self.provider_code or options["provider"]
        carrier_name = self._carrier_name(provider_code)
        team = self._resolve_team(options.get("team"))
        reference = options["reference"].strip().upper()
        reference_kind = REFERENCE_KINDS[options["by"]]

        self.stdout.write(f"Asking {carrier_name} about {reference} as team '{team.slug}'…")

        # require_integration=True: without the team's own integration there is
        # nothing to call, and a stub client would look like "no data".
        client = self._build_client(provider_code, team)
        parser = build_carrier_parser(provider_code)

        try:
            payload = client.fetch_tracking(**{reference_kind: reference})
        except CarrierNoDataError:
            self.stdout.write(self.style.WARNING(f"{carrier_name} has no data for {reference}."))
            return
        except CarrierError as exc:
            # Carrier error messages are already sanitised — no keys, no bodies.
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

        events = parser.parse_tracking_events(payload)
        self.stdout.write(self.style.SUCCESS(f"{carrier_name} returned {len(events)} event(s) for {reference}."))
        if not events:
            return

        first = events[0]
        self.stdout.write(f"First event: {first.event_type or first.event_code} at {first.event_datetime}")

        latest = _latest(events)
        self.stdout.write("Latest event:")
        for label, value in (
            ("when", latest.event_datetime),
            ("type", latest.event_type or latest.event_code),
            ("code", latest.event_code),
            ("classifier", latest.event_classifier),
            ("location", latest.location_name or latest.location_unlocode),
            ("vessel", latest.vessel_name),
            ("voyage", latest.voyage_number),
            ("description", latest.description),
        ):
            if value:
                self.stdout.write(f"  {label}: {value}")

    def _carrier_name(self, provider_code: str) -> str:
        try:
            return get_carrier_definition(provider_code).name
        except UnknownCarrierError as exc:
            raise CommandError(str(exc)) from exc

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

    def _build_client(self, provider_code: str, team: Team):
        try:
            return build_carrier_client(provider_code, team=team, require_integration=True)
        except CarrierError as exc:
            raise CommandError(
                f"{exc} Run: python manage.py setup_{provider_code}_integration --team {team.slug}",
            ) from exc
