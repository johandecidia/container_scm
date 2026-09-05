"""How a place's identifiers are canonicalised.

Pure functions over strings and numbers: no models, no queries, no settings. They
exist as their own module because three layers need exactly the same answer and must
not each have their own — :class:`~apps.scm.containers.models.ContainerLocation` and
:class:`~apps.scm.containers.models.LocationAlias` normalise on save,
:mod:`apps.scm.containers.location_resolver` normalises what a provider reported
before comparing it, and the seed command normalises what it is asked to create. A
second implementation of "the same UN/LOCODE" would eventually disagree with the
stored column and quietly stop matching.

Two rules run through all of it.

**Normalisation is canonicalisation, never interpretation.** ``se got`` and ``SEGOT``
are the same code written two ways, and folding the diacritic in ``Göteborg`` yields
the same string Maersk sends as ``GOTEBORG`` — those are transformations of one
identifier. Deciding that ``GOTHENBURG, SE`` means the same place as ``Göteborg`` is
not: it needs the English exonym and the knowledge that a trailing ``, SE`` is a
country. That is a judgment, it belongs in an explicit alias, and nothing here will
make it.

**An identifier that cannot be canonicalised is not stored.** ``normalize_unlocode``
returns ``""`` for anything not shaped like a UN/LOCODE rather than storing the
fragment it was given, so the canonical column never holds something that will not
match itself.
"""

from __future__ import annotations

import math
import re
import unicodedata

# A UN/LOCODE is five characters: an ISO 3166-1 alpha-2 country followed by a
# three-character place code. Separators are noise — "SE GOT", "SE-GOT" and "segot"
# are one code — so they come out before the shape is checked.
_UNLOCODE_NOISE_RE = re.compile(r"[^A-Za-z0-9]")
_UNLOCODE_RE = re.compile(r"^([A-Z]{2})([A-Z0-9]{3})$")

_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")

_WHITESPACE_RE = re.compile(r"\s+")

# Earth's mean radius, for the great-circle distance in `distance_km`.
_EARTH_RADIUS_KM = 6371.0088


def normalize_unlocode(value: str | None) -> str:
    """Return *value* as a canonical UN/LOCODE, or ``""`` if it is not one.

    ``segot``, ``SE GOT``, ``se-got`` and ``SEGOT`` all give ``SEGOT``.

    Anything that does not have the shape of a UN/LOCODE — too short, too long, a
    country part that is not two letters — gives ``""``. The caller keeps whatever it
    was actually sent somewhere else; what it must not do is file it under a code
    that no lookup will ever produce.
    """
    if not value:
        return ""
    candidate = _UNLOCODE_NOISE_RE.sub("", str(value)).upper()
    return candidate if _UNLOCODE_RE.match(candidate) else ""


def normalize_country_code(value: str | None) -> str:
    """Return *value* as a two-letter country code, or ``""``.

    Only the alpha-2 form is canonical. A country *name* — "Sweden", "SE " with a
    stray space is fine, but "Sweden" is not a code — gives ``""`` rather than being
    truncated to ``SW``, which is a real country code for something else entirely.
    """
    if not value:
        return ""
    candidate = str(value).strip().upper()
    return candidate if _COUNTRY_CODE_RE.match(candidate) else ""


def normalize_location_name(value: str | None) -> str:
    """Return a comparable form of a place name.

    Case, surrounding and repeated whitespace, and diacritics are all removed:
    ``Gothenburg``, ``GOTHENBURG``, ``" Gothenburg "`` and ``Göteborg`` /
    ``GOTEBORG`` each reduce to a single string, and the last pair reduce to the
    same one. That is deterministic transliteration, not fuzzy matching — there is
    no edit distance here and no scoring.

    Punctuation is kept. ``gothenburg, se`` therefore stays distinct from
    ``gothenburg``, because the trailing country is something a provider added and
    reading it off is interpretation. That mapping is what an alias is for.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    # Drop the combining marks NFKD just separated out: "ö" is now "o" + U+0308.
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def normalize_external_code(value: str | None) -> str:
    """Return a provider's own code for a place in comparable form.

    Trimmed and upper-cased only. A provider's code is opaque — its internal
    punctuation may well be significant — so nothing else is touched.
    """
    return str(value).strip().upper() if value else ""


def normalize_alias_source(value: str | None) -> str:
    """Return an alias source in comparable form.

    Lower-cased and trimmed to match ``TrackingProvider.code``, which is how the
    tracking pipeline names the provider an alias belongs to.
    """
    return str(value).strip().lower() if value else ""


def distance_km(lat1, lon1, lat2, lon2) -> float | None:
    """Great-circle distance between two points in kilometres, or None.

    None when any coordinate is missing, which is the common case: most locations
    have no coordinates and most carrier events send none. Callers treat that as
    "coordinates cannot answer this", never as distance zero.

    Computed here rather than in the database because the project does not use
    PostGIS, and introducing it to compare a handful of rows would be a large
    dependency for a fallback rule.
    """
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None

    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(float(lon2) - float(lon1))

    haversine = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(haversine))
