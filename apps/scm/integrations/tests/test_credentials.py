"""Tests for the integration credential service (Fernet encryption at rest)."""

import base64
import json

from django.test import TestCase

from apps.scm.integrations.credentials import (
    _decode,
    _encode,
    get_integration_credentials,
    mask_secret,
    set_integration_credentials,
)
from apps.scm.integrations.models import Integration, IntegrationCredential
from apps.teams.models import Team


def _integration(slug="cred-team"):
    team, _ = Team.objects.get_or_create(name=slug, slug=slug)
    return Integration.objects.create(
        team=team,
        name="BC",
        provider_code="business_central",
        provider_family=Integration.ProviderFamily.BUSINESS_SYSTEM,
    )


class CredentialEncryptionTest(TestCase):
    def test_encode_decode_round_trip(self):
        data = {"client_id": "abc", "client_secret": "s3cr3t-value"}
        self.assertEqual(_decode(_encode(data)), data)

    def test_encoded_value_is_not_plaintext(self):
        secret = "very-secret-client-secret"
        encoded = _encode({"client_secret": secret})
        self.assertNotIn(secret, encoded)

    def test_encryption_is_nondeterministic(self):
        # Fernet embeds a random IV, so encrypting the same data twice differs —
        # the old base64-JSON placeholder would have produced identical output.
        data = {"client_secret": "same-value"}
        self.assertNotEqual(_encode(data), _encode(data))

    def test_legacy_base64_payload_still_readable(self):
        # Simulate a row written by the old placeholder implementation.
        legacy = base64.b64encode(json.dumps({"client_id": "old"}).encode()).decode()
        self.assertEqual(_decode(legacy), {"client_id": "old"})

    def test_decode_empty_returns_empty_dict(self):
        self.assertEqual(_decode(""), {})

    def test_set_and_get_credentials(self):
        integration = _integration()
        set_integration_credentials(
            integration,
            IntegrationCredential.AuthType.OAUTH2,
            {"client_id": "cid", "client_secret": "csecret"},
        )
        self.assertEqual(
            get_integration_credentials(integration),
            {"client_id": "cid", "client_secret": "csecret"},
        )
        # Stored blob must not contain the raw secret.
        stored = IntegrationCredential.objects.get(integration=integration).encrypted_data
        self.assertNotIn("csecret", stored)

    def test_get_credentials_missing_returns_empty(self):
        integration = _integration("cred-team-empty")
        self.assertEqual(get_integration_credentials(integration), {})

    def test_update_credentials_reencrypts(self):
        integration = _integration("cred-team-update")
        set_integration_credentials(integration, IntegrationCredential.AuthType.OAUTH2, {"client_secret": "one"})
        set_integration_credentials(integration, IntegrationCredential.AuthType.OAUTH2, {"client_secret": "two"})
        self.assertEqual(get_integration_credentials(integration), {"client_secret": "two"})

    def test_mask_secret(self):
        self.assertEqual(mask_secret("short"), "***")
        masked = mask_secret("abcd1234567890wxyz")
        self.assertTrue(masked.startswith("abcd"))
        self.assertTrue(masked.endswith("wxyz"))
        self.assertNotIn("1234567890", masked)
