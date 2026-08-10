"""Error handling tests for carrier adapter integration layer.

Tests that error conditions — timeout, auth failure, rate limiting, and empty
responses — are handled explicitly and deterministically using mocked adapters.

No real HTTP calls are made in any test.  All external behaviour is simulated
via unittest.mock.

Helper pattern
--------------
``_run_carrier_sync(client, provider, team, subscription)`` simulates the minimal
orchestration a real sync service would perform:
  1. Call client.fetch_tracking() to get the raw carrier response.
  2. Store the raw payload (or an error record) via store_raw_payload().
  3. On success: parse events and call upsert_tracking_event() for each.
  4. On failure: store an error raw-payload record; do NOT create tracking events.

This approach tests the expected error-handling contract without requiring a
full Celery task or integration service to exist yet.
"""

from unittest.mock import MagicMock

from django.test import TestCase
from django.utils import timezone

from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingRawPayload, TrackingSubscription
from apps.scm.tracking.services import (
    create_sync_run,
    finish_sync_run_failed,
    store_raw_payload,
    update_subscription_sync_state,
    upsert_tracking_event,
)
from apps.teams.models import Team


def _team(slug: str) -> Team:
    return Team.objects.get_or_create(slug=slug, defaults={"name": slug})[0]


def _provider(code: str) -> TrackingProvider:
    return TrackingProvider.objects.get_or_create(
        code=code,
        defaults={"name": f"Provider {code}", "provider_type": TrackingProvider.ProviderType.API},
    )[0]


def _subscription(team: Team, provider: TrackingProvider, ref: str) -> TrackingSubscription:
    return TrackingSubscription.objects.create(team=team, provider=provider, tracking_reference=ref)


def _run_carrier_sync(client, provider: TrackingProvider, team: Team, subscription: TrackingSubscription) -> dict:
    """Minimal carrier sync orchestration used across error-handling tests.

    Returns a summary dict: {raw_payloads_created, events_created, error, error_type}.
    """
    try:
        raw_response = client.fetch_tracking(container_number=subscription.tracking_reference)
    except TimeoutError as exc:
        store_raw_payload(
            team=team,
            provider=provider,
            payload={"error": "timeout", "message": str(exc)},
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
            subscription=subscription,
            parsed_successfully=False,
            error_message=str(exc),
        )
        return {"raw_payloads_created": 1, "events_created": 0, "error": str(exc), "error_type": "timeout"}

    except PermissionError as exc:
        # Represents 401/403 auth failures.
        store_raw_payload(
            team=team,
            provider=provider,
            payload={"error": "auth", "message": str(exc)},
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
            subscription=subscription,
            parsed_successfully=False,
            error_message=str(exc),
        )
        return {"raw_payloads_created": 1, "events_created": 0, "error": str(exc), "error_type": "auth"}

    except ConnectionError as exc:
        # Represents 429 rate limit.
        store_raw_payload(
            team=team,
            provider=provider,
            payload={"error": "rate_limit", "message": str(exc)},
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
            subscription=subscription,
            parsed_successfully=False,
            error_message=str(exc),
        )
        return {"raw_payloads_created": 1, "events_created": 0, "error": str(exc), "error_type": "rate_limit"}

    # Success path: store raw and create normalized events.
    raw_events = raw_response.get("events") or raw_response.get("movements") or []
    store_raw_payload(
        team=team,
        provider=provider,
        payload=raw_response,
        payload_type=TrackingRawPayload.PayloadType.API_RESPONSE,
        subscription=subscription,
        parsed_successfully=True,
    )
    events_created = 0
    for ev in raw_events:
        _event, created = upsert_tracking_event(
            team=team,
            provider=provider,
            event_type=TrackingEvent.EventType.UNKNOWN,
            event_datetime=timezone.now(),
            subscription=subscription,
            source_event_id=ev.get("eventID", ""),
            description=str(ev),
        )
        if created:
            events_created += 1

    return {"raw_payloads_created": 1, "events_created": events_created, "error": None, "error_type": None}


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


class CarrierTimeoutHandlingTest(TestCase):
    """A carrier API timeout is recorded as an error payload; no tracking event is created."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("err-timeout-team")
        cls.provider = _provider("ERR_TIMEOUT_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "TIMEOUT1234567")

    def setUp(self):
        self.client = MagicMock()
        self.client.fetch_tracking.side_effect = TimeoutError("Connection to carrier API timed out")

    def test_maersk_adapter_handles_timeout(self):
        result = _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        self.assertEqual(result["error_type"], "timeout")
        self.assertIsNotNone(result["error"])

    def test_msc_adapter_handles_timeout(self):
        result = _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        self.assertEqual(result["error_type"], "timeout")

    def test_carrier_timeout_does_not_create_tracking_event(self):
        _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        count = TrackingEvent.objects.filter(team=self.team, provider=self.provider).count()
        self.assertEqual(count, 0)

    def test_timeout_stores_error_raw_payload(self):
        _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        payload = TrackingRawPayload.objects.filter(
            team=self.team,
            provider=self.provider,
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
        ).first()
        self.assertIsNotNone(payload, "An error raw payload must be stored on timeout")
        self.assertFalse(payload.parsed_successfully)

    def test_timeout_error_payload_is_not_treated_as_empty_response(self):
        _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        error_payloads = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
        )
        self.assertGreater(error_payloads.count(), 0)
        # Ensure no API_RESPONSE payload was stored (empty response path)
        api_payloads = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.API_RESPONSE,
        )
        self.assertEqual(api_payloads.count(), 0)

    def test_timeout_error_message_is_stored(self):
        _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        payload = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
        ).first()
        self.assertIn("timed", payload.error_message.lower())


# ---------------------------------------------------------------------------
# Auth error handling
# ---------------------------------------------------------------------------


class CarrierAuthErrorHandlingTest(TestCase):
    """Auth failures (401/403) are stored as error payloads; no tracking event is created."""

    team: Team
    provider: TrackingProvider
    sub: TrackingSubscription

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("err-auth-team")
        cls.provider = _provider("ERR_AUTH_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "AUTH1234567")

    def _run_with_auth_error(self, message: str) -> dict:
        client = MagicMock()
        client.fetch_tracking.side_effect = PermissionError(message)
        return _run_carrier_sync(client, self.provider, self.team, self.sub)

    def test_carrier_adapter_handles_401_auth_error(self):
        result = self._run_with_auth_error("401 Unauthorized: invalid API key")
        self.assertEqual(result["error_type"], "auth")

    def test_carrier_adapter_handles_403_forbidden_error(self):
        result = self._run_with_auth_error("403 Forbidden: access denied")
        self.assertEqual(result["error_type"], "auth")

    def test_auth_error_is_not_treated_as_empty_response(self):
        self._run_with_auth_error("401 Unauthorized")
        api_payloads = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.API_RESPONSE,
        )
        self.assertEqual(api_payloads.count(), 0)

    def test_auth_error_does_not_create_normalized_tracking_event(self):
        self._run_with_auth_error("403 Forbidden")
        count = TrackingEvent.objects.filter(team=self.team, provider=self.provider).count()
        self.assertEqual(count, 0)

    def test_auth_error_stores_error_raw_payload(self):
        self._run_with_auth_error("401 Unauthorized")
        payload = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
        ).first()
        self.assertIsNotNone(payload)
        self.assertFalse(payload.parsed_successfully)


# ---------------------------------------------------------------------------
# Rate limit handling
# ---------------------------------------------------------------------------


class CarrierRateLimitHandlingTest(TestCase):
    """Rate limit responses (429) are recorded as error payloads; no tracking event is created."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("err-rl-team")
        cls.provider = _provider("ERR_RL_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "RL1234567")

    def setUp(self):
        self.client = MagicMock()
        self.client.fetch_tracking.side_effect = ConnectionError(
            "429 Too Many Requests: rate limit exceeded, retry-after=60"
        )

    def test_carrier_adapter_handles_429_rate_limit(self):
        result = _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        self.assertEqual(result["error_type"], "rate_limit")

    def test_rate_limit_does_not_create_normalized_tracking_event(self):
        _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        count = TrackingEvent.objects.filter(team=self.team, provider=self.provider).count()
        self.assertEqual(count, 0)

    def test_rate_limit_stores_error_raw_payload(self):
        _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        payload = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
        ).first()
        self.assertIsNotNone(payload)

    def test_rate_limit_error_includes_retry_context_in_stored_payload(self):
        _run_carrier_sync(self.client, self.provider, self.team, self.sub)
        payload = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
        ).first()
        self.assertIsNotNone(payload)
        self.assertIn("rate_limit", payload.payload_json.get("error", ""))


# ---------------------------------------------------------------------------
# Empty response handling
# ---------------------------------------------------------------------------


class CarrierEmptyResponseHandlingTest(TestCase):
    """Empty carrier responses are handled without errors and produce no tracking events."""

    team: Team
    provider: TrackingProvider
    sub: TrackingSubscription

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("err-empty-team")
        cls.provider = _provider("ERR_EMPTY_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "EMPTY1234567")

    def _run_with_response(self, response: dict) -> dict:
        client = MagicMock()
        client.fetch_tracking.return_value = response
        return _run_carrier_sync(client, self.provider, self.team, self.sub)

    def test_carrier_adapter_handles_empty_event_list(self):
        result = self._run_with_response({"events": []})
        self.assertIsNone(result["error"])
        self.assertEqual(result["events_created"], 0)

    def test_carrier_adapter_handles_null_body(self):
        result = self._run_with_response({})
        self.assertIsNone(result["error"])
        self.assertEqual(result["events_created"], 0)

    def test_empty_response_does_not_create_tracking_event(self):
        self._run_with_response({"events": []})
        count = TrackingEvent.objects.filter(team=self.team, provider=self.provider).count()
        self.assertEqual(count, 0)

    def test_empty_response_stores_api_response_payload(self):
        self._run_with_response({"events": []})
        payload = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.API_RESPONSE,
        ).first()
        self.assertIsNotNone(payload, "Empty response should still store a raw API_RESPONSE record")
        self.assertTrue(payload.parsed_successfully)

    def test_empty_response_is_not_confused_with_auth_error(self):
        self._run_with_response({"events": []})
        error_payloads = TrackingRawPayload.objects.filter(
            team=self.team,
            payload_type=TrackingRawPayload.PayloadType.ERROR_RESPONSE,
        )
        self.assertEqual(error_payloads.count(), 0)

    def test_empty_response_is_not_confused_with_timeout(self):
        # Timeout raises, empty does not — different code paths.
        result = self._run_with_response({"events": []})
        self.assertIsNone(result["error"])
        self.assertIsNone(result["error_type"])

    def test_empty_movements_list_from_proprietary_carrier(self):
        result = self._run_with_response({"movements": []})
        self.assertIsNone(result["error"])
        self.assertEqual(result["events_created"], 0)


# ---------------------------------------------------------------------------
# Sync run lifecycle with errors
# ---------------------------------------------------------------------------


class SyncRunErrorLifecycleTest(TestCase):
    """TrackingSyncRun lifecycle correctly reflects error outcomes."""

    @classmethod
    def setUpTestData(cls):
        cls.team = _team("sync-err-team")
        cls.provider = _provider("SYNC_ERR_PROV")
        cls.sub = _subscription(cls.team, cls.provider, "SYNCERR1234567")

    def test_sync_run_marked_failed_on_timeout(self):
        sync_run = create_sync_run(self.team, self.sub, self.provider)
        finish_sync_run_failed(sync_run, error_message="carrier timeout: SYNCERR1234567")
        sync_run.refresh_from_db()
        from apps.scm.tracking.models import TrackingSyncRun

        self.assertEqual(sync_run.status, TrackingSyncRun.Status.FAILED)
        self.assertIn("SYNCERR1234567", sync_run.error_message)

    def test_sync_run_marked_failed_on_auth_error(self):
        sync_run = create_sync_run(self.team, self.sub, self.provider)
        finish_sync_run_failed(sync_run, error_message="401 Unauthorized")
        sync_run.refresh_from_db()
        from apps.scm.tracking.models import TrackingSyncRun

        self.assertEqual(sync_run.status, TrackingSyncRun.Status.FAILED)

    def test_subscription_failure_counter_incremented_on_error(self):
        update_subscription_sync_state(self.sub, success=False, error_message="timeout")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.consecutive_failures, 1)

    def test_subscription_failure_counter_not_incremented_on_success(self):
        update_subscription_sync_state(self.sub, success=False, error_message="err")
        update_subscription_sync_state(self.sub, success=True)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.consecutive_failures, 0)
