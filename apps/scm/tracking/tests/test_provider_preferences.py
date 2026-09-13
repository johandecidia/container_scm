"""Choosing a container's tracking provider: what may be chosen, and what routing then does.

Four things are pinned down here.

*Only valid options are offered.* A direct carrier appears for a container only when
it is the carrier actually moving that box, the team's integration is active, and it
holds credentials. Carrier identity and tracking provider are separate facts and the
option list is where they have to agree.

*An explicit choice is obeyed.* Traqo means Traqo even where the carrier could be
called directly — which is the whole point of the setting, since direct otherwise
always wins.

*An explicit choice does not fall back.* A chosen provider that cannot be asked gives
an unavailable route naming the choice, not a quiet substitution. That is the
difference between a visible configuration problem and a container silently tracked
through a provider somebody deselected.

*Nothing changes without a choice.* The default path is the routing that already
existed: direct first, the team default second, nothing third.
"""

from django.test import TestCase, override_settings

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.credentials import set_integration_credentials
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.scm.integrations.services import connect_carrier_integration
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
from apps.scm.tracking.models import CarrierSource, TeamTrackingSettings
from apps.scm.tracking.preferences import (
    TEAM_DEFAULT,
    InvalidTrackingProvider,
    get_allowed_provider_codes,
    get_container_provider_override,
    get_provider_options,
    get_team_default_provider,
    set_container_provider_override,
    set_team_default_provider,
)
from apps.scm.tracking.provider_routing import (
    AGGREGATOR,
    DIRECT,
    DIRECT_PROVIDER_AVAILABLE,
    NO_PROVIDER_AVAILABLE,
    NONE,
    OVERRIDE_PROVIDER_AVAILABLE,
    OVERRIDE_PROVIDER_UNAVAILABLE,
    resolve_tracking_route,
)
from apps.teams.models import Team

TRAQO_LIVE = {"TRAQO_ENABLED": True, "TRAQO_API_KEY": "preference-key"}
TRAQO_OFF = {"TRAQO_ENABLED": False, "TRAQO_API_KEY": ""}


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, serial="123456") -> Container:
    return Container.objects.create(
        team=team,
        owner_code="MRK",
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit("MRK", "U", serial),
        equipment_type=_equipment_type(),
    )


class PreferenceBase(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="prefs", slug="prefs")
        self.container = _container(self.team)

    def connect_carrier(self, provider_code="maersk", *, active=True, credentials=True):
        """Connect a direct carrier the way Settings → Tracking does."""
        if credentials:
            integration = connect_carrier_integration(self.team, provider_code, {"api_key": "k"})
        else:
            integration = Integration.objects.create(
                team=self.team,
                name=provider_code,
                provider_code=provider_code,
                provider_family=Integration.ProviderFamily.CARRIER,
                is_active=True,
            )
        if not active:
            integration.is_active = False
            integration.save(update_fields=["is_active"])
        return integration

    def verify_carrier(self, carrier_code="maersk", provider_code=None):
        """Give the container a verified source, which is how its carrier becomes known."""
        return get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=provider_code or carrier_code,
            provider_name=carrier_code,
            carrier_code=carrier_code,
            carrier_name=carrier_code,
            carrier_source=CarrierSource.DIRECT_API,
        )


class TeamDefaultTest(PreferenceBase):
    """Every team has a default provider, and it is Traqo until somebody changes it."""

    def test_a_team_starts_on_traqo(self):
        self.assertEqual(get_team_default_provider(self.team), TRAQO_PROVIDER_CODE)

    def test_the_settings_row_is_created_on_first_read(self):
        self.assertFalse(TeamTrackingSettings.objects.filter(team=self.team).exists())
        get_team_default_provider(self.team)
        self.assertEqual(TeamTrackingSettings.objects.filter(team=self.team).count(), 1)

    def test_reading_twice_does_not_create_two_rows(self):
        get_team_default_provider(self.team)
        get_team_default_provider(self.team)
        self.assertEqual(TeamTrackingSettings.objects.filter(team=self.team).count(), 1)

    def test_a_provider_the_poller_cannot_fetch_is_refused_as_a_default(self):
        # Vizion can track and is deliberately not schedulable — see tracking.sources.
        with self.assertRaises(InvalidTrackingProvider):
            set_team_default_provider(self.team, "vizion")
        with self.assertRaises(InvalidTrackingProvider):
            set_team_default_provider(self.team, "maersk")

    def test_traqo_can_be_set_explicitly(self):
        set_team_default_provider(self.team, TRAQO_PROVIDER_CODE)
        self.assertEqual(get_team_default_provider(self.team), TRAQO_PROVIDER_CODE)

    def test_the_default_is_per_team(self):
        other = Team.objects.create(name="other", slug="other-prefs")
        get_team_default_provider(self.team)
        self.assertEqual(get_team_default_provider(other), TRAQO_PROVIDER_CODE)
        self.assertEqual(TeamTrackingSettings.objects.count(), 2)


@override_settings(**TRAQO_LIVE)
class AllowedProviderTest(PreferenceBase):
    """Only providers that could actually answer for this container are offered."""

    def test_traqo_is_offered_whenever_it_is_configured(self):
        self.assertIn(TRAQO_PROVIDER_CODE, get_allowed_provider_codes(self.team, self.container))

    @override_settings(**TRAQO_OFF)
    def test_traqo_is_not_offered_when_it_is_not_configured(self):
        self.assertNotIn(TRAQO_PROVIDER_CODE, get_allowed_provider_codes(self.team, self.container))

    def test_a_direct_carrier_is_not_offered_while_the_containers_carrier_is_unknown(self):
        self.connect_carrier("maersk")
        self.assertEqual(get_allowed_provider_codes(self.team, self.container), {TRAQO_PROVIDER_CODE})

    def test_a_direct_carrier_is_offered_once_it_is_the_containers_carrier(self):
        self.connect_carrier("maersk")
        self.verify_carrier("maersk")
        self.assertEqual(get_allowed_provider_codes(self.team, self.container), {TRAQO_PROVIDER_CODE, "maersk"})

    def test_another_connected_carrier_is_not_offered(self):
        """One carrier is moving the box; the team's other integrations are irrelevant."""
        self.connect_carrier("maersk")
        self.connect_carrier("hapag_lloyd", credentials=False)
        set_integration_credentials(
            Integration.objects.get(team=self.team, provider_code="hapag_lloyd"),
            IntegrationCredential.AuthType.CUSTOM,
            {"client_id": "a", "client_secret": "b"},
        )
        self.verify_carrier("maersk")
        self.assertNotIn("hapag_lloyd", get_allowed_provider_codes(self.team, self.container))

    def test_a_deactivated_carrier_is_not_offered(self):
        self.connect_carrier("maersk", active=False)
        self.verify_carrier("maersk")
        self.assertEqual(get_allowed_provider_codes(self.team, self.container), {TRAQO_PROVIDER_CODE})

    def test_a_carrier_without_credentials_is_not_offered(self):
        self.connect_carrier("maersk", credentials=False)
        self.verify_carrier("maersk")
        self.assertEqual(get_allowed_provider_codes(self.team, self.container), {TRAQO_PROVIDER_CODE})

    def test_the_option_list_always_starts_with_the_team_default(self):
        options = get_provider_options(self.team, self.container)
        self.assertEqual(options[0].value, TEAM_DEFAULT)
        self.assertIn("Traqo", str(options[0].label))
        self.assertTrue(options[0].is_selected)

    def test_a_stored_override_that_is_no_longer_valid_is_shown_as_unavailable(self):
        self.connect_carrier("maersk")
        self.verify_carrier("maersk")
        set_container_provider_override(team=self.team, container=self.container, provider_code="maersk")
        Integration.objects.filter(team=self.team, provider_code="maersk").update(is_active=False)

        selected = [o for o in get_provider_options(self.team, self.container) if o.is_selected]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].value, "maersk")
        self.assertIn("unavailable", str(selected[0].label))


@override_settings(**TRAQO_LIVE)
class SetOverrideTest(PreferenceBase):
    """Storing the preference, and refusing what cannot be stored."""

    def test_a_container_starts_on_the_team_default(self):
        self.assertEqual(get_container_provider_override(self.container), TEAM_DEFAULT)

    def test_traqo_can_be_chosen(self):
        set_container_provider_override(team=self.team, container=self.container, provider_code=TRAQO_PROVIDER_CODE)
        self.container.refresh_from_db()
        self.assertEqual(self.container.tracking_provider_override, TRAQO_PROVIDER_CODE)

    def test_the_override_can_be_cleared(self):
        set_container_provider_override(team=self.team, container=self.container, provider_code=TRAQO_PROVIDER_CODE)
        set_container_provider_override(team=self.team, container=self.container, provider_code="")
        self.container.refresh_from_db()
        self.assertEqual(self.container.tracking_provider_override, "")

    def test_a_provider_that_cannot_answer_for_this_container_is_refused(self):
        with self.assertRaises(InvalidTrackingProvider):
            set_container_provider_override(team=self.team, container=self.container, provider_code="maersk")
        self.container.refresh_from_db()
        self.assertEqual(self.container.tracking_provider_override, "")

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(InvalidTrackingProvider):
            set_container_provider_override(team=self.team, container=self.container, provider_code="not-a-provider")


@override_settings(**TRAQO_LIVE)
class OverrideRoutingTest(PreferenceBase):
    """What routing does with a stored preference."""

    def route(self, carrier_code="maersk"):
        return resolve_tracking_route(team=self.team, carrier_code=carrier_code, container=self.container)

    # -- existing behaviour, unchanged ---------------------------------------

    def test_without_an_override_a_connected_carrier_still_wins(self):
        self.connect_carrier("maersk")
        route = self.route("maersk")
        self.assertEqual(route.provider_code, "maersk")
        self.assertEqual(route.route_type, DIRECT)
        self.assertEqual(route.reason, DIRECT_PROVIDER_AVAILABLE)
        self.assertFalse(route.is_override)

    def test_without_an_override_an_unconnected_carrier_falls_through_to_the_team_default(self):
        route = self.route("one")
        self.assertEqual(route.provider_code, TRAQO_PROVIDER_CODE)
        self.assertEqual(route.route_type, AGGREGATOR)
        self.assertFalse(route.is_override)

    def test_routing_without_a_container_is_unaffected(self):
        """Callers that do not start from a container see the routing they always did."""
        self.connect_carrier("maersk")
        set_container_provider_override(team=self.team, container=self.container, provider_code=TRAQO_PROVIDER_CODE)
        route = resolve_tracking_route(team=self.team, carrier_code="maersk")
        self.assertEqual(route.provider_code, "maersk")
        self.assertFalse(route.is_override)

    # -- explicit Traqo -------------------------------------------------------

    def test_an_explicit_traqo_override_beats_a_connected_direct_carrier(self):
        self.connect_carrier("maersk")
        set_container_provider_override(team=self.team, container=self.container, provider_code=TRAQO_PROVIDER_CODE)

        route = self.route("maersk")

        self.assertEqual(route.provider_code, TRAQO_PROVIDER_CODE)
        self.assertEqual(route.route_type, AGGREGATOR)
        self.assertEqual(route.reason, OVERRIDE_PROVIDER_AVAILABLE)
        self.assertTrue(route.is_override)
        # The sealine is what Traqo has to be asked with, and it is the carrier's.
        self.assertEqual(route.provider_reference, "MAEU")
        self.assertEqual(route.carrier_code, "maersk")

    def test_an_explicit_traqo_override_offers_no_alternatives(self):
        """A chosen provider is not a search, so there is nothing a fallback may try."""
        self.connect_carrier("maersk")
        set_container_provider_override(team=self.team, container=self.container, provider_code=TRAQO_PROVIDER_CODE)
        self.assertEqual(self.route("maersk").alternatives, ())

    def test_an_explicit_traqo_override_does_not_fall_back_when_traqo_goes_away(self):
        self.connect_carrier("maersk")
        set_container_provider_override(team=self.team, container=self.container, provider_code=TRAQO_PROVIDER_CODE)
        with self.settings(**TRAQO_OFF):
            route = self.route("maersk")

        self.assertFalse(route.available)
        self.assertEqual(route.provider_code, "")
        self.assertEqual(route.route_type, NONE)
        self.assertEqual(route.reason, OVERRIDE_PROVIDER_UNAVAILABLE)
        self.assertEqual(route.requested_provider_code, TRAQO_PROVIDER_CODE)

    def test_an_explicit_traqo_override_is_unavailable_for_a_carrier_it_has_no_sealine_for(self):
        """Evergreen is registered here and has no Traqo sealine — see traqo/sealines.py."""
        set_container_provider_override(team=self.team, container=self.container, provider_code=TRAQO_PROVIDER_CODE)

        route = resolve_tracking_route(team=self.team, carrier_code="evergreen", container=self.container)

        self.assertFalse(route.available)
        self.assertEqual(route.reason, OVERRIDE_PROVIDER_UNAVAILABLE)
        self.assertEqual(route.requested_provider_code, TRAQO_PROVIDER_CODE)

    # -- explicit direct provider --------------------------------------------

    def test_an_explicit_direct_override_routes_to_that_carrier(self):
        self.connect_carrier("maersk")
        self.verify_carrier("maersk")
        set_container_provider_override(team=self.team, container=self.container, provider_code="maersk")

        route = self.route("maersk")

        self.assertEqual(route.provider_code, "maersk")
        self.assertEqual(route.route_type, DIRECT)
        self.assertEqual(route.reason, OVERRIDE_PROVIDER_AVAILABLE)
        self.assertTrue(route.is_override)

    def test_a_direct_override_whose_integration_is_deactivated_does_not_fall_back_to_traqo(self):
        """The important one: the provider somebody deselected must not quietly return."""
        self.connect_carrier("maersk")
        self.verify_carrier("maersk")
        set_container_provider_override(team=self.team, container=self.container, provider_code="maersk")
        Integration.objects.filter(team=self.team, provider_code="maersk").update(is_active=False)

        route = self.route("maersk")

        self.assertFalse(route.available)
        self.assertNotEqual(route.provider_code, TRAQO_PROVIDER_CODE)
        self.assertEqual(route.reason, OVERRIDE_PROVIDER_UNAVAILABLE)
        self.assertEqual(route.requested_provider_code, "maersk")

    def test_a_direct_override_for_a_different_carrier_is_unavailable(self):
        """Carrier identity is not the tracking provider, and only one carrier moves the box."""
        self.connect_carrier("maersk")
        self.verify_carrier("maersk")
        set_container_provider_override(team=self.team, container=self.container, provider_code="maersk")
        # The box turns out to be moving with CMA CGM: Maersk cannot answer about it.
        route = resolve_tracking_route(team=self.team, carrier_code="cma_cgm", container=self.container)

        self.assertFalse(route.available)
        self.assertEqual(route.reason, OVERRIDE_PROVIDER_UNAVAILABLE)
        self.assertEqual(route.carrier_code, "cma_cgm")

    def test_an_unknown_carrier_is_still_unknown_with_an_override_set(self):
        set_container_provider_override(team=self.team, container=self.container, provider_code=TRAQO_PROVIDER_CODE)
        route = resolve_tracking_route(team=self.team, carrier_code="", container=self.container)
        self.assertFalse(route.available)
        self.assertNotEqual(route.reason, OVERRIDE_PROVIDER_UNAVAILABLE)


@override_settings(**TRAQO_OFF)
class NoProviderTest(PreferenceBase):
    """With nothing configured, the answer is still a clean "nobody can help"."""

    def test_no_override_and_no_provider_is_no_provider_available(self):
        route = resolve_tracking_route(team=self.team, carrier_code="one", container=self.container)
        self.assertFalse(route.available)
        self.assertEqual(route.reason, NO_PROVIDER_AVAILABLE)
        self.assertFalse(route.is_override)
