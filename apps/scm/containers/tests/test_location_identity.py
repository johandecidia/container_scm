"""Canonical location identity: normalisation, plurality and hierarchy.

Three things are worth protecting here.

* **A UN/LOCODE names a place, not a row.** Göteborg, Oceanterminalen and a third
  terminal beside them legitimately share ``SEGOT``. The schema must let them, and
  the tests assert it rather than trusting nobody adds a unique constraint later.

* **Normalisation canonicalises, it does not interpret.** ``segot`` and ``SE GOT``
  are one code; ``Göteborg`` and ``GOTEBORG`` are one name. ``GOTHENBURG, SE`` is
  *not* the same as ``Gothenburg`` — reading the country off the end is a judgment,
  and it is asserted here that the normaliser refuses to make it.

* **A hierarchy is a hierarchy.** Self-parenting and cycles are rejected, because
  each would detach a location from every query that starts at a root while leaving
  the individual edge looking valid.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.scm.containers.choices import LocationType
from apps.scm.containers.location_identity import (
    distance_km,
    normalize_country_code,
    normalize_external_code,
    normalize_location_name,
    normalize_unlocode,
)
from apps.scm.containers.models import ContainerLocation, LocationAlias
from apps.scm.containers.services import create_location, create_location_alias
from apps.teams.models import Team


class UnlocodeNormalisationTest(TestCase):
    """One code, however it was typed."""

    def test_the_same_code_written_four_ways_is_one_code(self):
        for written in ("segot", "SE GOT", "SEGOT", "se-got"):
            with self.subTest(written=written):
                self.assertEqual(normalize_unlocode(written), "SEGOT")

    def test_surrounding_whitespace_is_not_part_of_the_code(self):
        self.assertEqual(normalize_unlocode("  SEGOT  "), "SEGOT")

    def test_a_code_with_digits_in_the_place_part_is_still_a_code(self):
        """UN/LOCODE place codes are alphanumeric, e.g. USNY1."""
        self.assertEqual(normalize_unlocode("us ny1"), "USNY1")

    def test_something_that_is_not_a_code_yields_nothing(self):
        """Better an empty column than one holding a value no lookup can produce."""
        for written in ("", None, "SE", "SEGOTX", "Gothenburg", "12GOT"):
            with self.subTest(written=written):
                self.assertEqual(normalize_unlocode(written), "")


class CountryCodeNormalisationTest(TestCase):
    def test_a_two_letter_code_is_upper_cased(self):
        self.assertEqual(normalize_country_code(" se "), "SE")

    def test_a_country_name_is_not_truncated_into_a_code(self):
        """ "Sweden" clipped to "SW" would name Eswatini. Reject rather than guess."""
        self.assertEqual(normalize_country_code("Sweden"), "")


class NameNormalisationTest(TestCase):
    """Case, whitespace and diacritics fold. Meaning does not."""

    def test_case_and_surrounding_whitespace_do_not_distinguish_names(self):
        for written in ("Gothenburg", "GOTHENBURG", " Gothenburg ", "gothenburg"):
            with self.subTest(written=written):
                self.assertEqual(normalize_location_name(written), "gothenburg")

    def test_repeated_internal_whitespace_collapses(self):
        self.assertEqual(normalize_location_name("Port  of   Gothenburg"), "port of gothenburg")

    def test_a_diacritic_is_not_a_different_name(self):
        """Which is why Maersk's "GOTEBORG" needs no alias to reach "Göteborg"."""
        self.assertEqual(normalize_location_name("Göteborg"), normalize_location_name("GOTEBORG"))

    def test_a_trailing_country_makes_it_a_different_name(self):
        """Reading ", SE" as a country is interpretation. That is an alias's job."""
        self.assertNotEqual(normalize_location_name("GOTHENBURG, SE"), normalize_location_name("Gothenburg"))

    def test_nothing_normalises_to_nothing(self):
        for written in ("", None, "   "):
            with self.subTest(written=written):
                self.assertEqual(normalize_location_name(written), "")


class ExternalCodeNormalisationTest(TestCase):
    def test_a_provider_code_is_trimmed_and_upper_cased(self):
        self.assertEqual(normalize_external_code(" got-01 "), "GOT-01")

    def test_internal_punctuation_is_left_alone(self):
        """A provider's code is opaque; its punctuation may well be significant."""
        self.assertEqual(normalize_external_code("SE.GOT.1"), "SE.GOT.1")


class DistanceTest(TestCase):
    def test_a_missing_coordinate_is_not_a_distance_of_zero(self):
        self.assertIsNone(distance_km(57.7, 11.9, None, 11.9))
        self.assertIsNone(distance_km(None, None, None, None))

    def test_the_same_point_is_no_distance_away(self):
        self.assertAlmostEqual(distance_km(57.7, 11.9, 57.7, 11.9), 0.0, places=6)

    def test_a_known_separation_comes_out_about_right(self):
        """One degree of latitude is roughly 111 km anywhere on Earth."""
        self.assertAlmostEqual(distance_km(57.0, 11.9, 58.0, 11.9), 111.2, delta=1.0)


class CanonicalIdentityTest(TestCase):
    """What the canonical model does and does not enforce."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Identity", slug="loc-identity")

    def test_several_locations_may_share_one_unlocode(self):
        """A port and two terminals inside it are three places under SEGOT."""
        port = create_location(self.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"})
        for name in ("Oceanterminalen", "APM Terminals Gothenburg"):
            create_location(
                self.team,
                {
                    "name": name,
                    "location_type": LocationType.TERMINAL,
                    "unlocode": "SEGOT",
                    "parent_location": port,
                },
            )
        self.assertEqual(ContainerLocation.objects.filter(team=self.team, unlocode="SEGOT").count(), 3)

    def test_a_code_is_stored_canonically_however_it_was_given(self):
        location = ContainerLocation.objects.create(team=self.team, name="Göteborg", unlocode="se got")
        location.refresh_from_db()
        self.assertEqual(location.unlocode, "SEGOT")

    def test_a_code_that_is_not_one_is_not_stored(self):
        location = ContainerLocation.objects.create(team=self.team, name="Somewhere", unlocode="not-a-code")
        location.refresh_from_db()
        self.assertEqual(location.unlocode, "")

    def test_the_matching_name_is_derived_on_save(self):
        location = ContainerLocation.objects.create(team=self.team, name="  Göteborg  ")
        location.refresh_from_db()
        self.assertEqual(location.normalized_name, "goteborg")

    def test_renaming_a_location_re_derives_its_matching_name(self):
        location = ContainerLocation.objects.create(team=self.team, name="Old Depot")
        location.name = "New Depot"
        location.save()
        location.refresh_from_db()
        self.assertEqual(location.normalized_name, "new depot")

    def test_a_partial_save_still_re_derives_the_matching_name(self):
        """`update_fields` must not be a way to leave the derived column stale."""
        location = ContainerLocation.objects.create(team=self.team, name="Old Depot")
        location.name = "Renamed Depot"
        location.save(update_fields=["name"])
        location.refresh_from_db()
        self.assertEqual(location.normalized_name, "renamed depot")

    def test_a_country_code_is_stored_canonically(self):
        location = ContainerLocation.objects.create(team=self.team, name="Göteborg", country_code="se")
        location.refresh_from_db()
        self.assertEqual(location.country_code, "SE")


class HierarchyTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Hierarchy", slug="loc-hierarchy")
        cls.other_team = Team.objects.create(name="Theirs", slug="loc-hierarchy-theirs")

    def test_a_terminal_sits_inside_its_port(self):
        port = create_location(self.team, {"name": "Göteborg", "location_type": LocationType.PORT})
        terminal = create_location(
            self.team,
            {"name": "Oceanterminalen", "location_type": LocationType.DEPOT, "parent_location": port},
        )
        self.assertEqual(terminal.parent_location, port)
        self.assertIn(terminal, port.child_locations.all())

    def test_the_full_name_reads_the_place_inside_its_parent(self):
        port = create_location(self.team, {"name": "Göteborg", "location_type": LocationType.PORT})
        terminal = create_location(self.team, {"name": "Oceanterminalen", "parent_location": port})
        self.assertEqual(terminal.full_name, "Göteborg / Oceanterminalen")
        self.assertEqual(port.full_name, "Göteborg")

    def test_a_location_cannot_be_its_own_parent(self):
        location = create_location(self.team, {"name": "Göteborg"})
        location.parent_location = location
        with self.assertRaises(ValidationError):
            location.full_clean()

    def test_a_cycle_is_rejected(self):
        """Each edge looks fine alone; together they detach both from every root."""
        first = create_location(self.team, {"name": "A"})
        second = create_location(self.team, {"name": "B", "parent_location": first})
        first.parent_location = second
        with self.assertRaises(ValidationError):
            first.full_clean()

    def test_a_parent_from_another_team_is_rejected(self):
        theirs = create_location(self.other_team, {"name": "Their Port"})
        mine = ContainerLocation(team=self.team, name="My Terminal", parent_location=theirs)
        with self.assertRaises(ValidationError):
            mine.full_clean()

    def test_deleting_a_port_does_not_delete_its_terminals(self):
        """A terminal outlives the record of what contained it."""
        port = create_location(self.team, {"name": "Göteborg", "location_type": LocationType.PORT})
        terminal = create_location(self.team, {"name": "Oceanterminalen", "parent_location": port})
        port.delete()
        terminal.refresh_from_db()
        self.assertIsNone(terminal.parent_location_id)


class AliasIdentityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Aliases", slug="loc-aliases")
        cls.other_team = Team.objects.create(name="Theirs", slug="loc-aliases-theirs")

    def setUp(self):
        self.port = create_location(
            self.team, {"name": "Göteborg", "location_type": LocationType.PORT, "unlocode": "SEGOT"}
        )

    def test_an_alias_records_what_a_source_calls_the_place(self):
        alias = create_location_alias(
            self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG", "external_code": ""}
        )
        self.assertEqual(alias.location, self.port)
        self.assertEqual(alias.normalized_name, "gothenburg")

    def test_the_source_is_stored_canonically(self):
        alias = create_location_alias(self.team, self.port, {"source": " Traqo ", "external_name": "GOTHENBURG"})
        self.assertEqual(alias.source, "traqo")

    def test_an_alias_with_neither_identifier_is_refused(self):
        """No row exists purely to satisfy the schema."""
        with self.assertRaises(ValidationError):
            create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "", "external_code": ""})

    def test_one_source_cannot_name_two_places_the_same_thing(self):
        other = create_location(self.team, {"name": "Rotterdam"})
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        with self.assertRaises(ValidationError):
            create_location_alias(self.team, other, {"source": "traqo", "external_name": "gothenburg"})

    def test_one_source_cannot_use_one_code_for_two_places(self):
        other = create_location(self.team, {"name": "Rotterdam"})
        create_location_alias(self.team, self.port, {"source": "maersk", "external_code": "GOT"})
        with self.assertRaises(ValidationError):
            create_location_alias(self.team, other, {"source": "maersk", "external_code": "got"})

    def test_two_sources_may_use_the_same_name_for_different_places(self):
        """Traqo's "CENTRAL" and Maersk's "CENTRAL" need not be one place."""
        other = create_location(self.team, {"name": "Central Warehouse"})
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "CENTRAL"})
        create_location_alias(self.team, other, {"source": "maersk", "external_name": "CENTRAL"})
        self.assertEqual(LocationAlias.objects.filter(team=self.team, normalized_name="central").count(), 2)

    def test_two_teams_may_each_map_the_same_name_to_their_own_place(self):
        """The uniqueness is per tenant; it must not be a cross-tenant collision."""
        theirs = create_location(self.other_team, {"name": "Their Göteborg"})
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        create_location_alias(self.other_team, theirs, {"source": "traqo", "external_name": "GOTHENBURG"})
        self.assertEqual(LocationAlias.objects.filter(normalized_name="gothenburg").count(), 2)

    def test_an_alias_cannot_point_at_another_teams_location(self):
        theirs = create_location(self.other_team, {"name": "Their Port"})
        with self.assertRaises(ValueError):
            create_location_alias(self.team, theirs, {"source": "traqo", "external_name": "THEIRS"})

    def test_deleting_a_location_takes_its_aliases_with_it(self):
        """An alias has no meaning apart from the place it names."""
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "GOTHENBURG"})
        self.port.delete()
        self.assertFalse(LocationAlias.objects.filter(team=self.team).exists())
