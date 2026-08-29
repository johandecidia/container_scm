"""Prove the Vizion POC end to end from one command.

The acceptance case is a container whose carrier nobody knows::

    make manage ARGS='vizion_test BBCU3273070 --resolve'
    make manage ARGS='vizion_test BBCU3273090 --resolve'

``--resolve`` sends the container number and **nothing else** — no carrier, no sealine,
no owner-prefix hint — which is what invokes Vizion's Auto Carrier Identification. It
then polls the reference until Vizion says whether it found a carrier, and prints the
answer beside Container SCM's own existing evidence so a disagreement is visible rather
than resolved.

Add ``--track`` to go on and fetch the reference's updates, normalise them and print the
diagnostic — raw milestones beside what reached the canonical model::

    make manage ARGS='vizion_test BBCU3273070 --resolve --track --dry-run'
    make manage ARGS='vizion_test BBCU3273070 --resolve --track'

``--dry-run`` fetches and maps without writing anything, which is the quickest way to see
how a payload would be interpreted. Without it the events are ingested through the
ordinary tracking pipeline and the stored rows are printed back.

``--reference`` skips resolution and works against a reference that already exists, so a
second observation costs no new Vizion reference::

    make manage ARGS='vizion_test BBCU3273070 --reference <uuid> --track'

``--compare`` prints what every provider has stored for the container, canonically. It
fetches nothing and calls nobody — it reads Container SCM's own rows::

    make manage ARGS='vizion_test BBCU3273070 --compare'

Phase 1B adds the pieces a live validation needs.

``--observe`` fetches and ingests **twice** and reports whether Vizion's milestone ids are
stable — the one question Phase 1A could not answer from documentation, and the question
the fingerprint strategy turns on. ``--record`` leaves the raw responses behind as
sanitized fixtures, so the synthetic Phase 1A fixtures can be replaced by real ones.
``--deactivate`` unsubscribes the reference afterwards, releasing Vizion's billable unit::

    make manage ARGS='vizion_test BBCU3273070 --resolve --track --observe \
        --record apps/scm/integrations/tests/fixtures/vizion/live --output BBCU3273070.json'

The full acceptance run, cleaning up after itself::

    make manage ARGS='vizion_test BBCU3273070 --resolve --track --observe --compare \
        --verbose-events --record /tmp/vizion --output /tmp/BBCU3273070.json --deactivate'

Costs, stated before they are incurred: resolving creates a Vizion reference, and a
reference is Vizion's billable unit. Unlike Traqo's free carrier lookup, identification
and tracking are the same purchase. ``--demo`` uses the demo host, which is metered
against the same key rather than free.

Nothing in this command routes anything. It never changes which provider tracks a
container, never writes a carrier onto the Container, and never feeds Vizion's answer
into carrier discovery.
"""

import json
import pathlib
import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.scm.integrations.carriers.exceptions import CarrierError, CarrierNoDataError
from apps.scm.integrations.vizion import PROVIDER_CODE
from apps.scm.integrations.vizion.diagnostics import build_diagnostic, compare_stored_providers
from apps.scm.integrations.vizion.eta import read_vizion_eta_observation
from apps.scm.integrations.vizion.mapper import map_vizion_updates, read_latest_payload
from apps.scm.integrations.vizion.observation import compare_fetches
from apps.scm.integrations.vizion.recording import write_fixture
from apps.scm.integrations.vizion.schemas import ACI_IDENTIFIED, ACI_NOT_FOUND, ACI_PENDING, read_reference
from apps.scm.integrations.vizion.service import (
    DEFAULT_ACI_POLL_ATTEMPTS,
    DEFAULT_ACI_POLL_INTERVAL_SECONDS,
    fetch_vizion_updates,
    ingest_vizion_container,
    resolve_carrier_via_aci,
)
from apps.teams.models import Team


def _tristate(value: bool | None) -> str:
    """Render a tri-state flag without collapsing "not stated" into "false"."""
    if value is None:
        return "not stated by Vizion"
    return "true" if value else "false"


class Command(BaseCommand):
    help = "Resolve a container's carrier through Vizion ACI, and optionally track and normalise it."

    def add_arguments(self, parser):
        parser.add_argument("container_number", help="Container number, e.g. BBCU3273070.")
        parser.add_argument("--team", help="Team slug. Optional when the installation has exactly one team.")
        parser.add_argument(
            "--demo", action="store_true", help="Use Vizion's demo host (metered against the same key)."
        )

        resolution = parser.add_argument_group("carrier resolution (ACI)")
        resolution.add_argument(
            "--resolve",
            action="store_true",
            help="Create a Vizion reference with the container number only, invoking ACI.",
        )
        resolution.add_argument(
            "--reference",
            help="Work against an existing Vizion reference id instead of creating one.",
        )
        resolution.add_argument(
            "--poll-attempts",
            type=int,
            default=DEFAULT_ACI_POLL_ATTEMPTS,
            help=f"How often to re-read the reference while ACI is pending (default: {DEFAULT_ACI_POLL_ATTEMPTS}).",
        )
        resolution.add_argument(
            "--poll-interval",
            type=float,
            default=DEFAULT_ACI_POLL_INTERVAL_SECONDS,
            help=f"Seconds between polls (default: {DEFAULT_ACI_POLL_INTERVAL_SECONDS}).",
        )

        tracking = parser.add_argument_group("tracking retrieval")
        tracking.add_argument("--track", action="store_true", help="Fetch and normalise the reference's updates.")
        tracking.add_argument("--dry-run", action="store_true", help="Fetch and map only; write nothing.")

        observation = parser.add_argument_group("repeat observation (Phase 1B)")
        observation.add_argument(
            "--observe",
            action="store_true",
            help="Fetch and ingest twice, then report whether Vizion milestone ids are stable.",
        )
        observation.add_argument(
            "--observe-interval",
            type=float,
            default=0.0,
            help="Seconds to wait between the two --observe fetches (default: 0).",
        )
        observation.add_argument(
            "--record",
            help="Directory to write sanitized raw responses to, as committable fixtures.",
        )
        observation.add_argument(
            "--deactivate",
            action="store_true",
            help="Unsubscribe the reference afterwards, releasing Vizion's billable unit.",
        )

        output = parser.add_argument_group("output")
        output.add_argument(
            "--compare",
            action="store_true",
            help="Print what every provider has stored for this container. Fetches nothing.",
        )
        output.add_argument("--json", action="store_true", help="Print the result as JSON.")
        output.add_argument("--output", help="Write the result JSON to this path.")
        output.add_argument("--verbose-events", action="store_true", help="Print every normalised event.")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        container_number = options["container_number"].strip().upper()

        if not any((options["resolve"], options["reference"], options["compare"])):
            raise CommandError(
                "Nothing to do. Pass --resolve to identify the carrier through ACI, --reference to work "
                "against an existing Vizion reference, or --compare to read stored rows without fetching."
            )

        result: dict = {"container_number": container_number}

        if options["compare"] and not (options["resolve"] or options["reference"]):
            self._compare(container_number=container_number, options=options, result=result)
            self._emit(result, options)
            return

        self._announce(container_number=container_number, options=options)

        reference = None
        if options["resolve"]:
            reference = self._resolve(container_number=container_number, options=options, result=result)
        elif options["reference"]:
            reference = self._read_existing_reference(options=options, container_number=container_number, result=result)

        if options["track"] or options["observe"]:
            if reference is None or not reference.reference_id:
                raise CommandError("There is no Vizion reference to track. Resolve one first, or pass --reference.")
            self._track(container_number=container_number, reference=reference, options=options, result=result)

        if options["observe"]:
            self._observe(container_number=container_number, reference=reference, options=options, result=result)

        if options["compare"]:
            self._compare(container_number=container_number, options=options, result=result)

        if options["deactivate"]:
            self._deactivate(reference=reference, options=options, result=result)

        self._emit(result, options)

    # ------------------------------------------------------------------
    # Modes
    # ------------------------------------------------------------------

    def _announce(self, *, container_number: str, options: dict) -> None:
        """State the side effects before causing any of them."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Vizion POC — {container_number}"))
        self.stdout.write(f"  Mode:              {'DEMO' if options['demo'] else 'PRODUCTION'}")
        if options["resolve"]:
            self.stdout.write(
                self.style.WARNING(
                    "  Vizion request:    POST /references — this CREATES A REFERENCE, which is Vizion's billable unit"
                )
            )
            self.stdout.write("  Carrier hint:      none — the container number is the entire request body")
        if options["reference"]:
            self.stdout.write(f"  Existing reference:{options['reference']}")
        if options["track"] or options["observe"]:
            self.stdout.write("  Tracking:          GET /references/{id}/updates")
        if options["observe"]:
            self.stdout.write("  Repeat fetch:      yes — a second GET, to test milestone id stability")
        if options["record"]:
            self.stdout.write(f"  Recording to:      {options['record']} (sanitized)")
        if options["deactivate"]:
            self.stdout.write("  Cleanup:           DELETE /references/{id} afterwards")
        if options["dry_run"]:
            self.stdout.write("  Database:          nothing will be written (--dry-run)")
        self.stdout.write("")

    def _resolve(self, *, container_number: str, options: dict, result: dict):
        """Run ACI with the container number alone and report what Vizion decided."""
        aci = self._call(
            lambda: resolve_carrier_via_aci(
                container_number=container_number,
                demo=options["demo"],
                poll_attempts=options["poll_attempts"],
                poll_interval_seconds=options["poll_interval"],
            ),
            container_number,
        )
        if aci is None:
            return None

        reference = aci.reference
        self.stdout.write(self.style.MIGRATE_HEADING("  Auto Carrier Identification"))
        self.stdout.write(f"    ACI status:        {reference.aci_state}")
        self.stdout.write(f"    Vizion status:     {reference.last_update_status or '—'}")
        self.stdout.write(f"    Resolved carrier:  {reference.carrier_identifier or '—'}")
        self.stdout.write(f"    Carrier name:      {reference.carrier_name or '—'}")
        self.stdout.write(f"    Carrier SCAC:      {reference.carrier_scac or '—'}")
        self.stdout.write(f"    Vizion reference:  {reference.reference_id or '—'}")
        self.stdout.write(f"    auto_carrier flag: {_tristate(reference.auto_carrier)}")
        self.stdout.write(f"    Reference active:  {_tristate(reference.active)}")
        self.stdout.write(f"    Polls / waited:    {aci.polls} / {aci.waited_seconds}s")

        self._write_resolution_caveat(reference)
        self._write_existing_evidence(options, container_number)

        result["aci"] = {**aci.reference.as_dict(), "polls": aci.polls, "waited_seconds": aci.waited_seconds}
        result["raw_create_response"] = aci.create_payload
        if aci.reference_payload:
            result["raw_reference_response"] = aci.reference_payload

        if options["record"]:
            self._record(options["record"], f"{container_number}_reference_create", aci.create_payload)
            if aci.reference_payload:
                self._record(options["record"], f"{container_number}_reference_get", aci.reference_payload)
        return reference

    def _write_resolution_caveat(self, reference) -> None:
        """Say what this state does and does not entitle a caller to conclude."""
        self.stdout.write("")
        if reference.aci_state == ACI_IDENTIFIED:
            self.stdout.write(
                self.style.SUCCESS(
                    f"    Vizion identified {reference.carrier_identifier} for this container. That is carrier "
                    "RESOLUTION only — it does not decide which provider should track it."
                )
            )
            if reference.used_aci is not True:
                self.stdout.write(
                    self.style.WARNING(
                        "    auto_carrier is not true, so this reference's carrier may not have come from ACI. "
                        "Check whether the reference already existed."
                    )
                )
        elif reference.aci_state == ACI_PENDING:
            self.stdout.write(
                self.style.WARNING(
                    "    Still pending. Vizion identifies asynchronously; this is not a failure. Re-run with "
                    f"--reference {reference.reference_id} later rather than resolving again, which would "
                    "create a second reference."
                )
            )
        elif reference.aci_state == ACI_NOT_FOUND:
            self.stdout.write(
                self.style.WARNING(
                    "    No supported carrier had data. Vizion retries daily for up to seven days, so this is "
                    "'not yet', not 'no'."
                )
            )
        else:
            self.stdout.write(self.style.ERROR("    Identification failed; the reference will not be retried."))

    def _write_existing_evidence(self, options: dict, container_number: str) -> None:
        """Print Container SCM's own carrier evidence beside Vizion's, without merging it."""
        from apps.scm.integrations.carriers.registry import suggest_carrier_for_owner_code
        from apps.scm.tracking.models import TrackingSubscription

        hint = suggest_carrier_for_owner_code(container_number[:4]) or ""
        codes: tuple[str, ...] = ()
        try:
            team = self._resolve_team(options.get("team"))
        except CommandError:
            team = None
        if team is not None:
            codes = tuple(
                sorted(
                    {
                        subscription.provider.code
                        for subscription in TrackingSubscription.objects.filter(
                            team=team, container__isnull=False
                        ).select_related("provider", "container")
                        if subscription.container.container_id == container_number
                        and subscription.provider.code != PROVIDER_CODE
                    }
                )
            )

        self.stdout.write("")
        self.stdout.write("    Container SCM's own evidence, for comparison — not merged:")
        self.stdout.write(f"      ISO 6346 prefix hint: {hint or 'none (prefix not in the registry)'}")
        self.stdout.write(f"      already evidenced:    {', '.join(codes) if codes else 'none'}")

    def _read_existing_reference(self, *, options: dict, container_number: str, result: dict):
        """Read a reference that already exists, spending no new one."""
        from apps.scm.integrations.vizion.client import VizionClient

        client = VizionClient.from_settings(demo=options["demo"])
        payload = self._call(lambda: client.get_reference(options["reference"]), container_number)
        if payload is None:
            return None

        reference = read_reference(payload, container_number=container_number)
        self.stdout.write(self.style.MIGRATE_HEADING("  Existing Vizion reference"))
        self.stdout.write(f"    ACI status:        {reference.aci_state}")
        self.stdout.write(f"    Resolved carrier:  {reference.carrier_identifier or '—'}")
        self.stdout.write(f"    Vizion reference:  {reference.reference_id or '—'}")
        result["aci"] = reference.as_dict()
        result["raw_reference_response"] = payload
        return reference

    def _track(self, *, container_number: str, reference, options: dict, result: dict) -> None:
        """Fetch the reference's updates, normalise them, and print the diagnostic."""
        updates = self._call(
            lambda: fetch_vizion_updates(reference_id=reference.reference_id, demo=options["demo"]),
            container_number,
        )
        if updates is None:
            return

        events = map_vizion_updates(updates, container_number=container_number)
        payload = read_latest_payload(updates)
        observation = read_vizion_eta_observation(payload, observed_at=timezone.now())
        diagnostic = build_diagnostic(
            container_number=container_number,
            reference=reference,
            payload=payload,
            events=events,
            updates_returned=len(updates),
            eta_observation=observation,
        )

        self._write_diagnostic(diagnostic)
        if options["verbose_events"]:
            self._write_events(events)

        result["diagnostic"] = diagnostic.as_dict()
        result["raw_updates"] = updates

        if options["record"]:
            self._record(options["record"], f"{container_number}_updates", updates)

        if options["dry_run"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("  Nothing was written (--dry-run)."))
            return

        team = self._resolve_team(options.get("team"))
        container = self._resolve_container(team, container_number)
        ingest = self._call(
            lambda: ingest_vizion_container(
                team=team,
                container=container,
                reference_id=reference.reference_id,
                demo=options["demo"],
                updates=updates,
                reference=reference,
            ),
            container_number,
        )
        if ingest is None:
            return

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"  Ingested: {ingest.events_mapped} mapped, {ingest.events_created} created, "
                f"{ingest.events_updated} updated, {ingest.events_failed} failed."
            )
        )
        self.stdout.write(f"  Raw payloads stored this run: {ingest.raw_payloads_created}")
        self.stdout.write(
            f"  Subscription #{ingest.subscription.pk} "
            f"(provider={ingest.subscription.provider.code}, status={ingest.subscription.status}, "
            f"tracking_status={ingest.subscription.tracking_status})"
        )
        self.stdout.write(f"  ETA observation recorded: {'yes' if ingest.eta_observation_recorded else 'no'}")
        self._write_stored_events(team, container)
        self._write_phase1_note()

        result["ingest"] = {
            "events_mapped": ingest.events_mapped,
            "events_created": ingest.events_created,
            "events_updated": ingest.events_updated,
            "events_failed": ingest.events_failed,
            "raw_payloads_created": ingest.raw_payloads_created,
            "eta_observation_recorded": ingest.eta_observation_recorded,
        }

    def _observe(self, *, container_number: str, reference, options: dict, result: dict) -> None:
        """Fetch and ingest a second time, then report what moved between the two.

        This is the Phase 1B identity experiment. Vizion reuses one milestone for the ETA
        and the ATA, so whether its ``id`` survives that flip decides which fingerprint
        strategy is correct — and the documentation does not say. Two fetches answer it
        empirically, or report INCONCLUSIVE, which is the honest and expected result when
        nothing has moved between them.
        """
        first_updates = result.get("raw_updates")
        if first_updates is None:
            raise CommandError("--observe needs a first fetch to compare against; it runs after --track.")

        if options["observe_interval"]:
            self.stdout.write("")
            self.stdout.write(f"  Waiting {options['observe_interval']}s before the second fetch…")
            time.sleep(options["observe_interval"])

        second_updates = self._call(
            lambda: fetch_vizion_updates(reference_id=reference.reference_id, demo=options["demo"]),
            container_number,
        )
        if second_updates is None:
            return

        if not options["dry_run"]:
            team = self._resolve_team(options.get("team"))
            container = self._resolve_container(team, container_number)
            second = self._call(
                lambda: ingest_vizion_container(
                    team=team,
                    container=container,
                    reference_id=reference.reference_id,
                    demo=options["demo"],
                    updates=second_updates,
                    reference=reference,
                ),
                container_number,
            )
            if second is not None:
                self.stdout.write("")
                self.stdout.write(self.style.MIGRATE_HEADING("  Second ingest — idempotency"))
                self.stdout.write(f"    events mapped:  {second.events_mapped}")
                self.stdout.write(f"    events created: {second.events_created}")
                self.stdout.write(f"    events updated: {second.events_updated}")
                self.stdout.write(f"    events failed:  {second.events_failed}")
                if second.events_created:
                    self.stdout.write(
                        self.style.WARNING(
                            "    The second ingest CREATED rows. Inspect the comparison below: either the "
                            "journey genuinely moved, or the fingerprint is not stable for this provider."
                        )
                    )
                else:
                    self.stdout.write(self.style.SUCCESS("    No new rows — the refetch was idempotent."))
                result["second_ingest"] = {
                    "events_mapped": second.events_mapped,
                    "events_created": second.events_created,
                    "events_updated": second.events_updated,
                    "events_failed": second.events_failed,
                }

        comparison = compare_fetches(first_updates, second_updates, container_number=container_number)
        self._write_comparison(comparison)
        result["refetch_comparison"] = comparison.as_dict()

        if options["record"]:
            self._record(options["record"], f"{container_number}_updates_refetch", second_updates)

    def _write_comparison(self, comparison) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("  Refetch comparison — milestone identity"))
        self.stdout.write(f"    updates:            {comparison.first_update_count} → {comparison.second_update_count}")
        self.stdout.write(
            f"    milestones:         {comparison.first_milestone_count} → {comparison.second_milestone_count}"
        )
        self.stdout.write(f"    carrying an id:     {comparison.ids_present_first} → {comparison.ids_present_second}")
        self.stdout.write(f"    matched milestones: {comparison.common_milestones}")
        self.stdout.write(f"    added:              {len(comparison.added)}")
        self.stdout.write(f"    removed:            {len(comparison.removed)}")
        self.stdout.write(f"    order changed:      {'yes' if comparison.order_changed else 'no'}")
        self.stdout.write(f"    new update ids:     {len(comparison.new_update_ids)}")
        self.stdout.write(f"    ids stable:         {_tristate(comparison.ids_stable)}")

        realisations = comparison.forecast_realisations
        self.stdout.write(f"    forecasts realised: {len(realisations)}")
        for change in realisations:
            self.stdout.write(
                f"      {' / '.join(part or '—' for part in change.key)}: "
                f"{change.first_classifier}→{change.second_classifier}, "
                f"id {change.first_id or '—'}→{change.second_id or '—'} "
                f"({'REPLACED' if change.id_changed else 'reused'})"
            )

        enriched = [change for change in comparison.changes if change.enriched_fields]
        self.stdout.write(f"    enriched later:     {len(enriched)}")
        for change in enriched:
            self.stdout.write(
                f"      {' / '.join(part or '—' for part in change.key)}: +{', '.join(change.enriched_fields)}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"    Verdict: {comparison.identity_verdict}"))
        self.stdout.write(f"      {comparison.recommendation}")

    def _deactivate(self, *, reference, options: dict, result: dict) -> None:
        """Release the Vizion reference this run created."""
        from apps.scm.integrations.vizion.client import VizionClient

        if reference is None or not reference.reference_id:
            self.stdout.write(self.style.WARNING("  Nothing to deactivate — no reference was established."))
            return

        client = VizionClient.from_settings(demo=options["demo"])
        response = self._call(lambda: client.deactivate_reference(reference.reference_id), reference.reference_id)
        if response is None:
            return

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"  Reference {reference.reference_id} unsubscribed: {response.get('message') or 'ok'}")
        )
        self.stdout.write(
            "  Vizion will generate no further updates for it. Note that unsubscribing is not the same "
            "as never having created it — the reference was billable when created."
        )
        result["deactivated"] = {"reference_id": reference.reference_id, "response": response}

    def _record(self, directory: str, name: str, payload) -> None:
        """Write a sanitized copy of a live response, so it can become a fixture."""
        path = write_fixture(directory, name, payload)
        self.stdout.write(
            f"  Recorded {path} (organization_id, callback_url and any secret-shaped keys removed; "
            "reference and milestone ids kept, because they are the evidence)."
        )

    def _compare(self, *, container_number: str, options: dict, result: dict) -> None:
        """Print stored canonical coverage per provider. Reads rows; calls nobody."""
        team = self._resolve_team(options.get("team"))
        container = self._find_container(team, container_number)
        if container is None:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"  Team '{team.slug}' has no container {container_number}, so there is nothing stored "
                    "to compare. Run with --track first."
                )
            )
            return

        comparison = compare_stored_providers(team=team, container=container)
        present = comparison.present

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"  Stored coverage per provider — {container_number}"))
        self.stdout.write("  Canonical rows only. Nothing was fetched, and no provider's data was merged.")
        if not present:
            self.stdout.write("    No provider has stored an event for this container.")
            return

        header = "    " + "metric".ljust(22) + "".join(column.provider_code.rjust(14) for column in present)
        self.stdout.write("")
        self.stdout.write(header)
        self.stdout.write("    " + "-" * (22 + 14 * len(present)))
        for label, attribute in (
            ("events", "events"),
            ("classified", "classified"),
            ("actual", "actual"),
            ("forecast", "forecast"),
            ("with UN/LOCODE", "with_unlocode"),
            ("with coordinates", "with_coordinates"),
            ("with vessel", "with_vessel"),
            ("with IMO", "with_imo"),
            ("with voyage", "with_voyage"),
            ("distinct voyages", "distinct_voyages"),
        ):
            row = "    " + label.ljust(22)
            row += "".join(str(getattr(column, attribute)).rjust(14) for column in present)
            self.stdout.write(row)

        eta_row = "    " + "ETA".ljust(22)
        eta_row += "".join(
            (column.eta_at.strftime("%Y-%m-%d") if column.eta_at else "—").rjust(14) for column in present
        )
        self.stdout.write(eta_row)

        latest_row = "    " + "latest event".ljust(22)
        latest_row += "".join(
            (column.latest_event_at.strftime("%Y-%m-%d") if column.latest_event_at else "—").rjust(14)
            for column in present
        )
        self.stdout.write(latest_row)

        result["comparison"] = comparison.as_dict()

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write_diagnostic(self, diagnostic) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("  Vizion tracking data"))
        for label, value in (
            ("Container", diagnostic.container_number),
            ("ACI status", diagnostic.aci_state),
            ("Resolved carrier", diagnostic.resolved_carrier or "—"),
            ("Vizion reference", diagnostic.reference_id or "—"),
            ("Journey status", diagnostic.journey_status or "—"),
            ("Container ISO", diagnostic.container_iso or "—"),
            ("Origin port", diagnostic.origin),
            ("Destination port", diagnostic.destination),
            ("Inland origin", diagnostic.inland_origin),
            ("Inland destination", diagnostic.inland_destination),
            ("Updates returned", diagnostic.updates_returned),
            ("Milestones (raw)", diagnostic.milestones_raw),
            ("Events normalised", diagnostic.events_normalised),
            ("  actual", diagnostic.events_actual),
            ("  estimated", diagnostic.events_estimated),
            ("  planned", diagnostic.events_planned),
            ("  unclassified time", diagnostic.events_unclassified_time),
            ("Locations w/ UNLOCODE", diagnostic.locations_with_unlocode),
            ("Locations w/ coords", diagnostic.locations_with_coordinates),
            ("Events w/ vessel", diagnostic.events_with_vessel),
            ("Events w/ IMO", diagnostic.events_with_imo),
            ("Events w/ MMSI (raw)", diagnostic.events_with_mmsi),
            ("Events w/ voyage", diagnostic.events_with_voyage),
            ("Distinct voyages", ", ".join(diagnostic.distinct_voyages) or "—"),
            ("Distinct vessels", ", ".join(diagnostic.distinct_vessels) or "—"),
            ("Transshipment legs", diagnostic.transshipment_legs),
        ):
            self.stdout.write(f"    {str(label).ljust(24)} {value}")

        self.stdout.write("")
        self.stdout.write(
            f"    {'Latest actual event'.ljust(24)} "
            f"{diagnostic.latest_actual_description or '—'} at {diagnostic.latest_actual_at or '—'}"
        )
        self.stdout.write(f"    {'ETA'.ljust(24)} {diagnostic.eta_at or '—'}")
        self.stdout.write(f"    {'ETA target'.ljust(24)} {diagnostic.eta_target or '—'}")
        self.stdout.write(f"    {'ETA location'.ljust(24)} {diagnostic.eta_location or '—'}")
        self.stdout.write(
            f"    {'ETA vessel / IMO'.ljust(24)} {diagnostic.eta_vessel or '—'} / {diagnostic.eta_imo or '—'}"
        )
        self.stdout.write(f"    {'ETA voyage'.ljust(24)} {diagnostic.eta_voyage or '—'}")
        self.stdout.write(
            f"    {'Raw response available'.ljust(24)} {'yes' if diagnostic.raw_response_available else 'no'}"
        )
        self.stdout.write(f"    {'Normalisation result'.ljust(24)} {diagnostic.normalisation_result}")

    def _write_events(self, events) -> None:
        self.stdout.write("")
        self.stdout.write("  Normalised events (oldest first):")
        for event in events:
            self.stdout.write(
                f"    {event.event_datetime} · {event.event_type or '?'}/{event.event_code or '?'}"
                f" ({event.event_classifier or 'unclassified'})"
                f" · {event.location_name or '(no place)'} [{event.location_unlocode or 'no locode'}]"
                f" · {event.vessel_name or 'no vessel'}/{event.voyage_number or 'no voyage'}"
                f" — {event.description}"
            )

    def _write_stored_events(self, team, container) -> None:
        """Read the events back through the ordinary selector the timeline uses."""
        from apps.scm.tracking.selectors import get_tracking_events_for_container

        events = list(get_tracking_events_for_container(team, container))
        self.stdout.write("")
        self.stdout.write(f"  TrackingEvents now stored for {container.container_id}: {len(events)}")
        for event in events:
            self.stdout.write(
                f"    [{event.provider.code}] {event.event_datetime} · {event.event_type}"
                f" ({event.event_time_type}) · {event.location_name or '(no place)'}"
                f" · {event.carrier_reference or '—'} — {event.description}"
            )

    def _write_phase1_note(self) -> None:
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "  Phase 1: Vizion is not in the carrier registry, so the scheduled poller and the container "
                "refresh button cannot drive this subscription — re-run this command with --reference to "
                "refresh it. See apps/scm/integrations/vizion/README.md."
            )
        )

    def _emit(self, result: dict, options: dict) -> None:
        if options["json"]:
            self.stdout.write(json.dumps(result, indent=2, default=str))
        if options["output"]:
            path = pathlib.Path(options["output"]).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, indent=2, default=str))
            self.stdout.write(self.style.SUCCESS(f"  Result JSON written to {path}"))

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _call(self, action, reference: str):
        """Run a Vizion call, reporting a typed failure instead of a traceback."""
        try:
            return action()
        except CarrierNoDataError:
            self.stdout.write(self.style.WARNING(f"  Vizion has no data for {reference}."))
            return None
        except CarrierError as exc:
            # Vizion's messages describe the problem and never echo the API key.
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

    def _resolve_team(self, slug: str | None) -> Team:
        if slug:
            try:
                return Team.objects.get(slug=slug)
            except Team.DoesNotExist as exc:
                raise CommandError(f"No team with slug '{slug}'.") from exc

        teams = list(Team.objects.all()[:2])
        if len(teams) == 1:
            return teams[0]
        if not teams:
            raise CommandError("There are no teams to run this against.")
        raise CommandError("More than one team exists — pass --team <team-slug>.")

    def _find_container(self, team: Team, container_number: str):
        """Return the team's container, or None. Creates nothing."""
        from django.core.exceptions import ValidationError

        from apps.scm.containers.models import Container
        from apps.scm.containers.utils import parse_container_id

        try:
            parts = parse_container_id(container_number)
        except (ValidationError, ValueError) as exc:
            raise CommandError(f"{container_number} is not a valid container number: {exc}") from exc

        return Container.objects.filter(
            team=team,
            owner_code=parts["owner_code"],
            category_id=parts["category_id"],
            serial_number=parts["serial_number"],
        ).first()

    def _resolve_container(self, team: Team, container_number: str):
        """Return the team's container, creating it through the existing linker if new."""
        from apps.scm.integrations.carriers.auto_link import create_or_link_discovered_container
        from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult

        existing = self._find_container(team, container_number)
        if existing is not None:
            return existing

        summary = create_or_link_discovered_container(
            team=team,
            shipment=None,
            result=ContainerDiscoveryResult(
                container_number=container_number,
                # The provider that found it, not a claim about who carries it — the
                # resolved carrier stays on the Vizion reference and is deliberately not
                # written onto the Container by this POC.
                carrier_code=PROVIDER_CODE,
                carrier_name="Vizion",
            ),
        )
        if summary["container"] is None:
            raise CommandError(f"Could not create container {container_number}. An EquipmentType must exist first.")
        self.stdout.write(f"  Created container {container_number} for team '{team.slug}'.")
        return summary["container"]
