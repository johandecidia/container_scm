"""Safe diagnostics for failed Business Central requests, for Team Administrators.

Business Central explains a 403 in the error body's ``message``, for example::

    Sorry, the current permissions prevented the action.
    (TableData 5496 Purchase Order Entity Buffer Read: Base Application)

or, on older versions, ``You do not have the following permissions on TableData
Purchase Header: Read``. The message itself is never passed on: only the object
type, id, name, permission, owning app and correlation id are extracted, each
against an allowlist, so tokens, secrets or record data in the body cannot leak.
"""

from __future__ import annotations

import re

from .exceptions import BusinessCentralError, BusinessCentralPermissionError, BusinessCentralResponseError

# Longest message we inspect; Business Central permission messages are short.
_MAX_MESSAGE_LENGTH = 4000
_OBJECT_TYPES = "TableData|Table|Page|Codeunit|Report|Query|XMLport|System"
_PERMISSIONS = "Read|Insert|Modify|Delete|Execute"
# Object and app names: letters (incl. å/ä/ö), digits, spaces and common punctuation only.
_NAME = r"[\w .,&'/\-]{1,120}?"
_SAFE_NAME = re.compile(r"^[\w .,&'/\-]{1,120}$")

_PERMISSION_PATTERNS = (
    # Current format: "(TableData 5496 Purchase Order Entity Buffer Read: Base Application)".
    re.compile(
        rf"\((?P<object_type>{_OBJECT_TYPES}) (?P<object_id>\d{{1,10}}) (?P<object_name>{_NAME}) "
        rf"(?P<permission>{_PERMISSIONS})(?:: (?P<app>[^)]{{1,120}}))?\)"
    ),
    # Older format: "You do not have the following permissions on TableData Purchase Header: Read".
    re.compile(
        rf"permissions on (?P<object_type>{_OBJECT_TYPES}) (?P<object_name>{_NAME}): (?P<permission>{_PERMISSIONS})"
    ),
    # Raised by API pages themselves, e.g. "You do not have permissions to read Purchase Orders."
    re.compile(
        r"You do not have permissions to (?P<permission>read|insert|modify|delete) (?P<object_name>[\w .\-]{1,80}?)\."
    ),
)
_CORRELATION_ID = re.compile(
    r"CorrelationId:\s*(?P<correlation_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def parse_permission_error(message: str) -> dict[str, str]:
    """Extract the denied object and permission from a Business Central error message.

    Returns only the keys found (object_type, object_id, object_name, permission,
    app, correlation_id); an empty dict when the message names nothing.
    """
    if not isinstance(message, str) or not message:
        return {}
    text = " ".join(message[:_MAX_MESSAGE_LENGTH].split())

    detail: dict[str, str] = {}
    for pattern in _PERMISSION_PATTERNS:
        match = pattern.search(text)
        if match:
            detail = {key: value.strip() for key, value in match.groupdict().items() if value}
            break
    if "app" in detail and not _SAFE_NAME.match(detail["app"]):
        del detail["app"]
    if "permission" in detail:
        detail["permission"] = detail["permission"].capitalize()

    correlation = _CORRELATION_ID.search(text)
    if correlation:
        detail["correlation_id"] = correlation.group("correlation_id").lower()
    return detail


def failure_diagnostic(exc: BusinessCentralError) -> dict:
    """Admin-facing detail for a failed request: which call failed and, for a 403, what was denied."""
    if not isinstance(exc, BusinessCentralResponseError) or not exc.endpoint:
        return {}
    diagnostic = {
        "method": "GET",
        "endpoint": exc.endpoint,
        "status_code": exc.status_code,
        "error_code": exc.error_code,
    }
    if isinstance(exc, BusinessCentralPermissionError):
        diagnostic.update(exc.permission)
    return diagnostic


def format_diagnostic(diagnostic: dict) -> str:
    """One readable line for the UI, the request log and the management command."""
    if not diagnostic:
        return ""
    parts = [f"Failed request: {diagnostic['method']} {diagnostic['endpoint']} (HTTP {diagnostic['status_code']})."]
    if diagnostic.get("object_name"):
        target = " ".join(str(diagnostic[key]) for key in ("object_type", "object_id") if diagnostic.get(key))
        missing = f'{target} "{diagnostic["object_name"]}"'.strip()
        app = f" ({diagnostic['app']})" if diagnostic.get("app") else ""
        parts.append(f"Missing permission: {missing}: {diagnostic['permission']}{app}.")
    elif diagnostic.get("status_code") == 403:
        parts.append("Business Central did not name the denied object.")
    if diagnostic.get("correlation_id"):
        parts.append(f"CorrelationId: {diagnostic['correlation_id']}.")
    return " ".join(parts)
