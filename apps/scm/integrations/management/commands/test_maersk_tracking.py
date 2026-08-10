"""Make one live Maersk Track & Trace call and report what came back.

Read-only: it uses the team's real integration and credentials, prints the event
count and the most recent event, and writes nothing but the ordinary sanitised
IntegrationRequestLog row. The consumer key is never printed.

Usage:
    python manage.py test_maersk_tracking TRDU9258963 --team <team-slug>
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.carriers.exceptions import CarrierError, CarrierNoDataError
from apps.scm.integrations.carriers.factory import build_carrier_client, build_carrier_parser
from apps.scm.integrations.carriers.maersk.client import PROVIDER_CODE
from apps.teams.models import Team


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
    help = "Make one live Maersk Track & Trace call for a container number (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("container_number", help="Container number to look up, e.g. TRDU9258963")
        parser.add_argument("--team", help="Team slug. Optional when the installation has exactly one team.")

    def handle(self, *args, **options):
        team = self._resolve_team(options.get("team"))
        container_number = options["container_number"].strip().upper()

        self.stdout.write(f"Asking Maersk about {container_number} as team '{team.slug}'…")

        # require_integration=True: without the team's own integration there is
        # nothing to call, and a stub client would look like "no data".
        client = self._build_client(team)
        parser = build_carrier_parser(PROVIDER_CODE)

        try:
            payload = client.fetch_tracking(container_number=container_number)
        except CarrierNoDataError:
            self.stdout.write(self.style.WARNING(f"Maersk has no data for {container_number}."))
            return
        except CarrierError as exc:
            # Carrier error messages are already sanitised — no keys, no bodies.
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

        events = parser.parse_tracking_events(payload)
        self.stdout.write(self.style.SUCCESS(f"Maersk returned {len(events)} event(s) for {container_number}."))
        if not events:
            return

        latest = _latest(events)
        self.stdout.write("Latest event:")
        for label, value in (
            ("when", latest.event_datetime),
            ("type", latest.event_type or latest.event_code),
            ("classifier", latest.event_classifier),
            ("location", latest.location_name or latest.location_unlocode),
            ("vessel", latest.vessel_name),
            ("voyage", latest.voyage_number),
            ("description", latest.description),
        ):
            if value:
                self.stdout.write(f"  {label}: {value}")

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

    def _build_client(self, team: Team):
        try:
            return build_carrier_client(PROVIDER_CODE, team=team, require_integration=True)
        except CarrierError as exc:
            raise CommandError(
                f"{exc} Run: python manage.py setup_maersk_integration --team {team.slug}",
            ) from exc
