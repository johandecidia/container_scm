"""Rendering a two-run comparison for a person to read.

Separate from :mod:`.report`, which renders one run. The two answer different questions
— "what does each provider say" versus "what changed" — and a reader of the second needs
the absences spelled out: a provider that reported nothing new is the finding, and a
blank line reads as though the section had not been filled in.
"""

from __future__ import annotations

from .drift import (
    ETA_TARGET_CHANGED,
    IDENTITY_STABLE,
    IDENTITY_UNKNOWN,
)

_ABSENT = "—"
_WIDTH = 78


def _hours(value) -> str:
    return _ABSENT if value is None else f"{value:+.2f} h"


def render_drift_text(diff: dict) -> str:
    """Return the human-readable comparison of two benchmark snapshots."""
    lines = ["", "=" * _WIDTH, f"ETA DRIFT — {diff['container']}", "=" * _WIDTH]
    lines.append(f"T0: {diff.get('previous_run_at') or _ABSENT}")
    lines.append(f"T1: {diff.get('current_run_at') or _ABSENT}")
    lines.append(f"interval: {_hours(diff.get('interval_hours'))}")
    state = diff.get("journey_state") or {}
    lines.append(f"journey state: {state.get('previous') or _ABSENT} -> {state.get('current') or _ABSENT}")

    lines.extend(_eta_section(diff))
    lines.extend(_events_section(diff))
    lines.extend(_identity_section(diff))
    lines.extend(_freshness_section(diff))
    return "\n".join(lines)


def _eta_section(diff: dict) -> list[str]:
    lines = ["", "ETA", "-" * _WIDTH]
    for side in ("reference", "candidate"):
        drift = (diff.get("eta_drift") or {}).get(side) or {}
        lines.append(f"{drift.get('provider') or side}: {drift.get('verdict') or _ABSENT}")
        lines.append(f"{'':4}{drift.get('previous_eta_at') or _ABSENT} -> {drift.get('current_eta_at') or _ABSENT}")
        if drift.get("drift_hours") is not None:
            lines.append(f"{'':4}drift {_hours(drift['drift_hours'])}")
        if drift.get("verdict") == ETA_TARGET_CHANGED:
            lines.append(
                f"{'':4}target moved {drift.get('previous_target')} -> {drift.get('current_target')}, so no "
                "drift figure is reported: the provider changed what it was forecasting"
            )
        else:
            lines.append(f"{'':4}target {drift.get('current_target') or _ABSENT}")
    return lines


def _events_section(diff: dict) -> list[str]:
    lines = ["", "EVENTS", "-" * _WIDTH]
    for side in ("reference", "candidate"):
        delta = (diff.get("events") or {}).get(side) or {}
        provider = delta.get("provider") or side
        lines.append(f"{provider}: {delta.get('previous_total', 0)} -> {delta.get('current_total', 0)} event(s)")
        lines.append(f"{'':4}new observed:   {len(delta.get('new_actual') or [])}")
        for event in delta.get("new_actual") or []:
            lines.append(
                f"{'':8}{event.get('event_datetime')} {event.get('event_type')} @ {event.get('location_name')}"
            )
        lines.append(f"{'':4}new forecast:   {len(delta.get('new_forecast') or [])}")
        for event in delta.get("new_forecast") or []:
            lines.append(
                f"{'':8}{event.get('event_datetime')} {event.get('event_type')} @ {event.get('location_name')}"
            )

        corrections = delta.get("corrections") or []
        lines.append(f"{'':4}corrections:    {len(corrections)}")
        for correction in corrections:
            lines.append(
                f"{'':8}{correction.get('event_type')} @ {correction.get('location_name')}: "
                f"{correction.get('previous_event_datetime')} -> {correction.get('current_event_datetime')} "
                f"({_hours(correction.get('shift_hours'))})"
            )

        disappeared = delta.get("disappeared") or []
        if disappeared:
            # A provider withdrawing an event it had already reported is worth seeing on
            # its own line: it is not a correction, and Container SCM keeps the row.
            lines.append(f"{'':4}withdrawn:      {len(disappeared)}")
            for event in disappeared:
                lines.append(
                    f"{'':8}{event.get('event_datetime')} {event.get('event_type')} @ {event.get('location_name')}"
                )
    return lines


def _identity_section(diff: dict) -> list[str]:
    identity = diff.get("event_identity") or {}
    lines = ["", "PROVIDER EVENT IDENTITY", "-" * _WIDTH]
    lines.append(f"verdict: {identity.get('verdict') or _ABSENT}")
    lines.append(f"{'':4}{identity.get('reason') or ''}")
    lines.append(f"{'':4}events compared: {identity.get('compared_events', 0)}")

    for name, report in (identity.get("fields") or {}).items():
        mark = "stable" if report.get("stable") else f"CHANGED on {report.get('changed_events')} event(s)"
        lines.append(f"{'':4}{name:12}{mark}")
        for example in report.get("examples") or []:
            event = example.get("event") or {}
            lines.append(
                f"{'':8}{event.get('timestamp')} {event.get('event_code') or '(no code)'} "
                f"@ {event.get('location')}: {example.get('previous')} -> {example.get('current')}"
            )

    if identity.get("verdict") == IDENTITY_STABLE:
        lines.append("")
        lines.append("  One stable interval is evidence, not proof. Adopting a provider identity as")
        lines.append("  source_event_id needs it to hold across a refetch that actually changed something.")
    elif identity.get("verdict") == IDENTITY_UNKNOWN:
        lines.append("")
        lines.append("  Nothing was comparable, so this interval says nothing about identity stability.")
    return lines


def _freshness_section(diff: dict) -> list[str]:
    freshness = diff.get("freshness") or {}
    lines = ["", "FRESHNESS", "-" * _WIDTH]
    lines.append(f"reported new movement first: {freshness.get('reported_new_movement_first') or _ABSENT}")
    lines.append(f"{'':4}{freshness.get('note') or ''}")
    return lines
