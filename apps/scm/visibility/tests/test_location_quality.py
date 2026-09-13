"""LOC-5: turning map coverage gaps into a finite list of master-data edits.

The failure this module guards against is not a wrong number on a page. It is the
queue quietly becoming a *second* location truth — a place that guesses, creates or
rewrites, and thereby disagrees with the resolver and the map it exists to serve.

So the tests are arranged around four refusals:

**Nothing is created.** The action records an alias to a location that already
exists. A one-click "create from carrier text" is how a location list ends up with
four spellings of Göteborg in it, and no path here offers one.

**Nothing is rewritten.** Recording an alias leaves every stored ``TrackingEvent``
exactly as it was, with the carrier's wording and the resolution it was given.
Historic re-resolution happens through the mechanisms that already exist — a refresh
or a re-parse — and the queue says so rather than inventing a bulk rewrite.

**Nothing is guessed.** A legitimate ambiguity stays ambiguous: two unrelated
locations sharing ``SEGOT`` are shown as two candidates, and picking one would make
the resolver's refusal worthless. A hierarchy is *not* an ambiguity, and the same
resolver says so.

**Nothing is recomputed.** The container counts on this page are LOC-4's own
``MapCoverage``, and the candidate list is ``resolve_location``'s own answer. A
second definition of "plottable" or a second place-name rule would eventually
contradict the map in front of an operator trying to work out why it is empty.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.choices import LocationResolutionMethod, LocationResolutionStatus, LocationType
from apps.scm.containers.location_resolver import LocationQuery, resolve_location
from apps.scm.containers.models import ContainerLocation, LocationAlias
from apps.scm.containers.services import create_location_alias, update_location
from apps.scm.tracking.models import TrackingEvent
from apps.scm.visibility.location_quality import (
    get_hierarchy_impact,
    get_location_data_quality,
    get_location_quality_summary,
    has_unmatched_evidence,
)
from apps.teams.models import Team

from .factories import (
    TEST_STORAGES,
    equipment_type,
    ingest_maersk_events,
    make_container,
    make_location,
    make_provider,
    make_user_and_team,
    place_container_at,
    with_check_digit,
)

# Real coordinates for a real place, used only as a fixture value. Nothing in the
# product invents these — that is the whole point of the queue being needed at all.
OCEANTERMINALEN = ("57.696629", "11.858448")


def _event(
    team,
    *,
    provider,
    name="",
    unlocode="",
    status=LocationResolutionStatus.UNRESOLVED,
    container=None,
    fingerprint="",
    occurred_at=None,
):
    """One stored piece of location evidence.

    Written directly rather than ingested, because these tests are about how
    *stored* evidence is aggregated and what statuses the resolver has already
    recorded. The tests that are about resolution itself go through the real
    ingestion path — see :class:`HistoricReResolutionTest`.
    """
    return TrackingEvent.objects.create(
        team=team,
        provider=provider,
        container=container,
        event_fingerprint=fingerprint or f"fp-{TrackingEvent.objects.count()}-{name}-{unlocode}",
        location_name=name,
        location_unlocode=unlocode,
        location_resolution_status=status,
        event_datetime=occurred_at or timezone.now(),
    )


class CoordinateGapTest(TestCase):
    """Which canonical locations cannot be plotted, and what that costs."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("gap@example.com", "loc5-gap")
        equipment_type()

    def test_a_location_with_no_coordinates_is_a_row_of_work(self):
        make_location(self.team, "John Evans Depot")
        quality = get_location_data_quality(self.team)
        self.assertEqual([gap.name for gap in quality.coordinate_gaps], ["John Evans Depot"])
        self.assertEqual(quality.summary.locations_missing_coordinates, 1)

    def test_a_location_with_coordinates_is_not_listed(self):
        make_location(self.team, "Oceanterminalen", latitude=OCEANTERMINALEN[0], longitude=OCEANTERMINALEN[1])
        quality = get_location_data_quality(self.team)
        self.assertEqual(quality.coordinate_gaps, [])
        self.assertEqual(quality.summary.locations_missing_coordinates, 0)
        self.assertEqual(quality.summary.locations_with_coordinates, 1)

    def test_half_a_coordinate_pair_is_not_a_point(self):
        """A latitude with no longitude cannot be drawn, and the model allows it."""
        make_location(self.team, "Half Known", latitude=OCEANTERMINALEN[0])
        self.assertEqual(len(get_location_data_quality(self.team).coordinate_gaps), 1)

    def test_the_row_counts_the_containers_standing_at_the_place(self):
        depot = make_location(self.team, "John Evans Depot")
        for index in range(2):
            place_container_at(self.team, make_container(self.team, with_check_digit(f"MSKU10000{index}")), depot)

        gap = get_location_data_quality(self.team).coordinate_gaps[0]
        self.assertEqual(gap.container_count, 2)
        self.assertTrue(gap.has_impact)

    def test_the_row_counts_the_active_shipments_routed_to_the_place(self):
        from datetime import timedelta

        from apps.scm.shipments.models import Shipment

        depot = make_location(self.team, "John Evans Depot")
        Shipment.objects.create(
            team=self.team,
            shipment_number="SHP-GAP",
            status=Shipment.Status.IN_TRANSIT,
            destination_location=depot,
            eta=timezone.localdate() + timedelta(days=4),
        )
        self.assertEqual(get_location_data_quality(self.team).coordinate_gaps[0].shipment_count, 1)

    def test_a_deactivated_location_is_not_master_data_anybody_is_maintaining(self):
        """The resolver only considers active locations, so neither does the queue."""
        retired = make_location(self.team, "Old Yard")
        retired.is_active = False
        retired.save(update_fields=["is_active"])
        self.assertEqual(get_location_data_quality(self.team).coordinate_gaps, [])

    def test_a_place_with_no_impact_yet_is_still_listed(self):
        """It will be wrong the first time a box arrives. Fixing it now is a minute."""
        make_location(self.team, "Future Depot")
        gap = get_location_data_quality(self.team).coordinate_gaps[0]
        self.assertFalse(gap.has_impact)
        self.assertEqual(gap.blocked_positions, 0)

    def test_the_worst_gap_is_listed_first(self):
        quiet = make_location(self.team, "Aaa Quiet Depot")
        busy = make_location(self.team, "Zzz Busy Depot")
        place_container_at(self.team, make_container(self.team, with_check_digit("MSKU200000")), busy)

        names = [gap.location.pk for gap in get_location_data_quality(self.team).coordinate_gaps]
        self.assertEqual(names, [busy.pk, quiet.pk])


@override_settings(STORAGES=TEST_STORAGES)
class BlockedPositionTest(TestCase):
    """The map's own complaint, attributed to the location that caused it."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("blocked@example.com", "loc5-blocked")
        cls.container = make_container(cls.team)
        ingest_maersk_events(cls.team, cls.container)
        cls.depot = make_location(cls.team, "John Evans Depot")
        place_container_at(cls.team, cls.container, cls.depot)

    def test_a_container_at_a_place_with_no_coordinates_blocks_a_marker(self):
        quality = get_location_data_quality(self.team)
        gap = next(row for row in quality.coordinate_gaps if row.location.pk == self.depot.pk)
        self.assertEqual(gap.blocked_positions, 1)

    def test_the_container_counts_are_the_maps_own(self):
        """Read off LOC-4's MapCoverage, not counted again with a second rule."""
        quality = get_location_data_quality(self.team)
        self.assertEqual(quality.coverage.containers_missing_coordinates, 1)
        self.assertEqual(quality.coverage.plotted_containers, 0)

    def test_giving_the_place_coordinates_clears_the_gap(self):
        """The whole promise of the page: normal master-data maintenance fixes the map."""
        self.depot.latitude = OCEANTERMINALEN[0]
        self.depot.longitude = OCEANTERMINALEN[1]
        self.depot.full_clean()
        self.depot.save()

        quality = get_location_data_quality(self.team)
        self.assertEqual(quality.coordinate_gaps, [])
        self.assertEqual(quality.coverage.plotted_containers, 1)
        self.assertEqual(quality.coverage.containers_missing_coordinates, 0)


class EvidenceAggregationTest(TestCase):
    """Repeated evidence is one decision, not four hundred rows."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("evidence@example.com", "loc5-evidence")
        cls.provider = make_provider("traqo", "Traqo")
        equipment_type()

    def test_repeated_evidence_for_one_place_becomes_one_task(self):
        for index in range(4):
            _event(self.team, provider=self.provider, name="GOTHENBURG", fingerprint=f"fp-rep-{index}")

        quality = get_location_data_quality(self.team)
        self.assertEqual(len(quality.unresolved_evidence), 1)
        row = quality.unresolved_evidence[0]
        self.assertEqual(row.raw_name, "GOTHENBURG")
        self.assertEqual(row.event_count, 4)
        self.assertEqual(quality.unresolved_total, 1)

    def test_the_task_counts_the_distinct_containers_affected(self):
        first = make_container(self.team, with_check_digit("MSKU300000"))
        second = make_container(self.team, with_check_digit("MSKU300001"))
        for index, container in enumerate([first, first, second]):
            _event(
                self.team,
                provider=self.provider,
                name="GOTHENBURG",
                container=container,
                fingerprint=f"fp-cnt-{index}",
            )

        row = get_location_data_quality(self.team).unresolved_evidence[0]
        self.assertEqual(row.event_count, 3)
        self.assertEqual(row.container_count, 2)

    def test_the_task_carries_the_most_recent_occurrence(self):
        from datetime import timedelta

        old = timezone.now() - timedelta(days=30)
        recent = timezone.now() - timedelta(days=1)
        _event(self.team, provider=self.provider, name="GOTHENBURG", occurred_at=old, fingerprint="fp-old")
        _event(self.team, provider=self.provider, name="GOTHENBURG", occurred_at=recent, fingerprint="fp-new")

        row = get_location_data_quality(self.team).unresolved_evidence[0]
        self.assertEqual(row.last_seen_at, recent)

    def test_two_providers_naming_the_same_place_are_two_decisions(self):
        """An alias belongs to one source. Maersk's spelling is not Traqo's."""
        maersk = make_provider("maersk", "Maersk")
        _event(self.team, provider=self.provider, name="GOTHENBURG", fingerprint="fp-traqo")
        _event(self.team, provider=maersk, name="GOTHENBURG", fingerprint="fp-maersk")

        rows = get_location_data_quality(self.team).unresolved_evidence
        self.assertEqual({row.provider_code for row in rows}, {"traqo", "maersk"})

    def test_ambiguous_and_unresolved_are_separate_queues(self):
        _event(self.team, provider=self.provider, name="GOTHENBURG", fingerprint="fp-u")
        _event(
            self.team,
            provider=self.provider,
            name="OCEAN TERMINAL",
            status=LocationResolutionStatus.AMBIGUOUS,
            fingerprint="fp-a",
        )

        quality = get_location_data_quality(self.team)
        self.assertEqual([row.raw_name for row in quality.unresolved_evidence], ["GOTHENBURG"])
        self.assertEqual([row.raw_name for row in quality.ambiguous_evidence], ["OCEAN TERMINAL"])

    def test_resolved_evidence_is_not_work(self):
        _event(
            self.team,
            provider=self.provider,
            name="GOTHENBURG",
            status=LocationResolutionStatus.RESOLVED,
            fingerprint="fp-res",
        )
        quality = get_location_data_quality(self.team)
        self.assertEqual(quality.unresolved_evidence, [])
        self.assertEqual(quality.summary.evidence_resolved, 1)

    def test_evidence_that_names_no_place_is_not_a_location_problem(self):
        """A transport document is released nowhere. There is nothing to decide."""
        _event(self.team, provider=self.provider, name="", unlocode="", fingerprint="fp-nowhere")
        quality = get_location_data_quality(self.team)
        self.assertEqual(quality.unresolved_evidence, [])
        self.assertEqual(quality.summary.evidence_unresolved, 0)

    def test_evidence_with_only_a_code_is_visible_but_offers_no_alias(self):
        """An alias matches on the reported name. A bare code needs a location edit."""
        _event(self.team, provider=self.provider, unlocode="SEGOT", fingerprint="fp-code")
        row = get_location_data_quality(self.team).unresolved_evidence[0]
        self.assertEqual(row.label, "SEGOT")
        self.assertFalse(row.can_record_alias)


class AmbiguousCandidateTest(TestCase):
    """The SEGOT case: legitimate plurality made visible rather than suppressed."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("ambiguous@example.com", "loc5-ambiguous")
        cls.provider = make_provider("traqo", "Traqo")

    def _ambiguous_row(self, name="GOTHENBURG", unlocode="SEGOT"):
        _event(
            self.team,
            provider=self.provider,
            name=name,
            unlocode=unlocode,
            status=LocationResolutionStatus.AMBIGUOUS,
            fingerprint="fp-amb",
        )
        return get_location_data_quality(self.team).ambiguous_evidence[0]

    def test_two_unrelated_locations_sharing_a_code_are_shown_as_candidates(self):
        port = make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        terminal = make_location(self.team, "Oceanterminalen", unlocode="SEGOT", location_type=LocationType.DEPOT)

        row = self._ambiguous_row()

        self.assertTrue(row.is_ambiguous)
        self.assertEqual({candidate.pk for candidate in row.candidates}, {port.pk, terminal.pk})

    def test_a_hierarchy_is_not_an_ambiguity(self):
        """A code names a port, not a berth. The resolver says so, and so does this.

        Once the terminal is recorded as sitting inside the port, ``SEGOT`` has one
        answer and there is no tie left to show — which is the resolver's own rule
        (``_narrow_to_outermost``) reaching the queue rather than being restated in it.
        """
        port = make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        make_location(
            self.team,
            "Oceanterminalen",
            unlocode="SEGOT",
            location_type=LocationType.DEPOT,
            parent=port,
        )
        self.assertEqual(self._ambiguous_row().candidates, [])

    def test_an_ambiguity_with_nothing_to_match_shows_no_candidates(self):
        """Ambiguity recorded against master data that has since gone. Still a row."""
        row = self._ambiguous_row()
        self.assertTrue(row.is_ambiguous)
        self.assertEqual(row.candidates, [])
        self.assertTrue(row.can_record_alias)


@override_settings(STORAGES=TEST_STORAGES)
class EvidenceAliasActionTest(TestCase):
    """Recording a row of the queue as an alias, through the existing mechanism."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("alias@example.com", "loc5-alias")
        cls.provider = make_provider("traqo", "Traqo")
        cls.port = make_location(cls.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        _event(self.team, provider=self.provider, name="GOTHENBURG", unlocode="SEGOT", fingerprint="fp-alias")

    def _post(self, **overrides):
        data = {"source": "traqo", "external_name": "GOTHENBURG", "location": str(self.port.pk)}
        data.update(overrides)
        return self.client.post(reverse("containers:location_evidence_alias"), data=data)

    def test_the_form_loads_with_the_evidence_stated(self):
        response = self.client.get(
            reverse("containers:location_evidence_alias"),
            {"source": "traqo", "name": "GOTHENBURG", "unlocode": "SEGOT"},
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GOTHENBURG")
        self.assertContains(response, "Göteborg")

    def test_an_operator_can_map_evidence_to_an_existing_location(self):
        response = self._post()
        self.assertEqual(response.status_code, 302)
        alias = LocationAlias.objects.get(team=self.team)
        self.assertEqual(alias.location, self.port)
        self.assertEqual(alias.source, "traqo")
        self.assertEqual(alias.normalized_name, "gothenburg")

    def test_the_alias_uses_the_existing_domain_mechanism(self):
        """Same row, same constraints, same normalisation as the location's own form."""
        self._post()
        alias = LocationAlias.objects.get(team=self.team)
        self.assertEqual(alias.external_name, "GOTHENBURG")
        # Nothing is filed under external_code: the tracking pipeline never sends a
        # provider code, so a UN/LOCODE there would sit where no lookup reads it.
        self.assertEqual(alias.external_code, "")

    def test_no_canonical_location_is_created(self):
        before = ContainerLocation.objects.count()
        self._post()
        self.assertEqual(ContainerLocation.objects.count(), before)

    def test_the_stored_evidence_is_not_rewritten(self):
        event = TrackingEvent.objects.get(team=self.team)
        self._post()
        event.refresh_from_db()
        self.assertEqual(event.location_name, "GOTHENBURG")
        self.assertIsNone(event.location_id)
        self.assertEqual(event.location_resolution_status, LocationResolutionStatus.UNRESOLVED)

    def test_a_string_no_evidence_names_is_refused(self):
        """Otherwise the queue's POST is a general-purpose alias endpoint."""
        response = self._post(external_name="ATLANTIS")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No unmatched evidence")
        self.assertFalse(LocationAlias.objects.filter(team=self.team).exists())

    def test_a_source_that_reported_nothing_is_refused(self):
        response = self._post(source="cma-cgm")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LocationAlias.objects.filter(team=self.team).exists())

    def test_resolved_evidence_is_not_a_licence_to_alias(self):
        TrackingEvent.objects.filter(team=self.team).update(
            location_resolution_status=LocationResolutionStatus.RESOLVED
        )
        self._post()
        self.assertFalse(LocationAlias.objects.filter(team=self.team).exists())

    def test_a_duplicate_is_a_message_rather_than_a_second_row(self):
        create_location_alias(self.team, self.port, {"source": "traqo", "external_name": "gothenburg"})
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LocationAlias.objects.filter(team=self.team).count(), 1)

    def test_an_htmx_submission_is_sent_back_to_the_queue(self):
        response = self._post_htmx()
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], reverse("visibility:location_quality"))

    def _post_htmx(self):
        return self.client.post(
            reverse("containers:location_evidence_alias"),
            data={"source": "traqo", "external_name": "GOTHENBURG", "location": str(self.port.pk)},
            headers={"hx-request": "true"},
        )

    def test_the_queue_reports_the_decision_as_taken(self):
        self._post()
        row = get_location_data_quality(self.team).unresolved_evidence[0]
        self.assertTrue(row.is_aliased)
        self.assertEqual(row.alias_location, self.port)
        self.assertFalse(row.can_record_alias)

    def test_an_alias_to_a_deactivated_location_is_not_a_decision(self):
        """It will not resolve, so saying the work is done would be wrong."""
        self._post()
        self.port.is_active = False
        self.port.save(update_fields=["is_active"])
        self.assertFalse(get_location_data_quality(self.team).unresolved_evidence[0].is_aliased)


class ResolutionAfterAliasTest(TestCase):
    """What the alias actually changes: the next resolution, and only that."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("resolve@example.com", "loc5-resolve")
        cls.provider = make_provider("traqo", "Traqo")
        cls.port = make_location(cls.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        cls.terminal = make_location(cls.team, "Oceanterminalen", unlocode="SEGOT", location_type=LocationType.DEPOT)

    def _query(self, name, unlocode="SEGOT"):
        return LocationQuery(source="traqo", name=name, unlocode=unlocode)

    def test_the_evidence_is_ambiguous_before_the_alias(self):
        resolution = resolve_location(self.team, self._query("GOTHENBURG"))
        self.assertEqual(resolution.status, LocationResolutionStatus.AMBIGUOUS)

    def test_the_resolver_uses_the_alias_afterwards(self):
        create_location_alias(self.team, self.terminal, {"source": "traqo", "external_name": "GOTHENBURG"})
        resolution = resolve_location(self.team, self._query("GOTHENBURG"))
        self.assertTrue(resolution.is_resolved)
        self.assertEqual(resolution.location, self.terminal)

    def test_an_alias_beats_the_code_it_arrived_with(self):
        """Somebody decided this. It outranks anything derived — including SEGOT."""
        create_location_alias(self.team, self.terminal, {"source": "traqo", "external_name": "OCEAN TERMINAL"})
        resolution = resolve_location(self.team, self._query("OCEAN TERMINAL"))
        self.assertEqual(resolution.location, self.terminal)

    def test_one_sources_alias_does_not_speak_for_another(self):
        create_location_alias(self.team, self.terminal, {"source": "traqo", "external_name": "JOHN EVANS"})
        resolution = resolve_location(self.team, LocationQuery(source="maersk", name="JOHN EVANS"))
        self.assertFalse(resolution.is_resolved)

    def test_another_teams_alias_never_resolves_my_evidence(self):
        other_user, other_team = make_user_and_team("other-resolve@example.com", "loc5-resolve-theirs")
        theirs = make_location(other_team, "Their Depot")
        create_location_alias(other_team, theirs, {"source": "traqo", "external_name": "GOTHENBURG"})
        resolution = resolve_location(self.team, self._query("GOTHENBURG"))
        self.assertEqual(resolution.status, LocationResolutionStatus.AMBIGUOUS)


@override_settings(STORAGES=TEST_STORAGES)
class HistoricReResolutionTest(TestCase):
    """Whether evidence already stored can pick up an alias — through what exists.

    LOC-5 invents no bulk rewrite. Two mechanisms already re-resolve an event, both
    of them the ordinary write path rather than a special one:

    * a **refresh** — the next sync of the same event, which
      ``_update_existing_event`` re-derives the canonical location for; and
    * a **re-parse** — ``reparse_tracking_payloads``, which reads the stored provider
      response again through that same ingestion path.

    This class pins the first, because it is the one an operator relies on without
    running anything: record the alias, and the next time the carrier mentions the
    event the link appears. The management command is the same code with a different
    trigger.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("historic@example.com", "loc5-historic")
        cls.container = make_container(cls.team)
        ingest_maersk_events(cls.team, cls.container)
        cls.depot = make_location(cls.team, "John Evans Depot")

    def _segot_events(self):
        return TrackingEvent.objects.filter(team=self.team, location_unlocode="SEGOT")

    def test_the_fixture_starts_with_unresolved_location_evidence(self):
        self.assertTrue(self._segot_events().exists())
        self.assertFalse(self._segot_events().exclude(location__isnull=True).exists())

    def test_the_queue_finds_that_evidence(self):
        quality = get_location_data_quality(self.team)
        self.assertTrue(quality.unresolved_evidence)
        self.assertTrue(all(row.provider_code == "maersk" for row in quality.unresolved_evidence))

    def test_a_refresh_applies_the_alias_to_the_events_already_stored(self):
        row = next(row for row in get_location_data_quality(self.team).unresolved_evidence if row.raw_name)
        create_location_alias(self.team, self.depot, {"source": row.provider_code, "external_name": row.raw_name})

        # The next sync of the same payload. Same fingerprints, same rows.
        ingest_maersk_events(self.team, self.container)

        refreshed = TrackingEvent.objects.filter(team=self.team, location_name=row.raw_name)
        self.assertTrue(refreshed.exists())
        for event in refreshed:
            self.assertEqual(event.location_id, self.depot.pk)
            self.assertEqual(event.location_resolution_status, LocationResolutionStatus.RESOLVED)

    def test_a_refresh_does_not_duplicate_the_events(self):
        before = TrackingEvent.objects.filter(team=self.team).count()
        ingest_maersk_events(self.team, self.container)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), before)


@override_settings(STORAGES=TEST_STORAGES)
class LocationQualityPageTest(TestCase):
    """The page, and the two ways out of it."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("page@example.com", "loc5-page")
        cls.provider = make_provider("traqo", "Traqo")
        cls.depot = make_location(cls.team, "John Evans Depot")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_page_is_at_the_visibility_route(self):
        self.assertEqual(reverse("visibility:location_quality"), "/scm/visibility/location-quality/")

    def test_it_requires_a_login(self):
        self.assertNotEqual(Client().get(reverse("visibility:location_quality")).status_code, 200)

    def test_it_renders_the_four_sections(self):
        response = self.client.get(reverse("visibility:location_quality"))
        for heading in (
            "Location data quality",
            "Locations missing coordinates",
            "Ambiguous location evidence",
            "Unresolved location evidence",
        ):
            with self.subTest(heading=heading):
                self.assertContains(response, heading)

    def test_a_missing_coordinate_row_links_to_the_location_form(self):
        response = self.client.get(reverse("visibility:location_quality"))
        self.assertContains(response, "John Evans Depot")
        self.assertContains(
            response,
            f"{reverse('containers:location_update', args=[self.depot.pk])}?return_to=location_quality",
        )

    def test_an_evidence_row_offers_the_alias_action(self):
        _event(self.team, provider=self.provider, name="GOTHENBURG", fingerprint="fp-page")
        response = self.client.get(reverse("visibility:location_quality"))
        self.assertContains(response, "Map to location")
        self.assertContains(response, reverse("containers:location_evidence_alias"))

    def test_saving_coordinates_returns_to_the_queue(self):
        response = self.client.post(
            reverse("containers:location_update", args=[self.depot.pk]),
            data={
                "name": "John Evans Depot",
                "location_type": LocationType.DEPOT,
                "latitude": OCEANTERMINALEN[0],
                "longitude": OCEANTERMINALEN[1],
                "is_active": True,
                "return_to": "location_quality",
            },
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], reverse("visibility:location_quality"))
        self.depot.refresh_from_db()
        self.assertIsNotNone(self.depot.latitude)

    def test_an_impossible_coordinate_is_still_refused_by_the_model(self):
        """Validation stays on ContainerLocation.clean, wherever the form is opened."""
        response = self.client.post(
            reverse("containers:location_update", args=[self.depot.pk]),
            data={
                "name": "John Evans Depot",
                "location_type": LocationType.DEPOT,
                "latitude": "200",
                "longitude": "11.9",
                "is_active": True,
                "return_to": "location_quality",
            },
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Latitude must be between -90 and 90.")
        self.depot.refresh_from_db()
        self.assertIsNone(self.depot.latitude)

    def test_a_return_flag_can_only_mean_a_page_we_named(self):
        """A flag, not a URL. There is nothing here to redirect openly to."""
        response = self.client.post(
            reverse("containers:location_update", args=[self.depot.pk]),
            data={
                "name": "John Evans Depot",
                "location_type": LocationType.DEPOT,
                "is_active": True,
                "return_to": "https://example.com/",
            },
            headers={"hx-request": "true"},
        )
        self.assertNotIn("HX-Redirect", response)

    def test_an_ambiguous_row_names_the_places_it_could_not_choose_between(self):
        """The SEGOT case, on screen. Shown, and not chosen from on the reader's behalf."""
        make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        make_location(self.team, "Oceanterminalen", unlocode="SEGOT", location_type=LocationType.DEPOT)
        _event(
            self.team,
            provider=self.provider,
            name="GOTHENBURG",
            unlocode="SEGOT",
            status=LocationResolutionStatus.AMBIGUOUS,
            fingerprint="fp-page-amb",
        )

        response = self.client.get(reverse("visibility:location_quality"))

        self.assertContains(response, "Could be:")
        self.assertContains(response, "Göteborg")
        self.assertContains(response, "Oceanterminalen")

    def test_a_row_whose_decision_is_taken_says_so_rather_than_offering_it_again(self):
        port = make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        _event(self.team, provider=self.provider, name="GOTHENBURG", fingerprint="fp-page-alias")
        create_location_alias(self.team, port, {"source": "traqo", "external_name": "GOTHENBURG"})

        response = self.client.get(reverse("visibility:location_quality"))

        self.assertContains(response, "Alias set")
        self.assertNotContains(response, "Map to location")

    def test_evidence_with_only_a_code_is_pointed_at_the_location_list(self):
        _event(self.team, provider=self.provider, unlocode="SEGOT", fingerprint="fp-page-code")
        response = self.client.get(reverse("visibility:location_quality"))
        self.assertContains(response, "Set this code on a location")

    def test_the_page_says_so_when_there_is_nothing_to_fix(self):
        self.depot.latitude = OCEANTERMINALEN[0]
        self.depot.longitude = OCEANTERMINALEN[1]
        self.depot.save()
        response = self.client.get(reverse("visibility:location_quality"))
        self.assertContains(response, "Nothing to fix")


@override_settings(STORAGES=TEST_STORAGES, MAPBOX_PUBLIC_TOKEN="pk.test-token")
class ControlTowerLinkTest(TestCase):
    """The Control Tower leads here rather than growing a second map."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("tower@example.com", "loc5-tower")
        cls.container = make_container(cls.team)
        ingest_maersk_events(cls.team, cls.container)
        cls.depot = make_location(cls.team, "John Evans Depot")
        place_container_at(cls.team, cls.container, cls.depot)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_coverage_line_links_into_the_workflow(self):
        response = self.client.get(reverse("visibility:overview"))
        self.assertContains(response, "Fix location coverage")
        self.assertContains(response, reverse("visibility:location_quality"))

    def test_it_still_names_the_locations_to_fix(self):
        """LOC-4's behaviour is preserved, not replaced by a generic link."""
        response = self.client.get(reverse("visibility:overview"))
        self.assertContains(response, "John Evans Depot")


class LocationQualityIsolationTest(TestCase):
    """One team's location evidence is not another's to see or to decide."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("mine@example.com", "loc5-mine")
        cls.other_user, cls.other_team = make_user_and_team("theirs@example.com", "loc5-theirs")
        cls.provider = make_provider("traqo", "Traqo")

        cls.mine = make_location(cls.team, "My Depot")
        cls.theirs = make_location(cls.other_team, "Their Depot")
        _event(cls.team, provider=cls.provider, name="MY PLACE", fingerprint="fp-mine")
        _event(cls.other_team, provider=cls.provider, name="THEIR PLACE", fingerprint="fp-theirs")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_queue_shows_only_my_evidence(self):
        rows = get_location_data_quality(self.team).unresolved_evidence
        self.assertEqual([row.raw_name for row in rows], ["MY PLACE"])

    def test_the_queue_shows_only_my_locations(self):
        gaps = get_location_data_quality(self.team).coordinate_gaps
        self.assertEqual([gap.location.pk for gap in gaps], [self.mine.pk])

    def test_the_summary_counts_only_my_data(self):
        summary = get_location_quality_summary(self.team)
        self.assertEqual(summary.locations_missing_coordinates, 1)
        self.assertEqual(summary.evidence_unresolved, 1)

    def test_the_page_does_not_render_another_teams_place(self):
        response = self.client.get(reverse("visibility:location_quality"))
        self.assertContains(response, "MY PLACE")
        self.assertNotContains(response, "THEIR PLACE")
        self.assertNotContains(response, "Their Depot")

    def test_i_cannot_alias_another_teams_evidence(self):
        response = self.client.post(
            reverse("containers:location_evidence_alias"),
            data={"source": "traqo", "external_name": "THEIR PLACE", "location": str(self.mine.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LocationAlias.objects.exists())

    def test_i_cannot_alias_my_evidence_to_another_teams_location(self):
        response = self.client.post(
            reverse("containers:location_evidence_alias"),
            data={"source": "traqo", "external_name": "MY PLACE", "location": str(self.theirs.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LocationAlias.objects.exists())

    def test_i_cannot_edit_another_teams_location_from_my_queue(self):
        response = self.client.post(
            reverse("containers:location_update", args=[self.theirs.pk]),
            data={"name": "Renamed", "location_type": LocationType.DEPOT, "is_active": True},
        )
        self.assertEqual(response.status_code, 404)

    def test_the_evidence_gate_is_team_scoped(self):
        self.assertTrue(has_unmatched_evidence(self.team, source="traqo", raw_name="MY PLACE"))
        self.assertFalse(has_unmatched_evidence(self.team, source="traqo", raw_name="THEIR PLACE"))


class LocationQualityQueryTest(TestCase):
    """The page is built from aggregates, so its cost cannot follow the data."""

    team: Team

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("queries@example.com", "loc5-queries")
        cls.provider = make_provider("traqo", "Traqo")
        make_location(cls.team, "John Evans Depot")

    def _cost(self) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            get_location_data_quality(self.team)
        return len(captured)

    def test_repeated_evidence_for_one_place_does_not_cost_more(self):
        """Four hundred events naming Göteborg are one aggregated row, and one read."""
        for index in range(5):
            _event(self.team, provider=self.provider, name="GOTHENBURG", fingerprint=f"fp-v1-{index}")
        first = self._cost()

        for index in range(40):
            _event(self.team, provider=self.provider, name="GOTHENBURG", fingerprint=f"fp-v2-{index}")

        self.assertEqual(self._cost(), first)

    def test_more_unresolved_places_do_not_cost_more(self):
        for index in range(3):
            _event(self.team, provider=self.provider, name=f"PLACE {index}", fingerprint=f"fp-p1-{index}")
        first = self._cost()

        for index in range(3, 20):
            _event(self.team, provider=self.provider, name=f"PLACE {index}", fingerprint=f"fp-p2-{index}")

        self.assertEqual(self._cost(), first)

    def test_the_resolver_is_asked_only_about_the_ambiguities_on_screen(self):
        """The one per-row cost there is, and the display cap is what bounds it.

        An ambiguous row carries the resolver's own candidate list, which means one
        resolver call each. Past the cap the queue stops asking, so a team with a
        hundred ambiguities pays for the twenty-five it is being shown.
        """
        from apps.scm.visibility.location_quality import EVIDENCE_LIMIT

        for index in range(EVIDENCE_LIMIT + 5):
            _event(
                self.team,
                provider=self.provider,
                name=f"TIE {index}",
                status=LocationResolutionStatus.AMBIGUOUS,
                fingerprint=f"fp-a1-{index}",
            )
        capped = self._cost()

        for index in range(EVIDENCE_LIMIT + 5, EVIDENCE_LIMIT + 25):
            _event(
                self.team,
                provider=self.provider,
                name=f"TIE {index}",
                status=LocationResolutionStatus.AMBIGUOUS,
                fingerprint=f"fp-a2-{index}",
            )

        self.assertEqual(self._cost(), capped)

    def test_more_locations_missing_coordinates_do_not_cost_more(self):
        first = self._cost()
        for index in range(20):
            make_location(self.team, f"Depot {index}")
        self.assertEqual(self._cost(), first)

    def test_the_summary_alone_is_two_queries(self):
        """What the Locations list pays to point at this queue."""
        with self.assertNumQueries(2):
            get_location_quality_summary(self.team)


class HierarchyHintTest(TestCase):
    """LOC-6: telling a *structural* tie apart from a naming one.

    An alias fixes one provider's word for one place. A parent relationship fixes the
    code for every provider at once. The queue is allowed to say which of the two is
    missing — and only when the resolver's own answer says so, because "these two look
    related" is exactly the inference this whole area refuses to make.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("hier@example.com", "loc5-hierarchy")
        cls.provider = make_provider("traqo", "Traqo")

    def _row(self, *, name="GOTHENBURG", unlocode="SEGOT"):
        _event(
            self.team,
            provider=self.provider,
            name=name,
            unlocode=unlocode,
            status=LocationResolutionStatus.AMBIGUOUS,
            fingerprint=f"fp-hier-{name}-{unlocode}",
        )
        return get_location_data_quality(self.team).ambiguous_evidence[0]

    def _two_unrelated_places(self):
        port = make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        terminal = make_location(self.team, "Oceanterminalen", unlocode="SEGOT", location_type=LocationType.TERMINAL)
        return port, terminal

    def test_a_shared_code_with_no_containment_is_flagged_for_hierarchy_review(self):
        port, terminal = self._two_unrelated_places()
        row = self._row()
        self.assertTrue(row.needs_hierarchy_review)
        self.assertEqual({candidate.pk for candidate in row.candidates}, {port.pk, terminal.pk})
        self.assertEqual(row.shared_identity, "SEGOT")

    def test_the_flag_carries_the_rule_that_produced_the_tie(self):
        self._two_unrelated_places()
        self.assertEqual(self._row().candidate_method, LocationResolutionMethod.UNLOCODE)

    def test_recording_the_containment_removes_the_hint_immediately(self):
        """Before any event has been re-resolved: the hint is a function of the
        current master data, which is why it can be trusted to disappear.

        The row itself stays — its stored status is still ``AMBIGUOUS``, because
        nothing here rewrites evidence — but the tie is gone and so is the suggestion.
        """
        port, terminal = self._two_unrelated_places()
        update_location(location=terminal, data={"parent_location": port})
        row = self._row()
        self.assertFalse(row.needs_hierarchy_review)
        self.assertFalse(row.has_candidates)
        self.assertTrue(row.is_ambiguous)

    def test_a_tie_between_places_with_no_shared_code_is_not_a_hierarchy_issue(self):
        """Two locations of the same name is a naming problem. An alias fixes it."""
        make_location(self.team, "Central")
        make_location(self.team, "Central")
        row = self._row(name="Central", unlocode="")
        self.assertTrue(row.is_ambiguous)
        self.assertFalse(row.needs_hierarchy_review)
        self.assertTrue(row.can_record_alias)

    def test_a_coordinate_tie_is_never_a_hierarchy_issue(self):
        """Nearby coordinates are not evidence of containment, so nothing is offered.

        Evidence groups are keyed on the named place and never on a fix, so a
        coordinate tie cannot even reach this hint — asserted so that stays true.
        """
        make_location(self.team, "Göteborg", latitude=OCEANTERMINALEN[0], longitude=OCEANTERMINALEN[1])
        make_location(self.team, "Skandiahamnen", latitude=OCEANTERMINALEN[0], longitude=OCEANTERMINALEN[1])
        row = self._row(name="SOMEWHERE", unlocode="")
        self.assertFalse(row.needs_hierarchy_review)

    def test_contradicting_aliases_are_not_a_hierarchy_issue(self):
        """The master data disagrees with itself; a parent would not settle it."""
        port, terminal = self._two_unrelated_places()
        create_location_alias(self.team, port, {"source": "traqo", "external_name": "GOTHENBURG"})
        create_location_alias(self.team, terminal, {"source": "traqo", "external_code": "OT1"})
        _event(
            self.team,
            provider=self.provider,
            name="GOTHENBURG",
            unlocode="SEGOT",
            status=LocationResolutionStatus.AMBIGUOUS,
            fingerprint="fp-hier-alias-clash",
        )
        row = get_location_data_quality(self.team).ambiguous_evidence[0]
        self.assertEqual(row.candidate_method, LocationResolutionMethod.ALIAS)
        self.assertFalse(row.needs_hierarchy_review)

    def test_an_ambiguity_with_nothing_left_to_relate_is_not_flagged(self):
        """One candidate, or none, is not a structural tie."""
        make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        self.assertFalse(self._row().needs_hierarchy_review)

    def test_unresolved_evidence_is_never_flagged(self):
        self._two_unrelated_places()
        _event(self.team, provider=self.provider, name="ATLANTIS", fingerprint="fp-hier-unresolved")
        row = next(r for r in get_location_data_quality(self.team).unresolved_evidence if r.raw_name == "ATLANTIS")
        self.assertFalse(row.needs_hierarchy_review)

    def test_the_page_counts_the_rows_a_relationship_would_settle(self):
        self._two_unrelated_places()
        self._row()
        self.assertEqual(get_location_data_quality(self.team).hierarchy_review_count, 1)

    def test_a_three_level_tie_does_not_cost_more_to_hint_at(self):
        """The hint reads the resolver's answer; it does not walk the tree per row."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._two_unrelated_places()
        _event(
            self.team,
            provider=self.provider,
            name="GOTHENBURG",
            unlocode="SEGOT",
            status=LocationResolutionStatus.AMBIGUOUS,
            fingerprint="fp-hier-cost",
        )
        with CaptureQueriesContext(connection) as flat:
            get_location_data_quality(self.team)

        deep = make_location(self.team, "Skandiahamnen", unlocode="SEGOT", location_type=LocationType.TERMINAL)
        for index in range(3):
            deep = make_location(self.team, f"Level {index}", unlocode="SEGOT", parent=deep)
        with CaptureQueriesContext(connection) as nested:
            get_location_data_quality(self.team)

        self.assertEqual(len(nested.captured_queries), len(flat.captured_queries))


class HierarchyImpactTest(TestCase):
    """The read-model estimate shown beside the parent selector."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("impact@example.com", "loc5-impact")
        cls.provider = make_provider("traqo", "Traqo")
        cls.container = make_container(cls.team)

    def setUp(self):
        self.port = make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        self.terminal = make_location(
            self.team, "Oceanterminalen", unlocode="SEGOT", location_type=LocationType.TERMINAL
        )

    def _ambiguous(self, count=1, unlocode="SEGOT", name="GOTHENBURG"):
        for index in range(count):
            _event(
                self.team,
                provider=self.provider,
                name=f"{name} {index}",
                unlocode=unlocode,
                status=LocationResolutionStatus.AMBIGUOUS,
                container=self.container,
                fingerprint=f"fp-impact-{unlocode}-{name}-{index}",
            )

    def test_it_reports_the_places_sharing_the_code_and_the_evidence_behind_it(self):
        self._ambiguous(count=2)
        impact = get_hierarchy_impact(self.team, self.terminal)
        self.assertTrue(impact.is_relevant)
        self.assertEqual([place.pk for place in impact.unrelated], [self.port.pk])
        self.assertEqual(impact.ambiguous_groups, 2)
        self.assertEqual(impact.event_count, 2)
        self.assertEqual(impact.container_count, 1)
        self.assertEqual(impact.shares_code_with, "Göteborg")

    def test_a_location_with_no_code_has_nothing_to_estimate(self):
        depot = make_location(self.team, "John Evans Depot")
        self.assertFalse(get_hierarchy_impact(self.team, depot).is_relevant)

    def test_a_code_nobody_else_carries_has_nothing_to_estimate(self):
        self._ambiguous()
        alone = make_location(self.team, "Rotterdam", unlocode="NLRTM", location_type=LocationType.PORT)
        self.assertFalse(get_hierarchy_impact(self.team, alone).is_relevant)

    def test_an_already_recorded_relationship_has_nothing_left_to_settle(self):
        self._ambiguous()
        update_location(location=self.terminal, data={"parent_location": self.port})
        self.assertFalse(get_hierarchy_impact(self.team, self.terminal).is_relevant)
        self.assertFalse(get_hierarchy_impact(self.team, self.port).is_relevant)

    def test_evidence_about_a_different_code_is_not_counted(self):
        self._ambiguous(unlocode="NLRTM", name="ROTTERDAM")
        impact = get_hierarchy_impact(self.team, self.terminal)
        self.assertEqual(impact.ambiguous_groups, 0)
        self.assertFalse(impact.is_relevant)

    def test_resolved_evidence_is_not_counted(self):
        _event(
            self.team,
            provider=self.provider,
            name="GOTHENBURG",
            unlocode="SEGOT",
            status=LocationResolutionStatus.RESOLVED,
            fingerprint="fp-impact-resolved",
        )
        self.assertEqual(get_hierarchy_impact(self.team, self.terminal).ambiguous_groups, 0)

    def test_another_teams_evidence_and_places_are_never_counted(self):
        other_user, other_team = make_user_and_team("impact-theirs@example.com", "loc5-impact-theirs")
        make_location(other_team, "Their Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        _event(
            other_team,
            provider=self.provider,
            name="GOTHENBURG",
            unlocode="SEGOT",
            status=LocationResolutionStatus.AMBIGUOUS,
            fingerprint="fp-impact-theirs",
        )
        impact = get_hierarchy_impact(self.team, self.terminal)
        self.assertEqual([place.pk for place in impact.unrelated], [self.port.pk])
        self.assertEqual(impact.ambiguous_groups, 0)

    def test_the_estimate_changes_nothing(self):
        """A preview that re-resolved would be a write dressed as a read."""
        self._ambiguous()
        event = TrackingEvent.objects.get(team=self.team, event_fingerprint="fp-impact-SEGOT-GOTHENBURG-0")
        get_hierarchy_impact(self.team, self.terminal)
        event.refresh_from_db()
        self.assertIsNone(event.location_id)
        self.assertEqual(event.location_resolution_status, LocationResolutionStatus.AMBIGUOUS)

    def test_it_costs_a_bounded_number_of_queries(self):
        """Four: the places sharing the code, the subtree, the grouped evidence and
        the containers. None of them a fleet read, and none per evidence row."""
        self._ambiguous(count=3)
        with self.assertNumQueries(4):
            get_hierarchy_impact(self.team, self.terminal)


@override_settings(STORAGES=TEST_STORAGES)
class HierarchyReviewPageTest(TestCase):
    """The route from an ambiguous row to the place where it gets fixed."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("hierpage@example.com", "loc5-hier-page")
        cls.provider = make_provider("traqo", "Traqo")

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.port = make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        self.terminal = make_location(
            self.team, "Oceanterminalen", unlocode="SEGOT", location_type=LocationType.TERMINAL
        )
        _event(
            self.team,
            provider=self.provider,
            name="GOTHENBURG",
            unlocode="SEGOT",
            status=LocationResolutionStatus.AMBIGUOUS,
            fingerprint="fp-hier-page",
        )

    def test_the_queue_names_the_hierarchy_problem(self):
        response = self.client.get(reverse("visibility:location_quality"))
        self.assertContains(response, "Potential hierarchy issue")
        self.assertContains(response, "no containment")

    def test_each_candidate_offers_a_way_into_its_own_edit_form(self):
        response = self.client.get(reverse("visibility:location_quality"))
        self.assertContains(response, "Review hierarchy")
        for candidate in (self.port, self.terminal):
            with self.subTest(candidate=candidate.name):
                self.assertContains(
                    response,
                    f"{reverse('containers:location_update', args=[candidate.pk])}?return_to=location_quality",
                )

    def test_the_form_previews_what_the_relationship_would_settle(self):
        response = self.client.get(
            f"{reverse('containers:location_update', args=[self.terminal.pk])}?return_to=location_quality",
            headers={"hx-request": "true"},
        )
        self.assertContains(response, "Recording one may resolve")
        self.assertContains(response, "1 ambiguous evidence group")
        self.assertContains(response, "Göteborg — SEGOT — Port")

    def test_saving_the_parent_returns_to_a_freshly_built_queue(self):
        response = self.client.post(
            reverse("containers:location_update", args=[self.terminal.pk]),
            data={
                "name": "Oceanterminalen",
                "location_type": LocationType.TERMINAL,
                "unlocode": "SEGOT",
                "parent_location": str(self.port.pk),
                "is_active": True,
                "return_to": "location_quality",
            },
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], reverse("visibility:location_quality"))
        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.parent_location, self.port)

    def test_the_hint_is_gone_from_the_queue_once_the_parent_is_recorded(self):
        update_location(location=self.terminal, data={"parent_location": self.port})
        response = self.client.get(reverse("visibility:location_quality"))
        self.assertNotContains(response, "Potential hierarchy issue")

    def test_the_queue_never_records_a_parent_itself(self):
        """No path on this page writes the hierarchy — it opens the form."""
        self.client.get(reverse("visibility:location_quality"))
        self.terminal.refresh_from_db()
        self.assertIsNone(self.terminal.parent_location_id)

    def test_a_form_with_no_hierarchy_problem_shows_no_preview(self):
        depot = make_location(self.team, "John Evans Depot")
        response = self.client.get(
            reverse("containers:location_update", args=[depot.pk]), headers={"hx-request": "true"}
        )
        self.assertNotContains(response, "Recording one may resolve")

    def test_creating_a_location_shows_no_preview(self):
        response = self.client.get(reverse("containers:location_create"), headers={"hx-request": "true"})
        self.assertNotContains(response, "Recording one may resolve")


@override_settings(STORAGES=TEST_STORAGES)
class HistoricHierarchyReResolutionTest(TestCase):
    """LOC-6 through the mechanism LOC-5 established: nothing rewrites stored events.

    The claim being pinned is the whole reason a hierarchy edit is worth making at
    all: an event the resolver refused to place, a parent recorded afterwards, and the
    *same row* resolving on the next ingestion of the same payload. No bulk rewrite,
    no second row, no new fingerprint.

    Ingestion is the same function ``reparse_tracking_payloads`` calls, so the command
    is this test with a different trigger. Its own known caveat is unchanged and not
    LOC-6's to fix: an event the provider gave no event ID is fingerprinted from the
    fields it reported, so a *parser* correction to one of those fields produces a new
    fingerprint and leaves the old row behind, which is what ``--prune-superseded``
    exists for. A hierarchy edit changes none of those fields — it changes what the
    resolver concludes about them — so it cannot trigger that path.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.team = make_user_and_team("hier-historic@example.com", "loc6-historic")
        cls.container = make_container(cls.team)

    def setUp(self):
        # Two places carrying the code the fixture reports, and nothing relating
        # them: the state in which SEGOT cannot be resolved.
        self.port = make_location(self.team, "Göteborg", unlocode="SEGOT", location_type=LocationType.PORT)
        self.terminal = make_location(
            self.team, "Oceanterminalen", unlocode="SEGOT", location_type=LocationType.TERMINAL
        )
        ingest_maersk_events(self.team, self.container)

    def _segot_events(self):
        return TrackingEvent.objects.filter(team=self.team, location_unlocode="SEGOT")

    def test_the_evidence_starts_ambiguous(self):
        events = self._segot_events()
        self.assertTrue(events.exists())
        for event in events:
            self.assertEqual(event.location_resolution_status, LocationResolutionStatus.AMBIGUOUS)
            self.assertIsNone(event.location_id)

    def test_the_queue_offers_the_hierarchy_for_it(self):
        rows = [row for row in get_location_data_quality(self.team).ambiguous_evidence if row.raw_unlocode == "SEGOT"]
        self.assertTrue(rows)
        self.assertTrue(all(row.needs_hierarchy_review for row in rows))

    def test_recording_the_parent_does_not_touch_the_stored_events(self):
        before = {event.pk: event.location_resolution_status for event in self._segot_events()}
        update_location(location=self.terminal, data={"parent_location": self.port})
        after = {event.pk: event.location_resolution_status for event in self._segot_events()}
        self.assertEqual(after, before)

    def test_the_next_ingestion_resolves_the_same_rows_in_place(self):
        ids_before = set(self._segot_events().values_list("pk", flat=True))
        update_location(location=self.terminal, data={"parent_location": self.port})

        ingest_maersk_events(self.team, self.container)

        events = self._segot_events()
        self.assertEqual(set(events.values_list("pk", flat=True)), ids_before)
        for event in events:
            self.assertEqual(event.location_id, self.port.pk)
            self.assertEqual(event.location_resolution_status, LocationResolutionStatus.RESOLVED)

    def test_re_ingestion_does_not_duplicate_the_events(self):
        before = TrackingEvent.objects.filter(team=self.team).count()
        update_location(location=self.terminal, data={"parent_location": self.port})
        ingest_maersk_events(self.team, self.container)
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), before)

    def test_the_queue_is_empty_of_that_tie_afterwards(self):
        update_location(location=self.terminal, data={"parent_location": self.port})
        ingest_maersk_events(self.team, self.container)
        remaining = [
            row for row in get_location_data_quality(self.team).ambiguous_evidence if row.raw_unlocode == "SEGOT"
        ]
        self.assertEqual(remaining, [])
