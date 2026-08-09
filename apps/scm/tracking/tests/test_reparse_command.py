"""Tests for reparse_tracking_payloads.

The command exists because a parser fix should reach the events we already have, not
only the ones that arrive next. The property that makes it safe to run is that it
writes through the same idempotent path a sync uses, so the test that matters most is
that running it twice changes nothing.
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from apps.scm.tracking.models import TrackingEvent, TrackingProvider, TrackingRawPayload, TrackingSubscription
from apps.teams.models import Team

from .test_maersk_live_payload import CONTAINER_NUMBER, live_payload


class ReparseTrackingPayloadsTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="reparse", slug="reparse")
        self.provider = TrackingProvider.objects.create(code="maersk", name="Maersk")
        self.subscription = TrackingSubscription.objects.create(
            team=self.team,
            provider=self.provider,
            tracking_reference=CONTAINER_NUMBER,
        )
        self.payload = TrackingRawPayload.objects.create(
            team=self.team,
            provider=self.provider,
            subscription=self.subscription,
            payload_json=live_payload(),
            payload_hash="reparse-hash",
            received_at=timezone.now(),
            parsed_successfully=True,
        )

    def _run(self, **options) -> str:
        out = StringIO()
        call_command("reparse_tracking_payloads", provider="maersk", stdout=out, stderr=out, **options)
        return out.getvalue()

    def test_it_creates_the_events_a_stored_payload_describes(self):
        self._run()
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 10)

    def test_reparsed_events_carry_the_nested_location_and_vessel(self):
        self._run()
        arrival = TrackingEvent.objects.get(team=self.team, event_code="ARRI")
        self.assertEqual(arrival.location_unlocode, "SEGOT")
        self.assertEqual(arrival.vessel_name, "JEBEL ALI")
        self.assertEqual(arrival.voyage_number, "623W")

    def test_events_are_linked_back_to_the_payload_they_came_from(self):
        self._run()
        for event in TrackingEvent.objects.filter(team=self.team):
            self.assertEqual(event.raw_payload_id, self.payload.pk)

    def test_running_it_twice_creates_no_duplicates(self):
        self._run()
        output = self._run()
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 10)
        self.assertIn("0 created, 10 updated", output)

    def test_it_fills_in_fields_an_older_parser_left_empty(self):
        """The whole point: an event stored blank is repaired, not duplicated."""
        self._run()
        TrackingEvent.objects.filter(team=self.team).update(location_name="", location_unlocode="", vessel_name="")
        self._run()
        arrival = TrackingEvent.objects.get(team=self.team, event_code="ARRI")
        self.assertEqual(arrival.location_unlocode, "SEGOT")
        self.assertEqual(arrival.vessel_name, "JEBEL ALI")
        self.assertEqual(TrackingEvent.objects.filter(team=self.team).count(), 10)

    def test_a_dry_run_writes_nothing(self):
        output = self._run(dry_run=True)
        self.assertEqual(TrackingEvent.objects.count(), 0)
        self.assertIn("Dry run", output)

    def test_it_can_be_narrowed_to_one_container(self):
        other = TrackingSubscription.objects.create(
            team=self.team, provider=self.provider, tracking_reference="MSKU0000000"
        )
        TrackingRawPayload.objects.create(
            team=self.team,
            provider=self.provider,
            subscription=other,
            payload_json=live_payload(),
            payload_hash="reparse-hash-2",
            received_at=timezone.now(),
        )
        output = self._run(container=CONTAINER_NUMBER, dry_run=True)
        self.assertIn("Re-parsing 1 stored payload(s)", output)

    def test_archived_payloads_are_skipped(self):
        """Retention dropped the body; there is nothing left to re-read."""
        self.payload.archived_at = timezone.now()
        self.payload.payload_json = {"_archived": True}
        self.payload.save(update_fields=["archived_at", "payload_json"])
        output = self._run()
        self.assertIn("No stored payloads match", output)
        self.assertEqual(TrackingEvent.objects.count(), 0)

    def test_error_responses_are_skipped(self):
        self.payload.payload_type = TrackingRawPayload.PayloadType.ERROR_RESPONSE
        self.payload.save(update_fields=["payload_type"])
        self._run()
        self.assertEqual(TrackingEvent.objects.count(), 0)

    def test_it_stays_inside_the_requested_team(self):
        Team.objects.create(name="reparse-other", slug="reparse-other")
        output = self._run(team="reparse-other", dry_run=True)
        self.assertIn("No stored payloads match", output)

    def test_an_unknown_team_is_an_error_not_a_silent_no_op(self):
        with self.assertRaises(CommandError):
            self._run(team="does-not-exist")

    def test_an_unknown_provider_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("reparse_tracking_payloads", provider="not-a-carrier", stdout=StringIO())
