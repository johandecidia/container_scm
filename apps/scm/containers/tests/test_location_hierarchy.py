"""The location hierarchy: what it may express, what it refuses, and what it decides.

``parent_location`` stopped being decoration the moment the resolver narrowed with
it. A wrong parent is now a wrong answer about where containers are, so this file is
arranged around the three ways that happens.

**A structure that is not a tree.** ``A → B → C → A`` detaches all three from every
query that starts at a root, and each of the three links is individually harmless.
The cycle tests are about the *chain*, not the link.

**Precision that does not follow the evidence.** A shared UN/LOCODE is the case that
makes a hierarchy worth having, and it can be got wrong in both directions:
``SEGOT`` must not become "Oceanterminalen", and ``name=Oceanterminalen`` plus
``SEGOT`` must not be flattened back to "Göteborg". Both are asserted, because fixing
one by hand is how the other gets introduced.

**Containment nobody recorded.** Nothing here infers a parent from a shared code, a
similar name or a nearby coordinate. The tests that matter most are the ones where
two places obviously *ought* to be related and the resolver says ``AMBIGUOUS`` anyway.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.scm.containers.choices import LocationResolutionMethod, LocationResolutionStatus, LocationType
from apps.scm.containers.location_hierarchy import (
    MAX_DEPTH,
    ancestor_chain,
    contained_ids,
    descendant_ids,
    is_contained_in,
    parent_options,
)
from apps.scm.containers.location_resolver import LocationQuery, resolve_location
from apps.scm.containers.location_workspace import get_location_hierarchy
from apps.scm.containers.models import ContainerLocation
from apps.scm.containers.services import create_location, create_location_alias, update_location
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


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
            "location_type": LocationType.TERMINAL,
            "unlocode": "SEGOT",
            "country_code": "SE",
            "parent_location": parent,
            **fields,
        },
    )


class ParentValidationTest(TestCase):
    """The structure has to stay a forest of trees, whoever is writing to it."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Validation", slug="hier-validation")
        cls.other_team = Team.objects.create(name="Theirs", slug="hier-validation-theirs")

    def test_a_location_cannot_be_its_own_parent(self):
        location = create_location(self.team, {"name": "Depot"})
        location.parent_location = location
        with self.assertRaises(ValidationError):
            location.full_clean()

    def test_a_direct_cycle_is_refused(self):
        first = create_location(self.team, {"name": "A"})
        second = create_location(self.team, {"name": "B", "parent_location": first})
        first.parent_location = second
        with self.assertRaises(ValidationError):
            first.full_clean()

    def test_a_three_level_cycle_is_refused(self):
        """A → B → C → A. Every link is legal on its own; the chain is not."""
        a = create_location(self.team, {"name": "A"})
        b = create_location(self.team, {"name": "B", "parent_location": a})
        c = create_location(self.team, {"name": "C", "parent_location": b})
        a.parent_location = c
        with self.assertRaises(ValidationError) as raised:
            a.full_clean()
        self.assertIn("parent_location", raised.exception.error_dict)

    def test_a_five_level_cycle_is_refused(self):
        chain = [create_location(self.team, {"name": "L0"})]
        for level in range(1, 5):
            chain.append(create_location(self.team, {"name": f"L{level}", "parent_location": chain[-1]}))
        chain[0].parent_location = chain[-1]
        with self.assertRaises(ValidationError):
            chain[0].full_clean()

    def test_a_cycle_cannot_be_written_through_the_service_either(self):
        """The rule is on the model, so nothing that saves properly can dodge it."""
        first = create_location(self.team, {"name": "A"})
        second = create_location(self.team, {"name": "B", "parent_location": first})
        with self.assertRaises(ValidationError):
            update_location(location=first, data={"parent_location": second})
        first.refresh_from_db()
        self.assertIsNone(first.parent_location_id)

    def test_another_teams_location_cannot_be_a_parent(self):
        theirs = create_location(self.other_team, {"name": "Their Port"})
        mine = ContainerLocation(team=self.team, name="My Terminal", parent_location=theirs)
        with self.assertRaises(ValidationError):
            mine.full_clean()

    def test_a_chain_deeper_than_the_bound_is_refused(self):
        chain = [create_location(self.team, {"name": "D0"})]
        for level in range(1, MAX_DEPTH):
            chain.append(create_location(self.team, {"name": f"D{level}", "parent_location": chain[-1]}))
        with self.assertRaises(ValidationError):
            create_location(self.team, {"name": "too deep", "parent_location": chain[-1]})

    def test_a_deactivated_parent_is_allowed_and_says_so(self):
        """Deactivating a port does not move its terminals out of it.

        Refusing the relationship would mean an unrelated edit to a child started
        failing the day somebody retired its parent. The form declines to *offer* an
        inactive parent; the model keeps the one already recorded.
        """
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        port.is_active = False
        port.save(update_fields=["is_active"])

        updated = update_location(location=terminal, data={"name": "Oceanterminalen", "notes": "edited"})
        self.assertEqual(updated.parent_location, port)

    def test_removing_a_parent_is_always_allowed(self):
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        update_location(location=terminal, data={"parent_location": None})
        terminal.refresh_from_db()
        self.assertIsNone(terminal.parent_location_id)


class TraversalTest(TestCase):
    """The primitives every reader of the hierarchy shares."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Traversal", slug="hier-traversal")
        cls.other_team = Team.objects.create(name="Theirs", slug="hier-traversal-theirs")

    def setUp(self):
        self.port = _port(self.team)
        self.terminal = _terminal(self.team, self.port)
        self.yard = create_location(self.team, {"name": "MCR Yard", "parent_location": self.terminal})

    def test_the_ancestor_chain_reads_outermost_first(self):
        self.assertEqual(ancestor_chain(self.team, self.yard), [self.port, self.terminal])

    def test_a_root_has_no_ancestors(self):
        self.assertEqual(ancestor_chain(self.team, self.port), [])

    def test_the_ancestor_chain_costs_one_query_per_level(self):
        with self.assertNumQueries(2):
            ancestor_chain(self.team, self.yard)

    def test_another_teams_chain_is_never_walked_into(self):
        """Cross-team parents are refused on write; this is what it guarantees."""
        theirs = _port(self.other_team, name="Their Port")
        ContainerLocation.objects.filter(pk=self.port.pk).update(parent_location=theirs)
        self.port.refresh_from_db()
        self.assertEqual(ancestor_chain(self.team, self.terminal), [self.port])

    def test_the_subtree_is_the_place_and_everything_beneath_it(self):
        self.assertCountEqual(descendant_ids(self.team, self.port), [self.port.pk, self.terminal.pk, self.yard.pk])

    def test_containment_is_reported_for_the_whole_chain(self):
        inside = contained_ids([self.terminal, self.yard], {self.port.pk}, team=self.team)
        self.assertEqual(inside, {self.terminal.pk, self.yard.pk})

    def test_containment_of_a_loaded_parent_costs_no_queries(self):
        """The case the resolver runs on every event: parents already in the set."""
        with self.assertNumQueries(0):
            contained_ids([self.terminal], {self.port.pk}, team=self.team)

    def test_an_unrelated_place_is_not_contained(self):
        other = create_location(self.team, {"name": "Gothenburg Free Port"})
        self.assertFalse(is_contained_in(other, {self.port.pk}, team=self.team))

    def test_a_parent_is_not_contained_in_its_child(self):
        self.assertFalse(is_contained_in(self.port, {self.terminal.pk}, team=self.team))

    def test_a_deactivated_intermediate_does_not_break_containment(self):
        """Retiring a terminal does not move the yard inside it out of the port."""
        self.terminal.is_active = False
        self.terminal.save(update_fields=["is_active"])
        self.assertTrue(is_contained_in(self.yard, {self.port.pk}, team=self.team))


class ParentOptionsTest(TestCase):
    """Which locations may be offered as a parent, and why the list is short."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Options", slug="hier-options")
        cls.other_team = Team.objects.create(name="Theirs", slug="hier-options-theirs")

    def setUp(self):
        self.port = _port(self.team)
        self.terminal = _terminal(self.team, self.port)
        self.yard = create_location(self.team, {"name": "MCR Yard", "parent_location": self.terminal})

    def options(self, location):
        return list(parent_options(self.team, location))

    def test_a_location_is_not_offered_itself(self):
        self.assertNotIn(self.port, self.options(self.port))

    def test_a_child_is_not_offered_as_a_parent(self):
        self.assertNotIn(self.terminal, self.options(self.port))

    def test_a_grandchild_is_not_offered_either(self):
        """Choosing it would be a cycle, and finding that out after saving is worse."""
        self.assertNotIn(self.yard, self.options(self.port))

    def test_a_sibling_is_offered(self):
        sibling = _terminal(self.team, self.port, name="APM Terminals Gothenburg")
        self.assertIn(sibling, self.options(self.terminal))

    def test_another_teams_location_is_not_offered(self):
        theirs = _port(self.other_team, name="Their Port")
        self.assertNotIn(theirs, self.options(self.terminal))

    def test_a_deactivated_location_is_not_offered(self):
        retired = create_location(self.team, {"name": "Old Yard"})
        retired.is_active = False
        retired.save(update_fields=["is_active"])
        self.assertNotIn(retired, self.options(self.terminal))

    def test_a_deactivated_location_already_recorded_as_the_parent_is_kept(self):
        """A ModelChoiceField without its own value saves as unset. That would delete
        a relationship somebody meant to keep."""
        self.port.is_active = False
        self.port.save(update_fields=["is_active"])
        self.assertIn(self.port, self.options(self.terminal))

    def test_creating_a_location_offers_every_active_place(self):
        self.assertCountEqual(self.options(None), [self.port, self.terminal, self.yard])


class SharedUnlocodeResolutionTest(TestCase):
    """The heart of LOC-6: one code, several places, and how precise the answer may be.

    ``SEGOT`` is a claim about Göteborg. Whether it is also a claim about a terminal
    depends entirely on what else the carrier sent, and on what MCR has recorded.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Shared code", slug="hier-shared")

    # -- no hierarchy --------------------------------------------------------

    def test_a_shared_code_with_no_hierarchy_is_ambiguous(self):
        """Two places carry SEGOT and nothing says either is inside the other."""
        port = _port(self.team, name="Göteborg")
        terminal = create_location(
            self.team, {"name": "Oceanterminalen", "unlocode": "SEGOT", "location_type": LocationType.TERMINAL}
        )
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertEqual(result.method, LocationResolutionMethod.UNLOCODE)
        self.assertCountEqual(result.candidates, [port, terminal])

    def test_a_shared_code_with_no_hierarchy_stays_ambiguous_however_alike_the_places_look(self):
        """Same country, same city, same code, adjacent coordinates. Still not proof."""
        first = _port(self.team, name="Göteborg", city="Göteborg", latitude="57.700000", longitude="11.900000")
        second = create_location(
            self.team,
            {
                "name": "Gothenburg Free Port",
                "unlocode": "SEGOT",
                "country_code": "SE",
                "city": "Göteborg",
                "latitude": "57.705000",
                "longitude": "11.905000",
            },
        )
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertCountEqual(result.candidates, [first, second])

    # -- with hierarchy ------------------------------------------------------

    def test_port_level_evidence_resolves_to_the_port(self):
        """The code names the port. It says nothing about which berth."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.method, LocationResolutionMethod.UNLOCODE)
        self.assertEqual(result.location, port)
        self.assertNotEqual(result.location, terminal)

    def test_terminal_specific_evidence_resolves_to_the_terminal(self):
        """The carrier named the terminal *and* sent the port's code. Both agree, and
        answering "Göteborg" would throw away the narrower of the two."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        result = resolve_location(self.team, LocationQuery(name="Oceanterminalen", unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.method, LocationResolutionMethod.NAME)
        self.assertEqual(result.location, terminal)

    def test_port_level_name_and_code_still_resolve_to_the_port(self):
        port = _port(self.team)
        _terminal(self.team, port)
        result = resolve_location(self.team, LocationQuery(name="Göteborg", unlocode="SEGOT"))
        self.assertEqual(result.location, port)

    def test_a_terminal_name_alone_resolves_to_the_terminal(self):
        """Unchanged by LOC-6, and asserted so the two paths cannot drift apart."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        result = resolve_location(self.team, LocationQuery(name="Oceanterminalen"))
        self.assertEqual(result.location, terminal)

    def test_a_name_of_a_place_inside_the_code_resolves_even_without_its_own_code(self):
        """The yard carries no code; the terminal above it does. The name is still
        specific evidence about somewhere inside SEGOT."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        yard = create_location(self.team, {"name": "MCR Yard", "parent_location": terminal})
        result = resolve_location(self.team, LocationQuery(name="MCR Yard", unlocode="SEGOT"))
        self.assertEqual(result.location, yard)

    def test_a_name_the_code_has_nothing_to_do_with_is_ambiguous(self):
        """Neither identifier may quietly overrule the other. Before LOC-6 the code
        won by rule order, crediting evidence about the Free Port to the port."""
        port = _port(self.team)
        free_port = create_location(self.team, {"name": "Gothenburg Free Port", "country_code": "SE"})
        result = resolve_location(self.team, LocationQuery(name="Gothenburg Free Port", unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertCountEqual(result.candidates, [port, free_port])

    def test_a_name_that_matches_nothing_leaves_the_code_to_answer(self):
        """The ordinary carrier event: a place name MCR has not recorded, plus a code."""
        port = _port(self.team)
        _terminal(self.team, port)
        result = resolve_location(self.team, LocationQuery(name="GOTHENBURG", unlocode="SEGOT"))
        self.assertEqual(result.method, LocationResolutionMethod.UNLOCODE)
        self.assertEqual(result.location, port)

    def test_a_code_that_matches_nothing_leaves_the_name_to_answer(self):
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        result = resolve_location(self.team, LocationQuery(name="Oceanterminalen", unlocode="NLRTM"))
        self.assertEqual(result.method, LocationResolutionMethod.NAME)
        self.assertEqual(result.location, terminal)

    def test_two_places_of_one_name_inside_the_code_leave_the_code_to_answer(self):
        """A naming problem inside one port. The code is still right about the port,
        so it answers rather than a coin being tossed between the two."""
        port = _port(self.team)
        _terminal(self.team, port, name="Central")
        second = create_location(self.team, {"name": "Central", "unlocode": "SEGOT", "parent_location": port})
        result = resolve_location(self.team, LocationQuery(name="Central", unlocode="SEGOT"))
        self.assertEqual(result.location, port)
        self.assertNotEqual(result.location, second)

    # -- multi-level ---------------------------------------------------------

    def test_generic_code_evidence_resolves_to_the_outermost_of_three_levels(self):
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        create_location(self.team, {"name": "MCR Yard", "unlocode": "SEGOT", "parent_location": terminal})
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.location, port)

    def test_specific_name_evidence_reaches_the_deepest_level(self):
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        yard = create_location(self.team, {"name": "MCR Yard", "unlocode": "SEGOT", "parent_location": terminal})
        result = resolve_location(self.team, LocationQuery(name="MCR Yard", unlocode="SEGOT"))
        self.assertEqual(result.location, yard)

    def test_the_middle_level_can_be_named_too(self):
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        create_location(self.team, {"name": "MCR Yard", "unlocode": "SEGOT", "parent_location": terminal})
        result = resolve_location(self.team, LocationQuery(name="Oceanterminalen", unlocode="SEGOT"))
        self.assertEqual(result.location, terminal)

    def test_a_branch_beside_the_hierarchy_still_makes_the_code_ambiguous(self):
        """Recording one containment does not settle a code three places share."""
        port = _port(self.team)
        _terminal(self.team, port)
        loose = create_location(self.team, {"name": "Skandiahamnen", "unlocode": "SEGOT"})
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertCountEqual(result.candidates, [port, loose])

    # -- aliases stay separate ----------------------------------------------

    def test_an_alias_outranks_the_hierarchy(self):
        """A provider's own word for the terminal, recorded by somebody, is a decision.
        A shared code above it does not get to reinterpret it."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        create_location_alias(self.team, terminal, {"source": "traqo", "external_name": "JOHN EVANS"})
        result = resolve_location(self.team, LocationQuery(source="traqo", name="JOHN EVANS", unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.method, LocationResolutionMethod.ALIAS)
        self.assertEqual(result.location, terminal)

    def test_an_alias_to_the_port_is_not_narrowed_by_a_hierarchy_below_it(self):
        port = _port(self.team)
        _terminal(self.team, port)
        create_location_alias(self.team, port, {"source": "maersk", "external_name": "GOTEBORG"})
        result = resolve_location(self.team, LocationQuery(source="maersk", name="GOTEBORG", unlocode="SEGOT"))
        self.assertEqual(result.location, port)

    def test_recording_a_parent_does_not_create_an_alias(self):
        """The two concepts stay separate in both directions."""
        from apps.scm.containers.models import LocationAlias

        port = _port(self.team)
        _terminal(self.team, port)
        self.assertEqual(LocationAlias.objects.filter(team=self.team).count(), 0)

    # -- inactive ------------------------------------------------------------

    def test_a_deactivated_parent_leaves_the_active_place_to_answer_the_code(self):
        """Current intended behaviour, stated explicitly. Deactivating the port says
        "route nothing here"; the terminal is then the only active claim on the code.
        The alternative — refusing to answer at all — would make retiring a place
        break evidence about the places still inside it."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        port.is_active = False
        port.save(update_fields=["is_active"])
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.location, terminal)

    def test_a_deactivated_terminal_is_not_an_answer_and_does_not_disturb_the_port(self):
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        terminal.is_active = False
        terminal.save(update_fields=["is_active"])
        self.assertEqual(resolve_location(self.team, LocationQuery(unlocode="SEGOT")).location, port)

    def test_a_deactivated_intermediate_still_places_the_yard_inside_the_port(self):
        """Containment is read regardless of active state, so the code still resolves
        to the port rather than becoming a tie with the yard."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        create_location(self.team, {"name": "MCR Yard", "unlocode": "SEGOT", "parent_location": terminal})
        terminal.is_active = False
        terminal.save(update_fields=["is_active"])
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.RESOLVED)
        self.assertEqual(result.location, port)

    # -- cost ----------------------------------------------------------------

    def test_a_shared_code_costs_one_query_however_deep_the_hierarchy(self):
        """The narrowing reads parents already loaded with the candidates."""
        port = _port(self.team)
        terminal = _terminal(self.team, port)
        create_location(self.team, {"name": "MCR Yard", "unlocode": "SEGOT", "parent_location": terminal})
        with self.assertNumQueries(1):
            resolve_location(self.team, LocationQuery(unlocode="SEGOT"))

    def test_name_and_code_together_cost_two_queries(self):
        port = _port(self.team)
        _terminal(self.team, port)
        with self.assertNumQueries(2):
            resolve_location(self.team, LocationQuery(name="Oceanterminalen", unlocode="SEGOT"))


class HierarchyIsolationTest(TestCase):
    """One team's structure must never decide another team's answers."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Mine", slug="hier-mine")
        cls.other_team = Team.objects.create(name="Theirs", slug="hier-theirs")

    def test_another_teams_hierarchy_does_not_collapse_my_ambiguity(self):
        """They have arranged their Göteborg and Oceanterminalen into a hierarchy.
        Mine are still two unrelated places carrying one code."""
        their_port = _port(self.other_team)
        _terminal(self.other_team, their_port)

        mine_port = _port(self.team)
        mine_terminal = create_location(self.team, {"name": "Oceanterminalen", "unlocode": "SEGOT"})
        result = resolve_location(self.team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertCountEqual(result.candidates, [mine_port, mine_terminal])

    def test_my_hierarchy_does_not_collapse_another_teams_ambiguity(self):
        port = _port(self.team)
        _terminal(self.team, port)

        their_port = _port(self.other_team)
        their_terminal = create_location(self.other_team, {"name": "Oceanterminalen", "unlocode": "SEGOT"})
        result = resolve_location(self.other_team, LocationQuery(unlocode="SEGOT"))
        self.assertEqual(result.status, LocationResolutionStatus.AMBIGUOUS)
        self.assertCountEqual(result.candidates, [their_port, their_terminal])

    def test_each_team_resolves_its_own_terminal_for_terminal_specific_evidence(self):
        mine = _terminal(self.team, _port(self.team))
        theirs = _terminal(self.other_team, _port(self.other_team))
        query = LocationQuery(name="Oceanterminalen", unlocode="SEGOT")
        self.assertEqual(resolve_location(self.team, query).location, mine)
        self.assertEqual(resolve_location(self.other_team, query).location, theirs)


class WorkspaceHierarchyTest(TestCase):
    """What the Location Workspace shows, and what it costs to show it."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Workspace", slug="hier-workspace")

    def setUp(self):
        self.port = _port(self.team)
        self.terminal = _terminal(self.team, self.port)
        self.yard = create_location(self.team, {"name": "MCR Yard", "parent_location": self.terminal})

    def test_the_path_reads_from_the_outermost_place_down(self):
        hierarchy = get_location_hierarchy(self.team, self.yard)
        self.assertEqual([place.name for place in hierarchy.path], ["Göteborg", "Oceanterminalen", "MCR Yard"])
        self.assertEqual(hierarchy.parent, self.terminal)

    def test_a_root_has_no_parent_and_says_so(self):
        hierarchy = get_location_hierarchy(self.team, self.port)
        self.assertTrue(hierarchy.is_root)
        self.assertIsNone(hierarchy.parent)
        self.assertEqual(hierarchy.path, [self.port])

    def test_the_immediate_children_are_listed_and_the_grandchildren_are_not(self):
        hierarchy = get_location_hierarchy(self.team, self.port)
        self.assertEqual([child.pk for child in hierarchy.children], [self.terminal.pk])
        self.assertEqual(hierarchy.child_count, 1)

    def test_a_leaf_says_nothing_is_inside_it(self):
        self.assertTrue(get_location_hierarchy(self.team, self.yard).is_leaf)

    def test_a_child_carries_its_own_inventory_count(self):
        from apps.scm.containers.models import Container, EquipmentType
        from apps.scm.containers.utils import calculate_check_digit

        equipment = EquipmentType.objects.get_or_create(
            iso_code="22G1",
            defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
        )[0]
        Container.objects.create(
            team=self.team,
            owner_code="MSK",
            category_id="U",
            serial_number="100001",
            check_digit=calculate_check_digit("MSK", "U", "100001"),
            equipment_type=equipment,
            current_location=self.terminal,
        )
        child = get_location_hierarchy(self.team, self.port).children[0]
        self.assertEqual(child.container_count, 1)

    def test_a_deactivated_child_is_still_part_of_the_structure(self):
        self.terminal.is_active = False
        self.terminal.save(update_fields=["is_active"])
        self.assertEqual(get_location_hierarchy(self.team, self.port).child_count, 1)

    def test_a_deactivated_parent_is_flagged(self):
        self.port.is_active = False
        self.port.save(update_fields=["is_active"])
        self.assertTrue(get_location_hierarchy(self.team, self.terminal).has_inactive_parent)

    def test_sharing_the_parents_code_is_reported(self):
        hierarchy = get_location_hierarchy(self.team, self.terminal)
        self.assertTrue(hierarchy.shares_unlocode_with_parent)
        self.assertFalse(get_location_hierarchy(self.team, self.yard).shares_unlocode_with_parent)

    def test_another_teams_children_never_appear(self):
        other_team = Team.objects.create(name="Theirs", slug="hier-workspace-theirs")
        theirs = _port(other_team, name="Their Port")
        ContainerLocation.objects.filter(pk=theirs.pk).update(parent_location=self.port)
        self.assertEqual(
            [child.pk for child in get_location_hierarchy(self.team, self.port).children], [self.terminal.pk]
        )

    # -- cost ----------------------------------------------------------------

    def test_the_hierarchy_costs_two_queries_for_a_three_level_path(self):
        with self.assertNumQueries(2):
            get_location_hierarchy(self.team, self.terminal)

    def test_the_cost_does_not_grow_with_the_number_of_places_inside(self):
        """The N+1 this panel would otherwise be: a port with forty terminals."""
        with CaptureQueriesContext(connection) as few:
            get_location_hierarchy(self.team, self.port)
        for index in range(20):
            _terminal(self.team, self.port, name=f"Terminal {index}")
        with CaptureQueriesContext(connection) as many:
            hierarchy = get_location_hierarchy(self.team, self.port)
        self.assertEqual(hierarchy.child_count, 21)
        self.assertEqual(len(many.captured_queries), len(few.captured_queries))


@override_settings(STORAGES=_TEST_STORAGES)
class HierarchyUiTest(TestCase):
    """The pages an operator maintains the hierarchy from."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="UI", slug="hier-ui")
        cls.user = CustomUser.objects.create_user(username="hierui@example.com", password="pw")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.port = _port(self.team)
        self.terminal = _terminal(self.team, self.port)
        self.yard = create_location(self.team, {"name": "MCR Yard", "parent_location": self.terminal})

    def workspace(self, location):
        return self.client.get(reverse("containers:location_detail", kwargs={"location_id": location.pk}))

    def form(self, location):
        return self.client.get(
            reverse("containers:location_update", kwargs={"location_id": location.pk}), HTTP_HX_REQUEST="true"
        )

    # -- the workspace -------------------------------------------------------

    def test_the_workspace_shows_the_hierarchy_panel(self):
        response = self.workspace(self.terminal)
        self.assertContains(response, "Hierarchy")
        self.assertContains(response, "Inside this location")

    def test_the_workspace_links_up_the_whole_path(self):
        response = self.workspace(self.yard)
        for ancestor in (self.port, self.terminal):
            with self.subTest(ancestor=ancestor.name):
                self.assertContains(response, reverse("containers:location_detail", args=[ancestor.pk]))

    def test_the_workspace_links_down_to_what_is_inside(self):
        response = self.workspace(self.terminal)
        self.assertContains(response, reverse("containers:location_detail", args=[self.yard.pk]))
        self.assertContains(response, "MCR Yard")

    def test_a_root_says_it_has_no_parent(self):
        response = self.workspace(self.port)
        self.assertContains(response, "No parent recorded")

    def test_a_leaf_says_nothing_is_inside_it(self):
        self.assertContains(self.workspace(self.yard), "No locations are recorded inside this one")

    def test_the_panel_explains_what_a_shared_code_costs_without_the_relationship(self):
        response = self.workspace(self.terminal)
        self.assertContains(response, "SEGOT")
        self.assertContains(response, "resolves to the outer place")

    def test_a_deactivated_parent_is_called_out_on_the_page(self):
        self.port.is_active = False
        self.port.save(update_fields=["is_active"])
        self.assertContains(self.workspace(self.terminal), "is deactivated")

    def test_the_page_cost_does_not_grow_with_the_number_of_places_inside(self):
        """A port with forty terminals must not cost forty queries to render."""
        with CaptureQueriesContext(connection) as few:
            self.workspace(self.port)
        for index in range(20):
            _terminal(self.team, self.port, name=f"Terminal {index}")
        with CaptureQueriesContext(connection) as many:
            self.workspace(self.port)
        self.assertEqual(len(many.captured_queries), len(few.captured_queries))

    def test_another_teams_location_workspace_is_not_reachable(self):
        other_team = Team.objects.create(name="Theirs", slug="hier-ui-theirs")
        theirs = _port(other_team, name="Their Port")
        self.assertEqual(self.workspace(theirs).status_code, 404)

    # -- the parent selector -------------------------------------------------

    def test_the_selector_labels_options_with_the_code_and_the_type(self):
        response = self.form(self.terminal)
        self.assertContains(response, "Göteborg — SEGOT — Port")

    def test_the_selector_does_not_offer_a_descendant(self):
        options = list(self.form(self.port).context["form"].fields["parent_location"].queryset)
        self.assertNotIn(self.terminal, options)
        self.assertNotIn(self.yard, options)

    def test_a_parent_can_be_recorded_through_the_form(self):
        loose = create_location(self.team, {"name": "Skandiahamnen", "unlocode": "SEGOT"})
        response = self.client.post(
            reverse("containers:location_update", kwargs={"location_id": loose.pk}),
            data={
                "name": "Skandiahamnen",
                "location_type": LocationType.TERMINAL,
                "parent_location": str(self.port.pk),
                "unlocode": "SEGOT",
                "is_active": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        loose.refresh_from_db()
        self.assertEqual(loose.parent_location, self.port)

    def test_posting_a_descendant_as_the_parent_is_refused(self):
        """Not offered, and not accepted either — the queryset is the gate."""
        response = self.client.post(
            reverse("containers:location_update", kwargs={"location_id": self.port.pk}),
            data={
                "name": "Göteborg",
                "location_type": LocationType.PORT,
                "parent_location": str(self.yard.pk),
                "is_active": True,
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.port.refresh_from_db()
        self.assertIsNone(self.port.parent_location_id)

    def test_posting_another_teams_location_as_the_parent_is_refused(self):
        other_team = Team.objects.create(name="Theirs", slug="hier-ui-parent-theirs")
        theirs = _port(other_team, name="Their Port")
        self.client.post(
            reverse("containers:location_update", kwargs={"location_id": self.terminal.pk}),
            data={
                "name": "Oceanterminalen",
                "location_type": LocationType.TERMINAL,
                "parent_location": str(theirs.pk),
                "is_active": True,
            },
            HTTP_HX_REQUEST="true",
        )
        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.parent_location, self.port)
