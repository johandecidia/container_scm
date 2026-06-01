from django.urls import include, path

urlpatterns = [
    path("containers/", include("apps.scm.containers.urls")),
    path("shipments/", include("apps.scm.shipments.urls")),
    path("rates/", include("apps.scm.rates.urls")),
    path("imports/", include("apps.scm.imports.urls")),
    path("integrations/", include("apps.scm.integrations.urls")),
    path("analytics/", include("apps.scm.analytics.urls")),
    path("tracking/", include("apps.scm.tracking.urls")),
    path("procurement/", include("apps.scm.procurement.urls")),
    path("supplier-deliveries/", include("apps.scm.supplier_deliveries.urls")),
    path("container-discovery/", include("apps.scm.container_discovery.urls")),
]
