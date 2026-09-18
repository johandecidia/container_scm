"""The two procurement↔container relationships: models, services, team isolation.

Everything here exists to hold one distinction in place: a container can be *what
was bought* or *what the bought goods travel in*, and the two must stay
distinguishable. The tests are grouped by the question they protect.
"""

import threading
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.container_links import (
    get_container_links_by_line,
    get_container_procurement,
    get_line_container_links,
    link_acquired_container,
    remove_container_load,
    set_container_load,
    unlink_acquired_container,
)
from apps.scm.procurement.models import (
    ContainerAcquisition,
    ContainerLoad,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSource,
)
from apps.teams.models import Team


def _equipment_type() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="45G1",
        defaults={"category": "GP", "length_ft": 45, "high_cube": True, "description": "45' HC"},
    )[0]


def _container(team: Team, owner: str = "MSC", serial: str = "123456") -> Container:
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=_equipment_type(),
    )


def _purchase_order(team: Team, po_number: str = "IF117064", source=PurchaseOrderSource.MANUAL) -> PurchaseOrder:
    return PurchaseOrder.objects.create(
        team=team,
        external_id=f"{team.slug}-{po_number}",
        po_number=po_number,
        supplier_no="SUP-1",
        supplier_name="Acme Equipment",
        source_system=source,
    )


def _line(purchase_order: PurchaseOrder, line_no: str = "10000", item_no: str = "CONT45G1") -> PurchaseOrderLine:
    return PurchaseOrderLine.objects.create(
        team=purchase_order.team,
        purchase_order=purchase_order,
        external_id=f"{purchase_order.external_id}-{line_no}",
        line_no=line_no,
        item_no=item_no,
        description=item_no,
        ordered_qty=Decimal("20"),
    )


class AcquisitionTest(TestCase):
    """Which purchase order line acquired this physical container?"""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Acq", slug="acq-team")
        cls.order = _purchase_order(cls.team)
        cls.line = _line(cls.order)

    def test_links_a_container_to_the_line_that_bought_it(self):
        container = _container(self.team)
        acquisition = link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)

        self.assertEqual(acquisition.purchase_order_line, self.line)
        self.assertEqual(acquisition.container, container)
        self.assertEqual(acquisition.team, self.team)
        # Readable from the box's side too — that is the question the container page asks.
        self.assertEqual(container.acquisition, acquisition)

    def test_one_line_can_acquire_many_containers(self):
        """A CONT45G1 × 20 line becomes twenty boxes, all pointing at the one line."""
        containers = [_container(self.team, serial=f"10000{index}") for index in range(3)]
        for container in containers:
            link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)

        self.assertEqual(self.line.acquired_containers.count(), 3)
        self.assertCountEqual(
            [acquisition.container_id for acquisition in self.line.acquired_containers.all()],
            [container.pk for container in containers],
        )

    def test_a_container_cannot_have_two_procurement_origins(self):
        """A box came from one place. A second line claiming it is refused, not merged."""
        container = _container(self.team)
        other_line = _line(self.order, line_no="20000", item_no="CONT22G1")
        link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)

        with self.assertRaises(ValidationError):
            link_acquired_container(team=self.team, purchase_order_line=other_line, container=container)

        self.assertEqual(ContainerAcquisition.objects.filter(container=container).count(), 1)
        self.assertEqual(container.acquisition.purchase_order_line, self.line)

    def test_the_model_itself_refuses_a_second_origin(self):
        """The rule is on the model, so a writer that bypasses the service still hits it."""
        container = _container(self.team)
        link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)

        with self.assertRaises(ValidationError):
            ContainerAcquisition(
                team=self.team,
                purchase_order_line=_line(self.order, line_no="30000"),
                container=container,
            ).save()

    def test_relinking_the_same_pair_is_a_no_op(self):
        """Idempotent, so replaying an import cannot produce a duplicate."""
        container = _container(self.team)
        first = link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)
        second = link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ContainerAcquisition.objects.count(), 1)

    def test_unlinking_leaves_the_container_and_the_line(self):
        container = _container(self.team)
        acquisition = link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)

        unlink_acquired_container(team=self.team, acquisition=acquisition)

        self.assertFalse(ContainerAcquisition.objects.exists())
        self.assertTrue(Container.objects.filter(pk=container.pk).exists())
        self.assertTrue(PurchaseOrderLine.objects.filter(pk=self.line.pk).exists())

    def test_unlinking_then_relinking_to_another_line_is_allowed(self):
        """Correcting a mistake is a two-step, deliberate act rather than an overwrite."""
        container = _container(self.team)
        other_line = _line(self.order, line_no="20000")
        acquisition = link_acquired_container(team=self.team, purchase_order_line=self.line, container=container)
        unlink_acquired_container(team=self.team, acquisition=acquisition)

        moved = link_acquired_container(team=self.team, purchase_order_line=other_line, container=container)

        self.assertEqual(moved.purchase_order_line, other_line)


class ContainerLoadTest(TestCase):
    """Which purchased goods are transported in this physical container?"""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Load", slug="load-team")
        cls.order = _purchase_order(cls.team, "PO-123")
        cls.doors = _line(cls.order, line_no="20000", item_no="DOORS")
        cls.panels = _line(cls.order, line_no="10000", item_no="PANELS")

    def test_records_a_load_with_a_quantity(self):
        container = _container(self.team)
        load = set_container_load(
            team=self.team,
            purchase_order_line=self.doors,
            container=container,
            quantity=Decimal("300"),
        )

        self.assertEqual(load.quantity, Decimal("300"))
        self.assertEqual(load.purchase_order_line, self.doors)
        self.assertEqual(load.team, self.team)

    def test_quantity_is_optional(self):
        """Knowing the goods are in the box is useful before anybody has counted them."""
        container = _container(self.team)
        load = set_container_load(team=self.team, purchase_order_line=self.doors, container=container)

        self.assertIsNone(load.quantity)

    def test_negative_quantity_is_refused(self):
        container = _container(self.team)
        with self.assertRaises(ValidationError):
            set_container_load(
                team=self.team,
                purchase_order_line=self.doors,
                container=container,
                quantity=Decimal("-1"),
            )

    def test_several_po_lines_can_share_one_container(self):
        """Doors and panels in the same box — two lines, one container."""
        container = _container(self.team)
        set_container_load(team=self.team, purchase_order_line=self.doors, container=container, quantity=Decimal("300"))
        set_container_load(
            team=self.team, purchase_order_line=self.panels, container=container, quantity=Decimal("100")
        )

        self.assertEqual(container.loads.count(), 2)

    def test_one_po_line_can_span_several_containers(self):
        """300 doors here, 200 there — one line, two boxes."""
        first = _container(self.team, owner="TCL", serial="123456")
        second = _container(self.team, owner="TCL", serial="765432")
        set_container_load(team=self.team, purchase_order_line=self.doors, container=first, quantity=Decimal("300"))
        set_container_load(team=self.team, purchase_order_line=self.doors, container=second, quantity=Decimal("200"))

        self.assertEqual(self.doors.container_loads.count(), 2)
        self.assertEqual(
            sum(load.quantity for load in self.doors.container_loads.all()),
            Decimal("500"),
        )

    def test_setting_the_same_pair_again_updates_rather_than_duplicates(self):
        """An upsert on the pair, which is what makes correcting a quantity possible."""
        container = _container(self.team)
        first = set_container_load(
            team=self.team, purchase_order_line=self.doors, container=container, quantity=Decimal("300")
        )
        second = set_container_load(
            team=self.team, purchase_order_line=self.doors, container=container, quantity=Decimal("250")
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ContainerLoad.objects.count(), 1)
        self.assertEqual(ContainerLoad.objects.get().quantity, Decimal("250"))

    def test_setting_the_same_pair_with_the_same_quantity_changes_nothing(self):
        container = _container(self.team)
        set_container_load(team=self.team, purchase_order_line=self.doors, container=container, quantity=Decimal("300"))
        set_container_load(team=self.team, purchase_order_line=self.doors, container=container, quantity=Decimal("300"))

        self.assertEqual(ContainerLoad.objects.count(), 1)
        self.assertEqual(ContainerLoad.objects.get().quantity, Decimal("300"))

    def test_a_recorded_quantity_can_be_cleared_back_to_unknown(self):
        container = _container(self.team)
        set_container_load(team=self.team, purchase_order_line=self.doors, container=container, quantity=Decimal("300"))

        set_container_load(team=self.team, purchase_order_line=self.doors, container=container, quantity=None)

        self.assertIsNone(ContainerLoad.objects.get().quantity)

    def test_removing_a_load_leaves_the_container_and_the_line(self):
        container = _container(self.team)
        load = set_container_load(team=self.team, purchase_order_line=self.doors, container=container)

        remove_container_load(team=self.team, load=load)

        self.assertFalse(ContainerLoad.objects.exists())
        self.assertTrue(Container.objects.filter(pk=container.pk).exists())
        self.assertTrue(PurchaseOrderLine.objects.filter(pk=self.doors.pk).exists())


class AcquisitionAndLoadAreDifferentTest(TestCase):
    """The two relationships coexist on one box without collapsing into each other."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Both", slug="both-team")
        cls.order = _purchase_order(cls.team, "PO-BOTH")
        cls.equipment_line = _line(cls.order, line_no="10000", item_no="CONT45G1")
        cls.goods_line = _line(cls.order, line_no="20000", item_no="DOORS")

    def test_a_box_can_be_bought_by_one_line_and_carry_another(self):
        container = _container(self.team)
        link_acquired_container(team=self.team, purchase_order_line=self.equipment_line, container=container)
        set_container_load(
            team=self.team, purchase_order_line=self.goods_line, container=container, quantity=Decimal("300")
        )

        procurement = get_container_procurement(self.team, container)

        self.assertEqual(procurement.acquisition.purchase_order_line, self.equipment_line)
        self.assertEqual([load.purchase_order_line for load in procurement.loads], [self.goods_line])

    def test_loading_a_line_into_a_box_is_not_acquiring_it(self):
        container = _container(self.team)
        set_container_load(team=self.team, purchase_order_line=self.goods_line, container=container)

        procurement = get_container_procurement(self.team, container)

        self.assertIsNone(procurement.acquisition)
        self.assertTrue(procurement.has_any)


class TeamIsolationTest(TestCase):
    """Neither link may cross a tenant boundary, whichever end is foreign."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Mine", slug="links-mine")
        cls.other = Team.objects.create(name="Theirs", slug="links-theirs")
        cls.line = _line(_purchase_order(cls.team, "PO-MINE"))
        cls.other_line = _line(_purchase_order(cls.other, "PO-THEIRS"))
        cls.container = _container(cls.team, serial="111111")
        cls.other_container = _container(cls.other, owner="CMA", serial="222222")

    def test_cannot_acquire_another_teams_container(self):
        with self.assertRaises(ValidationError):
            link_acquired_container(team=self.team, purchase_order_line=self.line, container=self.other_container)
        self.assertFalse(ContainerAcquisition.objects.exists())

    def test_cannot_acquire_through_another_teams_line(self):
        with self.assertRaises(ValidationError):
            link_acquired_container(team=self.team, purchase_order_line=self.other_line, container=self.container)
        self.assertFalse(ContainerAcquisition.objects.exists())

    def test_cannot_load_another_teams_container(self):
        with self.assertRaises(ValidationError):
            set_container_load(team=self.team, purchase_order_line=self.line, container=self.other_container)
        self.assertFalse(ContainerLoad.objects.exists())

    def test_cannot_load_through_another_teams_line(self):
        with self.assertRaises(ValidationError):
            set_container_load(team=self.team, purchase_order_line=self.other_line, container=self.container)
        self.assertFalse(ContainerLoad.objects.exists())

    def test_the_model_refuses_a_cross_team_link_on_save(self):
        """Declared on the model, so the admin and a shell session are covered too."""
        with self.assertRaises(ValidationError):
            ContainerLoad(team=self.team, purchase_order_line=self.line, container=self.other_container).save()

    def test_reads_do_not_leak_another_teams_links(self):
        link_acquired_container(team=self.other, purchase_order_line=self.other_line, container=self.other_container)

        procurement = get_container_procurement(self.team, self.other_container)

        self.assertIsNone(procurement.acquisition)
        self.assertEqual(procurement.loads, [])

    def test_cannot_unlink_another_teams_acquisition(self):
        acquisition = link_acquired_container(
            team=self.other, purchase_order_line=self.other_line, container=self.other_container
        )

        with self.assertRaises(ValidationError):
            unlink_acquired_container(team=self.team, acquisition=acquisition)
        self.assertTrue(ContainerAcquisition.objects.filter(pk=acquisition.pk).exists())

    def test_cannot_remove_another_teams_load(self):
        load = set_container_load(team=self.other, purchase_order_line=self.other_line, container=self.other_container)

        with self.assertRaises(ValidationError):
            remove_container_load(team=self.team, load=load)
        self.assertTrue(ContainerLoad.objects.filter(pk=load.pk).exists())


class BusinessCentralReadOnlyTest(TestCase):
    """A BC-owned order's container links are read-only, whoever is asking.

    The workspace hides the buttons, but that is chrome. These tests go straight at
    the service layer, which is where the invariant has to hold: a shell session, a
    management command or a hand-rolled POST reaches the same functions, and the
    next sync would revert whatever they wrote anyway.
    """

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="BC links", slug="bc-links")
        cls.bc_order = _purchase_order(cls.team, "BC-1", source=PurchaseOrderSource.BUSINESS_CENTRAL)
        cls.bc_line = _line(cls.bc_order)
        cls.other_bc_line = _line(cls.bc_order, line_no="20000", item_no="DOORS")
        cls.manual_order = _purchase_order(cls.team, "MAN-1", source=PurchaseOrderSource.MANUAL)
        cls.manual_line = _line(cls.manual_order)

    def _existing_acquisition(self, container):
        """A link as the source system would have left it, made without the service."""
        return ContainerAcquisition.objects.create(
            team=self.team, purchase_order_line=self.bc_line, container=container
        )

    def _existing_load(self, container, quantity=Decimal("300")):
        return ContainerLoad.objects.create(
            team=self.team, purchase_order_line=self.bc_line, container=container, quantity=quantity
        )

    # -- acquisition --------------------------------------------------------

    def test_refuses_to_acquire_a_container_through_a_bc_line(self):
        container = _container(self.team, serial="200001")

        with self.assertRaises(PermissionDenied):
            link_acquired_container(team=self.team, purchase_order_line=self.bc_line, container=container)

        self.assertFalse(ContainerAcquisition.objects.exists())

    def test_refuses_to_relink_an_existing_bc_acquisition(self):
        """Even the idempotent path is refused: it is still a write to BC's record."""
        acquisition = self._existing_acquisition(_container(self.team, serial="200002"))

        with self.assertRaises(PermissionDenied):
            link_acquired_container(team=self.team, purchase_order_line=self.bc_line, container=acquisition.container)

    def test_refuses_to_move_a_bc_acquisition_to_another_bc_line(self):
        acquisition = self._existing_acquisition(_container(self.team, serial="200003"))

        with self.assertRaises(PermissionDenied):
            link_acquired_container(
                team=self.team, purchase_order_line=self.other_bc_line, container=acquisition.container
            )

        acquisition.refresh_from_db()
        self.assertEqual(acquisition.purchase_order_line, self.bc_line)

    def test_refuses_to_unlink_a_bc_acquisition(self):
        acquisition = self._existing_acquisition(_container(self.team, serial="200004"))

        with self.assertRaises(PermissionDenied):
            unlink_acquired_container(team=self.team, acquisition=acquisition)

        self.assertTrue(ContainerAcquisition.objects.filter(pk=acquisition.pk).exists())

    # -- load ---------------------------------------------------------------

    def test_refuses_to_create_a_load_on_a_bc_line(self):
        container = _container(self.team, serial="200005")

        with self.assertRaises(PermissionDenied):
            set_container_load(
                team=self.team, purchase_order_line=self.bc_line, container=container, quantity=Decimal("10")
            )

        self.assertFalse(ContainerLoad.objects.exists())

    def test_refuses_to_update_an_existing_bc_load(self):
        load = self._existing_load(_container(self.team, serial="200006"))

        with self.assertRaises(PermissionDenied):
            set_container_load(
                team=self.team,
                purchase_order_line=self.bc_line,
                container=load.container,
                quantity=Decimal("999"),
            )

        load.refresh_from_db()
        self.assertEqual(load.quantity, Decimal("300"))

    def test_refuses_to_remove_a_bc_load(self):
        load = self._existing_load(_container(self.team, serial="200007"))

        with self.assertRaises(PermissionDenied):
            remove_container_load(team=self.team, load=load)

        self.assertTrue(ContainerLoad.objects.filter(pk=load.pk).exists())

    # -- the wall is about ownership, not about links -----------------------

    def test_an_scm_owned_order_is_still_writable(self):
        """The refusal is BC ownership, not a blanket freeze on linking."""
        container = _container(self.team, serial="200008")

        acquisition = link_acquired_container(team=self.team, purchase_order_line=self.manual_line, container=container)

        self.assertEqual(acquisition.purchase_order_line, self.manual_line)

    def test_the_refusal_reads_the_same_ownership_flag_the_workspace_does(self):
        """One definition of read-only: flipping the source makes the order writable."""
        container = _container(self.team, serial="200009")
        self.bc_order.source_system = PurchaseOrderSource.MANUAL
        self.bc_order.save(update_fields=["source_system"])
        line = PurchaseOrderLine.objects.select_related("purchase_order").get(pk=self.bc_line.pk)

        link_acquired_container(team=self.team, purchase_order_line=line, container=container)

        self.assertEqual(ContainerAcquisition.objects.get().container, container)


class AcquisitionConcurrencyTest(TransactionTestCase):
    """Two requests racing for the same box, which is what the row lock is for.

    ``TransactionTestCase`` rather than ``TestCase``: the point is what two real
    connections do to each other, and a test wrapped in one transaction would never
    let them see each other's rows.
    """

    def setUp(self):
        self.team = Team.objects.create(name="Race", slug="race-team")
        self.order = _purchase_order(self.team, "PO-RACE")
        self.first_line = _line(self.order, line_no="10000")
        self.second_line = _line(self.order, line_no="20000", item_no="CONT22G1")
        self.container = _container(self.team, serial="555555")

    def _race(self, lines: list[PurchaseOrderLine]) -> list:
        """Call the service from one thread per line, released together."""
        barrier = threading.Barrier(len(lines))
        results: list = [None] * len(lines)

        def attempt(index: int, line: PurchaseOrderLine) -> None:
            try:
                barrier.wait(timeout=10)
                results[index] = link_acquired_container(
                    team=self.team, purchase_order_line=line, container=self.container
                )
            except Exception as error:  # noqa: BLE001 — the result under test
                results[index] = error
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt, args=(index, line)) for index, line in enumerate(lines)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def test_the_same_pair_twice_at_once_yields_one_acquisition(self):
        """Idempotent under a race too: the loser reads the winner's row, not a 500."""
        results = self._race([self.first_line, self.first_line])

        self.assertEqual(ContainerAcquisition.objects.count(), 1)
        self.assertNotIsInstance(results[0], Exception, msg=f"first attempt raised {results[0]!r}")
        self.assertNotIsInstance(results[1], Exception, msg=f"second attempt raised {results[1]!r}")
        self.assertEqual(results[0].pk, results[1].pk)

    def test_two_lines_racing_for_one_box_give_one_winner_and_a_domain_error(self):
        """The loser gets the sentence an operator can act on, not an IntegrityError."""
        results = self._race([self.first_line, self.second_line])

        self.assertEqual(ContainerAcquisition.objects.count(), 1)
        winners = [result for result in results if isinstance(result, ContainerAcquisition)]
        losers = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIsInstance(losers[0], ValidationError)
        self.assertEqual(ContainerAcquisition.objects.get().pk, winners[0].pk)

    def test_the_database_constraint_is_still_the_last_word(self):
        """The lock is not a reason to drop the uniqueness that backs it up."""
        link_acquired_container(team=self.team, purchase_order_line=self.first_line, container=self.container)

        with self.assertRaises(IntegrityError), transaction.atomic():
            # bulk_create skips save() and so skips full_clean(); only the database
            # is left to say no.
            ContainerAcquisition.objects.bulk_create(
                [ContainerAcquisition(team=self.team, purchase_order_line=self.second_line, container=self.container)]
            )

        self.assertEqual(ContainerAcquisition.objects.count(), 1)


class ReadModelTest(TestCase):
    """The per-line and per-container reads, including what they say about nothing."""

    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Reads", slug="reads-team")
        cls.order = _purchase_order(cls.team, "PO-READ")
        cls.equipment_line = _line(cls.order, line_no="10000", item_no="CONT45G1")
        cls.goods_line = _line(cls.order, line_no="20000", item_no="DOORS")
        cls.untouched_line = _line(cls.order, line_no="30000", item_no="SPARES")
        cls.acquired = _container(cls.team, owner="MSC", serial="123456")
        cls.loaded = _container(cls.team, owner="TCL", serial="123456")
        link_acquired_container(team=cls.team, purchase_order_line=cls.equipment_line, container=cls.acquired)
        set_container_load(
            team=cls.team, purchase_order_line=cls.goods_line, container=cls.loaded, quantity=Decimal("300")
        )

    def test_links_are_grouped_by_line_and_kept_apart(self):
        links_by_line = get_container_links_by_line(self.order)

        equipment = get_line_container_links(links_by_line, self.equipment_line.pk)
        goods = get_line_container_links(links_by_line, self.goods_line.pk)

        self.assertEqual([link.container for link in equipment.acquired], [self.acquired])
        self.assertEqual(equipment.loads, [])
        self.assertEqual(goods.acquired, [])
        self.assertEqual([link.container for link in goods.loads], [self.loaded])

    def test_a_line_with_no_links_reads_as_empty_rather_than_missing(self):
        links_by_line = get_container_links_by_line(self.order)

        links = get_line_container_links(links_by_line, self.untouched_line.pk)

        self.assertFalse(links.has_any)
        self.assertEqual(links.acquired, [])
        self.assertEqual(links.loads, [])

    def test_container_procurement_is_empty_for_an_unlinked_box(self):
        stray = _container(self.team, owner="HLX", serial="999999")

        procurement = get_container_procurement(self.team, stray)

        self.assertFalse(procurement.has_any)

    def test_container_procurement_exposes_the_acquiring_order(self):
        procurement = get_container_procurement(self.team, self.acquired)

        self.assertEqual(procurement.acquired_line, self.equipment_line)
        self.assertEqual(procurement.acquired_order, self.order)

    def test_deleting_a_line_removes_its_links_and_nothing_else(self):
        self.goods_line.delete()

        self.assertFalse(ContainerLoad.objects.exists())
        self.assertTrue(Container.objects.filter(pk=self.loaded.pk).exists())
        self.assertTrue(ContainerAcquisition.objects.exists())
