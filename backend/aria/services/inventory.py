"""Inventory service — the shelter's stock ledger.

Backed by ``data/inventory.csv`` and the standard library's :mod:`csv` module.
The previous implementation kept *two* pandas DataFrames of the same file (one
in the logistics agent, one in the manager) and relied on remembering to call
``reload_inventory()`` after every write; whenever that call was missed the UI
showed stock that had already been reserved.  There is now exactly one ledger,
guarded by one lock, and every mutation writes the CSV atomically.

Accounting model
----------------
``Total`` is the bin's capacity.  ``Available`` is what is on the shelf,
``Reserved`` is what has been committed to an approved request but not yet used
up.  The invariant is ``Available + Reserved <= Total``.

    reserve   available → reserved      (manager approves a situation)
    release   reserved  → available     (request cancelled before dispatch)
    consume   reserved  → gone          (used on site; capacity unchanged)
    restore   reserved  → available     (came back unused)
    refill    → available = Total       (resupply)

The old code only ever restored, so anything a volunteer actually *used* stayed
counted as reserved forever and the shelf slowly filled with phantom stock.
``settle_return`` closes that loop: what came back is restored, the difference is
consumed.
"""

from __future__ import annotations

import csv
import os
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from aria.config import settings
from aria.core.errors import InsufficientStockError, NotFoundError, ValidationError
from aria.core.eventbus import EVENT_INVENTORY_CHANGED, EventBus, event_bus
from aria.core.logging import get_logger
from aria.schemas import InventoryMovementLog, InventoryRow, ItemMovement
from aria.utils.textutil import fuzzy_best_match, normalise

log = get_logger("inventory")

#: On-disk column order — unchanged, so an existing inventory.csv still loads.
CSV_COLUMNS = ("Item", "Available", "Reserved", "Total", "Bin Location", "Category")


class ReservationLine:
    """Outcome for one requested material."""

    __slots__ = ("requested", "reserved", "item", "matched", "reason")

    def __init__(self, item: str, requested: int, reserved: int, matched: Optional[str], reason: str) -> None:
        self.item = item
        self.requested = requested
        self.reserved = reserved
        self.matched = matched
        self.reason = reason

    @property
    def shortfall(self) -> int:
        return max(0, self.requested - self.reserved)

    @property
    def ok(self) -> bool:
        return self.shortfall == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "matched_item": self.matched,
            "requested": self.requested,
            "reserved": self.reserved,
            "shortfall": self.shortfall,
            "ok": self.ok,
            "reason": self.reason,
        }


class ReservationResult:
    def __init__(self, lines: list[ReservationLine]) -> None:
        self.lines = lines

    @property
    def reserved_items(self) -> list[ItemMovement]:
        return [
            ItemMovement(item=line.matched or line.item, quantity=line.reserved)
            for line in self.lines
            if line.reserved > 0
        ]

    @property
    def shortfalls(self) -> list[ReservationLine]:
        return [line for line in self.lines if not line.ok]

    @property
    def fully_satisfied(self) -> bool:
        return not self.shortfalls

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "fully_satisfied": self.fully_satisfied,
        }


class InventoryService:
    """Thread-safe CSV-backed stock ledger."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        bus: Optional[EventBus] = None,
        autosave: bool = True,
    ) -> None:
        self._path = Path(path or settings.paths.inventory_csv)
        self._bus = bus if bus is not None else event_bus
        self._autosave = autosave
        self._lock = threading.RLock()
        self._rows: list[InventoryRow] = []
        self._buffer: dict[str, dict[str, int]] = {}
        self._history: deque[InventoryMovementLog] = deque(
            maxlen=settings.inventory.history_limit
        )
        self.load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """(Re)read the CSV from disk.  Missing or malformed rows are skipped."""
        rows: list[InventoryRow] = []
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8", newline="") as handle:
                    for record in csv.DictReader(handle):
                        row = _row_from_csv(record)
                        if row is not None:
                            rows.append(row)
            except OSError as exc:
                log.error("Cannot read inventory %s: %s", self._path, exc)
        else:
            log.warning("Inventory file %s not found — starting empty", self._path)

        with self._lock:
            self._rows = rows
        log.info("Loaded %d inventory items from %s", len(rows), self._path.name)

    def _save(self) -> None:
        if not self._autosave:
            return
        with self._lock:
            snapshot = [_row_to_csv(row) for row in self._rows]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: a power cut mid-save must not truncate the ledger.
            handle = tempfile.NamedTemporaryFile(
                "w",
                delete=False,
                dir=str(self._path.parent),
                prefix=".inventory-",
                suffix=".tmp",
                encoding="utf-8",
                newline="",
            )
            with handle:
                writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
                writer.writeheader()
                writer.writerows(snapshot)
            os.replace(handle.name, self._path)
        except OSError as exc:
            log.error("Failed to persist inventory: %s", exc)

    # ── Lookups ───────────────────────────────────────────────────────────────

    def _index_of(self, item_name: str) -> Optional[int]:
        """Resolve a free-text item name to a row index, or None.

        Strict on purpose — see :func:`aria.utils.textutil.fuzzy_best_match`.
        """
        names = [row.item for row in self._rows]
        match = fuzzy_best_match(
            item_name, names, min_score=settings.inventory.fuzzy_min_score
        )
        return None if match is None else match[0]

    def resolve(self, item_name: str) -> Optional[InventoryRow]:
        with self._lock:
            index = self._index_of(item_name)
            return self._rows[index] if index is not None else None

    def availability(self, item_name: str, quantity: int = 1) -> dict[str, Any]:
        """What the logistics agent stamps onto a material line."""
        row = self.resolve(item_name)
        if row is None:
            return {
                "found": False,
                "available": False,
                "available_qty": 0,
                "bin": "?",
                "matched_item": None,
            }
        return {
            "found": True,
            "available": row.available >= max(1, quantity),
            "available_qty": row.available,
            "bin": row.bin or "?",
            "matched_item": row.item,
        }

    def all(self) -> list[dict[str, Any]]:
        threshold = settings.inventory.low_stock_threshold
        with self._lock:
            return [row.as_api(threshold) for row in self._rows]

    def rows(self) -> list[InventoryRow]:
        with self._lock:
            return [row.model_copy() for row in self._rows]

    def low_stock(self) -> list[dict[str, Any]]:
        return [row for row in self.all() if row["status"] in {"LOW", "OUT_OF_STOCK", "ALL_RESERVED"}]

    def buffer(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"item": name, "quantity": info["quantity"], "capacity": info["capacity"]}
                for name, info in sorted(self._buffer.items())
                if info["quantity"] > 0
            ]

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            entries = list(self._history)[-limit:]
        return [entry.model_dump(mode="json") for entry in reversed(entries)]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            rows = list(self._rows)
        total = sum(row.total for row in rows)
        available = sum(row.available for row in rows)
        reserved = sum(row.reserved for row in rows)
        return {
            "items": len(rows),
            "units_total": total,
            "units_available": available,
            "units_reserved": reserved,
            "fill_pct": round(available / total * 100) if total else 0,
            "low_stock_items": len(self.low_stock()),
        }

    # ── Mutations ─────────────────────────────────────────────────────────────

    def reserve_many(
        self,
        wanted: Sequence[tuple[str, int]],
        *,
        request_id: Optional[str] = None,
        allow_partial: bool = True,
    ) -> ReservationResult:
        """Reserve several materials in one atomic step.

        Demand for the same underlying row is aggregated first, so two situations
        that both need a CPR mask cannot each reserve the last one.  With
        ``allow_partial`` the caller gets what exists and an explicit shortfall
        (a volunteer with three of five bandages is still worth dispatching);
        without it the whole reservation is refused with
        :class:`InsufficientStockError` and nothing is touched.
        """
        lines: list[ReservationLine] = []
        with self._lock:
            # Phase 1 — resolve names and aggregate demand per row.
            demand: dict[int, int] = {}
            resolved: list[tuple[str, int, Optional[int]]] = []
            for name, qty in wanted:
                quantity = max(0, int(qty))
                index = self._index_of(name) if quantity > 0 else None
                resolved.append((name, quantity, index))
                if index is not None and quantity > 0:
                    demand[index] = demand.get(index, 0) + quantity

            # Phase 2 — decide how much of each row's demand can be met.
            grant: dict[int, int] = {}
            short: list[str] = []
            for index, needed in demand.items():
                stock = self._rows[index].available
                if stock >= needed:
                    grant[index] = needed
                else:
                    grant[index] = stock if allow_partial else 0
                    short.append(f"{self._rows[index].item} ({stock} of {needed})")

            # All-or-nothing mode: refuse before touching a single row.
            if short and not allow_partial:
                raise InsufficientStockError(
                    "Not enough stock to reserve everything: " + ", ".join(short),
                    request_id=request_id,
                    short=short,
                )

            # Phase 3 — apply, distributing each row's grant across its lines.
            remaining = dict(grant)
            for name, quantity, index in resolved:
                if quantity <= 0:
                    lines.append(ReservationLine(name, quantity, 0, None, "zero quantity"))
                    continue
                if index is None:
                    lines.append(ReservationLine(name, quantity, 0, None, "not stocked"))
                    continue
                row = self._rows[index]
                take = min(quantity, remaining.get(index, 0))
                if take > 0:
                    remaining[index] -= take
                    row.available -= take
                    row.reserved += take
                    self._record("reserve", row.item, take, request_id)
                reason = "reserved" if take == quantity else f"only {take} of {quantity} in stock"
                lines.append(ReservationLine(name, quantity, take, row.item, reason))

            if any(line.reserved for line in lines):
                self._save()
                self._notify("reserve", request_id)

        return ReservationResult(lines)

    def release_many(
        self, items: Iterable[ItemMovement], *, request_id: Optional[str] = None
    ) -> None:
        """Undo reservations (request cancelled): reserved → available."""
        changed = False
        with self._lock:
            for movement in items:
                index = self._index_of(movement.item)
                if index is None or movement.quantity <= 0:
                    continue
                row = self._rows[index]
                give_back = min(movement.quantity, row.reserved)
                # Room left once this hold is released. With a sane ledger the
                # released units always fit; the clamp only guards a CSV that
                # was hand-edited into an impossible state.
                room = max(0, row.total - row.available - (row.reserved - give_back))
                give_back = min(give_back, room)
                if give_back <= 0:
                    continue
                row.reserved -= give_back
                row.available += give_back
                self._record("release", row.item, give_back, request_id)
                changed = True
            if changed:
                self._save()
                self._notify("release", request_id)

    def settle_return(
        self,
        taken: Sequence[ItemMovement],
        returned: Sequence[ItemMovement],
        *,
        request_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Close out a mission: restock what came back, write off what did not.

        Returns a report containing the restored, consumed and buffered
        quantities so the caller can record exactly what happened on the request.
        """
        returned_by_item: dict[str, int] = {}
        for movement in returned:
            key = normalise(movement.item)
            returned_by_item[key] = returned_by_item.get(key, 0) + max(0, movement.quantity)

        restored: list[ItemMovement] = []
        consumed: list[ItemMovement] = []
        buffered: list[ItemMovement] = []

        with self._lock:
            for movement in taken:
                key = normalise(movement.item)
                back = min(returned_by_item.get(key, 0), movement.quantity)
                returned_by_item[key] = max(0, returned_by_item.get(key, 0) - back)
                used = max(0, movement.quantity - back)

                index = self._index_of(movement.item)
                if index is None:
                    if back:
                        self._add_to_buffer(movement.item, back)
                        buffered.append(ItemMovement(item=movement.item, quantity=back))
                    continue

                row = self._rows[index]
                if back:
                    # Release the hold first, then re-shelve into the space that
                    # frees up.  Computing room before the release would let
                    # available + reserved exceed the bin's capacity.
                    released = min(back, row.reserved)
                    row.reserved -= released
                    room = max(0, row.total - row.available - row.reserved)
                    to_shelf = min(back, room)
                    overflow = back - to_shelf
                    row.available += to_shelf
                    if to_shelf:
                        restored.append(ItemMovement(item=row.item, quantity=to_shelf))
                        self._record("restore", row.item, to_shelf, request_id)
                    if overflow:
                        self._add_to_buffer(row.item, overflow)
                        buffered.append(ItemMovement(item=row.item, quantity=overflow))
                        self._record("buffer", row.item, overflow, request_id)
                if used:
                    row.reserved = max(0, row.reserved - used)
                    consumed.append(ItemMovement(item=row.item, quantity=used))
                    self._record("consume", row.item, used, request_id)

            # Anything handed back that was never signed out still gets stocked.
            for movement in returned:
                key = normalise(movement.item)
                leftover = returned_by_item.get(key, 0)
                if leftover <= 0:
                    continue
                returned_by_item[key] = 0
                index = self._index_of(movement.item)
                if index is None:
                    self._add_to_buffer(movement.item, leftover)
                    buffered.append(ItemMovement(item=movement.item, quantity=leftover))
                    continue
                row = self._rows[index]
                # Nothing was signed out for this line, so no hold is released:
                # only genuinely free capacity can absorb it, the rest buffers.
                room = max(0, row.total - row.available - row.reserved)
                to_shelf = min(leftover, room)
                if to_shelf:
                    row.available += to_shelf
                    restored.append(ItemMovement(item=row.item, quantity=to_shelf))
                    self._record("restore", row.item, to_shelf, request_id, note="unlogged item")
                if leftover - to_shelf:
                    self._add_to_buffer(row.item, leftover - to_shelf)
                    buffered.append(ItemMovement(item=row.item, quantity=leftover - to_shelf))

            self._save()
            self._notify("return", request_id)

        return {
            "restored": [m.model_dump() for m in restored],
            "consumed": [m.model_dump() for m in consumed],
            "buffered": [m.model_dump() for m in buffered],
        }

    def add_stock(self, item_name: str, quantity: int) -> InventoryRow:
        """Top up an existing item, never exceeding its capacity."""
        if quantity <= 0:
            raise ValidationError("Quantity must be positive", item=item_name)
        with self._lock:
            index = self._index_of(item_name)
            if index is None:
                raise NotFoundError(f"'{item_name}' is not in the inventory", item=item_name)
            row = self._rows[index]
            room = max(0, row.total - row.available - row.reserved)
            if quantity > room:
                raise ValidationError(
                    f"Only {room} free slot(s) for '{row.item}' "
                    f"({row.available} available + {row.reserved} reserved of {row.total}). "
                    "Raise the capacity first.",
                    item=row.item,
                    free_slots=room,
                )
            row.available += quantity
            self._record("add_stock", row.item, quantity, None)
            self._save()
            self._notify("add_stock", None)
            return row.model_copy()

    def create_item(
        self,
        item_name: str,
        capacity: int,
        *,
        bin_location: str = "NEW",
        category: str = "General",
    ) -> InventoryRow:
        clean = str(item_name or "").strip()
        if not clean:
            raise ValidationError("Item name is required")
        if capacity <= 0:
            raise ValidationError("Capacity must be positive", item=clean)
        with self._lock:
            if any(normalise(row.item) == normalise(clean) for row in self._rows):
                raise ValidationError(f"'{clean}' already exists", item=clean)
            row = InventoryRow(
                item=clean,
                available=capacity,
                reserved=0,
                total=capacity,
                bin=bin_location or "NEW",
                category=category or "General",
            )
            self._rows.append(row)
            self._record("create", row.item, capacity, None)
            self._save()
            self._notify("create", None)
            return row.model_copy()

    def delete_item(self, item_name: str) -> None:
        with self._lock:
            index = self._index_of(item_name)
            if index is None:
                raise NotFoundError(f"'{item_name}' is not in the inventory", item=item_name)
            row = self._rows[index]
            if row.reserved > 0:
                raise ValidationError(
                    f"'{row.item}' still has {row.reserved} reserved unit(s)", item=row.item
                )
            self._rows.pop(index)
            self._record("delete", row.item, 0, None)
            self._save()
            self._notify("delete", None)

    def daily_refill(self) -> int:
        """Overnight resupply: everything back to capacity, holds cleared."""
        with self._lock:
            for row in self._rows:
                if row.available != row.total or row.reserved:
                    self._record("daily_refill", row.item, row.total - row.available, None)
                row.available = row.total
                row.reserved = 0
            self._save()
            self._notify("daily_refill", None)
            return len(self._rows)

    def partial_refill(self) -> int:
        """Top up only what has dropped to or below the refill threshold."""
        threshold = settings.inventory.refill_threshold
        refilled = 0
        with self._lock:
            for row in self._rows:
                if row.total <= 0:
                    continue
                if row.available / row.total <= threshold:
                    added = row.total - row.reserved - row.available
                    if added > 0:
                        row.available += added
                        self._record("partial_refill", row.item, added, None)
                        refilled += 1
            if refilled:
                self._save()
                self._notify("partial_refill", None)
        return refilled

    # ── Internals ─────────────────────────────────────────────────────────────

    def _add_to_buffer(self, item_name: str, quantity: int) -> None:
        """Overflow store for stock that no longer fits its bin."""
        entry = self._buffer.setdefault(
            item_name,
            {"quantity": 0, "capacity": settings.inventory.buffer_default_capacity},
        )
        entry["quantity"] += quantity
        entry["capacity"] = max(entry["capacity"], entry["quantity"])
        log.info("Buffer: %s now %d/%d", item_name, entry["quantity"], entry["capacity"])

    def _record(
        self,
        action: str,
        item: str,
        quantity: int,
        request_id: Optional[str],
        note: str = "",
    ) -> None:
        self._history.append(
            InventoryMovementLog(
                action=action, item=item, quantity=quantity, request_id=request_id, note=note
            )
        )

    def _notify(self, action: str, request_id: Optional[str]) -> None:
        if self._bus is not None:
            self._bus.publish(EVENT_INVENTORY_CHANGED, action=action, request_id=request_id)

    # ── Snapshot support (used by the persistence service) ────────────────────

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "buffer": {k: dict(v) for k, v in self._buffer.items()},
                "history": [entry.model_dump(mode="json") for entry in self._history],
            }

    def restore_snapshot(self, data: dict[str, Any]) -> None:
        with self._lock:
            buffer = data.get("buffer") or {}
            if isinstance(buffer, dict):
                self._buffer = {
                    str(name): {
                        "quantity": int(info.get("quantity", 0)),
                        "capacity": int(
                            info.get("capacity", settings.inventory.buffer_default_capacity)
                        ),
                    }
                    for name, info in buffer.items()
                    if isinstance(info, dict)
                }
            for entry in data.get("history") or []:
                try:
                    self._history.append(InventoryMovementLog.model_validate(entry))
                except Exception:  # noqa: BLE001 - a corrupt log line is not fatal
                    continue


def _row_from_csv(record: dict[str, str]) -> Optional[InventoryRow]:
    name = (record.get("Item") or "").strip()
    if not name:
        return None
    try:
        total = _as_int(record.get("Total"))
        available = _as_int(record.get("Available"))
        reserved = _as_int(record.get("Reserved"))
    except ValueError:
        log.warning("Skipping malformed inventory row: %r", record)
        return None
    # Repair impossible rows rather than trusting a hand-edited CSV.
    total = max(total, available + reserved)
    return InventoryRow(
        item=name,
        available=available,
        reserved=reserved,
        total=total,
        bin=(record.get("Bin Location") or "").strip(),
        category=(record.get("Category") or "").strip(),
    )


def _row_to_csv(row: InventoryRow) -> dict[str, Any]:
    return {
        "Item": row.item,
        "Available": row.available,
        "Reserved": row.reserved,
        "Total": row.total,
        "Bin Location": row.bin,
        "Category": row.category,
    }


def _as_int(value: object) -> int:
    if value in (None, ""):
        return 0
    return max(0, int(float(str(value).strip())))
