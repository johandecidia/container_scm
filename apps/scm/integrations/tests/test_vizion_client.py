"""Client and ACI tests: the request Vizion receives, and how its answers are read.

The most important assertion in this file is
``test_aci_sends_the_container_number_and_nothing_else``. Auto Carrier Identification is
invoked by *omitting* the carrier, so a hint leaking into the request body would silently
turn the acceptance cases into ordinary carrier-code lookups and prove nothing.
"""

import json
import pathlib

from django.test import SimpleTestCase, override_settings

from apps.scm.integrations.carriers.exceptions import (
    CarrierAuthenticationError,
    CarrierConfigurationError,
    CarrierInvalidResponseError,
    CarrierNoDataError,
    CarrierUnsupportedReferenceError,
)
from apps.scm.integrations.vizion.client import DEMO_BASE_URL, PRODUCTION_BASE_URL, VizionClient
from apps.scm.integrations.vizion.schemas import (
    ACI_FAILED,
    ACI_IDENTIFIED,
    ACI_NOT_FOUND,
    ACI_PENDING,
    read_aci_state,
    read_reference,
)
from apps.scm.integrations.vizion.service import resolve_carrier_via_aci

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vizion"
CONTAINER_NUMBER = "BBCU3273070"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records every request and replays queued responses per verb."""

    def __init__(self, *, post=None, get=None, delete=None):
        self.post_responses = list(post or [])
        self.get_responses = list(get or [])
        self.delete_responses = list(delete or [])
        self.requests = []

    def post(self, url, headers=None, params=None, json=None, timeout=None):
        self.requests.append({"method": "POST", "url": url, "headers": headers or {}, "json": json})
        return self.post_responses.pop(0) if self.post_responses else FakeResponse(200, {})

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"method": "GET", "url": url, "headers": headers or {}, "params": params or {}})
        return self.get_responses.pop(0) if self.get_responses else FakeResponse(200, {})

    def delete(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"method": "DELETE", "url": url, "headers": headers or {}})
        return self.delete_responses.pop(0) if self.delete_responses else FakeResponse(200, {})


def client_with(session, **kwargs) -> VizionClient:
    return VizionClient(base_url=PRODUCTION_BASE_URL, api_key="test-key", session=session, **kwargs)


class VizionAciRequestTest(SimpleTestCase):
    """What goes on the wire when the carrier is unknown."""

    def test_aci_sends_the_container_number_and_nothing_else(self):
        session = FakeSession(post=[FakeResponse(200, fixture("reference_create_aci_pending.json"))])

        client_with(session).create_reference(CONTAINER_NUMBER)

        request = session.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], f"{PRODUCTION_BASE_URL}/references")
        # The whole point: no carrier_code, no scac, no hint of any kind.
        self.assertEqual(request["json"], {"container_id": CONTAINER_NUMBER})
        self.assertNotIn("carrier_code", request["json"])

    def test_the_api_key_travels_in_the_x_api_key_header(self):
        session = FakeSession(post=[FakeResponse(200, fixture("reference_create_aci_pending.json"))])

        client_with(session).create_reference(CONTAINER_NUMBER)

        self.assertEqual(session.requests[0]["headers"]["X-API-Key"], "test-key")

    def test_a_carrier_code_is_sent_only_when_explicitly_asked_for(self):
        session = FakeSession(post=[FakeResponse(200, fixture("reference_create_aci_pending.json"))])

        client_with(session).create_reference(CONTAINER_NUMBER, carrier_code="oney")

        self.assertEqual(session.requests[0]["json"], {"container_id": CONTAINER_NUMBER, "carrier_code": "ONEY"})

    def test_an_invalid_container_number_never_reaches_the_network(self):
        session = FakeSession()

        with self.assertRaises(CarrierUnsupportedReferenceError):
            client_with(session).create_reference("NOT-A-CONTAINER")

        self.assertEqual(session.requests, [])

    def test_a_201_created_is_a_success_for_a_post(self):
        session = FakeSession(post=[FakeResponse(201, fixture("reference_create_aci_pending.json"))])

        payload = client_with(session).create_reference(CONTAINER_NUMBER)

        self.assertEqual(payload["message"], "Reference created successfully.")


class VizionUpdatesRetrievalTest(SimpleTestCase):
    """Both documented update-list shapes are accepted."""

    def test_a_bare_array_is_read(self):
        session = FakeSession(get=[FakeResponse(200, fixture("updates_transshipment.json"))])

        updates = client_with(session).list_updates("ref-1")

        self.assertEqual(len(updates), 2)
        self.assertEqual(session.requests[0]["url"], f"{PRODUCTION_BASE_URL}/references/ref-1/updates")

    def test_a_paginated_object_is_read(self):
        session = FakeSession(get=[FakeResponse(200, {"data": fixture("updates_transshipment.json")})])

        self.assertEqual(len(client_with(session).list_updates("ref-1")), 2)

    def test_an_unrecognised_shape_is_refused_rather_than_read_as_empty(self):
        session = FakeSession(get=[FakeResponse(200, {"unexpected": True})])

        with self.assertRaises(CarrierInvalidResponseError):
            client_with(session).list_updates("ref-1")

    def test_no_updates_yet_is_a_valid_answer(self):
        session = FakeSession(get=[FakeResponse(200, [])])

        self.assertEqual(client_with(session).list_updates("ref-1"), [])


class VizionDeactivationTest(SimpleTestCase):
    """Releasing the billable unit a POC run created."""

    def test_deactivating_issues_a_delete_on_the_reference(self):
        session = FakeSession(delete=[FakeResponse(200, {"message": "Reference unsubscribed successfully."})])

        response = client_with(session).deactivate_reference("ref-1")

        self.assertEqual(session.requests[0]["method"], "DELETE")
        self.assertEqual(session.requests[0]["url"], f"{PRODUCTION_BASE_URL}/references/ref-1")
        self.assertEqual(response["message"], "Reference unsubscribed successfully.")

    def test_it_carries_the_api_key(self):
        session = FakeSession(delete=[FakeResponse(200, {"message": "ok"})])

        client_with(session).deactivate_reference("ref-1")

        self.assertEqual(session.requests[0]["headers"]["X-API-Key"], "test-key")

    def test_an_unknown_reference_is_no_data(self):
        session = FakeSession(delete=[FakeResponse(404, {"message": "Not found"})])

        with self.assertRaises(CarrierNoDataError):
            client_with(session).deactivate_reference("gone")

    def test_a_blank_reference_never_reaches_the_network(self):
        session = FakeSession()

        with self.assertRaises(CarrierUnsupportedReferenceError):
            client_with(session).deactivate_reference("  ")

        self.assertEqual(session.requests, [])

    def test_neither_resolve_nor_ingest_deactivates_anything(self):
        """Deactivating is a decision about whether we still want the container watched."""
        session = FakeSession(
            post=[FakeResponse(200, fixture("reference_create_aci_pending.json"))],
            get=[FakeResponse(200, fixture("reference_aci_completed_oney.json"))],
        )

        resolve_carrier_via_aci(
            container_number=CONTAINER_NUMBER,
            client=client_with(session),
            poll_attempts=2,
            poll_interval_seconds=0,
            sleep=lambda _: None,
        )

        self.assertNotIn("DELETE", [request["method"] for request in session.requests])


class VizionErrorTest(SimpleTestCase):
    """Provider errors are normalised cleanly, and never expose the key."""

    def test_an_unknown_reference_is_no_data_rather_than_a_failure(self):
        session = FakeSession(get=[FakeResponse(404, {"message": "Not found"})])

        with self.assertRaises(CarrierNoDataError):
            client_with(session).get_reference("missing")

    def test_a_rejected_key_is_an_authentication_error(self):
        session = FakeSession(get=[FakeResponse(403, {"message": "A valid API key was not provided."})])

        with self.assertRaises(CarrierAuthenticationError) as caught:
            client_with(session).get_reference("ref-1")

        self.assertIn("valid API key", str(caught.exception))
        self.assertNotIn("test-key", str(caught.exception))

    def test_a_401_is_not_retried_because_a_static_key_cannot_be_refreshed(self):
        session = FakeSession(get=[FakeResponse(401, {"message": "Provided API key lacks required permissions."})])

        with self.assertRaises(CarrierAuthenticationError):
            client_with(session).get_reference("ref-1")

        # One attempt. The shared transport would otherwise spend a refresh-and-retry on
        # a credential that cannot change.
        self.assertEqual(len(session.requests), 1)

    def test_a_semantically_rejected_request_is_an_unsupported_reference(self):
        session = FakeSession(post=[FakeResponse(422, {"message": "container_id is not trackable"})])

        with self.assertRaises(CarrierUnsupportedReferenceError):
            client_with(session).create_reference(CONTAINER_NUMBER)

    def test_a_malformed_request_is_an_invalid_response(self):
        session = FakeSession(post=[FakeResponse(400, {"message": "Bad Request"})])

        with self.assertRaises(CarrierInvalidResponseError):
            client_with(session).create_reference(CONTAINER_NUMBER)

    def test_a_missing_key_fails_before_any_request(self):
        session = FakeSession()
        client = VizionClient(base_url=PRODUCTION_BASE_URL, api_key="", session=session)

        with self.assertRaises(CarrierConfigurationError):
            client.create_reference(CONTAINER_NUMBER)

        self.assertEqual(session.requests, [])


class VizionSettingsTest(SimpleTestCase):
    """Configuration gates both environments, because neither is free."""

    @override_settings(VIZION_ENABLED=False, VIZION_API_KEY="k")
    def test_disabled_refuses_production(self):
        with self.assertRaises(CarrierConfigurationError):
            VizionClient.from_settings()

    @override_settings(VIZION_ENABLED=False, VIZION_API_KEY="k")
    def test_disabled_refuses_the_demo_host_too(self):
        # Unlike Traqo's free sandbox, Vizion's demo is metered against the same key, so
        # it must not be reachable by default.
        with self.assertRaises(CarrierConfigurationError):
            VizionClient.from_settings(demo=True)

    @override_settings(
        VIZION_ENABLED=True,
        VIZION_API_KEY="k",
        VIZION_BASE_URL=PRODUCTION_BASE_URL,
        VIZION_DEMO_BASE_URL=DEMO_BASE_URL,
    )
    def test_the_demo_flag_selects_the_demo_host(self):
        self.assertEqual(VizionClient.from_settings(demo=True).base_url, DEMO_BASE_URL)
        self.assertEqual(VizionClient.from_settings().base_url, PRODUCTION_BASE_URL)


class VizionAciReadingTest(SimpleTestCase):
    """The ACI outcome, and the distinctions that must not be collapsed."""

    def test_a_completed_identification_names_the_carrier(self):
        reference = read_reference(fixture("reference_aci_completed_oney.json"))

        self.assertEqual(reference.aci_state, ACI_IDENTIFIED)
        self.assertTrue(reference.identified)
        self.assertTrue(reference.used_aci)
        self.assertEqual(reference.carrier_identifier, "ONEY")
        self.assertEqual(reference.carrier_name, "Ocean Network Express")
        self.assertEqual(reference.reference_id, "e8991c95-5db2-4c0c-8a02-119611f769df")

    def test_not_found_is_not_the_same_as_failed(self):
        not_found = read_reference(fixture("reference_aci_not_found.json"))
        failed = read_reference(fixture("reference_aci_failed.json"))

        # Vizion retries not_found daily for up to seven days; failed is terminal.
        # Collapsing them would discard a pending answer.
        self.assertEqual(not_found.aci_state, ACI_NOT_FOUND)
        self.assertEqual(failed.aci_state, ACI_FAILED)
        self.assertFalse(not_found.identified)
        self.assertFalse(failed.identified)
        self.assertIs(not_found.active, True)
        self.assertIs(failed.active, False)

    def test_a_freshly_created_reference_is_pending_not_failed(self):
        reference = read_reference(fixture("reference_create_aci_pending.json"))

        self.assertEqual(reference.aci_state, ACI_PENDING)
        self.assertEqual(reference.reference_id, "e8991c95-5db2-4c0c-8a02-119611f769df")
        self.assertEqual(reference.carrier_identifier, "")

    def test_an_unrecognised_status_stays_pending(self):
        self.assertEqual(read_aci_state("something_new"), ACI_PENDING)
        self.assertEqual(read_aci_state(""), ACI_PENDING)

    def test_the_create_wrapper_and_the_bare_object_read_identically(self):
        wrapped = read_reference({"message": "ok", "reference": fixture("reference_aci_completed_oney.json")})
        bare = read_reference(fixture("reference_aci_completed_oney.json"))

        self.assertEqual(wrapped.as_dict(), bare.as_dict())

    def test_an_unreadable_response_reports_nothing_identified_rather_than_raising(self):
        reference = read_reference("not a dict", container_number=CONTAINER_NUMBER)

        self.assertFalse(reference.identified)
        self.assertEqual(reference.container_number, CONTAINER_NUMBER)


class VizionAciResolutionTest(SimpleTestCase):
    """Resolution polls until Vizion settles, and reports rather than concludes."""

    def test_it_polls_until_the_carrier_is_identified(self):
        session = FakeSession(
            post=[FakeResponse(200, fixture("reference_create_aci_pending.json"))],
            get=[
                FakeResponse(200, fixture("reference_create_aci_pending.json")["reference"]),
                FakeResponse(200, fixture("reference_aci_completed_oney.json")),
            ],
        )

        result = resolve_carrier_via_aci(
            container_number=CONTAINER_NUMBER,
            client=client_with(session),
            poll_attempts=4,
            poll_interval_seconds=0,
            sleep=lambda _: None,
        )

        self.assertTrue(result.identified)
        self.assertEqual(result.reference.carrier_identifier, "ONEY")
        self.assertEqual(result.polls, 2)

    def test_running_out_of_polls_reports_pending_not_failure(self):
        session = FakeSession(
            post=[FakeResponse(200, fixture("reference_create_aci_pending.json"))],
            get=[FakeResponse(200, fixture("reference_create_aci_pending.json")["reference"]) for _ in range(3)],
        )

        result = resolve_carrier_via_aci(
            container_number=CONTAINER_NUMBER,
            client=client_with(session),
            poll_attempts=3,
            poll_interval_seconds=0,
            sleep=lambda _: None,
        )

        self.assertEqual(result.reference.aci_state, ACI_PENDING)
        self.assertFalse(result.identified)
        self.assertEqual(result.polls, 3)

    def test_it_stops_polling_once_vizion_has_answered(self):
        session = FakeSession(
            post=[FakeResponse(200, fixture("reference_create_aci_pending.json"))],
            get=[FakeResponse(200, fixture("reference_aci_not_found.json"))],
        )

        result = resolve_carrier_via_aci(
            container_number=CONTAINER_NUMBER,
            client=client_with(session),
            poll_attempts=5,
            poll_interval_seconds=0,
            sleep=lambda _: None,
        )

        self.assertEqual(result.reference.aci_state, ACI_NOT_FOUND)
        self.assertEqual(result.polls, 1)

    def test_resolution_never_sends_a_carrier_hint(self):
        session = FakeSession(
            post=[FakeResponse(200, fixture("reference_create_aci_pending.json"))],
            get=[FakeResponse(200, fixture("reference_aci_completed_oney.json"))],
        )

        resolve_carrier_via_aci(
            container_number=CONTAINER_NUMBER,
            client=client_with(session),
            poll_attempts=2,
            poll_interval_seconds=0,
            sleep=lambda _: None,
        )

        self.assertEqual(session.requests[0]["json"], {"container_id": CONTAINER_NUMBER})
