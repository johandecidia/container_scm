"""Prove the Traqo integration end to end against the sandbox, from one command.

Fetches a container from Traqo, stores the response, normalises its events through the
ordinary tracking ingestion path, and prints what reached TrackingEvent — so the whole
chain can be inspected without any UI.

    make manage ARGS='traqo_test MRSU6859427 --sealine MAEU --sandbox'
    make manage ARGS='traqo_test MRSU6859427 --sealine maersk --sandbox --dry-run'
    make manage ARGS='traqo_test MRKU1234567 --sealine MAEU --live'

Sandbox is the default because it is fixed demo data behind no credential. ``--live``
requires TRAQO_ENABLED and TRAQO_API_KEY; the key is never printed.

``--dry-run`` fetches and maps without writing anything, which is the quickest way to
see how a payload would be interpreted.

Phase 2 adds ``--compare``, which benchmarks Traqo's data against what a direct carrier
integration already stored for the same real container::

    make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live'
    make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live --json --output run.json'

``--compare`` requires an explicit ``--live`` or ``--sandbox``: sandbox returns fixed
demo data for one container whatever you ask about, so comparing real carrier events
against it would measure nothing, and silently choosing it would hide that.

Phase 2.2 adds two things to that. ``--candidates`` reports which containers are worth
spending a live request on, and refuses to name one when none is in transit::

    make manage ARGS='traqo_test --candidates'

And ``--previous`` reads a snapshot written by an earlier run and reports what moved
between the two — ETA drift, new events, corrections, and whether the provider's own row
identities survived the refetch::

    make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live --output T0.json'
    make manage ARGS='traqo_test CPWU2588229 --sealine MAEU --compare --live --previous T0.json --output T1.json'

There is no schedule for the second run. Phase 2.2 is an experiment, and when to take
the next observation is a decision, not a cadence.
"""

import json
import pathlib

from django.core.management.base import BaseCommand, CommandError

from apps.scm.integrations.carriers.exceptions import CarrierError, CarrierNoDataError
from apps.scm.integrations.traqo import PROVIDER_CODE
from apps.scm.integrations.traqo.benchmark import (
    DEFAULT_TOLERANCE_HOURS,
    REFERENCE_PROVIDER_CODE,
    SnapshotMismatchError,
    assess_reference_candidates,
    choose_candidate,
    compare_providers,
    compare_snapshots,
    render_drift_text,
    render_json,
    render_text,
)
from apps.scm.integrations.traqo.carrier_lookup import assess_lookup
from apps.scm.integrations.traqo.mapper import map_traqo_container_payload
from apps.scm.integrations.traqo.sealines import resolve_sealine
from apps.scm.integrations.traqo.service import (
    fetch_traqo_container,
    ingest_traqo_container,
    lookup_traqo_carrier,
)
from apps.teams.models import Team


def _tristate(value: bool | None) -> str:
    """Render a tri-state flag without collapsing "not stated" into "false"."""
    if value is None:
        return "not stated by Traqo"
    return "true" if value else "false"


class Command(BaseCommand):
    help = "Fetch one container from Traqo, ingest it, and optionally benchmark it against a direct carrier."

    def add_arguments(self, parser):
        parser.add_argument(
            "container_number",
            nargs="?",
            help="Container number, e.g. MRSU6859427. Omitted only with --candidates.",
        )
        parser.add_argument(
            "--sealine",
            help="Carrier for the container: a SCAC (MAEU) or a Container SCM carrier code (maersk).",
        )
        parser.add_argument("--team", help="Team slug. Optional when the installation has exactly one team.")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--sandbox", action="store_true", help="Use the Traqo sandbox (default outside --compare).")
        mode.add_argument("--live", action="store_true", help="Use the live Traqo API (needs TRAQO_ENABLED).")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and map only; write nothing to the database.",
        )

        benchmark = parser.add_argument_group("comparison (Phase 2 benchmark)")
        benchmark.add_argument(
            "--compare",
            action="store_true",
            help="Benchmark Traqo against the carrier data already stored for this container.",
        )
        benchmark.add_argument(
            "--reference-provider",
            default=REFERENCE_PROVIDER_CODE,
            help=f"Provider to benchmark against (default: {REFERENCE_PROVIDER_CODE}).",
        )
        benchmark.add_argument(
            "--tolerance-hours",
            type=int,
            default=DEFAULT_TOLERANCE_HOURS,
            help=f"How far apart two reports of one event may be and still match (default: {DEFAULT_TOLERANCE_HOURS}).",
        )
        benchmark.add_argument(
            "--refresh-reference",
            action="store_true",
            help="Refresh the reference provider through its own existing sync first (extra carrier call).",
        )
        benchmark.add_argument(
            "--no-fetch",
            action="store_true",
            help="Compare what is already stored without spending another Traqo request.",
        )
        benchmark.add_argument("--json", action="store_true", help="Print the benchmark result as JSON.")
        benchmark.add_argument("--output", help="Write the benchmark JSON to this path.")
        benchmark.add_argument("--verbose-events", action="store_true", help="Show every field difference per event.")

        observation = parser.add_argument_group("repeat observation (Phase 2.2)")
        observation.add_argument(
            "--candidates",
            action="store_true",
            help="Report which containers are worth a live request, and exit. Fetches nothing.",
        )
        observation.add_argument(
            "--previous",
            help="A benchmark JSON from an earlier run; report what changed since it was taken.",
        )
        observation.add_argument(
            "--lookup",
            action="store_true",
            help="Ask Traqo which carrier knows this reference, and exit. Consumes no shipment slot.",
        )

    def handle(self, *args, **options):
        if options["candidates"]:
            self._candidates(options)
            return

        if not options["container_number"]:
            raise CommandError("A container number is required unless --candidates is passed.")

        if options["lookup"]:
            self._lookup(container_number=options["container_number"].strip().upper(), options=options)
            return

        if not options["sealine"]:
            raise CommandError("--sealine is required unless --candidates or --lookup is passed.")

        container_number = options["container_number"].strip().upper()
        try:
            sealine = resolve_sealine(options["sealine"])
        except CarrierError as exc:
            raise CommandError(str(exc)) from exc

        if options["previous"] and not options["compare"]:
            raise CommandError("--previous compares two benchmark runs, so it needs --compare.")

        if options["compare"]:
            self._compare(container_number=container_number, sealine=sealine, options=options)
            return

        sandbox = not options["live"]
        mode = "sandbox" if sandbox else "live"
        self.stdout.write(f"Asking Traqo ({mode}) about {container_number}, sealine {sealine}…")

        if options["dry_run"]:
            self._dry_run(container_number=container_number, sealine=sealine, sandbox=sandbox)
            return

        team = self._resolve_team(options.get("team"))
        container = self._resolve_container(team, container_number)
        self._ingest(team=team, container=container, sealine=sealine, sandbox=sandbox)

    # ------------------------------------------------------------------
    # Modes
    # ------------------------------------------------------------------

    def _dry_run(self, *, container_number: str, sealine: str, sandbox: bool) -> None:
        payload = self._fetch(
            lambda: fetch_traqo_container(container_number=container_number, sealine=sealine, sandbox=sandbox),
            container_number,
        )
        if payload is None:
            return

        events = map_traqo_container_payload(payload, container_number=container_number)
        data = payload.get("data") or {}
        self.stdout.write(self.style.SUCCESS(f"Traqo returned {len(events)} event(s) — nothing written (--dry-run)."))
        self._write_shipment_summary(data)
        for event in events:
            self._write_normalised_event(event)

    def _compare(self, *, container_number: str, sealine: str, options: dict) -> None:
        """Benchmark Traqo against a provider that already tracks this real container."""
        if not (options["live"] or options["sandbox"]):
            raise CommandError(
                "--compare needs an explicit mode. Pass --live to benchmark real Traqo data, or "
                "--sandbox to exercise the benchmark machinery on fixed demo data. There is no "
                "default, because sandbox returns the same demo shipment whatever container you ask "
                "about, and comparing real carrier events against it would measure nothing."
            )
        sandbox = options["sandbox"]

        team = self._resolve_team(options.get("team"))
        container = self._require_container(team, container_number)
        reference_provider = options["reference_provider"].strip().lower()
        self._require_reference_data(team, container, reference_provider, options)
        self._announce_comparison(
            container_number=container_number,
            sealine=sealine,
            sandbox=sandbox,
            reference_provider=reference_provider,
            options=options,
        )

        try:
            result = compare_providers(
                team=team,
                container=container,
                sealine=sealine,
                reference_provider_code=reference_provider,
                sandbox=sandbox,
                ingest_candidate=not options["no_fetch"],
                refresh_reference=options["refresh_reference"],
                tolerance_hours=options["tolerance_hours"],
            )
        except CarrierNoDataError:
            raise CommandError(
                f"Traqo has no shipment for {container_number} with sealine {sealine}. "
                "Nothing was written, and no comparison is possible."
            ) from None
        except CarrierError as exc:
            # Already sanitised by the client — no key, no headers, no body.
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

        payload = render_json(result)
        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            self.stdout.write(render_text(result, verbose=options["verbose_events"]))

        if options["previous"]:
            self._write_drift(previous_path=options["previous"], current=payload)

        if options["output"]:
            path = pathlib.Path(options["output"]).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2))
            self.stdout.write(self.style.SUCCESS(f"Benchmark JSON written to {path}"))

    def _write_drift(self, *, previous_path: str, current: dict) -> None:
        """Report what changed since an earlier run's JSON.

        The comparison is of the two runs' snapshots, so a file from a build whose
        snapshot shape differs is refused rather than partially read.
        """
        path = pathlib.Path(previous_path).expanduser()
        try:
            previous = json.loads(path.read_text())
        except OSError as exc:
            raise CommandError(f"Could not read the previous benchmark at {path}: {exc}") from exc
        except ValueError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc

        previous_snapshot = previous.get("snapshot") if isinstance(previous, dict) else None
        if not previous_snapshot:
            raise CommandError(
                f"{path} carries no snapshot, so there is nothing to compare against. It was probably "
                "written before Phase 2.2 added one; take a fresh run as the new T0."
            )

        try:
            diff = compare_snapshots(previous_snapshot, current["snapshot"])
        except SnapshotMismatchError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(render_drift_text(diff))
        current["comparison_with_previous"] = {"previous_file": str(path), **diff}

    def _lookup(self, *, container_number: str, options: dict) -> None:
        """Ask Traqo which carrier knows this reference, and compare it with what we know.

        Deliberately does not register anything, does not pick a winner, and does not
        feed the answer into carrier discovery. It prints the lookup beside Container
        SCM's own evidence so a disagreement is visible rather than resolved.
        """
        sandbox = not options["live"]
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Traqo carrier lookup — {container_number}"))
        self.stdout.write(f"  Mode:          {'sandbox' if sandbox else 'PRODUCTION'}")
        self.stdout.write("  Shipment slot: none expected — verified against the response below")

        lookup = self._fetch(
            lambda: lookup_traqo_carrier(reference=container_number, sandbox=sandbox),
            container_number,
        )
        if lookup is None:
            return

        prefix_hint, known = self._existing_carrier_evidence(options, container_number)
        assessment = assess_lookup(lookup, prefix_suggestion=prefix_hint, known_carrier_codes=known)

        self.stdout.write("")
        self.stdout.write(f"  Detected carrier:  {lookup.carrier_name or '—'}")
        self.stdout.write(f"  SCAC:              {lookup.scac or '—'}")
        self.stdout.write(
            f"  Confidence:        {lookup.confidence}"
            + (f" (Traqo said {lookup.stated_confidence!r})" if lookup.stated_confidence else "")
        )
        self.stdout.write(f"  Source:            {lookup.source or '—'}")
        self.stdout.write(f"  Reason:            {lookup.reason or '—'}")
        self.stdout.write(
            "  Candidates:        "
            + (
                ", ".join(f"{c.scac} ({c.confidence})" for c in lookup.candidates)
                if lookup.candidates
                else "none listed"
            )
        )
        self.stdout.write(
            f"  Sources unavailable: {', '.join(lookup.unavailable_sources) if lookup.unavailable_sources else 'none'}"
        )
        self.stdout.write(f"  Cached:            {_tristate(lookup.cached)}")
        self.stdout.write(f"  slot_consumed:     {_tristate(lookup.slot_consumed)}")
        self.stdout.write(f"  Traqo-trackable:   {_tristate(lookup.carrier_supported_by_traqo)}")
        self.stdout.write(f"  Container SCM code:{lookup.carrier_code or ' (no direct adapter)'}")

        self.stdout.write("")
        self.stdout.write("  Container SCM's own evidence, for comparison — not merged:")
        self.stdout.write(f"    ISO 6346 prefix hint: {prefix_hint or 'none (prefix not in the registry)'}")
        self.stdout.write(f"    already evidenced:    {', '.join(known) if known else 'none'}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"  Verdict: {assessment.action}"))
        self.stdout.write(f"    {assessment.rationale}")
        for line in assessment.corroborated_by:
            self.stdout.write(f"    corroborated: {line}")
        for line in assessment.contradicted_by:
            self.stdout.write(self.style.WARNING(f"    CONTRADICTED: {line}"))

        if lookup.slot_consumed is True:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR("  WARNING: Traqo reported slot_consumed=true for a lookup."))
        elif lookup.slot_consumed is None:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "  Traqo did not state slot_consumed. That is not the same as stating false — "
                    "verify the account's shipment count independently before trusting the budget."
                )
            )

        if options["output"]:
            path = pathlib.Path(options["output"]).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "lookup": lookup.as_dict(),
                        "assessment": assessment.as_dict(),
                        # The envelope verbatim. A lookup carries no credential and no
                        # shipment data, and keeping it is what lets a field this
                        # reader did not recognise be found later instead of re-asked.
                        "raw_response": lookup.raw,
                    },
                    indent=2,
                )
            )
            self.stdout.write(self.style.SUCCESS(f"  Lookup JSON written to {path}"))

    def _existing_carrier_evidence(self, options: dict, container_number: str) -> tuple[str, tuple[str, ...]]:
        """Return (prefix hint, carriers already evidenced) for this container.

        Read through the existing registry and tracking rows. Returns empty values
        rather than failing when the container is not in Container SCM at all — which
        is the normal case for a reference somebody is asking about for the first time.
        """
        from apps.scm.integrations.carriers.registry import suggest_carrier_for_owner_code
        from apps.scm.tracking.models import TrackingSubscription

        hint = suggest_carrier_for_owner_code(container_number[:4]) or ""

        try:
            team = self._resolve_team(options.get("team"))
        except CommandError:
            return hint, ()

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
        return hint, codes

    def _candidates(self, options: dict) -> None:
        """Report which containers could carry a live benchmark, and name one or none."""
        team = self._resolve_team(options.get("team"))
        reference_provider = options["reference_provider"].strip().lower()

        assessments = assess_reference_candidates(team=team, reference_provider_code=reference_provider)
        if not assessments:
            raise CommandError(f"Team '{team.slug}' has no container with a {reference_provider} subscription.")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Live benchmark candidates — reference {reference_provider}"))
        self.stdout.write("Nothing was fetched to produce this; it reads canonical rows only.")

        for assessment in assessments:
            self.stdout.write("")
            self.stdout.write(f"{assessment.container_number}  [{assessment.journey_state_label}]")
            self.stdout.write(
                f"    latest actual:      {assessment.latest_actual_milestone or '—'} at "
                f"{assessment.latest_actual_event_at or '—'} "
                f"({assessment.latest_actual_provider or 'no provider'})"
            )
            forecast = (
                f" ({assessment.eta_event_type} @ {assessment.eta_location_name})" if assessment.current_eta_at else ""
            )
            self.stdout.write(f"    canonical ETA:      {assessment.current_eta_at or '—'}{forecast}")
            self.stdout.write(f"    ETA source:         {assessment.eta_source or '—'}")
            self.stdout.write(
                f"    subscription:       {assessment.subscription_status or '—'} / {assessment.tracking_status or '—'}"
            )
            self.stdout.write(f"    last {reference_provider} sync: {assessment.last_synced_at or '—'}")
            self.stdout.write(f"    {reference_provider} events:     {assessment.reference_event_count}")
            self.stdout.write(f"    arrived/completed:  {'yes' if assessment.has_arrived else 'no'}")
            if assessment.qualifies:
                self.stdout.write(self.style.SUCCESS("    QUALIFIES as a live benchmark candidate"))
            else:
                for rejection in assessment.rejections:
                    self.stdout.write(f"    rejected: {rejection}")

        chosen = choose_candidate(assessments)
        self.stdout.write("")
        self.stdout.write("=" * 78)
        if chosen is None:
            self.stdout.write(
                self.style.WARNING(
                    "No container qualifies. Do NOT spend a live Traqo request: a finished journey has "
                    "no arrival left to forecast, so it can produce no evidence about what a provider's "
                    "ETA means during one."
                )
            )
            return
        self.stdout.write(self.style.SUCCESS(f"Strongest candidate: {chosen.container_number}"))
        self.stdout.write(
            f"  Run: traqo_test {chosen.container_number} --sealine <SCAC> --compare --live --output T0.json"
        )

    def _announce_comparison(self, *, container_number, sealine, sandbox, reference_provider, options) -> None:
        """State the side effects before causing any of them."""
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Benchmark run"))
        self.stdout.write(f"  Container:          {container_number}")
        self.stdout.write(f"  Carrier / sealine:  {sealine}")
        self.stdout.write(f"  Reference provider: {reference_provider}")
        self.stdout.write(f"  Mode:               {'sandbox' if sandbox else 'PRODUCTION'}")
        self.stdout.write(f"  Match tolerance:    ±{options['tolerance_hours']}h")

        if options["no_fetch"]:
            self.stdout.write("  Traqo request:      none (--no-fetch; comparing stored data)")
        elif sandbox:
            self.stdout.write("  Traqo request:      1 sandbox request (fixed demo data, no slot consumed)")
        else:
            self.stdout.write(
                self.style.WARNING("  Traqo request:      1 PRODUCTION request — a Traqo shipment slot may be consumed")
            )
        if options["refresh_reference"]:
            self.stdout.write(f"  Reference refresh:  yes — one extra {reference_provider} carrier call")
        self.stdout.write("")

    def _require_container(self, team: Team, container_number: str):
        """Return the team's existing container, or explain that the benchmark needs one.

        The benchmark measures providers against a real journey, so it never creates a
        container — an empty one would compare nothing against nothing.
        """
        from django.core.exceptions import ValidationError

        from apps.scm.containers.models import Container
        from apps.scm.containers.utils import parse_container_id

        try:
            parts = parse_container_id(container_number)
        except (ValidationError, ValueError) as exc:
            raise CommandError(f"{container_number} is not a valid container number: {exc}") from exc

        container = Container.objects.filter(
            team=team,
            owner_code=parts["owner_code"],
            category_id=parts["category_id"],
            serial_number=parts["serial_number"],
        ).first()
        if container is None:
            raise CommandError(
                f"Team '{team.slug}' has no container {container_number}. The benchmark compares providers "
                "on a container Container SCM already tracks; it does not create one."
            )
        return container

    def _require_reference_data(self, team, container, reference_provider: str, options: dict) -> None:
        """Refuse to spend a production request when there is nothing to compare against."""
        from apps.scm.tracking.models import TrackingEvent

        count = TrackingEvent.objects.filter(team=team, container=container, provider__code=reference_provider).count()
        if count or options["refresh_reference"] or options["no_fetch"]:
            return
        raise CommandError(
            f"No {reference_provider} events are stored for {container.container_id}, so there is nothing to "
            f"benchmark against. Pick a container with {reference_provider} tracking, or pass "
            "--refresh-reference to fetch it first."
        )

    def _ingest(self, *, team, container, sealine: str, sandbox: bool) -> None:
        result = self._fetch(
            lambda: ingest_traqo_container(
                team=team,
                container=container,
                sealine=sealine,
                sandbox=sandbox,
            ),
            container.container_id,
        )
        if result is None:
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Traqo returned {result.events_mapped} event(s): "
                f"{result.events_created} created, {result.events_updated} updated, "
                f"{result.events_failed} failed."
            )
        )
        self.stdout.write(f"Raw payloads stored this run: {result.raw_payloads_created}")
        self.stdout.write(
            f"Subscription #{result.subscription.pk} "
            f"(provider={result.subscription.provider.code}, status={result.subscription.status}, "
            f"tracking_status={result.subscription.tracking_status})"
        )
        self._write_shipment_summary(result.payload.get("data") or {})
        self._write_stored_events(team, container)
        self._write_phase1_note()

    def _fetch(self, action, reference: str):
        """Run a Traqo call, reporting a typed failure instead of a traceback."""
        try:
            return action()
        except CarrierNoDataError:
            self.stdout.write(self.style.WARNING(f"Traqo has no shipment for {reference}."))
            return None
        except CarrierError as exc:
            # Traqo's messages describe the problem and never echo the credential.
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write_shipment_summary(self, data: dict) -> None:
        """Print the shipment-level values Phase 1 keeps in the raw payload only."""
        self.stdout.write("Shipment level (preserved in TrackingRawPayload, not modelled in Phase 1):")
        for label, key in (
            ("reference", "reference_number"),
            ("sealine", "sealine"),
            ("origin", "origin"),
            ("destination", "destination"),
            ("status", "status"),
            ("eta", "eta"),
            ("is_delayed", "is_delayed"),
            ("latitude", "latitude"),
            ("longitude", "longitude"),
            ("last_updated_at", "last_updated_at"),
        ):
            value = data.get(key)
            if value not in (None, ""):
                self.stdout.write(f"  {label}: {value}")

    def _write_normalised_event(self, event) -> None:
        parts = [
            str(event.event_datetime),
            f"{event.event_type}/{event.event_code}",
            event.event_classifier or "unclassified",
            event.location_name or "(no place)",
        ]
        self.stdout.write("  " + " · ".join(parts) + f" — {event.description}")

    def _write_stored_events(self, team, container) -> None:
        """Read the events back through the ordinary selector the timeline uses."""
        from apps.scm.tracking.selectors import get_tracking_events_for_container

        events = list(get_tracking_events_for_container(team, container))
        self.stdout.write(f"TrackingEvents now stored for {container.container_id}: {len(events)}")
        for event in events:
            self.stdout.write(
                f"  [{event.provider.code}] {event.event_datetime} · {event.event_type}"
                f" ({event.event_time_type}) · {event.location_name or '(no place)'}"
                f" · {event.carrier_reference or '—'} — {event.description}"
            )

    def _write_phase1_note(self) -> None:
        self.stdout.write(
            self.style.WARNING(
                "Phase 1: Traqo is not in the carrier registry, so the scheduled poller and the "
                "container refresh button cannot drive this subscription — re-run this command to "
                "refresh it. See apps/scm/integrations/traqo/README.md."
            )
        )

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

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

    def _resolve_container(self, team: Team, container_number: str):
        """Return the team's container, creating it through the existing linker if new."""
        from django.core.exceptions import ValidationError

        from apps.scm.containers.models import Container
        from apps.scm.containers.utils import parse_container_id
        from apps.scm.integrations.carriers.auto_link import create_or_link_discovered_container
        from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult

        try:
            parts = parse_container_id(container_number)
        except (ValidationError, ValueError) as exc:
            raise CommandError(f"{container_number} is not a valid container number: {exc}") from exc

        existing = Container.objects.filter(
            team=team,
            owner_code=parts["owner_code"],
            category_id=parts["category_id"],
            serial_number=parts["serial_number"],
        ).first()
        if existing is not None:
            return existing

        summary = create_or_link_discovered_container(
            team=team,
            shipment=None,
            result=ContainerDiscoveryResult(
                container_number=container_number,
                carrier_code=PROVIDER_CODE,
                carrier_name="Traqo Ocean",
            ),
        )
        if summary["container"] is None:
            raise CommandError(f"Could not create container {container_number}. An EquipmentType must exist first.")
        self.stdout.write(f"Created container {container_number} for team '{team.slug}'.")
        return summary["container"]
