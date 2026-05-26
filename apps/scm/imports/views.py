# Import views — request handling, response rendering, form handling only.
from django.http import HttpResponse


def import_list(request, *args, **kwargs):
    return HttpResponse(status=501)


def import_create(request, *args, **kwargs):
    return HttpResponse(status=501)


def import_detail(request, *args, **kwargs):
    return HttpResponse(status=501)
