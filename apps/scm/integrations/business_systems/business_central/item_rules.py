"""Classify Business Central items with an integration's ``BusinessCentralItemRule`` rows.

Whether an item is a physical container is decided only by explicit rules — never
by guessing from the item number — and an item no rule covers stays unclassified.

Precedence, highest first (deterministic; ties are impossible because rules are
unique per integration, match type, item and variant):

  1. exact item no. + variant code
  2. exact item no. (rule without a variant)
  3. the longest matching item-no. prefix
  4. unclassified

Variant codes: the Analytics API's item ledger entries carry no ``variantCode``
(verified, stage 1a), so entries resolve with an empty variant. An item whose
exact rules all name a variant cannot be resolved without one; it is reported as
``needs_variant`` instead of guessing which variant applies.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.scm.containers.models import EquipmentType
from apps.scm.integrations.models import BusinessCentralItemLedgerEntry, BusinessCentralItemRule, Integration

Classification = BusinessCentralItemLedgerEntry.ItemClassification
MatchType = BusinessCentralItemRule.MatchType


@dataclass(frozen=True)
class ItemResolution:
    classification: str
    equipment_type: EquipmentType | None = None
    rule: BusinessCentralItemRule | None = None
    # The item has only variant-specific rules and the entry has no variant.
    needs_variant: bool = False


_UNCLASSIFIED = ItemResolution(classification=Classification.UNCLASSIFIED)


class ItemRuleSet:
    """An integration's rules, loaded once and resolved in memory."""

    def __init__(self, rules: list[BusinessCentralItemRule]):
        self._exact: dict[tuple[str, str], BusinessCentralItemRule] = {}
        self._variant_items: set[str] = set()
        self._prefixes: list[BusinessCentralItemRule] = []
        for rule in rules:
            item_no = rule.item_no.upper()
            if rule.match_type == MatchType.EXACT:
                self._exact[(item_no, rule.variant_code.upper())] = rule
                if rule.variant_code:
                    self._variant_items.add(item_no)
            else:
                self._prefixes.append(rule)
        # Longest prefix first, so the most specific prefix wins.
        self._prefixes.sort(key=lambda r: len(r.item_no), reverse=True)

    @classmethod
    def for_integration(cls, integration: Integration) -> ItemRuleSet:
        rules = BusinessCentralItemRule.objects.filter(integration=integration).select_related("equipment_type")
        return cls(list(rules))

    def resolve(self, item_no: str, variant_code: str = "") -> ItemResolution:
        item, variant = (item_no or "").upper(), (variant_code or "").upper()
        if not item:
            return _UNCLASSIFIED
        if variant and (rule := self._exact.get((item, variant))):
            return _resolution(rule)
        if rule := self._exact.get((item, "")):
            return _resolution(rule)
        if item in self._variant_items:
            return ItemResolution(classification=Classification.UNCLASSIFIED, needs_variant=True)
        for prefix_rule in self._prefixes:
            if item.startswith(prefix_rule.item_no.upper()):
                return _resolution(prefix_rule)
        return _UNCLASSIFIED


def resolve_item(integration: Integration, item_no: str, variant_code: str = "") -> ItemResolution:
    """Resolve one item (loads the rules; use ``ItemRuleSet`` for many entries)."""
    return ItemRuleSet.for_integration(integration).resolve(item_no, variant_code)


def entry_variant_code(entry: BusinessCentralItemLedgerEntry) -> str:
    """The entry's BC variant code — only if BC sent one (the Analytics API currently does not)."""
    value = (entry.raw_payload or {}).get("variantCode")
    return value if isinstance(value, str) else ""


def _resolution(rule: BusinessCentralItemRule) -> ItemResolution:
    equipment_type = rule.equipment_type if rule.classification == Classification.CONTAINER else None
    return ItemResolution(classification=rule.classification, equipment_type=equipment_type, rule=rule)
