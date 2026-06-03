"""SCM Audit Log service — write helpers.

All write operations go through log_scm_action() to ensure consistency.
Never pass raw credentials, tokens, or auth data in the metadata argument.
"""

from __future__ import annotations

import logging
from typing import Any

from apps.teams.models import Team

logger = logging.getLogger(__name__)


def log_scm_action(
    team: Team,
    action: str,
    object_type: str = "",
    object_id: str = "",
    object_repr: str = "",
    metadata: dict[str, Any] | None = None,
    actor=None,
) -> None:
    """Create an SCMAuditLog entry.

    Silently swallows any exception so that an audit log failure never
    blocks the primary operation.

    Args:
        team:        The team this event belongs to.
        action:      One of SCMAuditLog.Action choices.
        object_type: Human-readable model/object class name.
        object_id:   String primary key of the affected object.
        object_repr: Human-readable representation of the object.
        metadata:    Additional context dict — must not contain secrets.
        actor:       CustomUser instance; None for system events.
    """
    try:
        from .models import SCMAuditLog

        SCMAuditLog.objects.create(
            team=team,
            actor=actor,
            action=action,
            object_type=object_type,
            object_id=str(object_id) if object_id else "",
            object_repr=object_repr[:255] if object_repr else "",
            metadata=metadata or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "audit_log: failed to create audit entry action=%s object_type=%s object_id=%s",
            action,
            object_type,
            object_id,
        )
