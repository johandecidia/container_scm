# Integration views — request handling, response rendering, form handling only.
from django.http import HttpResponse


def integration_list(request, *args, **kwargs):
    return HttpResponse(status=501)


def integration_create(request, *args, **kwargs):
    return HttpResponse(status=501)


def integration_detail(request, *args, **kwargs):
    return HttpResponse(status=501)


def integration_update(request, *args, **kwargs):
    return HttpResponse(status=501)


def integration_delete(request, *args, **kwargs):
    return HttpResponse(status=501)
