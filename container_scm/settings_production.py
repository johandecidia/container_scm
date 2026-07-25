# flake8: noqa: F405
from .settings import *  # noqa F401

# Note: it is recommended to use the "DEBUG" environment variable to override this value in your main settings.py file.
# A future release may remove it from here.
DEBUG = False

# Require SECRET_KEY from environment — no insecure default allowed in production.
SECRET_KEY = env("SECRET_KEY")

# fix ssl mixed content issues
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Django security checklist settings.
# More details here: https://docs.djangoproject.com/en/stable/howto/deployment/checklist/
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^health/"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Clickjacking protection — deny framing of the app entirely.
X_FRAME_OPTIONS = "DENY"

# HTTP Strict Transport Security — start conservative (60 s), increase after confirming SSL works.
# https://docs.djangoproject.com/en/stable/ref/middleware/#http-strict-transport-security
SECURE_HSTS_SECONDS = 60
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

USE_HTTPS_IN_ABSOLUTE_URLS = True

# If you don't want to use environment variables to set production hosts you can add them here
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
# Railway healthchecks use this hostname — always allow it.
ALLOWED_HOSTS.append("healthcheck.railway.app")

CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[],
)

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=CSRF_TRUSTED_ORIGINS,
)

DJANGO_VITE["default"]["dev_mode"] = False

# Your email config goes here.
# see https://github.com/anymail/django-anymail for more details / examples
# To use mailgun, uncomment the lines below and make sure your key and domain
# are available in the environment.
# EMAIL_BACKEND = "anymail.backends.mailgun.EmailBackend"

# ANYMAIL = {
#     "MAILGUN_API_KEY": env("MAILGUN_API_KEY", default=None),
#     "MAILGUN_SENDER_DOMAIN": env("MAILGUN_SENDER_DOMAIN", default=None),
# }

ADMINS = [("Johan", "johan@decidia.se")]

# In production a dedicated integration credential encryption key is mandatory —
# the SECRET_KEY-derived fallback is only acceptable for development/tests. This
# flag is checked by apps.scm.integrations.checks and the credential service.
SCM_INTEGRATION_REQUIRE_ENCRYPTION_KEY = True

# ---------------------------------------------------------------------------
# Production environment validation — fail fast on startup if required vars
# are missing so the app never starts in a broken/insecure state.
# ---------------------------------------------------------------------------
import sys as _sys  # noqa: E402

_issues: list[str] = []

if not env.list("ALLOWED_HOSTS", default=[]):
    _issues.append("ALLOWED_HOSTS must be set to one or more hostnames")

if not DATABASES.get("default", {}).get("NAME"):
    _issues.append("DATABASE_URL (or individual DB vars) must be configured")

# REDIS_URL is resolved into the REDIS_URL module-level variable in settings.py
if not globals().get("REDIS_URL"):
    _issues.append("REDIS_URL (or REDIS_TLS_URL / REDIS_HOST) must be configured")

if _issues:
    for _issue in _issues:
        print(f"[PRODUCTION STARTUP ERROR] {_issue}", file=_sys.stderr)
    _sys.exit(1)
