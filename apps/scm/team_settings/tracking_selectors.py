"""What Settings → Tracking reads: the team's direct carrier integrations, and nothing secret.

The rows are the carrier *registry* joined to the team's ``Integration`` records, not
a query over the integrations table. That direction matters: a carrier a team has
never connected still has to appear — with a Connect action — and a registry entry
whose adapter cannot call anything must not appear at all. Both facts live in
:mod:`apps.scm.integrations.carriers.registry`; this module only reads them.

**No secret ever leaves here.** A row says whether credentials are stored and which
fields the carrier's auth style expects. It does not decrypt them, and it does not
carry a partial value: a masked-but-real prefix is still the beginning of somebody's
API key in an HTML response and in whatever proxy logged it. The credential service
is the only reader of the stored value, and it is only ever read to make a call.
"""

from dataclasses import dataclass, field

from apps.scm.integrations.carriers.dcsa.client import credential_fields_for_auth_style
from apps.scm.integrations.carriers.registry import CarrierDefinition, list_connectable_carriers
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team

# What a stored credential is rendered as. A fixed placeholder, not a prefix of the
# real value — see the module docstring.
MASKED_PLACEHOLDER = "••••••••"


@dataclass
class CarrierSettingsRow:
    """One direct carrier integration as Settings → Tracking presents it."""

    definition: CarrierDefinition
    integration: Integration | None = None
    has_credentials: bool = False
    credential_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def provider_code(self) -> str:
        return self.definition.provider_code

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def is_connected(self) -> bool:
        """An integration row exists, whether or not it is currently switched on."""
        return self.integration is not None

    @property
    def is_active(self) -> bool:
        """Whether tracking may actually be routed here.

        Both flags, because they mean different things: ``is_active`` is the team's
        switch and ``status`` is what the last call said. Routing reads ``is_active``
        (see ``carriers.factory.get_carrier_integration``), so that is what this
        answers with — an integration in ERROR is still active and still routable,
        and the error is reported beside it rather than by hiding it.
        """
        return bool(self.integration and self.integration.is_active)

    @property
    def is_usable(self) -> bool:
        """Active *and* holding credentials — the state a container override needs."""
        return self.is_active and self.has_credentials

    @property
    def last_success_at(self):
        return self.integration.last_success_at if self.integration else None

    @property
    def last_tested_at(self):
        return self.integration.last_tested_at if self.integration else None

    @property
    def last_error_at(self):
        return self.integration.last_error_at if self.integration else None

    @property
    def last_error_message(self) -> str:
        return self.integration.last_error_message if self.integration else ""

    @property
    def status(self) -> str:
        return self.integration.status if self.integration else ""

    @property
    def masked_credentials(self) -> str:
        return MASKED_PLACEHOLDER if self.has_credentials else ""

    @property
    def test_connection_reference(self) -> str:
        """The container number the connection test probes with, if one is configured.

        Not a secret — a container number — and the one thing `test_connection` cannot
        run without. Maersk ships one; CMA CGM and Hapag-Lloyd deliberately do not,
        because a reference an account can see belongs to that account.
        """
        config = (self.integration.config if self.integration else None) or {}
        return str(config.get("test_connection_reference") or "")

    @property
    def can_test(self) -> bool:
        return self.is_connected and bool(self.test_connection_reference)


def _auth_style(definition: CarrierDefinition, integration: Integration | None) -> str:
    """The auth style in force: the team's own config if it has one, else the default.

    A team may point its integration at a contracted product with a different flow,
    so the stored config wins — asking the shipped default would then offer the wrong
    credential fields.
    """
    config = (integration.config if integration else None) or definition.default_config
    return str(config.get("auth_style") or "")


def get_carrier_settings_rows(team: Team) -> list[CarrierSettingsRow]:
    """Every connectable direct carrier, with this team's configuration state.

    Three queries regardless of how many carriers are registered.
    """
    definitions = list_connectable_carriers()
    codes = [d.provider_code for d in definitions]

    integrations = {
        integration.provider_code: integration
        for integration in Integration.objects.filter(
            team=team,
            provider_family=Integration.ProviderFamily.CARRIER,
            provider_code__in=codes,
        )
    }
    # Presence only. The encrypted value is never fetched into this layer.
    credentialed = set(
        IntegrationCredential.objects.filter(integration__in=integrations.values())
        .exclude(encrypted_data="")
        .values_list("integration__provider_code", flat=True)
    )

    return [
        CarrierSettingsRow(
            definition=definition,
            integration=integrations.get(definition.provider_code),
            has_credentials=definition.provider_code in credentialed,
            credential_fields=credential_fields_for_auth_style(
                _auth_style(definition, integrations.get(definition.provider_code))
            ),
        )
        for definition in definitions
    ]


def get_carrier_settings_row(team: Team, provider_code: str) -> CarrierSettingsRow | None:
    """One row by provider code, or None when that carrier cannot be connected here."""
    for row in get_carrier_settings_rows(team):
        if row.provider_code == provider_code:
            return row
    return None


def get_usable_carrier_codes(team: Team) -> set[str]:
    """Provider codes of direct carrier integrations this team can be tracked through.

    Active and credentialed. Read by the container override, which must not offer a
    provider that would fail the moment it was chosen.
    """
    return {row.provider_code for row in get_carrier_settings_rows(team) if row.is_usable}
