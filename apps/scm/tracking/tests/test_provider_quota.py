"""Traqo's shipment allowance: reading it, running out of it, and who may see it.

Three separable concerns, and the third is the one with teeth:

    parsing     the usage response, with ``used`` and ``active`` kept apart
    running out what a 402 does to an activation, a watch and the retry cadence
    secrecy     that the plan, allowance and billing URL reach a superuser and
                nobody else — asserted against rendered HTML, not just permissions

Every payload below is the one Traqo's keyless sandbox actually returns, recorded as a
fixture rather than paraphrased. Nothing here touches the network.
"""

import json
import pathlib
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.integrations.carriers.exceptions import (
    CarrierProviderBillingError,
    CarrierProviderQuotaError,
)
from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo.errors import classify_traqo_error
from apps.scm.integrations.traqo.usage import (
    TraqoUsageFetch,
    fetch_traqo_account_usage,
    parse_traqo_account_usage,
)
from apps.scm.tracking.models import CarrierSource, TrackingSubscription, TrackingSyncRun
from apps.scm.tracking.sources import USAGE_EXHAUSTED, USAGE_OK, USAGE_WARNING, ProviderUsage
from apps.scm.tracking.sync import outcome_for_carrier_error
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN, ROLE_MEMBER
from apps.users.models import CustomUser

FIXTURES = pathlib.Path(__file__).parents[2] / "integrations" / "tests" / "fixtures" / "traqo"
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "quota"}}

# Every substring that would mean our account's commercial position had leaked.
ACCOUNT_SECRETS = (
    "professional",
    "20 of 20",
    "manageUrl",
    "dashboard/billing",
    "shipment_limit_reached",
    "payment_overdue",
    "effective_limit",
    "addon_slots",
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def usage_payload() -> dict:
    return _fixture("sandbox_account_usage.json")


def limit_payload() -> dict:
    return _fixture("sandbox_402_shipment_limit_reached.json")


def overdue_payload() -> dict:
    return _fixture("sandbox_402_payment_overdue.json")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _container(team, owner="TRD", serial="925896") -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
    )


# ---------------------------------------------------------------------------
# Reading the allowance
# ---------------------------------------------------------------------------


class AccountUsageParsingTest(SimpleTestCase):
    """The recorded response, read into the model the status page renders."""

    def setUp(self):
        self.usage = parse_traqo_account_usage(usage_payload())

    def test_the_plan_and_cycle_are_read(self):
        self.assertEqual(self.usage.plan, "professional")
        self.assertEqual(self.usage.period_days, 30)
        self.assertEqual(self.usage.cycle_start.year, 2026)
        self.assertEqual(self.usage.cycle_start.month, 8)
        self.assertEqual(self.usage.cycle_end.month, 9)

    def test_the_allowance_is_read_from_the_payload_not_recomputed(self):
        """``effective_limit`` is what Traqo says enforcement compares against."""
        self.assertEqual(self.usage.limit, 20)
        self.assertEqual(self.usage.addon_slots, 5)
        self.assertEqual(self.usage.effective_limit, 25)

    def test_used_and_active_are_different_numbers(self):
        """The distinction the whole model exists to preserve."""
        self.assertEqual(self.usage.used, 12)
        self.assertEqual(self.usage.active, 9)
        self.assertNotEqual(self.usage.used, self.usage.active)

    def test_remaining_is_read_as_sent(self):
        self.assertEqual(self.usage.remaining, 13)

    def test_carried_slots_is_absent_on_a_monthly_plan_and_reads_as_zero(self):
        """Documented as required; genuinely missing from a monthly response."""
        self.assertNotIn("carried_slots", usage_payload()["data"]["shipments"])
        self.assertEqual(self.usage.carried_slots, 0)

    def test_the_sandbox_is_flagged_so_demo_numbers_are_not_mistaken_for_ours(self):
        self.assertTrue(self.usage.sandbox)

    def test_a_response_without_shipments_is_unreadable_rather_than_half_read(self):
        self.assertIsNone(parse_traqo_account_usage({"success": True, "data": {"plan": "x"}}))
        self.assertIsNone(parse_traqo_account_usage({}))

    def test_an_unparseable_cycle_bound_leaves_the_cycle_blank(self):
        payload = usage_payload()
        payload["data"]["cycle"]["start"] = "not-a-date"
        self.assertIsNone(parse_traqo_account_usage(payload).cycle_start)


class UsageStatusTest(SimpleTestCase):
    """The OK / WARNING / EXHAUSTED grade, which is ours and not Traqo's."""

    def _usage(self, *, effective_limit, used, remaining, active=0) -> ProviderUsage:
        return ProviderUsage(
            provider_code="traqo",
            provider_name="Traqo Ocean",
            effective_limit=effective_limit,
            used=used,
            active=active,
            remaining=remaining,
        )

    def test_plenty_left_is_ok(self):
        self.assertEqual(self._usage(effective_limit=25, used=12, remaining=13).status, USAGE_OK)

    def test_just_over_the_threshold_is_still_ok(self):
        self.assertEqual(self._usage(effective_limit=100, used=79, remaining=21).status, USAGE_OK)

    def test_exactly_a_fifth_left_is_a_warning(self):
        self.assertEqual(self._usage(effective_limit=100, used=80, remaining=20).status, USAGE_WARNING)

    def test_one_left_is_a_warning_not_exhausted(self):
        self.assertEqual(self._usage(effective_limit=25, used=24, remaining=1).status, USAGE_WARNING)

    def test_zero_remaining_is_exhausted(self):
        usage = self._usage(effective_limit=25, used=25, remaining=0)
        self.assertEqual(usage.status, USAGE_EXHAUSTED)
        self.assertTrue(usage.is_exhausted)

    def test_used_beyond_the_limit_with_few_active_is_still_exhausted(self):
        """Traqo's own example: 25 used, 10 active, 0 remaining is an ordinary state."""
        usage = self._usage(effective_limit=25, used=25, active=10, remaining=0)
        self.assertEqual(usage.status, USAGE_EXHAUSTED)


@override_settings(CACHES=_LOCMEM, TRAQO_ENABLED=True, TRAQO_API_KEY="usage-key")
class UsageFetchTest(TestCase):
    """The fetch always returns a state — a status page must never fail to render."""

    def test_a_good_response_yields_the_numbers(self):
        client = mock.Mock(get_account_usage=mock.Mock(return_value=usage_payload()))

        fetch = fetch_traqo_account_usage(client=client)

        self.assertTrue(fetch.available)
        self.assertEqual(fetch.usage.used, 12)
        self.assertEqual(fetch.provider_code, TRAQO_PROVIDER_CODE)

    def test_a_provider_error_becomes_a_state_not_an_exception(self):
        from apps.scm.integrations.carriers.exceptions import CarrierServerError

        client = mock.Mock(get_account_usage=mock.Mock(side_effect=CarrierServerError("502")))

        fetch = fetch_traqo_account_usage(client=client)

        self.assertFalse(fetch.available)
        self.assertTrue(fetch.configured)
        self.assertIn("CarrierServerError", fetch.error)

    @override_settings(TRAQO_ENABLED=False, TRAQO_API_KEY="")
    def test_an_unconfigured_provider_is_reported_as_such_without_being_called(self):
        fetch = fetch_traqo_account_usage()

        self.assertFalse(fetch.available)
        self.assertFalse(fetch.configured)
        self.assertEqual(fetch.provider_name, "Traqo Ocean")

    def test_an_unreadable_response_is_a_state_too(self):
        client = mock.Mock(get_account_usage=mock.Mock(return_value={"success": True}))

        fetch = fetch_traqo_account_usage(client=client)

        self.assertFalse(fetch.available)
        self.assertIn("cannot read", fetch.error)


# ---------------------------------------------------------------------------
# Running out
# ---------------------------------------------------------------------------


class Traqo402ClassificationTest(SimpleTestCase):
    """Both 402s, from the bodies Traqo actually sends."""

    def test_a_spent_allowance_is_a_provider_quota_error(self):
        error = classify_traqo_error(402, FakeResponse(402, limit_payload()))

        self.assertIsInstance(error, CarrierProviderQuotaError)
        self.assertFalse(error.transient)

    def test_an_unpaid_account_is_a_provider_billing_error(self):
        error = classify_traqo_error(402, FakeResponse(402, overdue_payload()))

        self.assertIsInstance(error, CarrierProviderBillingError)
        self.assertFalse(error.transient)

    def test_a_402_that_says_nothing_is_treated_as_the_case_a_retry_cannot_fix(self):
        error = classify_traqo_error(402, FakeResponse(402, {"success": False, "message": "Payment required."}))

        self.assertIsInstance(error, CarrierProviderBillingError)

    def test_the_account_facts_are_kept_structured_for_the_log_and_the_platform_view(self):
        error = classify_traqo_error(402, FakeResponse(402, limit_payload()))

        self.assertEqual(error.provider_detail["plan"], "professional")
        self.assertEqual(error.provider_detail["used"], 20)
        self.assertEqual(error.provider_detail["limit"], 20)

    def test_no_account_fact_reaches_the_safe_message(self):
        for payload in (limit_payload(), overdue_payload()):
            error = classify_traqo_error(402, FakeResponse(402, payload))
            with self.subTest(reason=error.provider_detail.get("error")):
                for secret in ACCOUNT_SECRETS:
                    self.assertNotIn(secret, error.safe_message)
                self.assertIn("system administrator", error.safe_message)

    def test_the_manage_url_is_never_carried_off_the_error_at_all(self):
        """It is the one field whose only purpose is to be clicked by whoever pays."""
        error = classify_traqo_error(402, FakeResponse(402, limit_payload()))

        self.assertNotIn("manageUrl", error.provider_detail)


class QuotaRetrySemanticsTest(SimpleTestCase):
    """A spent allowance must not be retried like a network blip."""

    def _classify(self, payload):
        error = classify_traqo_error(402, FakeResponse(402, payload))
        assert error is not None
        return outcome_for_carrier_error(error)

    def test_a_spent_allowance_is_skipped_rather_than_failed(self):
        """SKIPPED is what stops ``consecutive_failures`` driving the backoff ladder."""
        outcome = self._classify(limit_payload())

        self.assertEqual(outcome.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(outcome.error_type, TrackingSyncRun.ErrorType.PROVIDER_QUOTA)

    def test_an_unpaid_account_is_skipped_with_its_own_reason(self):
        outcome = self._classify(overdue_payload())

        self.assertEqual(outcome.status, TrackingSyncRun.Status.SKIPPED)
        self.assertEqual(outcome.error_type, TrackingSyncRun.ErrorType.PROVIDER_BILLING)

    def test_neither_is_labelled_a_rate_limit(self):
        for payload in (limit_payload(), overdue_payload()):
            with self.subTest():
                self.assertNotEqual(self._classify(payload).error_type, TrackingSyncRun.ErrorType.RATE_LIMIT)

    def test_the_stored_message_is_sanitised(self):
        for payload in (limit_payload(), overdue_payload()):
            outcome = self._classify(payload)
            with self.subTest():
                for secret in ACCOUNT_SECRETS:
                    self.assertNotIn(secret, outcome.error_message)


@override_settings(CACHES=_LOCMEM)
class QuotaDoesNotFakeTrackingTest(TestCase):
    """A container that could not be taken on is not tracked, and says so."""

    def setUp(self):
        self.team = Team.objects.create(name="quota", slug="quota-activation")
        self.container = _container(self.team)

    def test_a_skipped_quota_run_leaves_the_watch_out_of_the_due_queue(self):
        """The cadence a quota earns: slow, and not escalating."""
        from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
        from apps.scm.tracking.polling import INTERVAL_NOT_CONFIGURED, base_interval_minutes
        from apps.scm.tracking.services import create_sync_run
        from apps.scm.tracking.sync import apply_sync_outcome

        subscription = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=TRAQO_PROVIDER_CODE,
            provider_name="Traqo Ocean",
            carrier_code="one",
            carrier_name="ONE",
            carrier_source=CarrierSource.TRAQO_LOOKUP,
            provider_reference="ONEY",
        )
        sync_run = create_sync_run(team=self.team, subscription=subscription, provider=subscription.provider)
        outcome = outcome_for_carrier_error(classify_traqo_error(402, FakeResponse(402, limit_payload())))

        apply_sync_outcome(subscription, sync_run, outcome)
        subscription.refresh_from_db()

        # Not a failure, so nothing accumulates to escalate.
        self.assertEqual(subscription.consecutive_failures, 0)
        self.assertEqual(base_interval_minutes(subscription), INTERVAL_NOT_CONFIGURED)

    def test_the_message_on_the_watch_is_the_sanitised_one(self):
        """``last_error_message`` is rendered on the container workspace."""
        from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
        from apps.scm.tracking.services import create_sync_run
        from apps.scm.tracking.sync import apply_sync_outcome

        subscription = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=TRAQO_PROVIDER_CODE,
            provider_name="Traqo Ocean",
            carrier_source=CarrierSource.TRAQO_LOOKUP,
            provider_reference="ONEY",
        )
        sync_run = create_sync_run(team=self.team, subscription=subscription, provider=subscription.provider)
        outcome = outcome_for_carrier_error(classify_traqo_error(402, FakeResponse(402, limit_payload())))
        apply_sync_outcome(subscription, sync_run, outcome)

        subscription.refresh_from_db()
        sync_run.refresh_from_db()
        for secret in ACCOUNT_SECRETS:
            self.assertNotIn(secret, subscription.last_error_message)
            self.assertNotIn(secret, sync_run.error_message)


# ---------------------------------------------------------------------------
# Who may see the account
# ---------------------------------------------------------------------------


@override_settings(CACHES=_LOCMEM, TRAQO_ENABLED=True, TRAQO_API_KEY="status-key")
class ProviderStatusVisibilityTest(TestCase):
    """The page exists for one kind of user, and the server tells nobody else."""

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-provider-status")
        self.superuser = CustomUser.objects.create_user(
            username="root@platform.test", email="root@platform.test", is_staff=True, is_superuser=True
        )
        self.admin = CustomUser.objects.create(username="admin@mcr.test", email="admin@mcr.test")
        self.member = CustomUser.objects.create(username="member@mcr.test", email="member@mcr.test")
        for user, role in ((self.superuser, ROLE_ADMIN), (self.admin, ROLE_ADMIN), (self.member, ROLE_MEMBER)):
            self.team.members.add(user, through_defaults={"role": role})
        self.url = reverse("tracking:provider_status")

    def _as(self, user) -> Client:
        client = Client()
        client.force_login(user)
        return client

    def _with_usage(self):
        return mock.patch(
            "apps.scm.tracking.platform_views.get_provider_usage",
            return_value=TraqoUsageFetch(
                provider_code=TRAQO_PROVIDER_CODE,
                provider_name="Traqo Ocean",
                usage=parse_traqo_account_usage(usage_payload()),
            ),
        )

    def test_a_superuser_sees_the_plan_the_spend_and_the_cycle(self):
        with self._with_usage():
            response = self._as(self.superuser).get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "professional")
        self.assertContains(response, "12")
        self.assertContains(response, "13")
        self.assertContains(response, "Billing cycle")

    def test_a_superuser_sees_used_and_active_as_separate_figures(self):
        with self._with_usage():
            response = self._as(self.superuser).get(self.url)

        self.assertContains(response, "Shipments used")
        self.assertContains(response, "Still tracking")

    def test_a_team_admin_cannot_reach_the_page_at_all(self):
        with self._with_usage():
            response = self._as(self.admin).get(self.url)

        self.assertNotEqual(response.status_code, 200)
        self.assertNotContains(response, "professional", status_code=response.status_code)

    def test_a_member_cannot_reach_the_page_at_all(self):
        with self._with_usage():
            response = self._as(self.member).get(self.url)

        self.assertNotEqual(response.status_code, 200)

    def test_an_anonymous_visitor_cannot_reach_the_page(self):
        self.assertNotEqual(Client().get(self.url).status_code, 200)

    def test_a_staff_user_who_is_not_a_superuser_cannot_reach_it(self):
        """Staff is enough for the Django admin and deliberately not enough for this."""
        staff = CustomUser.objects.create_user(
            username="staff@platform.test", email="staff@platform.test", is_staff=True
        )
        self.team.members.add(staff, through_defaults={"role": ROLE_ADMIN})

        with self._with_usage():
            self.assertNotEqual(self._as(staff).get(self.url).status_code, 200)

    def test_an_unconfigured_provider_renders_as_a_state_not_an_error(self):
        """A deployment fact, and the page still has to render."""
        with mock.patch(
            "apps.scm.tracking.platform_views.get_provider_usage",
            return_value=TraqoUsageFetch(
                provider_code=TRAQO_PROVIDER_CODE, provider_name="Traqo Ocean", configured=False
            ),
        ):
            response = self._as(self.superuser).get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not configured")
        self.assertNotContains(response, "Shipments used")

    def test_a_provider_that_could_not_be_asked_renders_the_reason(self):
        """An incident, and distinct from not being configured."""
        with mock.patch(
            "apps.scm.tracking.platform_views.get_provider_usage",
            return_value=TraqoUsageFetch(
                provider_code=TRAQO_PROVIDER_CODE,
                provider_name="Traqo Ocean",
                error="CarrierServerError: 502",
            ),
        ):
            response = self._as(self.superuser).get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be asked")
        self.assertContains(response, "CarrierServerError")

    def test_an_exhausted_allowance_is_shown_as_such(self):
        exhausted = ProviderUsage(
            provider_code=TRAQO_PROVIDER_CODE,
            provider_name="Traqo Ocean",
            plan="professional",
            effective_limit=25,
            used=25,
            active=10,
            remaining=0,
        )
        with mock.patch(
            "apps.scm.tracking.platform_views.get_provider_usage",
            return_value=TraqoUsageFetch(
                provider_code=TRAQO_PROVIDER_CODE, provider_name="Traqo Ocean", usage=exhausted
            ),
        ):
            response = self._as(self.superuser).get(self.url)

        self.assertContains(response, "Exhausted")

    def test_sandbox_figures_are_labelled_so_they_are_not_read_as_ours(self):
        with self._with_usage():
            response = self._as(self.superuser).get(self.url)

        self.assertContains(response, "Sandbox figures")

    def test_the_team_admin_nav_does_not_link_to_it(self):
        response = self._as(self.admin).get(reverse("containers:list"))

        self.assertNotContains(response, self.url)

    def test_the_superuser_nav_does_link_to_it(self):
        response = self._as(self.superuser).get(reverse("containers:list"))

        self.assertContains(response, self.url)


@override_settings(CACHES=_LOCMEM)
class NoAccountDataOnTeamPagesTest(TestCase):
    """After a quota failure, no team-facing page carries our account's position.

    Asserted against rendered HTML rather than against permissions, because the leak
    this replaces was not a permission bug: the fields were readable by design and the
    provider's prose had been copied into them.
    """

    def setUp(self):
        self.team = Team.objects.create(name="MCR", slug="mcr-no-leak")
        self.admin = CustomUser.objects.create(username="admin@noleak.test", email="admin@noleak.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.container = _container(self.team)
        self.client = Client()
        self.client.force_login(self.admin)
        self.subscription = self._watch_that_hit_the_limit()

    def _watch_that_hit_the_limit(self) -> TrackingSubscription:
        from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
        from apps.scm.tracking.services import create_sync_run
        from apps.scm.tracking.sync import apply_sync_outcome

        subscription = get_or_create_container_subscription(
            team=self.team,
            container=self.container,
            provider_code=TRAQO_PROVIDER_CODE,
            provider_name="Traqo Ocean",
            carrier_code="one",
            carrier_name="ONE",
            carrier_source=CarrierSource.TRAQO_LOOKUP,
            provider_reference="ONEY",
        )
        sync_run = create_sync_run(team=self.team, subscription=subscription, provider=subscription.provider)
        error = classify_traqo_error(402, FakeResponse(402, limit_payload()))
        assert error is not None
        apply_sync_outcome(subscription, sync_run, outcome_for_carrier_error(error))
        return subscription

    def _assert_clean(self, response):
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for secret in ACCOUNT_SECRETS:
            self.assertNotIn(secret, body, f"{secret!r} leaked into a team-facing page")

    def test_the_container_workspace_carries_no_account_data(self):
        self._assert_clean(self.client.get(reverse("containers:detail", args=[self.container.pk])))

    def test_the_tracking_detail_page_carries_no_account_data(self):
        """It renders ``last_error_message`` and the sync-run table verbatim."""
        self._assert_clean(self.client.get(reverse("tracking:detail", args=[self.subscription.pk])))

    def test_the_tracking_list_carries_no_account_data(self):
        self._assert_clean(self.client.get(reverse("tracking:list")))

    def test_the_container_list_carries_no_account_data(self):
        self._assert_clean(self.client.get(reverse("containers:list")))

    def test_team_settings_tracking_carries_no_account_data(self):
        self._assert_clean(self.client.get(reverse("team_settings:tracking")))
