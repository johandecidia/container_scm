from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.teams.models import BaseTeamModel

from .choices import (
    OBSERVED_LOCATION_SOURCES,
    ColorSystem,
    ContainerCategory,
    ContainerCondition,
    ContainerStatus,
    EquipmentCategory,
    LocationSource,
    LocationType,
    MovementType,
)
from .location_identity import (
    normalize_alias_source,
    normalize_country_code,
    normalize_external_code,
    normalize_location_name,
    normalize_unlocode,
)
from .utils import validate_container_id

# How deep a location hierarchy may be walked when checking for a cycle. A port
# inside a port inside a port is already past anything the domain describes; the
# bound exists so a cycle written directly to the database cannot spin `clean()`
# forever.
_MAX_PARENT_DEPTH = 10


class PlannedContainerStatus(models.TextChoices):
    PLANNED = "planned", _("Planned")
    DETECTED = "detected", _("Detected")
    IN_TRANSIT = "in_transit", _("In Transit")
    ARRIVED = "arrived", _("Arrived")
    CANCELLED = "cancelled", _("Cancelled")
    EXPIRED = "expired", _("Expired")


class PlannedContainerResult(models.TextChoices):
    """The outcome of the most recent discovery attempt.

    NOT_FOUND is a valid answer — the carrier does not know the number yet — and is
    kept distinct from SKIPPED (never asked) and ERROR (asked and failed).
    """

    PENDING = "pending", _("Not checked yet")
    NOT_FOUND = "not_found", _("Not known at carrier yet")
    DETECTED = "detected", _("Detected")
    SKIPPED = "skipped", _("Skipped — carrier not available")
    ERROR = "error", _("Error")


def equipment_type_image_path(instance, filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1]
    return f"equipment_types/{instance.iso_code}.{ext}"


class ContainerLocation(BaseTeamModel):
    """MCR's canonical identity for a real place. One row, one place.

    This is the *identity* layer, and it is deliberately the only one of the three
    location concepts that owns a name:

    ``ContainerLocation``
        Identity. What MCR considers a place to be. Owned by MCR, edited by MCR,
        never created as a side effect of reading a carrier response.
    :class:`LocationAlias`
        Evidence about naming. What Traqo, Maersk or CMA CGM call this place.
    ``Container.current_location`` / :class:`ContainerMovement`
        State. Where a box is believed to be. LOC-2's territory.

    **UN/LOCODE is not unique here, on purpose.** Göteborg the port,
    Oceanterminalen inside it and APM Terminals Gothenburg beside that are three
    operational places under one code, ``SEGOT``. Making the column unique would
    force two of the three to be misfiled or invented as something else. It is
    indexed, not constrained, and ``location_resolver`` handles the plurality by
    refusing to guess between them.

    ``normalized_name`` is derived, maintained by ``save``, and exists so a name can
    be looked up on an index rather than by loading every location a team has and
    comparing in Python. It is the same relationship ``TrackingEvent.event_fingerprint``
    has to the fields it is built from: a stored form of a pure function, not a
    second source of truth. Nothing should ever write to it directly.
    """

    name = models.CharField(_("name"), max_length=200)
    normalized_name = models.CharField(
        _("normalised name"),
        max_length=200,
        blank=True,
        editable=False,
        help_text=_("Derived from the name on save, for matching. Not edited directly."),
    )
    location_type = models.CharField(
        _("location type"),
        max_length=30,
        choices=LocationType.choices,
        default=LocationType.UNKNOWN,
    )
    unlocode = models.CharField(
        _("UN/LOCODE"),
        max_length=5,
        blank=True,
        help_text=_("Stored canonically, e.g. SEGOT. Several locations may share one code."),
    )
    country_code = models.CharField(
        _("country code"),
        max_length=2,
        blank=True,
        help_text=_("ISO 3166-1 alpha-2, e.g. SE. Kept apart from the free-text country name."),
    )
    country = models.CharField(_("country"), max_length=100, blank=True)
    city = models.CharField(_("city"), max_length=100, blank=True)
    latitude = models.DecimalField(_("latitude"), max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(_("longitude"), max_digits=9, decimal_places=6, null=True, blank=True)
    timezone = models.CharField(
        _("timezone"),
        max_length=64,
        blank=True,
        help_text=_("IANA name, e.g. Europe/Stockholm."),
    )
    parent_location = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_locations",
        verbose_name=_("parent location"),
        help_text=_("The larger place this one sits inside, e.g. the port a terminal belongs to."),
    )
    address = models.TextField(_("address"), blank=True)
    external_reference = models.CharField(_("external reference"), max_length=100, blank=True)
    owner_name = models.CharField(_("owner name"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["team", "location_type"]),
            models.Index(fields=["team", "is_active"]),
            # The resolver's three lookup paths. None is a unique constraint: a code,
            # a name and a parent may each legitimately be shared.
            models.Index(fields=["team", "unlocode"]),
            models.Index(fields=["team", "normalized_name"]),
            models.Index(fields=["team", "parent_location"]),
        ]
        verbose_name = _("Container Location")
        verbose_name_plural = _("Container Locations")

    def __str__(self) -> str:
        parts = [self.name]
        if self.city:
            parts.append(self.city)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)

    def clean(self) -> None:
        """Reject a hierarchy that is not one, and a parent from another tenant."""
        super().clean()
        parent_id = self.parent_location_id
        if parent_id is None:
            return

        if self.pk is not None and parent_id == self.pk:
            raise ValidationError({"parent_location": _("A location cannot be its own parent.")})

        parent = self.parent_location
        if parent is not None and self.team_id and parent.team_id != self.team_id:
            raise ValidationError({"parent_location": _("The parent location must belong to the same team.")})

        # Walking up from the proposed parent must not arrive back here. Without
        # this, "make A the child of B" and "make B the child of A" are each
        # individually valid and together detach both from every query that starts
        # at a root.
        seen = {self.pk} if self.pk is not None else set()
        current = parent
        for _step in range(_MAX_PARENT_DEPTH):
            if current is None:
                return
            if current.pk in seen:
                raise ValidationError({"parent_location": _("That would make the location hierarchy circular.")})
            seen.add(current.pk)
            current = current.parent_location
        raise ValidationError({"parent_location": _("The location hierarchy is nested too deeply.")})

    def _canonicalise(self) -> None:
        """Put the identifying fields into their canonical form.

        Called from both ``clean_fields`` and ``save`` so validation and persistence
        see the same values. Without the first, ``full_clean`` would reject the very
        inputs normalisation exists to accept: "SE GOT" is six characters and the
        column holds five, so a code somebody typed with a space would fail
        length validation before ``save`` ever got the chance to fix it.
        """
        self.unlocode = normalize_unlocode(self.unlocode)
        self.country_code = normalize_country_code(self.country_code)
        self.normalized_name = normalize_location_name(self.name)

    def clean_fields(self, exclude=None):
        self._canonicalise()
        super().clean_fields(exclude=exclude)

    def save(self, *args, **kwargs):
        """Canonicalise the identifying fields, then save.

        Normalisation happens in the model rather than in the form so it holds for
        every writer — the admin, the seed command, a shell session — and so the
        stored UN/LOCODE is always in the form the resolver looks for.
        """
        self._canonicalise()
        if (update_fields := kwargs.get("update_fields")) is not None:
            # A caller updating one column must not silently drop the normalisation
            # of the others; add only what this save actually recomputed.
            kwargs["update_fields"] = {*update_fields, "unlocode", "country_code", "normalized_name"}
        return super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        """This place inside its parent, e.g. "Göteborg / Oceanterminalen".

        One level up only. A location's own name is what operators use; the parent
        is context for the cases — a terminal name that means nothing on its own —
        where it is needed.
        """
        if self.parent_location_id and self.parent_location is not None:
            return f"{self.parent_location.name} / {self.name}"
        return self.name


class LocationAlias(BaseTeamModel):
    """What somebody outside MCR calls a :class:`ContainerLocation`.

    The alias layer is what keeps the canonical model clean. Without it, every
    provider that spells Göteborg differently would want a column —
    ``traqo_name``, ``maersk_name``, ``cma_name`` — and the canonical row would
    become a junk drawer of other people's vocabularies, with no way to add the
    next provider except another migration.

    So instead:

    .. code-block:: text

        traqo      "GOTHENBURG"       ┐
        maersk     "GOTEBORG"         ├──▶  ContainerLocation "Göteborg"  (SEGOT, PORT)
        cma-cgm    "GOTHENBURG, SE"   │
        unlocode   "SEGOT"            ┘
        internal   "Oceanterminalen"  ───▶  ContainerLocation "Oceanterminalen"

    **An alias must actually say something.** At least one of ``external_code`` and
    ``external_name`` is required, so no row exists purely to satisfy the schema.

    **One source cannot name two places the same thing.** The unique constraints
    make (team, source, code) and (team, source, name) single-valued, which is what
    lets the resolver treat an alias hit as certain rather than as one candidate
    among several. Both are scoped to the team, so one tenant's aliases can never
    resolve against another's locations.
    """

    location = models.ForeignKey(
        ContainerLocation,
        on_delete=models.CASCADE,
        related_name="aliases",
        verbose_name=_("location"),
    )
    source = models.CharField(
        _("source"),
        max_length=50,
        help_text=_("A TrackingProvider code, or one of the reserved sources: unlocode, internal."),
    )
    external_code = models.CharField(
        _("external code"),
        max_length=100,
        blank=True,
        help_text=_("The identifier this source uses for the place, if it has one."),
    )
    external_name = models.CharField(
        _("external name"),
        max_length=200,
        blank=True,
        help_text=_("The place name this source reports, verbatim."),
    )
    normalized_name = models.CharField(
        _("normalised name"),
        max_length=200,
        blank=True,
        editable=False,
        help_text=_("Derived from the external name on save, for matching. Not edited directly."),
    )
    latitude = models.DecimalField(_("latitude"), max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(_("longitude"), max_digits=9, decimal_places=6, null=True, blank=True)
    metadata = models.JSONField(
        _("metadata"),
        default=dict,
        blank=True,
        help_text=_("Anything else this source published about the place, kept verbatim."),
    )

    class Meta:
        ordering = ["source", "external_name", "external_code"]
        indexes = [
            models.Index(fields=["team", "source", "normalized_name"]),
            models.Index(fields=["team", "source", "external_code"]),
            models.Index(fields=["team", "location"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "source", "external_code"],
                condition=models.Q(external_code__gt=""),
                name="unique_location_alias_code_per_source",
            ),
            models.UniqueConstraint(
                fields=["team", "source", "normalized_name"],
                condition=models.Q(normalized_name__gt=""),
                name="unique_location_alias_name_per_source",
            ),
        ]
        verbose_name = _("Location Alias")
        verbose_name_plural = _("Location Aliases")

    def __str__(self) -> str:
        return f"{self.source}: {self.external_name or self.external_code}"

    def _canonicalise(self) -> None:
        self.source = normalize_alias_source(self.source)
        self.external_code = normalize_external_code(self.external_code)
        self.external_name = (self.external_name or "").strip()
        self.normalized_name = normalize_location_name(self.external_name)

    def clean_fields(self, exclude=None):
        # Before validation, so `validate_unique` compares the derived
        # `normalized_name` this row will actually be stored with — otherwise two
        # aliases spelling one name differently would both pass and then collide in
        # the database.
        self._canonicalise()
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        if not self.external_code and not self.normalized_name:
            raise ValidationError(_("An alias needs an external code or an external name."))
        if self.location_id and self.team_id and self.location.team_id != self.team_id:
            raise ValidationError({"location": _("The alias and its location must belong to the same team.")})

    def save(self, *args, **kwargs):
        self._canonicalise()
        if (update_fields := kwargs.get("update_fields")) is not None:
            kwargs["update_fields"] = {*update_fields, "source", "external_code", "external_name", "normalized_name"}
        return super().save(*args, **kwargs)


class EquipmentType(models.Model):
    """ISO 6346 equipment type, identified by a 4-character ISO code (e.g. 22G1 → 20GP)."""

    iso_code = models.CharField(_("ISO code"), max_length=4, primary_key=True)
    category = models.CharField(_("category"), max_length=10, choices=EquipmentCategory.choices)
    length_ft = models.PositiveSmallIntegerField(_("length (ft)"))
    high_cube = models.BooleanField(_("high cube"), default=False)
    description = models.CharField(_("description"), max_length=100)

    image = models.ImageField(
        _("image"),
        upload_to=equipment_type_image_path,
        null=True,
        blank=True,
    )

    std_external_length = models.PositiveIntegerField(_("std external length (mm)"), null=True, blank=True)
    std_external_width = models.PositiveIntegerField(_("std external width (mm)"), null=True, blank=True)
    std_external_height = models.PositiveIntegerField(_("std external height (mm)"), null=True, blank=True)

    std_tare_weight = models.PositiveIntegerField(_("std tare weight (kg)"), null=True, blank=True)
    std_max_payload = models.PositiveIntegerField(_("std max payload (kg)"), null=True, blank=True)

    std_cubic_capacity = models.DecimalField(
        _("std cubic capacity (m³)"),
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
    )

    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["length_ft", "category"]
        verbose_name = _("Equipment Type")
        verbose_name_plural = _("Equipment Types")

    def __str__(self) -> str:
        return f"{self.iso_code} — {self.description}"

    @property
    def image_url(self) -> str | None:
        return self.image.url if self.image else None


class Container(BaseTeamModel):
    """A physical shipping container identified by an ISO 6346 container ID."""

    owner_code = models.CharField(_("owner code"), max_length=3)
    category_id = models.CharField(
        _("category identifier"),
        max_length=1,
        choices=ContainerCategory.choices,
        default=ContainerCategory.U,
    )
    serial_number = models.CharField(_("serial number"), max_length=6)
    check_digit = models.PositiveSmallIntegerField(_("check digit"))

    equipment_type = models.ForeignKey(
        EquipmentType,
        on_delete=models.PROTECT,
        related_name="containers",
        verbose_name=_("equipment type"),
    )

    status = models.CharField(
        _("status"),
        max_length=20,
        choices=ContainerStatus.choices,
        default=ContainerStatus.AVAILABLE,
    )
    condition = models.CharField(
        _("condition"),
        max_length=10,
        choices=ContainerCondition.choices,
        default=ContainerCondition.GOOD,
    )

    color_code = models.CharField(_("color code"), max_length=50, blank=True)
    color_system = models.CharField(
        _("color system"),
        max_length=10,
        choices=ColorSystem.choices,
        default=ColorSystem.UNKNOWN,
    )

    manufacture_date = models.DateField(_("manufacture date"), null=True, blank=True)
    manufacturer = models.CharField(_("manufacturer"), max_length=100, blank=True)
    manufacturer_id = models.CharField(_("manufacturer ID"), max_length=100, blank=True)
    current_location = models.ForeignKey(
        ContainerLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="containers",
        verbose_name=_("current location"),
    )
    last_location_update = models.DateTimeField(_("last location update"), null=True, blank=True)
    location_source = models.CharField(
        _("location source"),
        max_length=30,
        choices=LocationSource.choices,
        blank=True,
    )
    location_text = models.CharField(_("location (text)"), max_length=200, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="containers_created",
        verbose_name=_("created by"),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="containers_updated",
        verbose_name=_("updated by"),
    )

    class Meta:
        indexes = [
            models.Index(fields=["team", "owner_code", "category_id", "serial_number"]),
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "condition"]),
            models.Index(fields=["team", "equipment_type"]),
            models.Index(fields=["team", "current_location"]),
            models.Index(fields=["team", "last_location_update"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "owner_code", "category_id", "serial_number"],
                name="unique_container_per_team",
            )
        ]
        ordering = ["-created_at"]
        verbose_name = _("Container")
        verbose_name_plural = _("Containers")

    def __str__(self) -> str:
        return self.container_id

    @property
    def container_id(self) -> str:
        return f"{self.owner_code}{self.category_id}{self.serial_number}{self.check_digit}"

    def clean(self) -> None:
        super().clean()
        validate_container_id(
            self.owner_code,
            self.category_id,
            self.serial_number,
            self.check_digit,
        )

    def save(self, *args, **kwargs):
        self.owner_code = self.owner_code.upper()
        self.category_id = self.category_id.upper()
        self.full_clean()
        return super().save(*args, **kwargs)


class PlannedContainer(BaseTeamModel):
    """A container number that is planned/expected but may not yet exist at the carrier.

    Used in the container discovery workflow: planned numbers are polled against
    carrier APIs until they are detected, then transitioned to tracking.
    """

    container_number = models.CharField(
        _("container number"),
        max_length=11,
        help_text=_("Full ISO 6346 container number, e.g. MCUU1234561"),
    )
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=PlannedContainerStatus.choices,
        default=PlannedContainerStatus.PLANNED,
    )
    carrier = models.CharField(_("carrier"), max_length=100, blank=True)
    shipment = models.ForeignKey(
        "scm_shipments.Shipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_containers",
        verbose_name=_("shipment"),
    )
    # Linked actual Container once detected and validated
    container = models.ForeignKey(
        Container,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_entries",
        verbose_name=_("container"),
    )
    detected_at = models.DateTimeField(_("detected at"), null=True, blank=True)
    last_checked_at = models.DateTimeField(_("last checked at"), null=True, blank=True)
    next_check_at = models.DateTimeField(_("next check at"), null=True, blank=True)
    attempts = models.PositiveIntegerField(_("discovery attempts"), default=0)
    max_attempts = models.PositiveIntegerField(
        _("max attempts"),
        null=True,
        blank=True,
        help_text=_("Give up after this many attempts. Falls back to the team/global default."),
    )
    expires_at = models.DateTimeField(
        _("expires at"),
        null=True,
        blank=True,
        help_text=_("Stop looking for this container number after this time."),
    )
    last_result = models.CharField(
        _("last result"),
        max_length=20,
        choices=PlannedContainerResult.choices,
        default=PlannedContainerResult.PENDING,
    )
    last_error_message = models.TextField(_("last error message"), blank=True)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["team", "container_number"]),
            models.Index(fields=["last_checked_at"]),
            models.Index(fields=["status", "next_check_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["team", "container_number"],
                name="unique_planned_container_per_team",
            )
        ]
        verbose_name = _("Planned Container")
        verbose_name_plural = _("Planned Containers")

    def __str__(self) -> str:
        return f"{self.container_number} ({self.get_status_display()})"


class ContainerMovement(BaseTeamModel):
    """One accepted physical movement of a container. The audit trail of position.

    Three layers describe where a box is, and a value in one never silently becomes
    a value in another:

    :class:`~apps.scm.tracking.models.TrackingEvent`
        External evidence. A carrier saying "DISCHARGED — GOTHENBURG". Stored
        verbatim, never authoritative on its own.
    ``ContainerMovement``
        An accepted movement. Somebody — an operator, a depot, or the conservative
        interpretation layer in ``apps.scm.tracking.physical_movements`` — decided
        this really happened to the box.
    ``Container.current_location``
        A *projection* of the movement history, not an independent field. See
        :func:`apps.scm.containers.movements.project_container_state`.

    The invariant the projection maintains:

    .. code-block:: text

        Container.current_location
            = the location implied by the winning state-affecting movement

    ``affects_current_state`` is what makes a movement a claim about position rather
    than a note in the history. A movement recorded purely for the record — evidence
    somebody wants kept but does not want acted on — sets it False and is skipped by
    the projection entirely. It does not mean "this movement won"; whether it won is
    derived by comparing it against the rest of the history, and can change when a
    later, or a stronger, movement arrives.

    ``gate_name`` is the gate a box passed through, e.g. "John Evans". A gate is a
    point a container passes, not a place it is at, so it is a string on the
    movement rather than a :class:`ContainerLocation` — inventing a canonical
    location per gate would put a container "at" somewhere it can never rest.

    Nothing writes rows here directly. Every writer goes through
    :func:`apps.scm.containers.movements.record_container_movement`, which is the
    only place validation, precedence and the projection live.
    """

    container = models.ForeignKey(
        Container,
        on_delete=models.CASCADE,
        related_name="movements",
        verbose_name=_("container"),
    )
    from_location = models.ForeignKey(
        ContainerLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="departures",
        verbose_name=_("from location"),
    )
    to_location = models.ForeignKey(
        ContainerLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="arrivals",
        verbose_name=_("to location"),
    )
    movement_type = models.CharField(
        _("movement type"),
        max_length=30,
        choices=MovementType.choices,
        default=MovementType.UNKNOWN,
    )
    occurred_at = models.DateTimeField(_("occurred at"))
    source = models.CharField(
        _("source"),
        max_length=30,
        choices=LocationSource.choices,
        default=LocationSource.MANUAL,
    )
    related_shipment = models.ForeignKey(
        "scm_shipments.Shipment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="container_movements",
        verbose_name=_("related shipment"),
    )
    related_supplier_delivery = models.ForeignKey(
        "scm_supplier_deliveries.SupplierDelivery",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="container_movements",
        verbose_name=_("related supplier delivery"),
    )
    related_tracking_event = models.ForeignKey(
        "scm_tracking.TrackingEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="container_movements",
        verbose_name=_("related tracking event"),
        help_text=_("The carrier event this movement was interpreted from, when it came from one."),
    )
    gate_name = models.CharField(
        _("gate"),
        max_length=100,
        blank=True,
        help_text=_("The gate the container passed through, e.g. John Evans. Not a location."),
    )
    affects_current_state = models.BooleanField(
        _("affects current state"),
        default=True,
        help_text=_("Whether this movement is a claim about where the container is, or history only."),
    )
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        ordering = ["-occurred_at", "-created_at"]
        indexes = [
            models.Index(fields=["team", "container"]),
            models.Index(fields=["team", "occurred_at"]),
            # The projection's own query: this container's state-affecting history,
            # newest physical event first.
            models.Index(fields=["team", "container", "-occurred_at"]),
            # "Has anything arrived at this location", for Expected Arrivals.
            models.Index(fields=["team", "to_location", "movement_type"]),
        ]
        constraints = [
            # One tracking event yields at most one movement. This is what makes
            # automatic interpretation idempotent: re-ingesting a carrier event
            # cannot add a second movement for it, whatever the timestamps say.
            models.UniqueConstraint(
                fields=["related_tracking_event"],
                condition=models.Q(related_tracking_event__isnull=False),
                name="unique_movement_per_tracking_event",
            ),
        ]
        verbose_name = _("Container Movement")
        verbose_name_plural = _("Container Movements")

    def __str__(self) -> str:
        return f"{self.container} → {self.to_location} ({self.occurred_at:%Y-%m-%d})"

    @property
    def is_tracking_derived(self) -> bool:
        """True when a carrier event, not a person, is behind this movement."""
        return self.source == LocationSource.TRACKING_EVENT or self.related_tracking_event_id is not None

    @property
    def is_observed(self) -> bool:
        """True when somebody physically handled or saw the box.

        The distinction the Activity tab draws: an operator's gate move and a
        carrier's report are different kinds of claim, and a reader has to be able
        to tell which they are looking at.
        """
        return self.source in OBSERVED_LOCATION_SOURCES

    @property
    def resolved_location_id(self) -> int | None:
        """Where this movement leaves the container, as an id.

        Always ``to_location``. Every movement type expresses its destination the
        same way, including ``GATE_OUT``: a departure to nowhere recorded is
        ``to_location=None``, which is the honest answer, and a departure to a known
        place names it. There is no second field saying where the box ended up.
        """
        return self.to_location_id
