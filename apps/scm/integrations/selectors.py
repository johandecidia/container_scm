# Integration selectors — all read/query operations.
from dataclasses import dataclass

from django.db.models import Count, Q, QuerySet

from apps.teams.models import Team

from .business_systems.business_central import resources as bc_resources
from .models import (
    BusinessCentralItemLedgerEntry,
    Integration,
    IntegrationRequestLog,
    IntegrationSyncRun,
    IntegrationWebhookEvent,
)


def get_team_integrations(team: Team) -> QuerySet[Integration]:
    return Integration.objects.filter(team=team).order_by("name")


def get_active_team_integrations(team: Team) -> QuerySet[Integration]:
    return Integration.objects.filter(team=team, status=Integration.Status.ACTIVE, is_active=True)


def get_team_integration_by_provider(team: Team, provider_code: str) -> Integration:
    """Return the integration for the given team and provider_code.

    Raises Integration.DoesNotExist if not found.
    """
    return Integration.objects.get(team=team, provider_code=provider_code)


def get_recent_request_logs(team: Team, limit: int = 50) -> QuerySet[IntegrationRequestLog]:
    return IntegrationRequestLog.objects.filter(team=team).order_by("-created_at")[:limit]


def get_unprocessed_webhook_events(team: Team) -> QuerySet[IntegrationWebhookEvent]:
    return IntegrationWebhookEvent.objects.filter(team=team, status=IntegrationWebhookEvent.Status.RECEIVED).order_by(
        "created_at"
    )


def get_team_business_central_integrations(team: Team) -> QuerySet[Integration]:
    """BC business-system integrations for a team (for the monitoring UI)."""
    return Integration.objects.filter(
        team=team,
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
        provider_code="business_central",
    ).order_by("name")


@dataclass(frozen=True)
class ResourceSyncStatus:
    """Monitoring summary for one resource of one integration, built from its sync-run history."""

    resource_type: str
    label: str
    implemented: bool
    enabled: bool
    latest_run: IntegrationSyncRun | None
    last_finished_run: IntegrationSyncRun | None
    last_completed_run: IntegrationSyncRun | None

    @property
    def never_synced(self) -> bool:
        return self.latest_run is None

    @property
    def in_progress(self) -> bool:
        return bool(self.latest_run and self.latest_run.status == IntegrationSyncRun.Status.RUNNING)

    @property
    def has_open_error(self) -> bool:
        """The most recent finished run did not complete fully (failed or partial)."""
        return bool(self.last_finished_run and self.last_finished_run.status != IntegrationSyncRun.Status.COMPLETED)

    @property
    def error_summary(self) -> str:
        run = self.last_finished_run
        return run.error_summary if run and self.has_open_error else ""


_FINISHED_STATUSES = [
    IntegrationSyncRun.Status.COMPLETED,
    IntegrationSyncRun.Status.PARTIALLY_COMPLETED,
    IntegrationSyncRun.Status.FAILED,
]


def _latest_run_per_resource(runs: QuerySet[IntegrationSyncRun], order: str) -> dict[str, IntegrationSyncRun]:
    # DISTINCT ON (resource_type): one query for all resources (PostgreSQL).
    return {run.resource_type: run for run in runs.order_by("resource_type", order).distinct("resource_type")}


def get_resource_sync_statuses(integration: Integration) -> list[ResourceSyncStatus]:
    """One status per defined resource type, including resources that never synced."""
    runs = IntegrationSyncRun.objects.filter(integration=integration)
    latest = _latest_run_per_resource(runs, "-started_at")
    finished = _latest_run_per_resource(runs.filter(status__in=_FINISHED_STATUSES), "-finished_at")
    completed = _latest_run_per_resource(runs.filter(status=IntegrationSyncRun.Status.COMPLETED), "-finished_at")
    statuses = []
    for resource_type in IntegrationSyncRun.ResourceType.values:
        settings = bc_resources.get_resource_settings(integration, resource_type)
        statuses.append(
            ResourceSyncStatus(
                resource_type=resource_type,
                label=settings.label,
                implemented=settings.implemented,
                enabled=settings.enabled,
                latest_run=latest.get(resource_type),
                last_finished_run=finished.get(resource_type),
                last_completed_run=completed.get(resource_type),
            )
        )
    return statuses


def get_open_sync_errors(integration: Integration, *, exclude: str | None = None) -> list[ResourceSyncStatus]:
    """Resources whose most recent finished run failed or was partial (optionally excluding one)."""
    return [s for s in get_resource_sync_statuses(integration) if s.has_open_error and s.resource_type != exclude]


def get_latest_sync_run(
    integration: Integration, resource_type: str = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS
) -> IntegrationSyncRun | None:
    return (
        IntegrationSyncRun.objects.filter(integration=integration, resource_type=resource_type)
        .order_by("-started_at")
        .first()
    )


def is_sync_in_progress(
    integration: Integration, resource_type: str = IntegrationSyncRun.ResourceType.PURCHASE_ORDERS
) -> bool:
    return IntegrationSyncRun.objects.filter(
        integration=integration,
        resource_type=resource_type,
        status=IntegrationSyncRun.Status.RUNNING,
    ).exists()


def get_integration_monitoring_context(integration: Integration) -> dict:
    """Sanitised monitoring summary for one integration (never exposes credentials).

    The top-level run keys describe purchase orders (the resource the page can
    sync now); ``resources`` has one status per resource type, and
    ``resource_errors`` the resources whose latest finished run did not complete.
    """
    config = integration.config or {}
    resources = get_resource_sync_statuses(integration)
    purchase_orders = next(r for r in resources if r.resource_type == IntegrationSyncRun.ResourceType.PURCHASE_ORDERS)
    last_failed = (
        IntegrationSyncRun.objects.filter(
            integration=integration,
            resource_type=IntegrationSyncRun.ResourceType.PURCHASE_ORDERS,
            status=IntegrationSyncRun.Status.FAILED,
        )
        .order_by("-finished_at")
        .first()
    )
    last_completed = purchase_orders.last_completed_run
    return {
        "integration": integration,
        "environment": config.get("environment", ""),
        "company_id": config.get("company_id", ""),
        "latest_run": purchase_orders.latest_run,
        "last_completed_run": last_completed,
        "last_failed_run": last_failed,
        "current_watermark": last_completed.watermark_to if last_completed else None,
        "in_progress": purchase_orders.in_progress,
        "resources": resources,
        "resource_errors": [r for r in resources if r.has_open_error],
    }


def get_item_summary(integration: Integration) -> list[dict]:
    """Every item number in the integration's item ledger entries, with its classification.

    Unclassified items come first, so an administrator sees what still needs a rule.
    """
    from .business_systems.business_central.item_rules import ItemRuleSet

    rows = (
        BusinessCentralItemLedgerEntry.objects.filter(integration=integration)
        .values("item_no")
        .annotate(
            entries=Count("id"),
            valid_serials=Count("id", filter=Q(serial_status=BusinessCentralItemLedgerEntry.SerialStatus.VALID_ISO)),
        )
        .order_by()
    )
    rules = ItemRuleSet.for_integration(integration)
    summary = [{**row, "resolution": rules.resolve(row["item_no"])} for row in rows]
    unclassified = BusinessCentralItemLedgerEntry.ItemClassification.UNCLASSIFIED
    summary.sort(key=lambda r: (r["resolution"].classification != unclassified, -r["entries"], r["item_no"]))
    return summary
