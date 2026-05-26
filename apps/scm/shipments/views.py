# Shipment views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.http import HttpResponse


def shipment_list(request, *args, **kwargs):
    return HttpResponse(status=501)


def shipment_create(request, *args, **kwargs):
    return HttpResponse(status=501)


def shipment_detail(request, *args, **kwargs):
    return HttpResponse(status=501)


def shipment_update(request, *args, **kwargs):
    return HttpResponse(status=501)


def shipment_delete(request, *args, **kwargs):
    return HttpResponse(status=501)
