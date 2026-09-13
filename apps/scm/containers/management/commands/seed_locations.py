"""Create the Göteborg / Oceanterminalen example, for development and demos.

A management command rather than a migration. Canonical locations are a team's own
master data: a migration that inserted them would push one customer's places into
every database the project is ever deployed to, including other tenants' and the
test suite's, and there would be no way to decline. Running a command is a decision
somebody makes for a named team.

What it establishes is the shape LOC-1 exists to support — a port with a terminal
inside it, both under the same UN/LOCODE, and the provider names that have to
resolve to the port:

.. code-block:: text

    Göteborg                      PORT    SEGOT
      └── Oceanterminalen         DEPOT   SEGOT   parent = Göteborg

    traqo     "GOTHENBURG"        ─┐
    maersk    "GOTEBORG"           ├──▶  Göteborg
    cma-cgm   "GOTHENBURG, SE"     │
    unlocode  "SEGOT"             ─┘

The aliases point at the port and not at the terminal, deliberately. "GOTHENBURG"
from a carrier names the city; deciding it means one particular berth is exactly
the false positive the canonical layer exists to prevent, and no seed should teach
the system to make it.

**Coordinates are left unset.** Plausible latitudes and longitudes for these two
places are not established anywhere in this project, and inventing them would put
fabricated data behind the resolver's coordinate rule — where it would be
indistinguishable from surveyed data. Null costs a fallback; a guess costs trust.

Idempotent: run it as often as you like. Existing rows are matched by name within
the team and updated, never duplicated.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.scm.containers.choices import LocationAliasSource, LocationType
from apps.scm.containers.models import ContainerLocation, LocationAlias
from apps.teams.models import Team

_PORT_NAME = "Göteborg"
_TERMINAL_NAME = "Oceanterminalen"
_UNLOCODE = "SEGOT"

# The provider spellings that must reach the port. Each is a mapping somebody has
# decided; none of them is derivable, which is why they are aliases and not rules.
_PORT_ALIASES = (
    ("traqo", "", "GOTHENBURG"),
    ("maersk", "", "GOTEBORG"),
    ("cma-cgm", "", "GOTHENBURG, SE"),
    (LocationAliasSource.UNLOCODE, _UNLOCODE, ""),
)

_TERMINAL_ALIASES = ((LocationAliasSource.INTERNAL, "", _TERMINAL_NAME),)


class Command(BaseCommand):
    help = "Seed the Göteborg / Oceanterminalen canonical locations and their aliases for one team."

    def add_arguments(self, parser):
        parser.add_argument(
            "--team",
            required=True,
            help="Slug of the team to seed. Locations are team-owned; there is no global default.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        slug = options["team"]
        try:
            team = Team.objects.get(slug=slug)
        except Team.DoesNotExist as error:
            raise CommandError(f"No team with slug {slug!r}.") from error

        port = self._upsert_location(
            team,
            name=_PORT_NAME,
            location_type=LocationType.PORT,
            unlocode=_UNLOCODE,
            country_code="SE",
            country="Sweden",
            city="Göteborg",
            timezone="Europe/Stockholm",
        )
        terminal = self._upsert_location(
            team,
            name=_TERMINAL_NAME,
            location_type=LocationType.DEPOT,
            unlocode=_UNLOCODE,
            country_code="SE",
            country="Sweden",
            city="Göteborg",
            timezone="Europe/Stockholm",
            parent_location=port,
        )

        for source, code, name in _PORT_ALIASES:
            self._upsert_alias(team, port, source=source, external_code=code, external_name=name)
        for source, code, name in _TERMINAL_ALIASES:
            self._upsert_alias(team, terminal, source=source, external_code=code, external_name=name)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {port.name} ({port.unlocode}) with {terminal.name} inside it for team {team.slug}."
            )
        )
        self.stdout.write("Coordinates were left unset on purpose — see this command's docstring.")

    def _upsert_location(self, team: Team, *, name: str, **fields) -> ContainerLocation:
        """Create or update one location, matched by name within the team."""
        location = ContainerLocation.objects.filter(team=team, name=name).first()
        if location is None:
            location = ContainerLocation(team=team, name=name)
            action = "Created"
        else:
            action = "Updated"
        for field, value in fields.items():
            setattr(location, field, value)
        location.is_active = True
        location.full_clean()
        location.save()
        self.stdout.write(f"{action} location {location.name}.")
        return location

    def _upsert_alias(
        self, team: Team, location: ContainerLocation, *, source: str, external_code: str, external_name: str
    ) -> None:
        """Point one external name at *location*, moving it if it pointed elsewhere.

        Matched on the identifier the unique constraints use, so re-running after
        the seed's own definitions change re-targets the alias rather than colliding
        with itself.
        """
        lookup = {"team": team, "source": source}
        if external_code:
            lookup["external_code"] = external_code
        else:
            lookup["external_name"] = external_name

        alias = LocationAlias.objects.filter(**lookup).first() or LocationAlias(team=team, source=source)
        alias.location = location
        alias.external_code = external_code
        alias.external_name = external_name
        alias.full_clean()
        alias.save()
        self.stdout.write(f"  {source}: {external_name or external_code} → {location.name}")
