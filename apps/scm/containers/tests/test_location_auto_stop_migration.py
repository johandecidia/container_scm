"""``scm_containers.0013``: each location takes its team's previous stop-on-receive; nothing else changes."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [
    ("scm_containers", "0012_container_tracking_provider_override"),
    ("scm_tracking", "0017_teamtrackingsettings_stop_tracking_on_receive"),
]
AFTER = [("scm_containers", "0013_location_auto_stop_tracking_on_receive")]


class LocationAutoStopMigrationTest(TransactionTestCase):
    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        executor.loader.build_graph()
        return executor.loader.project_state(targets).apps

    def _restore_all_migrations(self) -> None:
        """Back to every leaf, so the rest of the suite shares a complete schema (see test_conditions)."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        executor.loader.build_graph()

    def setUp(self):
        self.addCleanup(self._restore_all_migrations)

    def test_team_policy_is_copied_to_every_location_and_containers_are_untouched(self):
        apps = self._migrate(BEFORE)
        Team = apps.get_model("teams", "Team")
        Settings = apps.get_model("scm_tracking", "TeamTrackingSettings")
        Location = apps.get_model("scm_containers", "ContainerLocation")
        EquipmentType = apps.get_model("scm_containers", "EquipmentType")
        Container = apps.get_model("scm_containers", "Container")

        on, off, unset = (Team.objects.create(name=s, slug=f"mig-autostop-{s}") for s in ("on", "off", "unset"))
        Settings.objects.create(team=on, stop_tracking_on_receive=True)
        Settings.objects.create(team=off, stop_tracking_on_receive=False)
        locations = {
            "on-1": Location.objects.create(team=on, name="Oceanterminalen"),
            "on-2": Location.objects.create(team=on, name="Conspect"),
            "off-1": Location.objects.create(team=off, name="Oceanterminalen"),
            "off-2": Location.objects.create(team=off, name="Depot"),
            "unset-1": Location.objects.create(team=unset, name="Depot"),
        }
        equipment = EquipmentType.objects.get_or_create(
            iso_code="22G1", defaults={"category": "GP", "length_ft": 20, "description": "20'"}
        )[0]
        container = Container.objects.create(
            team=on, owner_code="PSL", category_id="U", serial_number="291303", check_digit=6,
            equipment_type=equipment, current_location=locations["on-1"],
        )  # fmt: skip
        container_before = Container.objects.filter(pk=container.pk).values().get()

        apps = self._migrate(AFTER)
        Location = apps.get_model("scm_containers", "ContainerLocation")
        Container = apps.get_model("scm_containers", "Container")
        flags = {key: Location.objects.get(pk=loc.pk).auto_stop_tracking_on_receive for key, loc in locations.items()}
        self.assertEqual(flags, {"on-1": True, "on-2": True, "off-1": False, "off-2": False, "unset-1": False})
        self.assertEqual(Container.objects.filter(pk=container.pk).values().get(), container_before)

        self._restore_all_migrations()
        from apps.scm.containers.models import ContainerLocation
        from apps.scm.tracking.models import TeamTrackingSettings

        new = ContainerLocation.objects.create(team_id=on.pk, name="New depot")
        self.assertFalse(new.auto_stop_tracking_on_receive)
        self.assertNotIn("stop_tracking_on_receive", {f.name for f in TeamTrackingSettings._meta.get_fields()})
