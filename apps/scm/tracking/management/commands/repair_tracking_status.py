"""Re-derive tracking statuses the retired carrier-poller bug overwrote.

    make manage ARGS='repair_tracking_status --dry-run'
    make manage ARGS='repair_tracking_status'
    make manage ARGS='repair_tracking_status --team decidia'

Only non-carrier providers are considered, and only where ``tracking_status`` is
NOT_CONFIGURED — the signature the old poller left on providers it does not drive. A
carrier's NOT_CONFIGURED is a real report about a real gap and is never touched.

Nothing is fetched and no sync run is created: each status is re-derived from the
subscription's own most recent successful run. See :mod:`apps.scm.tracking.repair`.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.scm.tracking.repair import repair_non_carrier_tracking_statuses
from apps.teams.models import Team


class Command(BaseCommand):
    help = "Repair non-carrier tracking subscription statuses left stale by the old carrier poller."

    def add_arguments(self, parser):
        parser.add_argument("--team", help="Team slug. Every team is considered when omitted.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        team = self._resolve_team(options.get("team"))
        commit = not options["dry_run"]

        repairs = repair_non_carrier_tracking_statuses(team=team, commit=commit)
        if not repairs:
            self.stdout.write("No non-carrier subscription is in a repairable state. Nothing to do.")
            return

        for repair in repairs:
            self.stdout.write("")
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    f"Subscription #{repair.subscription_id} — {repair.provider_code} {repair.reference}"
                )
            )
            self.stdout.write(f"  before: {repair.before}")
            self.stdout.write(f"  after:  {repair.after}")
            self.stdout.write(f"  reason: {repair.reason}")

        changed = [repair for repair in repairs if repair.changed]
        self.stdout.write("")
        if commit:
            self.stdout.write(self.style.SUCCESS(f"Repaired {len(changed)} of {len(repairs)} subscription(s)."))
        else:
            would_change = [repair for repair in repairs if repair.before != repair.after]
            self.stdout.write(
                self.style.WARNING(f"--dry-run: {len(would_change)} of {len(repairs)} subscription(s) would change.")
            )

    def _resolve_team(self, slug: str | None) -> Team | None:
        if not slug:
            return None
        try:
            return Team.objects.get(slug=slug)
        except Team.DoesNotExist as exc:
            raise CommandError(f"No team with slug '{slug}'.") from exc
