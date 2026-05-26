# Analytics views — request handling, response rendering, form handling only.
from django.http import HttpResponse


def analytics_dashboard(request, *args, **kwargs):
    return HttpResponse(status=501)
