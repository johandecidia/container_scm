"""Supply chain visibility — a read-only composition layer over the SCM domain.

This app owns no data. Containers, shipments, tracking subscriptions, tracking
events and ETA history are all sources of truth elsewhere; visibility reads them,
composes one answer to "where is everything, and how well do we know it", and
renders that as a page and as GeoJSON for Mapbox.

Because it stores nothing it deliberately has no ``models.py``, ``forms.py`` or
``services.py``: adding empty ones would suggest this layer is somewhere data can
originate, which is exactly what it must not become.
"""
