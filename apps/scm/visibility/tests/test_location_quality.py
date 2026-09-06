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

from apps.scm.containers.choices import LocationResolutionStatus, LocationType
from apps.scm.containers.location_resolver import LocationQuery, resolve_location
from apps.scm.containers.models import ContainerLocation, LocationAlias
from apps.scm.containers.services import create_location_alias
from apps.scm.tracking.models import TrackingEvent
from apps.scm.visibility.location_quality import (
    get_location_data_quality,
    get_location_quality_summary,
    has_unmatched_evidence,
)

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
