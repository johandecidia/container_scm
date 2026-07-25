"""Tests for the integration credential service (Fernet encryption at rest)."""

import base64
import json

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from apps.scm.integrations.checks import check_credential_encryption_key
from apps.scm.integrations.credentials import (
    CredentialDecryptionError,
    _decode,
    _encode,
    _get_fernet,
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

    def test_new_format_has_version_prefix(self):
        self.assertTrue(_encode({"a": "b"}).startswith("fernet:v1:"))

    def test_legacy_base64_payload_still_readable(self):
        # Simulate a row written by the old placeholder implementation (unprefixed).
        legacy = base64.b64encode(json.dumps({"client_id": "old"}).encode()).decode()
        self.assertEqual(_decode(legacy), {"client_id": "old"})

    def test_legacy_prefixed_payload_readable(self):
        legacy = "legacy:base64:" + base64.b64encode(json.dumps({"client_id": "old"}).encode()).decode()
        self.assertEqual(_decode(legacy), {"client_id": "old"})

    def test_unprefixed_raw_fernet_token_readable(self):
        # A raw Fernet token from the first release (no version prefix).
        raw = _get_fernet().encrypt(json.dumps({"client_id": "m1"}).encode()).decode()
        self.assertEqual(_decode(raw), {"client_id": "m1"})

    def test_versioned_corrupt_ciphertext_raises_not_legacy(self):
        # A fernet:v1 value that fails to decrypt must NOT be reinterpreted as legacy.
        with self.assertRaises(CredentialDecryptionError):
            _decode("fernet:v1:not-a-valid-token")

    def test_corrupt_error_contains_no_secret(self):
        try:
            _decode("fernet:v1:not-a-valid-token")
        except CredentialDecryptionError as exc:
            self.assertNotIn("not-a-valid-token", str(exc))

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


class ProductionEncryptionKeyTest(SimpleTestCase):
    def test_check_passes_when_not_required(self):
        self.assertEqual(check_credential_encryption_key(None), [])

    @override_settings(SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY=True, SCM_INTEGRATION_ENCRYPTION_KEY="")
    def test_check_errors_when_required_and_missing(self):
        errors = check_credential_encryption_key(None)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, "scm_integrations.E001")
        # The error must not leak any secret material.
        self.assertNotIn("SECRET_KEY", errors[0].msg.upper().replace("SECRET_KEY-DERIVED", ""))

    @override_settings(
        SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY=True,
        SCM_INTEGRATION_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )
    def test_check_passes_when_required_and_present(self):
        self.assertEqual(check_credential_encryption_key(None), [])

    @override_settings(SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY=True, SCM_INTEGRATION_ENCRYPTION_KEY="")
    def test_encrypt_refuses_fallback_in_production(self):
        with self.assertRaises(ImproperlyConfigured):
            _encode({"client_secret": "x"})

    @override_settings(
        SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY=True,
        SCM_INTEGRATION_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )
    def test_encrypt_uses_configured_key_in_production(self):
        data = {"client_secret": "prod-secret"}
        self.assertEqual(_decode(_encode(data)), data)
