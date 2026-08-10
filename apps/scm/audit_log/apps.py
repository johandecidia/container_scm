from django.apps import AppConfig


class AuditLogConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scm.audit_log"
    label = "scm_audit_log"
    verbose_name = "SCM Audit Log"
