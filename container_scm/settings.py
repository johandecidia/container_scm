"""
Django settings for Container Orchistration Layer project.

For more information on this file, see
https://docs.djangoproject.com/en/stable/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/stable/ref/settings/
"""

import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import environ
from corsheaders.defaults import default_headers
from django.utils.translation import gettext_lazy

# Build paths inside the project like this: BASE_DIR / "subdir".
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
env.read_env(os.path.join(BASE_DIR, ".env"))

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/stable/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env("SECRET_KEY", default="django-insecure-s3SBpLwfwrs4eREGAOmUUuyPxpVs07rvgfhOGJ5o")

# SECURITY WARNING: don"t run with debug turned on in production!
DEBUG = env.bool("DEBUG", default=True)

# Note: It is not recommended to set ALLOWED_HOSTS to "*" in production
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])


# Application definition

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.admindocs",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sitemaps",
    "django.contrib.messages",
    "django.contrib.postgres",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.forms",
]

# Put your third-party apps here
THIRD_PARTY_APPS = [
    "whitenoise.runserver_nostatic",
    "allauth",  # allauth account/registration management
    "allauth.account",
    "allauth.headless",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.twitter_oauth2",
    "allauth.socialaccount.providers.github",
    "channels",
    "django_htmx",
    "django_vite",
    "allauth.mfa",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "drf_spectacular",
    "rest_framework_api_key",
    "celery_progress",
    "hijack",  # "login as" functionality
    "hijack.contrib.admin",  # hijack buttons in the admin
    "djstripe",  # stripe integration
    "waffle",
    "health_check",
    "django_celery_beat",
]

WAGTAIL_APPS = [
    "wagtail.contrib.forms",
    "wagtail.contrib.redirects",
    "wagtail.contrib.simple_translation",
    "wagtail.embeds",
    "wagtail.sites",
    "wagtail.users",
    "wagtail.snippets",
    "wagtail.documents",
    "wagtail.images",
    "wagtail.locales",
    "wagtail.search",
    "wagtail.admin",
    "wagtail",
    "modelcluster",
    "taggit",
]

PEGASUS_APPS = [
    "pegasus.apps.examples.apps.PegasusExamplesConfig",
    "pegasus.apps.employees.apps.PegasusEmployeesConfig",
]

# Put your project-specific apps here
PROJECT_APPS = [
    "apps.content",
    "apps.subscriptions.apps.SubscriptionConfig",
    "apps.users.apps.UserConfig",
    "apps.dashboard.apps.DashboardConfig",
    "apps.api.apps.APIConfig",
    "apps.utils",
    "apps.web",
    "apps.teams.apps.TeamConfig",
    "apps.teams_example.apps.TeamsExampleConfig",
    "apps.chat",
    "apps.ai.apps.AiConfig",
    "apps.group_chat",
    # SCM domain apps
    "apps.scm.containers.apps.ContainersConfig",
    "apps.scm.shipments.apps.ShipmentsConfig",
    "apps.scm.rates.apps.RatesConfig",
    "apps.scm.imports.apps.ImportsConfig",
    "apps.scm.integrations.apps.IntegrationsConfig",
    "apps.scm.analytics.apps.AnalyticsConfig",
    "apps.scm.tracking.apps.TrackingConfig",
    "apps.scm.procurement.apps.ProcurementConfig",
    "apps.scm.supplier_deliveries.apps.SupplierDeliveriesConfig",
    "apps.scm.audit_log.apps.AuditLogConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + PEGASUS_APPS + PROJECT_APPS + WAGTAIL_APPS

if DEBUG:
    # in debug mode, add daphne to the beginning of INSTALLED_APPS to enable async support
    INSTALLED_APPS.insert(0, "daphne")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.teams.middleware.TeamsMiddleware",
    "apps.web.middleware.locale.UserLocaleMiddleware",
    "apps.web.middleware.locale.UserTimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "wagtail.contrib.redirects.middleware.RedirectMiddleware",
    "hijack.middleware.HijackUserMiddleware",
    "waffle.middleware.WaffleMiddleware",
]


# add browser reload only in debug mode
if DEBUG:
    INSTALLED_APPS.append("django_browser_reload")
    MIDDLEWARE.append("django_browser_reload.middleware.BrowserReloadMiddleware")

# add watchfiles only in debug mode
if DEBUG:
    INSTALLED_APPS.append("django_watchfiles")

ROOT_URLCONF = "container_scm.urls"

# used to disable the cache in dev, but turn it on in production.
# more here: https://nickjanetakis.com/blog/django-4-1-html-templates-are-cached-by-default-with-debug-true
_DEFAULT_LOADERS = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]

_CACHED_LOADERS = [("django.template.loaders.cached.Loader", _DEFAULT_LOADERS)]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
        ],
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context_processors.project_meta",
                "apps.teams.context_processors.team",
                "apps.teams.context_processors.user_teams",
                # this line can be removed if not using google analytics
                "apps.web.context_processors.google_analytics_id",
                "apps.chat.context_processors.chat_websocket_url",
            ],
            "loaders": _DEFAULT_LOADERS if DEBUG else _CACHED_LOADERS,
        },
    },
]

WSGI_APPLICATION = "container_scm.wsgi.application"

FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

# Database
# https://docs.djangoproject.com/en/stable/ref/settings/#databases

if "DATABASE_URL" in env:
    DATABASES = {"default": env.db()}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DJANGO_DATABASE_NAME", default="container_scm"),
            "USER": env("DJANGO_DATABASE_USER", default="postgres"),
            "PASSWORD": env("DJANGO_DATABASE_PASSWORD", default="***"),
            "HOST": env("DJANGO_DATABASE_HOST", default="localhost"),
            "PORT": env("DJANGO_DATABASE_PORT", default="5432"),
        }
    }

# Auth and Login

# Django recommends overriding the user model even if you don"t think you need to because it makes
# future changes much easier.
AUTH_USER_MODEL = "users.CustomUser"
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "/"

# Password validation
# https://docs.djangoproject.com/en/stable/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Allauth setup

ACCOUNT_ADAPTER = "apps.teams.adapter.AcceptInvitationAdapter"
HEADLESS_ADAPTER = "apps.users.adapter.CustomHeadlessAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*"]

ACCOUNT_EMAIL_SUBJECT_PREFIX = ""
ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS = False  # don't send "forgot password" emails to unknown accounts
ACCOUNT_CONFIRM_EMAIL_ON_GET = True
ACCOUNT_UNIQUE_EMAIL = True
# This configures a honeypot field to prevent bots from signing up.
# The ID strikes a balance of "realistic" - to catch bots,
# and "not too common" - to not trip auto-complete in browsers.
# You can change the ID or remove it entirely to disable the honeypot.
ACCOUNT_SIGNUP_FORM_HONEYPOT_FIELD = "phone_number_x"
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_LOGOUT_ON_GET = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_USER_DISPLAY = lambda user: user.get_display_name()  # noqa: E731

ACCOUNT_FORMS = {
    "signup": "apps.teams.forms.TeamSignupForm",
}
SOCIALACCOUNT_FORMS = {
    "signup": "apps.users.forms.CustomSocialSignupForm",
}

FRONTEND_ADDRESS = env("FRONTEND_ADDRESS", default="http://localhost:5174")
USE_HEADLESS_URLS = env.bool("USE_HEADLESS_URLS", default=False)
if USE_HEADLESS_URLS:
    # These URLs will use the React front end instead of the Django views
    HEADLESS_FRONTEND_URLS = {
        "account_confirm_email": f"{FRONTEND_ADDRESS}/account/verify-email/{{key}}",
        "account_reset_password_from_key": f"{FRONTEND_ADDRESS}/account/password/reset/key/{{key}}",
        "account_signup": f"{FRONTEND_ADDRESS}/account/signup",
    }

# needed for cross-origin CSRF
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[FRONTEND_ADDRESS],
)
CSRF_COOKIE_DOMAIN = env("CSRF_COOKIE_DOMAIN", default=None)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = (*default_headers, "x-password-reset-key", "x-email-verification-key")
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[FRONTEND_ADDRESS])
SESSION_COOKIE_DOMAIN = env("SESSION_COOKIE_DOMAIN", default=None)

# User signup configuration: change to "mandatory" to require users to confirm email before signing in.
# or "optional" to send confirmation emails but not require them
ACCOUNT_EMAIL_VERIFICATION = env("ACCOUNT_EMAIL_VERIFICATION", default="none")

AUTHENTICATION_BACKENDS = (
    # Needed to login by username in Django admin, regardless of `allauth`
    "django.contrib.auth.backends.ModelBackend",
    # `allauth` specific authentication methods, such as login by e-mail
    "allauth.account.auth_backends.AuthenticationBackend",
)

# enable social login
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APPS": [
            {
                "client_id": env("GOOGLE_CLIENT_ID", default=""),
                "secret": env("GOOGLE_SECRET_ID", default=""),
                "key": "",
            },
        ],
        "SCOPE": [
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
    },
    "twitter_oauth2": {
        "APPS": [
            {
                "client_id": env("TWITTER_CLIENT_ID", default=""),
                "secret": env("TWITTER_SECRET_ID", default=""),
                "key": "",
            },
        ],
    },
    "github": {
        "APPS": [
            {
                "client_id": env("GITHUB_CLIENT_ID", default=""),
                "secret": env("GITHUB_SECRET_ID", default=""),
                "key": "",
            },
        ],
        "SCOPE": [
            "user",
        ],
    },
}

# For turnstile captchas
TURNSTILE_KEY = env("TURNSTILE_KEY", default=None)
TURNSTILE_SECRET = env("TURNSTILE_SECRET", default=None)


# Internationalization
# https://docs.djangoproject.com/en/stable/topics/i18n/

LANGUAGE_CODE = "en-us"
LANGUAGE_COOKIE_NAME = "container_scm_language"
LANGUAGES = WAGTAIL_CONTENT_LANGUAGES = [
    ("en", gettext_lazy("English")),
    ("fr", gettext_lazy("French")),
]
LOCALE_PATHS = (BASE_DIR / "locale",)

TIME_ZONE = "UTC"

USE_I18N = WAGTAIL_I18N_ENABLED = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/stable/howto/static-files/

STATIC_ROOT = BASE_DIR / "static_root"
STATIC_URL = "/static/"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        # swap these to use manifest storage to bust cache when files change
        # note: this may break image references in sass/css files which is why it is not enabled by default
        # "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
        # "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# CompressedManifestStaticFilesStorage requires a pre-built manifest which doesn't exist during tests
if "test" in sys.argv:
    STORAGES["staticfiles"]["BACKEND"] = "django.contrib.staticfiles.storage.StaticFilesStorage"

MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

USE_S3_MEDIA = env.bool("USE_S3_MEDIA", default=False)
if USE_S3_MEDIA:
    # Media file storage in S3
    # Using this will require configuration of the S3 bucket
    # See https://docs.saaspegasus.com/configuration/#storing-media-files
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="container_scm-media")
    AWS_S3_CUSTOM_DOMAIN = f"{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com"
    PUBLIC_MEDIA_LOCATION = "media"
    MEDIA_URL = f"https://{AWS_S3_CUSTOM_DOMAIN}/{PUBLIC_MEDIA_LOCATION}/"
    STORAGES["default"] = {
        "BACKEND": "apps.web.storage_backends.PublicMediaStorage",
    }

# Vite Integration
DJANGO_VITE = {
    "default": {
        "dev_mode": env.bool("DJANGO_VITE_DEV_MODE", default=DEBUG),
        "dev_server_host": env("DJANGO_VITE_HOST", default="localhost"),
        "dev_server_port": env.int("DJANGO_VITE_PORT", default=5173),
        "manifest_path": BASE_DIR / "static" / ".vite" / "manifest.json",
    }
}

# Default primary key field type
# https://docs.djangoproject.com/en/stable/ref/settings/#default-auto-field

# future versions of Django will use BigAutoField as the default, but it can result in unwanted library
# migration files being generated, so we stick with AutoField for now.
# change this to BigAutoField if you"re sure you want to use it and aren"t worried about migrations.
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Removes deprecation warning for future compatibility.
# see https://adamj.eu/tech/2023/12/07/django-fix-urlfield-assume-scheme-warnings/ for details.
FORMS_URLFIELD_ASSUME_HTTPS = True

# Email setup

# default email used by your server
SERVER_EMAIL = env("SERVER_EMAIL", default="noreply@ratecore.io")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="johan@decidia.se")

# The default value will print emails to the console, but you can change that here
# and in your environment.
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")

# Most production backends will require further customization. The below example uses Mailgun.
# ANYMAIL = {
#     "MAILGUN_API_KEY": env("MAILGUN_API_KEY", default=None),
#     "MAILGUN_SENDER_DOMAIN": env("MAILGUN_SENDER_DOMAIN", default=None),
# }

# use in production
# see https://github.com/anymail/django-anymail for more details/examples
# EMAIL_BACKEND = "anymail.backends.mailgun.EmailBackend"

EMAIL_SUBJECT_PREFIX = "[Container Orchistration Layer] "

# Django sites

SITE_ID = 1

# DRF config
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ("apps.api.permissions.IsAuthenticatedOrHasUserAPIKey",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 100,
}


SPECTACULAR_SETTINGS = {
    "TITLE": "Container Orchistration Layer",
    "DESCRIPTION": "Container Supply Chain Layer is an operational platform built for container trading, depot operations, and inbound logistics management.  The system connects procurement, shipment tracking, inventory, landed cost, reservation management, and dispatch planning into one unified operational layer. It provides real-time visibility into what containers are ordered, in transit, available, reserved, and ready for delivery.  Designed to integrate with ERP systems, depot systems, and shipping line tracki",  # noqa: E501
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_SETTINGS": {
        "displayOperationId": True,
    },
    "PREPROCESSING_HOOKS": [
        "apps.api.schema.filter_schema_apis",
    ],
    "APPEND_COMPONENTS": {
        "securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "Authorization"}}
    },
    "SECURITY": [
        {
            "ApiKeyAuth": [],
        }
    ],
}
# Redis, cache, and/or Celery setup
if "REDIS_URL" in env:
    REDIS_URL = env("REDIS_URL")
elif "REDIS_TLS_URL" in env:
    REDIS_URL = env("REDIS_TLS_URL")
else:
    REDIS_HOST = env("REDIS_HOST", default="localhost")
    REDIS_PORT = env("REDIS_PORT", default="6379")
    REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

DUMMY_CACHE = {
    "BACKEND": "django.core.cache.backends.dummy.DummyCache",
}
REDIS_CACHE = {
    "BACKEND": "django.core.cache.backends.redis.RedisCache",
    "LOCATION": REDIS_URL,
}
CACHES = {
    "default": DUMMY_CACHE if DEBUG else REDIS_CACHE,
}

CELERY_BROKER_URL = CELERY_RESULT_BACKEND = REDIS_URL
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Add tasks to this dict and run `python manage.py bootstrap_celery_tasks` to create them
SCHEDULED_TASKS: dict[str, Any] = {
    "test-celerybeat": {
        "task": "pegasus.apps.examples.tasks.example_log_task",
        "schedule": 60,
        "expire_seconds": 60,
    },
    "sync-subscriptions-every-day": {
        "task": "apps.subscriptions.tasks.sync_subscriptions_task",
        "schedule": timedelta(days=1),
        "expire_seconds": 60 * 60,
    },
    # Example of a crontab schedule
    # from celery import schedules
    # "daily-4am-task": {
    #     "task": "some.task.path",
    #     "schedule": schedules.crontab(minute=0, hour=4),
    # },
}

# Channels / Daphne setup

ASGI_APPLICATION = "container_scm.asgi.application"
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
        },
    },
}

# Health Checks
# A list of tokens that can be used to access the health check endpoint
HEALTH_CHECK_TOKENS = env.list("HEALTH_CHECK_TOKENS", default="")

# Wagtail config

WAGTAIL_SITE_NAME = "Container Orchistration Layer Content"
WAGTAILADMIN_BASE_URL = "http://ratecore.io"

# Waffle config

WAFFLE_FLAG_MODEL = "teams.Flag"

# SCM PDF extraction service (used for PDF purchase order imports)
SCM_PDF_FASTAPI_BASE_URL = env.str("SCM_PDF_FASTAPI_BASE_URL", default="")
SCM_PDF_FASTAPI_TIMEOUT_SECONDS = env.int("SCM_PDF_FASTAPI_TIMEOUT_SECONDS", default=30)

# SCM integration credential encryption
# Fernet key (url-safe base64, 32 bytes) used to encrypt IntegrationCredential data
# at rest. If unset, a key is derived from SECRET_KEY so local development works
# without extra configuration. Set an explicit, stable key in production so stored
# credentials survive a SECRET_KEY rotation. Never commit a real key.
SCM_INTEGRATION_ENCRYPTION_KEY = env.str("SCM_INTEGRATION_ENCRYPTION_KEY", default="")

# Pegasus config

# replace any values below with specifics for your project
PROJECT_METADATA = {
    "NAME": gettext_lazy("Container Orchistration Layer"),
    "URL": "http://ratecore.io",
    "DESCRIPTION": gettext_lazy(
        "Container Supply Chain Layer is an operational platform built for container trading, depot operations, and inbound logistics management.  The system connects procurement, shipment tracking, inventory, landed cost, reservation management, and dispatch planning into one unified operational layer. It provides real-time visibility into what containers are ordered, in transit, available, reserved, and ready for delivery.  Designed to integrate with ERP systems, depot systems, and shipping line tracki"  # noqa: E501
    ),
    "IMAGE": "https://upload.wikimedia.org/wikipedia/commons/2/20/PEO-pegasus_black.svg",
    "KEYWORDS": "SaaS, django",
    "CONTACT_EMAIL": "johan@decidia.se",
}

# set this to True in production to have URLs generated with https instead of http
USE_HTTPS_IN_ABSOLUTE_URLS = env.bool("USE_HTTPS_IN_ABSOLUTE_URLS", default=False)

ADMINS = [("Johan", "johan@decidia.se")]

# Add your google analytics ID to the environment to connect to Google Analytics
GOOGLE_ANALYTICS_ID = env("GOOGLE_ANALYTICS_ID", default="")

# these daisyui themes are used to set the dark and light themes for the site
# they must be valid themes included in your tailwind.config.js file.
# more here: https://daisyui.com/docs/themes/
LIGHT_THEME = "light"
DARK_THEME = "dark"

# Stripe config
# modeled to be the same as https://github.com/dj-stripe/dj-stripe
# Note: don"t edit these values here - edit them in your .env file or environment variables!
# The defaults are provided to prevent crashes if your keys don"t match the expected format.
STRIPE_LIVE_PUBLIC_KEY = env("STRIPE_LIVE_PUBLIC_KEY", default="pk_live_***")
STRIPE_LIVE_SECRET_KEY = env("STRIPE_LIVE_SECRET_KEY", default="sk_live_***")
STRIPE_TEST_PUBLIC_KEY = env("STRIPE_TEST_PUBLIC_KEY", default="pk_test_***")
STRIPE_TEST_SECRET_KEY = env("STRIPE_TEST_SECRET_KEY", default="sk_test_***")
# Change to True in production
STRIPE_LIVE_MODE = env.bool("STRIPE_LIVE_MODE", False)
STRIPE_PRICING_TABLE_ID = env("STRIPE_PRICING_TABLE_ID", default="")

# djstripe settings

DJSTRIPE_FOREIGN_KEY_TO_FIELD = "id"  # change to "djstripe_id" if not a new installation
DJSTRIPE_SUBSCRIBER_MODEL = "teams.Team"
DJSTRIPE_SUBSCRIBER_MODEL_REQUEST_CALLBACK = lambda request: request.team  # noqa E731

SILENCED_SYSTEM_CHECKS = [
    "djstripe.I002",  # Pegasus uses the same settings as dj-stripe for keys, so don't complain they are here
]

if "test" in sys.argv:
    # Silence unnecessary warnings in tests
    SILENCED_SYSTEM_CHECKS.append("djstripe.I002")


# AI Chat / Agent Setup
# See: https://ai.pydantic.dev/models/overview/ for model options
# Format: "provider:model_name" e.g. "openai:gpt-4o", "anthropic:claude-sonnet-4-5-20250929"
# API keys are read from environment variables: OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, etc.
# For Ollama: use "openai:model_name" with OPENAI_BASE_URL=http://localhost:11434/v1
DEFAULT_AI_MODEL = env("DEFAULT_AI_MODEL", default="openai:gpt-5-mini")


# Sentry setup

# populate this to configure sentry. should take the form: "https://****@sentry.io/12345"
SENTRY_DSN = env("SENTRY_DSN", default="")


if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        environment=env("SENTRY_ENVIRONMENT", default="production"),
        send_default_pii=False,
    )

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": '[{asctime}] {levelname} "{name}" {message}',
            "style": "{",
            "datefmt": "%d/%b/%Y %H:%M:%S",  # match Django server time format
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL", default="INFO"),
        },
        "apps": {
            "handlers": ["console"],
            "level": env("CONTAINER_SCM_LOG_LEVEL", default="INFO"),
        },
        "pegasus": {
            "handlers": ["console"],
            "level": env("PEGASUS_LOG_LEVEL", default="DEBUG"),
        },
    },
}
