"""BBCU3273070 again — this time without paying Vizion for the answer.

The same real container, and the observation that motivated candidate probing:

    Traqo's free carrier lookup did not recognise BBCU3273070.
    Vizion's ACI was paid to identify ONE.
    Traqo then tracked the box perfectly, once told ``sealine=ONEY``.

Traqo had the shipment the whole time. It could not answer "who moves this" without
being told who to ask about — so the chain now asks it, about a few likely carriers,
before spending the team's direct rate limits or Vizion's reference.

What this file asserts is therefore as much about what does *not* happen:

    carrier                = one          (registry code; SCAC ONEY)
    carrier source         = TRAQO_PROBE  (not TRAQO_LOOKUP — Traqo returned the events)
    verified               = True
    provider               = traqo
    provider reference     = ONEY

    direct carrier discovery   never called
    Vizion ACI                 never called
    Traqo container endpoint   called exactly once, for the whole refresh

The last one is the point of the payload reuse: the probe proves the carrier by fetching
this container's tracking data, and activation must store *that* rather than ask Traqo
the same question a second time.

Nothing is mocked below the resolution chain's two injected discovery calls: the probe
and the tracking write both run for real, through the real Traqo client with an injected
session, so the request Traqo would receive is asserted on rather than assumed.
"""

import json
import pathlib
from unittest import mock

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.workspace import get_container_workspace
from apps.scm.integrations.carriers import carrier_resolution
from apps.scm.integrations.carriers.carrier_resolution import resolve_carrier_for_container
from apps.scm.integrations.models import Integration
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo import carrier_probe
from apps.scm.integrations.traqo import discovery as traqo_discovery
from apps.scm.integrations.traqo.client import TraqoClient
from apps.scm.tracking import activation as activation_module
from apps.scm.tracking.activation import activate_tracking_route
from apps.scm.tracking.manual_refresh import UPDATED, refresh_container_tracking
from apps.scm.tracking.models import (
    CarrierSource,
    TrackingEvent,
    TrackingRawPayload,
    TrackingSubscription,
    TrackingSyncRun,
)
from apps.scm.tracking.selectors import get_container_tracking_provenance
from apps.teams.models import Team

FIXTURES = pathlib.Path(__file__).parents[2] / "integrations" / "tests" / "fixtures" / "traqo"

CONTAINER_NUMBER = "BBCU3273070"
ONE_CODE = "one"
ONE_SCAC = "ONEY"

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "probe-acceptance"}}
TRAQO_LIVE = {"TRAQO_ENABLED": True, "TRAQO_API_KEY": "acceptance-key", "CACHES": _LOCMEM}


def one_payload() -> dict:
    """A Traqo container response for BBCU3273070 under sealine ONEY.

    The recorded sandbox envelope with this container's number and ONE as the sealine, so
    the mapper and the probe's verification rules are the real ones reading a real shape.
    """
    payload = json.loads((FIXTURES / "sandbox_container_MRSU6859427.json").read_text())
    data = payload["data"]
    data["reference_number"] = CONTAINER_NUMBER
    data["sealine"] = ONE_SCAC
    data["sealine_name"] = "ONE"
    for event in data.get("events_table") or []:
        event["container_number"] = CONTAINER_NUMBER
    return payload


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class TraqoSession:
    """Answers for ONEY and 404s for every other sealine, recording every request.

    The 404 matters as much as the hit: it is what Traqo returns for a container it has
    no shipment for, and it is how the probe is supposed to learn that a candidate is
    wrong. ``requests`` is the cost assertion — one entry per Traqo call actually made.
    """

    def __init__(self, payload=None, answering_sealine=ONE_SCAC):
        self.payload = payload if payload is not None else one_payload()
        self.answering_sealine = answering_sealine
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        self.requests.append({"url": url, "params": params})
        if params.get("sealine") != self.answering_sealine:
            return FakeResponse(404, {"success": False, "message": "No shipment found."})
        return FakeResponse(200, self.payload)

    @property
    def sealines_asked(self) -> list[str]:
        return [request["params"].get("sealine") for request in self.requests]


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _traqo_lookup_not_found(container_number):
    """What Traqo's free lookup actually returned for this container: nothing."""
    return traqo_discovery.TraqoCarrierDiscovery(
        container_number=container_number,
        status=traqo_discovery.NOT_FOUND,
        reason="No shipment found for this number.",
    )


@override_settings(**TRAQO_LIVE)
class Bbcu3273070ProbeResolutionTest(TestCase):
    """Step 1: establishing ONE from Traqo's container endpoint, without Vizion."""

    def setUp(self):
        self.team = Team.objects.create(name="probe", slug="probe-bbcu")
        self.container = Container.objects.create(
            team=self.team,
            owner_code="BBC",
            category_id="U",
            serial_number="327307",
            check_digit=0,
            equipment_type=_equipment_type(),
        )
        # A connected carrier that is not ONE. If the chain ever reached the direct sweep
        # it would have somebody real to ask — so this is what makes "never called" a
        # meaningful assertion rather than an empty one.
        Integration.objects.create(
            team=self.team,
            name="cosco",
            provider_code="cosco",
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        )
        self.session = TraqoSession()
        self.vizion_calls: list[str] = []
        self.direct_calls: list[str] = []

    def spy_vizion(self, container_number):  # pragma: no cover — asserted never to run
        self.vizion_calls.append(container_number)
        raise AssertionError("Vizion ACI was called after the Traqo probe had answered.")

    def probe(self, **kwargs):
        """The real probe, over the real client, with a session instead of a socket."""
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=self.session)
        candidates = carrier_probe.build_traqo_probe_candidates(
            container_number=kwargs.get("container_number", CONTAINER_NUMBER),
            preferred_carrier_codes=kwargs.get("preferred_carrier_codes", ()),
            exclude_carrier_codes=kwargs.get("exclude_carrier_codes", frozenset()),
        )
        return carrier_probe.probe_candidate_carriers(
            container_number=kwargs.get("container_number", CONTAINER_NUMBER),
            candidates=candidates,
            client=client,
        )

    def resolve(self):
        from apps.scm.integrations.carriers.exceptions import CarrierNoDataError
        from apps.scm.tracking.tests.test_manual_refresh import _fake_client, _patch_carriers

        def _spying_client(code, behaviour):
            client = _fake_client(code, behaviour)
            self.direct_calls.append(code)
            return client

        clients = {"cosco": _spying_client("cosco", CarrierNoDataError("404"))}
        self.direct_calls.clear()
        with _patch_carriers(clients, {}):
            resolution = resolve_carrier_for_container(
                team=self.team,
                container=self.container,
                clients=clients,
                traqo_lookup=_traqo_lookup_not_found,
                traqo_probe=self.probe,
                vizion_identify=self.spy_vizion,
                use_trusted_knowledge=False,
            )
        self.cosco_calls = clients["cosco"].calls
        return resolution

    def test_the_probe_establishes_one_where_the_lookup_could_not(self):
        resolution = self.resolve()

        self.assertEqual(resolution.carrier_code, ONE_CODE)
        self.assertEqual(resolution.source, CarrierSource.TRAQO_PROBE)
        self.assertIn("Ocean Network Express", resolution.carrier_name)

    def test_a_probe_hit_is_verified_because_traqo_returned_this_boxs_events(self):
        self.assertTrue(self.resolve().verified)

    def test_the_chain_records_the_lookup_failing_and_the_probe_answering(self):
        resolution = self.resolve()

        self.assertEqual(
            resolution.step_for(carrier_resolution.STEP_TRAQO_LOOKUP).outcome,
            carrier_resolution.NOT_FOUND,
        )
        self.assertEqual(
            resolution.step_for(carrier_resolution.STEP_TRAQO_PROBE).outcome,
            carrier_resolution.FOUND,
        )

    def test_one_leads_the_discovery_order_so_the_hit_costs_one_call(self):
        """ONE heads the default order precisely because of this container."""
        self.resolve()

        self.assertEqual(self.session.sealines_asked, [ONE_SCAC])

    def test_the_direct_sweep_never_runs(self):
        self.resolve()

        self.assertEqual(self.cosco_calls, [], "the direct sweep ran after the probe had answered")

    def test_vizion_is_never_called(self):
        """The saving this step exists for: no billable reference for this container."""
        self.resolve()

        self.assertEqual(self.vizion_calls, [])
        self.assertIsNone(self.resolve().step_for(carrier_resolution.STEP_VIZION_ACI))

    def test_the_resolution_carries_traqos_payload_and_handle(self):
        resolution = self.resolve()

        self.assertTrue(resolution.has_tracking_payload)
        self.assertEqual(resolution.tracking_provider_code, TRAQO_PROVIDER_CODE)
        self.assertEqual(resolution.provider_reference, ONE_SCAC)
        self.assertEqual(len(resolution.events), len(one_payload()["data"]["events_table"]))

    def test_resolution_still_writes_nothing(self):
        self.resolve()

        self.assertEqual(TrackingSubscription.objects.count(), 0)
        self.assertEqual(TrackingEvent.objects.count(), 0)


@override_settings(**TRAQO_LIVE)
class Bbcu3273070ProbeActivationTest(Bbcu3273070ProbeResolutionTest):
    """Steps 2 and 3: routing ONE to Traqo, and storing what the probe already fetched."""

    def activate(self):
        resolution = self.resolve()
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=self.session)
        result = activate_tracking_route(
            team=self.team,
            container=self.container,
            resolution=resolution,
            traqo_client=client,
        )
        return result, resolution

    def test_one_is_routed_to_traqo_even_though_traqo_already_answered(self):
        """Routing stays a separate decision; it simply reaches the same provider."""
        result, _resolution = self.activate()

        self.assertEqual(result.route.carrier_code, ONE_CODE)
        self.assertEqual(result.route.provider_code, TRAQO_PROVIDER_CODE)
        self.assertTrue(result.route.is_aggregator)

    def test_activation_asks_traqo_nothing_further(self):
        """The whole refresh is one Traqo call: the probe's. This is the reuse assertion."""
        self.activate()

        self.assertEqual(self.session.sealines_asked, [ONE_SCAC])

    def test_the_payload_reuse_is_reported_rather_than_implied(self):
        result, _resolution = self.activate()

        self.assertTrue(result.metadata.get("payload_reused"))
        self.assertEqual(result.metadata.get("sealine"), ONE_SCAC)

    def test_the_subscription_records_carrier_provider_and_provenance_separately(self):
        result, _resolution = self.activate()

        subscription = result.subscription
        self.assertEqual(subscription.carrier_code, ONE_CODE)
        self.assertEqual(subscription.carrier_source, CarrierSource.TRAQO_PROBE)
        self.assertEqual(subscription.provider.code, TRAQO_PROVIDER_CODE)
        self.assertFalse(subscription.is_direct)

    def test_the_sealine_that_answered_is_kept_for_the_next_fetch(self):
        """What scheduled polling will need in order to ask the question that worked."""
        result, _resolution = self.activate()

        self.assertEqual(result.subscription.provider_reference, ONE_SCAC)

    def test_events_land_in_the_existing_tracking_write_path(self):
        result, _resolution = self.activate()

        self.assertTrue(result.activated)
        events = TrackingEvent.objects.filter(team=self.team, container=self.container)
        self.assertGreater(events.count(), 0)
        self.assertEqual(events.count(), result.events_created)

    def test_the_events_belong_to_this_container_this_team_and_traqo(self):
        self.activate()

        for event in TrackingEvent.objects.all():
            self.assertEqual(event.container_id, self.container.pk)
            self.assertEqual(event.team_id, self.team.pk)
            self.assertEqual(event.provider.code, TRAQO_PROVIDER_CODE)

    def test_the_raw_payload_and_sync_run_are_recorded_like_any_other_source(self):
        result, _resolution = self.activate()

        self.assertTrue(TrackingRawPayload.objects.filter(team=self.team, subscription=result.subscription).exists())
        self.assertEqual(result.sync_run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(result.sync_run.provider.code, TRAQO_PROVIDER_CODE)

    def test_the_stored_payload_is_the_one_traqo_sent(self):
        result, _resolution = self.activate()

        stored = TrackingRawPayload.objects.filter(subscription=result.subscription).first()
        self.assertEqual(stored.payload_json["data"]["sealine"], ONE_SCAC)
        self.assertEqual(stored.payload_json["data"]["reference_number"], CONTAINER_NUMBER)

    def test_the_eta_traqo_published_is_recorded_as_it_is_on_the_ordinary_path(self):
        """Re-using the payload must not quietly lose what fetching it would have recorded.

        The ETA observation is written by the same function either way, which is the
        point of there being one Traqo write path rather than two.
        """
        from apps.scm.tracking.models import ETAHistory

        self.activate()

        self.assertTrue(ETAHistory.objects.filter(team=self.team, container=self.container).exists())

    def test_provenance_reads_as_one_tracked_via_traqo(self):
        self.activate()

        provenance = get_container_tracking_provenance(self.team, self.container)

        self.assertEqual(len(provenance), 1)
        entry = provenance[0]
        self.assertIn("ONE", entry.carrier_name)
        self.assertEqual(entry.carrier_source_label, "Traqo container tracking data")
        self.assertEqual(entry.provider_label, "Traqo Ocean")
        self.assertFalse(entry.is_direct)

    def test_the_workspace_shows_one_as_the_carrier_and_traqo_as_the_source(self):
        self.activate()

        workspace = get_container_workspace(self.team, self.container)

        self.assertIn("ONE", workspace.tracking_carrier_name)
        self.assertNotIn("Traqo", workspace.tracking_carrier_name)
        self.assertEqual(workspace.tracking_provider_label, "Traqo Ocean")


@override_settings(**TRAQO_LIVE)
class Bbcu3273070ProbeFromTheRefreshButtonTest(Bbcu3273070ProbeResolutionTest):
    """The same scenario through the button a person actually presses."""

    def refresh(self):
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=self.session)
        from apps.scm.integrations.carriers.exceptions import CarrierNoDataError
        from apps.scm.tracking.tests.test_manual_refresh import _fake_client, _patch_carriers

        clients = {"cosco": _fake_client("cosco", CarrierNoDataError("404"))}
        with (
            mock.patch.object(carrier_resolution, "_default_traqo_lookup", _traqo_lookup_not_found),
            mock.patch.object(carrier_resolution, "_default_traqo_probe", self.probe),
            mock.patch.object(carrier_resolution, "_default_vizion_identify", self.spy_vizion),
            mock.patch.object(activation_module, "activate_tracking_route", _with_traqo_client(client)),
            _patch_carriers(clients, {}),
        ):
            result = refresh_container_tracking(team=self.team, container=self.container)
        self.cosco_calls = clients["cosco"].calls
        return result

    def test_the_refresh_reports_the_carrier_and_the_provider(self):
        result = self.refresh()

        self.assertEqual(result.state, UPDATED)
        self.assertEqual(result.carrier_code, ONE_CODE)
        self.assertTrue(result.tracked)
        message = str(result.message)
        self.assertIn("ONE", message)
        self.assertIn("Traqo", message)

    def test_the_refresh_creates_one_traqo_watch_carrying_one(self):
        self.refresh()

        subscription = TrackingSubscription.objects.get(team=self.team, container=self.container)
        self.assertEqual(subscription.provider.code, TRAQO_PROVIDER_CODE)
        self.assertEqual(subscription.carrier_code, ONE_CODE)
        self.assertEqual(subscription.carrier_source, CarrierSource.TRAQO_PROBE)
        self.assertEqual(subscription.provider_reference, ONE_SCAC)

    def test_one_refresh_costs_exactly_one_traqo_container_call(self):
        self.refresh()

        self.assertEqual(self.session.sealines_asked, [ONE_SCAC])

    def test_the_refresh_reaches_neither_the_direct_sweep_nor_vizion(self):
        self.refresh()

        self.assertEqual(self.cosco_calls, [])
        self.assertEqual(self.vizion_calls, [])

    def test_pressing_refresh_twice_neither_duplicates_nor_re_probes(self):
        """A container that already has a verified watch must never be probed again.

        The cost this guards against is specific: five Traqo guesses on every press of
        the refresh button, for a container whose carrier was settled the first time.
        The second refresh reaches ``_sync_verified_subscriptions`` instead, which asks
        the sources it has — and a Traqo watch is not one the carrier poller drives (see
        ``apps/scm/tracking/sources.py``), so it spends no Traqo call at all here.
        """
        self.refresh()
        self.session.requests.clear()

        self.refresh()

        self.assertEqual(TrackingSubscription.objects.filter(team=self.team, container=self.container).count(), 1)
        self.assertEqual(
            self.session.sealines_asked,
            [],
            "a second refresh re-probed carriers instead of refreshing the watch it had",
        )

    def test_a_second_refresh_creates_no_duplicate_events(self):
        self.refresh()
        before = TrackingEvent.objects.count()

        self.refresh()

        self.assertEqual(TrackingEvent.objects.count(), before)


def _with_traqo_client(client):
    """Wrap activation so the Traqo fetch uses an injected client, changing nothing else."""
    real = activation_module.activate_tracking_route

    def _activate(**kwargs):
        kwargs.setdefault("traqo_client", client)
        return real(**kwargs)

    return _activate
