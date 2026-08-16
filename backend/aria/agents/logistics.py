"""Step 5 — logistics annotation.

Stamps live stock information onto every material line so the shelter manager
sees, before approving anything, exactly what is on the shelf and what is not.
Unavailable lines are greyed out in the UI rather than hidden: a volunteer needs
to know the oxygen mask is *not* coming.

This agent reads through the one inventory service — it no longer keeps its own
copy of the CSV, which is what used to make the panel disagree with the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from aria.core.logging import get_logger
from aria.schemas import Situation
from aria.services.inventory import InventoryService

log = get_logger("agents.logistics")


@dataclass
class LogisticsReport:
    situations: list[Situation] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    short: list[str] = field(default_factory=list)

    @property
    def all_available(self) -> bool:
        return not self.missing and not self.short


def annotate_situations(
    situations: Sequence[Situation], inventory: InventoryService
) -> LogisticsReport:
    """Fill in availability for every material and report what is lacking."""
    missing: list[str] = []
    short: list[str] = []

    for situation in situations:
        for material in situation.materials:
            info = inventory.availability(material.item, material.quantity)
            material.available = bool(info["available"])
            material.available_qty = int(info["available_qty"])
            material.bin = str(info["bin"])
            material.matched_item = info.get("matched_item")

            if not info["found"]:
                if material.item not in missing:
                    missing.append(material.item)
            elif not info["available"] and material.item not in short:
                short.append(material.item)

    if missing:
        log.info("Not stocked at this shelter: %s", ", ".join(missing))
    if short:
        log.info("Insufficient stock: %s", ", ".join(short))

    return LogisticsReport(situations=list(situations), missing=missing, short=short)
