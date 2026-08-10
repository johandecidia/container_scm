"""Re-encrypt all legacy integration credentials to the current versioned format.

Usage:
    python manage.py migrate_integration_credentials --dry-run
    python manage.py migrate_integration_credentials

Never prints or logs any credential value — only counts and integration ids.
"""

from django.core.management.base import BaseCommand

from apps.scm.integrations.credentials import (
    CredentialDecryptionError,
    is_legacy_format,
    reencrypt_legacy_credential,
)
from apps.scm.integrations.models import IntegrationCredential


class Command(BaseCommand):
    help = "Re-encrypt legacy integration credentials to the current fernet:v1 format."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be migrated without writing any changes.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        total = migrated = skipped = failed = 0

        for credential in IntegrationCredential.objects.all().iterator():
            total += 1
            if not is_legacy_format(credential.encrypted_data):
                skipped += 1
                continue
            try:
                if dry_run:
                    # Verify it is decodable without writing.
                    from apps.scm.integrations.credentials import _decode

                    _decode(credential.encrypted_data)
                    migrated += 1
                    self.stdout.write(f"[dry-run] would migrate credential for integration {credential.integration_id}")
                elif reencrypt_legacy_credential(credential):
                    migrated += 1
                    self.stdout.write(f"migrated credential for integration {credential.integration_id}")
                else:
                    skipped += 1
            except CredentialDecryptionError:
                failed += 1
                # Sanitised — no secret material in the message.
                self.stderr.write(
                    self.style.ERROR(
                        f"could not decrypt credential for integration {credential.integration_id} — skipped"
                    )
                )

        summary = f"total={total} migrated={migrated} skipped={skipped} failed={failed}"
        if dry_run:
            self.stdout.write(self.style.WARNING(f"[dry-run] {summary} (no changes written)"))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
