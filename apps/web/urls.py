from django.conf import settings
from django.urls import path
from django.views import defaults as default_views
from django.views.generic import TemplateView
from redis.asyncio import Redis

from . import views

app_name = "web"
urlpatterns = [
    path("", views.home, name="home"),
    path("terms/", TemplateView.as_view(template_name="web/terms.html"), name="terms"),
    path("robots.txt", TemplateView.as_view(template_name="robots.txt", content_type="text/plain"), name="robots.txt"),
    # these views are just for testing error pages
    # actual error handling is handled by Django: https://docs.djangoproject.com/en/stable/ref/views/#error-views
    path("400/", default_views.bad_request, kwargs={"exception": Exception("Bad Request!")}, name="400"),
    path("403/", default_views.permission_denied, kwargs={"exception": Exception("Permission Denied")}, name="403"),
    path("404/", default_views.page_not_found, kwargs={"exception": Exception("Page not Found")}, name="404"),
    path("429/", TemplateView.as_view(template_name="429.html"), name="429"),
    path("500/", default_views.server_error, name="500"),
    path("simulate_error/", views.simulate_error),
    path(
        "health/",
        views.HealthCheck.as_view(
            checks=[
                "health_check.Database",
                "health_check.contrib.celery.Ping",
                (
                    "health_check.contrib.redis.Redis",
                    {"client": Redis.from_url(settings.REDIS_URL)},
                ),
            ]
        ),
        name="health_check",
    ),
    path(
        "ui/flowbite/",
        TemplateView.as_view(
            template_name="web/ui_demos/flowbite/flowbite_demo.html", extra_context={"active_tab": "flowbite-demo"}
        ),
        name="flowbite",
    ),
]


team_urlpatterns = (
    [
        path("", views.team_home, name="home"),
    ],
    "web_team",
)
