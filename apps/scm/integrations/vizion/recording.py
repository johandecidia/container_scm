"""Turning a live Vizion response into a committable fixture.

Phase 1A's fixtures are synthetic, built from Vizion's published schema, and the tests
built on them therefore prove that the mapper reads the *specification* correctly. Only a
recorded response can promote those assertions into evidence about reality — so a live run
should leave fixtures behind, and they have to be safe to commit.

What is removed, and why each one:

``organization_id``
    Identifies this Vizion account. Not a credential, but an account identifier has no
    business in a public repository and nothing in the mapper reads it.

``callback_url``
    A webhook endpoint. Can carry a token in its path or query — Vizion's own docs
    describe storing metadata in the callback URL — so it is dropped rather than parsed.

``organization``
    The nested account object: name, contract cadence, timestamps.

Anything whose key looks like a secret
    Belt and braces. The API key travels in a request *header* and is never in a response
    body, so this should never fire; it exists so that a future Vizion field which does
    carry one cannot be committed by accident.

What is deliberately **kept**: every field the mapper reads, plus ``reference_id`` and the
milestone ``id`` values. Those are opaque per-account identifiers, and they are precisely
what the Phase 1B identity experiment is about — redacting them would destroy the evidence
the recording exists to preserve.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re

logger = logging.getLogger(__name__)

REDACTED = "[redacted]"

# Keys dropped wherever they appear, at any depth.
_DROP_KEYS = frozenset({"organization_id", "organization", "callback_url", "webhooks"})

# Keys whose value is replaced rather than dropped, so the shape stays readable.
_SECRET_KEY_RE = re.compile(r"(api[_-]?key|secret|token|password|authorization|credential)", re.IGNORECASE)


def sanitize(value):
    """Return ``value`` with account identifiers and secret-shaped fields removed.

    Recursive and shape-preserving: a sanitized payload is still a valid Vizion payload
    that the mapper can read, which is what makes it usable as a fixture.
    """
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key in _DROP_KEYS:
                continue
            if _SECRET_KEY_RE.search(str(key)):
                cleaned[key] = REDACTED
                continue
            cleaned[key] = sanitize(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def write_fixture(directory: str | pathlib.Path, name: str, payload) -> pathlib.Path:
    """Sanitize ``payload`` and write it as ``name``.json, returning the path.

    The caller chooses the directory. Nothing is written into the fixtures package
    automatically: overwriting a checked-in fixture as a side effect of a live run would
    change what the test suite asserts without anybody deciding to.
    """
    target = pathlib.Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{name}.json"
    path.write_text(json.dumps(sanitize(payload), indent=2, sort_keys=False))
    logger.info("Vizion response recorded to %s", path)
    return path
