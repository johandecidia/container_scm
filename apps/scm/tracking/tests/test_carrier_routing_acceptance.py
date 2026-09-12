"""The BBCU3273070 acceptance case, end to end and from the button.

This is the scenario that motivated separating carrier identity from tracking provider,
and it is taken from a real run rather than invented:

    BBCU3273070 is moving with ONE.
    Traqo's carrier lookup did not find the container.
    Vizion's ACI identified ONE.
    ONE has no working direct tracking path in this installation.
    Traqo can nevertheless track it, once told the sealine.

So the expected behaviour is a carrier and a provider that are different names::

    carrier        = one          (canonical registry code; SCAC ONEY)
    carrier source = VIZION_ACI
    provider       = traqo

**What this file now pins down is the Vizion *fallback*.** The chain has since gained
Traqo candidate probing between the free lookup and the direct sweep, and for this very
container the probe answers — which is why it exists, and which is covered in
``test_traqo_probe_acceptance.py``. Here the probe is injected as finding nothing, so the
scenario is "not even Traqo's shipment endpoint has this box under any likely carrier".
Vizion must still be reached, still identify ONE, and still route to Traqo: the last
fallback has to keep working, and the step added in front of it must not have become the
only way there.

Every provider is faked. The three aggregator discovery calls are injected, and Traqo's
tracking fetch goes through the real client with an injected session — so the request
Traqo would actually receive, sealine included, is asserted on rather than assumed.
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
from apps.scm.integrations.traqo import discovery as traqo_discovery
from apps.scm.integrations.traqo.client import TraqoClient
from apps.scm.integrations.vizion import discovery as vizion_discovery
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

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "acceptance"}}
TRAQO_LIVE = {"TRAQO_ENABLED": True, "TRAQO_API_KEY": "acceptance-key", "CACHES": _LOCMEM}


def one_payload() -> dict:
    """A Traqo container response for BBCU3273070 under sealine ONEY.

    Built from the shape of the recorded sandbox response — the same envelope, with this
    container's number and ONE as the sealine — so the mapper under test is the real one
    reading a real payload shape.
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


class FakeSession:
    """Records every request, so the sealine actually sent can be asserted on."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else one_payload()
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "params": params or {}})
        return FakeResponse(200, self.payload)


def _equipment_type():
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _traqo_lookup_not_found(container_number):
    """What Traqo's lookup actually returned for this container: nothing."""
    return traqo_discovery.TraqoCarrierDiscovery(
        container_number=container_number,
        status=traqo_discovery.NOT_FOUND,
        reason="No shipment found for this number.",
    )


def _traqo_probe_finds_nothing(**kwargs):
    """No likely carrier has this box at Traqo either — so the chain must reach Vizion."""
    from apps.scm.integrations.traqo import carrier_probe

    return carrier_probe.TraqoCarrierProbeResult(
        container_number=kwargs.get("container_number", CONTAINER_NUMBER),
        outcome=carrier_probe.NOT_FOUND,
        attempts=tuple(
            carrier_probe.TraqoProbeAttempt(carrier_code=code, sealine=sealine, outcome=carrier_probe.NOT_FOUND)
            for code, sealine in (("one", "ONEY"), ("maersk", "MAEU"))
        ),
    )


def _vizion_identifies_one(container_number):
    """What Vizion's ACI actually returned: ONE, with a reference that cost money."""
    return vizion_discovery.VizionCarrierIdentification(
        container_number=container_number,
        status=vizion_discovery.FOUND,
        carrier_code=ONE_CODE,
        carrier_name="ONE (Ocean Network Express)",
        scac=ONE_SCAC,
        reference_id="vz-bbcu-3273070",
        aci_state="IDENTIFIED",
        polls=3,
    )


class AcceptanceBase(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="acceptance", slug="acceptance-bbcu")
        self.container = Container.objects.create(
            team=self.team,
            owner_code="BBC",
            category_id="U",
            serial_number="327307",
            check_digit=0,
            equipment_type=_equipment_type(),
        )
        # A connected carrier that is *not* ONE, so the direct sweep has somebody real to
        # ask and honestly answer NOT_FOUND — which is what makes the chain reach Vizion.
        Integration.objects.create(
            team=self.team,
            name="cosco",
            provider_code="cosco",
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        )
        self.vizion_calls: list[str] = []

    def spy_vizion(self, container_number):
        self.vizion_calls.append(container_number)
        return _vizion_identifies_one(container_number)

    def resolve(self):
        """Run the chain exactly as the acceptance scenario played out."""
        from apps.scm.integrations.carriers.exceptions import CarrierNoDataError
        from apps.scm.tracking.tests.test_manual_refresh import _fake_client, _patch_carriers

        clients = {"cosco": _fake_client("cosco", CarrierNoDataError("404"))}
        with _patch_carriers(clients, {}):
            return resolve_carrier_for_container(
                team=self.team,
                container=self.container,
                clients=clients,
                traqo_lookup=_traqo_lookup_not_found,
                traqo_probe=_traqo_probe_finds_nothing,
                vizion_identify=self.spy_vizion,
                use_trusted_knowledge=False,
            )


@override_settings(**TRAQO_LIVE)
class Bbcu3273070ResolutionTest(AcceptanceBase):
    """Step 1 of the scenario: working out that ONE is carrying the box."""

    def test_the_chain_reaches_vizion_and_resolves_one(self):
        resolution = self.resolve()

        self.assertEqual(resolution.carrier_code, ONE_CODE)
        self.assertEqual(resolution.source, CarrierSource.VIZION_ACI)
        self.assertIn("ONE", resolution.carrier_name)

    def test_each_step_answered_the_way_the_real_run_did(self):
        resolution = self.resolve()

        self.assertEqual(
            resolution.step_for(carrier_resolution.STEP_TRAQO_LOOKUP).outcome,
            carrier_resolution.NOT_FOUND,
        )
        self.assertEqual(
            resolution.step_for(carrier_resolution.STEP_TRAQO_PROBE).outcome,
            carrier_resolution.NOT_FOUND,
        )
        self.assertEqual(
            resolution.step_for(carrier_resolution.STEP_DIRECT_API).outcome,
            carrier_resolution.NOT_FOUND,
        )
        self.assertEqual(
            resolution.step_for(carrier_resolution.STEP_VIZION_ACI).outcome,
            carrier_resolution.FOUND,
        )

    def test_the_paid_reference_is_reported_rather_than_lost(self):
        self.assertEqual(self.resolve().vizion_reference_id, "vz-bbcu-3273070")

    def test_vizion_is_called_exactly_once(self):
        self.resolve()

        self.assertEqual(self.vizion_calls, [CONTAINER_NUMBER])


@override_settings(**TRAQO_LIVE)
class Bbcu3273070RoutingAndActivationTest(AcceptanceBase):
    """Steps 2 and 3: routing ONE to Traqo, and tracking it there."""

    def activate(self, resolution=None, session=None):
        resolution = resolution if resolution is not None else self.resolve()
        session = session if session is not None else FakeSession()
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=session)
        result = activate_tracking_route(
            team=self.team,
            container=self.container,
            resolution=resolution,
            traqo_client=client,
        )
        return result, session

    def test_one_is_routed_to_traqo_because_its_own_api_is_unavailable(self):
        result, _session = self.activate()

        self.assertEqual(result.route.carrier_code, ONE_CODE)
        self.assertEqual(result.route.provider_code, TRAQO_PROVIDER_CODE)
        self.assertTrue(result.route.is_aggregator)

    def test_traqo_is_told_which_carrier_to_ask_about(self):
        """The sealine on the wire. Without it Traqo answers about the wrong carrier."""
        _result, session = self.activate()

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.requests[0]["params"], {"sealine": ONE_SCAC})
        self.assertIn(CONTAINER_NUMBER, session.requests[0]["url"])

    def test_the_subscription_records_carrier_and_provider_as_different_things(self):
        """The central assertion of this whole change."""
        result, _session = self.activate()

        subscription = result.subscription
        self.assertEqual(subscription.carrier_code, ONE_CODE)
        self.assertEqual(subscription.carrier_source, CarrierSource.VIZION_ACI)
        self.assertEqual(subscription.provider.code, TRAQO_PROVIDER_CODE)
        self.assertFalse(subscription.is_direct)

    def test_the_sealine_is_kept_so_a_later_fetch_can_ask_again(self):
        result, _session = self.activate()

        self.assertEqual(result.subscription.provider_reference, ONE_SCAC)

    def test_events_land_in_the_existing_tracking_write_path(self):
        result, _session = self.activate()

        self.assertTrue(result.activated)
        events = TrackingEvent.objects.filter(team=self.team, container=self.container)
        self.assertGreater(events.count(), 0)
        self.assertEqual(events.count(), result.events_created)

    def test_the_events_belong_to_this_container_and_this_team(self):
        self.activate()

        for event in TrackingEvent.objects.all():
            self.assertEqual(event.container_id, self.container.pk)
            self.assertEqual(event.team_id, self.team.pk)
            self.assertEqual(event.provider.code, TRAQO_PROVIDER_CODE)

    def test_the_raw_payload_and_sync_run_are_recorded_like_any_other_source(self):
        result, _session = self.activate()

        self.assertTrue(TrackingRawPayload.objects.filter(team=self.team, subscription=result.subscription).exists())
        self.assertEqual(result.sync_run.status, TrackingSyncRun.Status.SUCCESS)
        self.assertEqual(result.sync_run.provider.code, TRAQO_PROVIDER_CODE)

    def test_a_retry_creates_no_second_subscription(self):
        """A double click must not produce two Traqo watches for one container."""
        first, _ = self.activate()
        second, _ = self.activate()

        self.assertEqual(first.subscription.pk, second.subscription.pk)
        self.assertEqual(TrackingSubscription.objects.filter(team=self.team, container=self.container).count(), 1)

    def test_a_retry_creates_no_duplicate_events(self):
        first, _ = self.activate()
        before = TrackingEvent.objects.count()

        second, _ = self.activate()

        self.assertEqual(TrackingEvent.objects.count(), before)
        self.assertEqual(second.events_created, 0)
        self.assertGreater(second.events_updated, 0)

    def test_a_retry_costs_no_further_vizion_call(self):
        """The carrier is already recorded, so the chain stops at trusted knowledge."""
        self.activate()
        self.vizion_calls.clear()

        from apps.scm.integrations.carriers.carrier_resolution import get_trusted_carrier_for_container

        code, _name, source = get_trusted_carrier_for_container(self.team, self.container)

        self.assertEqual(code, ONE_CODE)
        self.assertEqual(source, CarrierSource.EXISTING_VERIFIED_SOURCE)
        self.assertEqual(self.vizion_calls, [])

    def test_provenance_answers_all_three_questions_separately(self):
        self.activate()

        provenance = get_container_tracking_provenance(self.team, self.container)

        self.assertEqual(len(provenance), 1)
        entry = provenance[0]
        self.assertIn("ONE", entry.carrier_name)
        self.assertEqual(entry.carrier_source_label, "Vizion Auto Carrier Identification")
        self.assertEqual(entry.provider_label, "Traqo Ocean")
        self.assertFalse(entry.is_direct)

    def test_the_workspace_shows_one_as_the_carrier_and_traqo_as_the_source(self):
        """Traqo must never appear where the carrier belongs."""
        self.activate()

        workspace = get_container_workspace(self.team, self.container)

        self.assertIn("ONE", workspace.tracking_carrier_name)
        self.assertNotIn("Traqo", workspace.tracking_carrier_name)
        self.assertEqual(workspace.tracking_provider_label, "Traqo Ocean")
        self.assertFalse(workspace.tracking_is_direct)


@override_settings(**TRAQO_LIVE)
class Bbcu3273070FromTheRefreshButtonTest(AcceptanceBase):
    """The same scenario through the button a person actually presses."""

    def refresh(self):
        session = FakeSession()
        client = TraqoClient(base_url="https://traqocontainer.com/api/v1", api_key="k", session=session)
        with (
            mock.patch.object(carrier_resolution, "_default_traqo_lookup", _traqo_lookup_not_found),
            mock.patch.object(carrier_resolution, "_default_traqo_probe", _traqo_probe_finds_nothing),
            mock.patch.object(carrier_resolution, "_default_vizion_identify", self.spy_vizion),
            mock.patch.object(
                activation_module,
                "activate_tracking_route",
                _with_traqo_client(client),
            ),
            self._faked_direct_carriers(),
        ):
            return refresh_container_tracking(team=self.team, container=self.container), session

    def _faked_direct_carriers(self):
        from apps.scm.integrations.carriers.exceptions import CarrierNoDataError
        from apps.scm.tracking.tests.test_manual_refresh import _fake_client, _patch_carriers

        return _patch_carriers({"cosco": _fake_client("cosco", CarrierNoDataError("404"))}, {})

    def test_the_refresh_reports_the_carrier_and_the_provider(self):
        result, _session = self.refresh()

        self.assertEqual(result.state, UPDATED)
        self.assertEqual(result.carrier_code, ONE_CODE)
        self.assertTrue(result.tracked)
        message = str(result.message)
        self.assertIn("ONE", message)
        self.assertIn("Traqo", message)

    def test_the_refresh_creates_exactly_one_traqo_watch_carrying_one(self):
        self.refresh()

        subscription = TrackingSubscription.objects.get(team=self.team, container=self.container)
        self.assertEqual(subscription.provider.code, TRAQO_PROVIDER_CODE)
        self.assertEqual(subscription.carrier_code, ONE_CODE)
        self.assertEqual(subscription.carrier_source, CarrierSource.VIZION_ACI)

    def test_pressing_refresh_twice_neither_duplicates_nor_re_identifies(self):
        self.refresh()
        self.vizion_calls.clear()

        self.refresh()

        self.assertEqual(TrackingSubscription.objects.filter(team=self.team, container=self.container).count(), 1)
        self.assertEqual(self.vizion_calls, [], "a second refresh paid for another Vizion reference")


def _with_traqo_client(client):
    """Wrap activation so the Traqo fetch uses an injected client, changing nothing else.

    The production call passes no client and builds one from settings. Patching the
    function rather than the client keeps the whole of activation — routing, the
    subscription, the write path — under test.
    """
    real = activation_module.activate_tracking_route

    def _activate(**kwargs):
        kwargs.setdefault("traqo_client", client)
        return real(**kwargs)

    return _activate
