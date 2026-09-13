"""The location resolver: what it will decide, and what it refuses to.

The refusals matter more than the successes, so most of this file is about them.
A resolver that guesses is worse than one that shrugs: an unresolved location is a
visible gap somebody can close by recording an alias, whereas a shipment credited to
the wrong terminal is a wrong answer with nothing in the data to contradict it.

So the assertions here are largely of the form "this is ambiguous" and "this stays
unresolved", including for inputs a fuzzy matcher would happily resolve.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.scm.containers.choices import LocationResolutionMethod, LocationResolutionStatus, LocationType
from apps.scm.containers.location_resolver import LocationQuery, resolve_location
from apps.scm.containers.services import create_location, create_location_alias
from apps.teams.models import Team


def _port(team, name="Göteborg", **fields):
    return create_location(
        team,
        {"name": name, "location_type": LocationType.PORT, "unlocode": "SEGOT", "country_code": "SE", **fields},
    )


def _terminal(team, parent, name="Oceanterminalen", **fields):
    return create_location(
        team,
        {
            "name": name,
            "location_type": LocationType.DEPOT,
            "unlocode": "SEGOT",
            "country_code": "SE",
            "parent_location": parent,
            **fields,
        },
    )


class AliasResolutionTest(TestCase):
    """An explicit alias is somebody's decision and outranks everything derived."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Alias res", slug="res-alias")

    def setUp(self):
        self.port = _port(self.team)
        self.terminal = _terminal(self.team, self.port)

    def test_a_configured_alias_resolves_the_name_it_was_recorded_for(self):
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        result = resolve_location(self.team, LocationQuery(source="traqo", name="GOTHENBURG"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.method, LocationResolutionMethod.ALIAS)
        self.assertEqual(result.location, self.port)

    def test_an_alias_can_point_at_a_terminal_rather_than_the_port(self):
        """The resolver will name a terminal — when told to, never by inference."""
        create_location_alias(self.team, self.terminal, {"source": "internal", "external_name": "OCEAN"})
        result = resolve_location(self.team, LocationQuery(source="internal", name="Ocean"))
        self.assertEqual(result.location, self.terminal)

    def test_an_alias_matches_regardless_of_case_and_whitespace(self):
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        for written in ("gothenburg", " Gothenburg ", "GOTHENBURG"):
            with self.subTest(written=written):
                result = resolve_location(self.team, LocationQuery(source="traqo", name=written))
                self.assertEqual(result.location, self.port)

    def test_a_provider_specific_spelling_needs_only_its_own_alias(self):
        """Each provider's vocabulary is separate; none pollutes the canonical row."""
        create_location_alias(self.team, self.port, {"source": "cma-cgm", "external_name": "GOTHENBURG, SE"})
        result = resolve_location(self.team, LocationQuery(source="cma-cgm", name="GOTHENBURG, SE"))
        self.assertEqual(result.location, self.port)

    def test_one_sources_alias_does_not_answer_for_another_source(self):
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        result = resolve_location(self.team, LocationQuery(source="maersk", name="GOTHENBURG"))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    def test_a_provider_code_resolves_through_its_own_method(self):
        create_location_alias(self.team, self.terminal, {"source": "maersk", "external_code": "SEGOT-OT"})
        result = resolve_location(self.team, LocationQuery(source="maersk", external_code="segot-ot"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.method, LocationResolutionMethod.EXTERNAL_CODE)
        self.assertEqual(result.location, self.terminal)

    def test_an_alias_beats_the_unlocode_it_would_otherwise_resolve_by(self):
        """Someone said this name means the terminal. That outranks the code."""
        create_location_alias(self.team, self.terminal, {"source": "traqo", "external_name": "GOTHENBURG"})
        result = resolve_location(self.team, LocationQuery(source="traqo", name="GOTHENBURG", unlocode="SEGOT"))
        self.assertEqual(result.method, LocationResolutionMethod.ALIAS)
        self.assertEqual(result.location, self.terminal)

    def test_contradicting_aliases_are_reported_rather_than_silently_ordered(self):
        """A source's name and its code disagreeing is bad master data, not a tie
        for this function's rule order to settle."""
        create_location_alias(self.team, self.port, {"source": "maersk", "external_name": "GOTEBORG"})
        create_location_alias(self.team, self.terminal, {"source": "maersk", "external_code": "OT1"})
        result = resolve_location(self.team, LocationQuery(source="maersk", name="GOTEBORG", external_code="OT1"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertCountEqual(result.candidates, [self.port, self.terminal])

    def test_an_alias_pointing_at_a_deactivated_location_does_not_resolve(self):
        create_location_alias(self.team, self.terminal, {"source": "traqo", "external_name": "OLD DEPOT"})
        self.terminal.is_active = False
        self.terminal.save()
        result = resolve_location(self.team, LocationQuery(source="traqo", name="OLD DEPOT"))
        self.assertNotEqual(result.location, self.terminal)


class UnlocodeResolutionTest(TestCase):
    """A code names a port, not a berth."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Code res", slug="res-code")

    def test_a_code_matching_one_location_resolves_to_it(self):
        port = _port(self.team)
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.method, LocationResolutionMethod.UNLOCODE)
        self.assertEqual(result.location, port)

    def test_a_code_resolves_however_it_was_written(self):
        port = _port(self.team)
        for written in ("segot", "SE GOT", "se-got"):
            with self.subTest(written=written):
                self.assertEqual(resolve_location(self.team, LocationQuery(unlocode=written)).location, port)

    def test_a_code_shared_by_a_port_and_its_terminal_resolves_to_the_port(self):
        """ "SEGOT" is a claim about Göteborg. It says nothing about which berth."""
        port = _port(self.team)
        _terminal(self.team, port)
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.location, port)

    def test_a_code_shared_by_two_terminals_still_resolves_to_the_port(self):
        port = _port(self.team)
        _terminal(self.team, port, name="Oceanterminalen")
        _terminal(self.team, port, name="APM Terminals Gothenburg")
        self.assertEqual(resolve_location(self.team, LocationQuery(unlocode="SEGOT")).location, port)

    def test_a_code_resolves_down_a_two_level_hierarchy(self):
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        create_location(self.team, {"name": "Berth 12", "unlocode": "SEGOT", "parent_location": terminal})
        self.assertEqual(resolve_location(self.team, LocationQuery(unlocode="SEGOT")).location, port)

    def test_a_code_shared_by_unrelated_locations_is_ambiguous(self):
        """Nothing contains the other, so the code cannot choose. It says so."""
        first = _port(self.team, name="Göteborg")
        second = create_location(self.team, {"name": "Gothenburg Free Port", "unlocode": "SEGOT"})
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertEqual(result.method, LocationResolutionMethod.UNLOCODE)
        self.assertCountEqual(result.candidates, [first, second])

    def test_a_terminal_is_never_arbitrarily_chosen_for_a_port_level_code(self):
        """The regression this whole design exists to prevent."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        self.assertNotEqual(resolve_location(self.team, LocationQuery(unlocode="SEGOT")).location, terminal)

    def test_an_unknown_code_resolves_to_nothing(self):
        _port(self.team)
        result = resolve_location(self.team, LocationQuery(unlocode="NLRTM"))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)
        self.assertIsNone(result.location)

    def test_a_deactivated_location_is_not_considered(self):
        port = _port(self.team)
        port.is_active = False
        port.save()
        self.assertEqual(resolve_location(self.team, LocationQuery(unlocode="SEGOT")).status, "unresolved")


class NameResolutionTest(TestCase):
    """A location's own name, matched exactly once normalised — and no further."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Name res", slug="res-name")

    def test_a_name_resolves_regardless_of_case_and_whitespace(self):
        depot = create_location(self.team, {"name": "Gothenburg Depot"})
        for written in ("Gothenburg Depot", "GOTHENBURG DEPOT", " gothenburg  depot "):
            with self.subTest(written=written):
                result = resolve_location(self.team, LocationQuery(name=written))
                self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
                self.assertEqual(result.method, LocationResolutionMethod.NAME)
                self.assertEqual(result.location, depot)

    def test_a_carrier_spelling_without_a_diacritic_still_finds_the_place(self):
        """Maersk's "GOTEBORG" reaches "Göteborg" without needing an alias."""
        port = _port(self.team, name="Göteborg")
        self.assertEqual(resolve_location(self.team, LocationQuery(name="GOTEBORG")).location, port)

    def test_an_english_exonym_is_not_inferred(self):
        """ "Gothenburg" is not a spelling of "Göteborg" — it is a translation, and
        translating is what an alias is for."""
        _port(self.team, name="Göteborg")
        result = resolve_location(self.team, LocationQuery(name="Gothenburg"))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    def test_a_near_miss_is_not_resolved(self):
        """No edit distance anywhere in this resolver."""
        create_location(self.team, {"name": "Oceanterminalen"})
        for written in ("Oceanterminal", "Ocean Terminalen", "Oceanterminalen 2"):
            with self.subTest(written=written):
                self.assertEqual(
                    resolve_location(self.team, LocationQuery(name=written)).status,
                    LocationResolutionStatus.UNRESOLVED,
                )

    def test_a_name_two_unrelated_locations_share_is_ambiguous(self):
        first = create_location(self.team, {"name": "Central"})
        second = create_location(self.team, {"name": "Central"})
        result = resolve_location(self.team, LocationQuery(name="central"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertCountEqual(result.candidates, [first, second])

    def test_a_country_narrows_a_shared_name(self):
        """A "Central" in Sweden must not answer for one in Vietnam."""
        swedish = create_location(self.team, {"name": "Central", "country_code": "SE"})
        create_location(self.team, {"name": "Central", "country_code": "VN"})
        result = resolve_location(self.team, LocationQuery(name="Central", country_code="se"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.location, swedish)

    def test_a_city_narrows_a_shared_name(self):
        gothenburg = create_location(self.team, {"name": "Central", "city": "Göteborg"})
        create_location(self.team, {"name": "Central", "city": "Malmö"})
        result = resolve_location(self.team, LocationQuery(name="Central", city="GOTEBORG"))
        self.assertEqual(result.location, gothenburg)

    def test_a_country_that_would_empty_the_set_is_ignored_rather_than_applied(self):
        """A location with no recorded country is not evidence of a different one."""
        depot = create_location(self.team, {"name": "Central"})
        result = resolve_location(self.team, LocationQuery(name="Central", country_code="SE"))
        self.assertEqual(result.location, depot)


@override_settings(SCM_LOCATION_COORDINATE_RADIUS_KM=5.0)
class CoordinateResolutionTest(TestCase):
    """A conservative fallback that would rather be ambiguous than wrong."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Coord res", slug="res-coord")

    def test_one_location_within_the_radius_resolves(self):
        depot = create_location(self.team, {"name": "Yard", "latitude": "57.700000", "longitude": "11.900000"})
        result = resolve_location(self.team, LocationQuery(latitude=57.701, longitude=11.901))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.method, LocationResolutionMethod.COORDINATES)
        self.assertEqual(result.location, depot)

    def test_two_locations_within_the_radius_are_ambiguous(self):
        """Even a parent and its child: a point near both is not evidence of which."""
        port = create_location(
            self.team,
            {"name": "Göteborg", "latitude": "57.700000", "longitude": "11.900000", "location_type": LocationType.PORT},
        )
        terminal = create_location(
            self.team,
            {"name": "Oceanterminalen", "latitude": "57.705000", "longitude": "11.905000", "parent_location": port},
        )
        result = resolve_location(self.team, LocationQuery(latitude=57.702, longitude=11.902))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertCountEqual(result.candidates, [port, terminal])

    def test_nothing_outside_the_radius_resolves(self):
        create_location(self.team, {"name": "Yard", "latitude": "57.700000", "longitude": "11.900000"})
        result = resolve_location(self.team, LocationQuery(latitude=51.900, longitude=4.480))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    def test_a_location_without_coordinates_is_never_a_coordinate_match(self):
        create_location(self.team, {"name": "Yard"})
        result = resolve_location(self.team, LocationQuery(latitude=57.700, longitude=11.900))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    @override_settings(SCM_LOCATION_COORDINATE_RADIUS_KM=0.1)
    def test_the_radius_is_configurable(self):
        create_location(self.team, {"name": "Yard", "latitude": "57.700000", "longitude": "11.900000"})
        # ~1.1 km away: inside the default radius, outside this one.
        result = resolve_location(self.team, LocationQuery(latitude=57.710, longitude=11.900))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    def test_a_code_is_preferred_over_coordinates(self):
        """Coordinates are the fallback, not the identity system."""
        port = _port(self.team, name="Göteborg")
        create_location(self.team, {"name": "Yard", "latitude": "57.700000", "longitude": "11.900000"})
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT", latitude=57.700, longitude=11.900))
        self.assertEqual(result.method, LocationResolutionMethod.UNLOCODE)
        self.assertEqual(result.location, port)


class EmptyQueryTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Empty res", slug="res-empty")

    def test_an_event_naming_no_place_costs_no_queries(self):
        """A booking confirmation names nowhere. That is ordinary, not an error."""
        _port(self.team)
        with self.assertNumQueries(0):
            result = resolve_location(self.team, LocationQuery(source="traqo"))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)
        self.assertEqual(result.method, LocationResolutionMethod.NONE)

    def test_a_query_with_only_whitespace_is_empty(self):
        self.assertTrue(LocationQuery(name="   ", unlocode="  ").is_empty)


class ResolverNeverWritesTest(TestCase):
    """Reading a carrier response must not grow the master data."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="No write", slug="res-no-write")

    def test_an_unresolved_name_does_not_create_a_location(self):
        from apps.scm.containers.models import ContainerLocation

        resolve_location(self.team, LocationQuery(source="traqo", name="SOMEWHERE NEW", unlocode="XXXXX"))
        self.assertEqual(ContainerLocation.objects.filter(team=self.team).count(), 0)

    def test_a_resolved_name_does_not_create_an_alias(self):
        from apps.scm.containers.models import LocationAlias

        _port(self.team)
        resolve_location(self.team, LocationQuery(source="traqo", name="Göteborg"))
        self.assertEqual(LocationAlias.objects.filter(team=self.team).count(), 0)


class TenantIsolationTest(TestCase):
    """The critical property: one team's evidence never reaches another's places."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Mine", slug="res-mine")
        cls.other_team = Team.objects.create(name="Theirs", slug="res-theirs")

    def setUp(self):
        self.theirs = _port(self.other_team, name="Göteborg")
        create_location_alias(self.other_team, self.theirs, {"source": "traqo", "external_name": "GOTHENBURG"})

    def test_another_teams_alias_does_not_resolve_for_me(self):
        result = resolve_location(self.team, LocationQuery(source="traqo", name="GOTHENBURG"))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    def test_another_teams_unlocode_does_not_resolve_for_me(self):
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    def test_another_teams_name_does_not_resolve_for_me(self):
        result = resolve_location(self.team, LocationQuery(name="Göteborg"))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    @override_settings(SCM_LOCATION_COORDINATE_RADIUS_KM=50.0)
    def test_another_teams_coordinates_do_not_resolve_for_me(self):
        self.theirs.latitude, self.theirs.longitude = "57.700000", "11.900000"
        self.theirs.save()
        result = resolve_location(self.team, LocationQuery(latitude=57.700, longitude=11.900))
        self.assertEqual(result.status, LocationResolutionStatus.UNRESOLVED)

    def test_each_team_resolves_to_its_own_place(self):
        mine = _port(self.team, name="Göteborg")
        create_location_alias(self.team, mine, {"source": "traqo", "external_name": "GOTHENBURG"})
        query = LocationQuery(source="traqo", name="GOTHENBURG")
        self.assertEqual(resolve_location(self.team, query).location, mine)
        self.assertEqual(resolve_location(self.other_team, query).location, self.theirs)
