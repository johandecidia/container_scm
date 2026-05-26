# Rate views — request handling, response rendering, form handling only.
from django.http import HttpResponse


def rate_list(request, *args, **kwargs):
    return HttpResponse(status=501)


def rate_create(request, *args, **kwargs):
    return HttpResponse(status=501)


def rate_detail(request, *args, **kwargs):
    return HttpResponse(status=501)


def rate_update(request, *args, **kwargs):
    return HttpResponse(status=501)


def rate_delete(request, *args, **kwargs):
    return HttpResponse(status=501)
