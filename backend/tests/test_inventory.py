"""Stock ledger: matching, reservation, and the reserve → consume loop."""

from __future__ import annotations

import pytest

from aria.core.errors import NotFoundError, ValidationError
from aria.schemas import ItemMovement


def _row(inventory, name):
    return next(row for row in inventory.all() if row["item"] == name)


def test_loads_the_csv(inventory):
    assert len(inventory.rows()) == 12
    assert _row(inventory, "AED")["available"] == 2


def test_matching_is_strict_enough_to_be_safe(inventory):
    """The old fuzzy threshold matched almost anything against anything."""
    assert inventory.resolve("AED").item == "AED"
    assert inventory.resolve("aed").item == "AED"
    assert inventory.resolve("cpr mask").item == "CPR Mask"
    assert inventory.resolve("Nitrile Gloves").item == "Nitrile Gloves (pair)"
    # Not stocked here — must not silently match something else.
    assert inventory.resolve("Ventilator") is None
    assert inventory.resolve("kit") is None


def test_reserve_moves_available_to_reserved(inventory):
    result = inventory.reserve_many([("AED", 1), ("CPR Mask", 2)])

    assert result.fully_satisfied
    assert _row(inventory, "AED") == {**_row(inventory, "AED"), "available": 1, "reserved": 1}
    assert _row(inventory, "CPR Mask")["available"] == 3
    assert _row(inventory, "CPR Mask")["reserved"] == 2


def test_reserve_aggregates_demand_for_the_same_item(inventory):
    """Two situations wanting the last AED cannot both get it."""
    result = inventory.reserve_many([("AED", 2), ("AED", 2)])

    reserved = sum(line.reserved for line in result.lines)
    assert reserved == 2
    assert _row(inventory, "AED")["available"] == 0
    assert result.fully_satisfied is False


def test_reserve_reports_shortfall_without_over_committing(inventory):
    result = inventory.reserve_many([("Oxygen Mask", 3)])

    line = result.lines[0]
    assert line.reserved == 1
    assert line.shortfall == 2
    assert _row(inventory, "Oxygen Mask")["available"] == 0


def test_unstocked_items_are_reported_not_matched(inventory):
    result = inventory.reserve_many([("Portable X-Ray", 1)])

    assert result.lines[0].reserved == 0
    assert result.lines[0].reason == "not stocked"


def test_settle_return_restores_and_consumes(inventory):
    inventory.reserve_many([("Bandage Roll", 4)])
    taken = [ItemMovement(item="Bandage Roll", quantity=4)]

    report = inventory.settle_return(taken, [ItemMovement(item="Bandage Roll", quantity=1)])

    assert report["restored"] == [{"item": "Bandage Roll", "quantity": 1}]
    assert report["consumed"] == [{"item": "Bandage Roll", "quantity": 3}]

    row = _row(inventory, "Bandage Roll")
    # Started 15/5 of 20: reserved 4 (11/9), returned 1, used 3.
    assert row["available"] == 12
    assert row["reserved"] == 5
    assert row["available"] + row["reserved"] <= row["total"]


def test_consumed_stock_does_not_stay_reserved_for_ever(inventory):
    """The leak that made the old ledger fill up with phantom holds."""
    before = _row(inventory, "Sterile Gauze")["reserved"]
    inventory.reserve_many([("Sterile Gauze", 5)])
    inventory.settle_return([ItemMovement(item="Sterile Gauze", quantity=5)], [])

    assert _row(inventory, "Sterile Gauze")["reserved"] == before


def test_release_returns_a_cancelled_reservation(inventory):
    inventory.reserve_many([("Leg Splint", 2)])
    inventory.release_many([ItemMovement(item="Leg Splint", quantity=2)])

    row = _row(inventory, "Leg Splint")
    assert (row["available"], row["reserved"]) == (4, 0)


def test_overflow_goes_to_the_buffer(inventory):
    """Handing back more than the bin holds must not exceed capacity."""
    inventory.settle_return([], [ItemMovement(item="AED", quantity=5)])

    row = _row(inventory, "AED")
    assert row["available"] + row["reserved"] <= row["total"]
    assert inventory.buffer()[0]["item"] == "AED"


def test_add_stock_respects_capacity(inventory):
    """Reserved units still occupy the bin, so they count against capacity."""
    inventory.reserve_many([("Leg Splint", 1)])  # 3 available + 1 reserved of 4

    with pytest.raises(ValidationError):
        inventory.add_stock("Leg Splint", 1)

    # Once that splint is used on site the bin has room for a replacement.
    inventory.settle_return([ItemMovement(item="Leg Splint", quantity=1)], [])
    row = inventory.add_stock("Leg Splint", 1)
    assert (row.available, row.reserved) == (4, 0)


def test_create_and_delete_item(inventory):
    inventory.create_item("Burn Dressing", 12, bin_location="A-05", category="Medical")
    assert _row(inventory, "Burn Dressing")["available"] == 12

    with pytest.raises(ValidationError):
        inventory.create_item("Burn Dressing", 5)

    inventory.delete_item("Burn Dressing")
    with pytest.raises(NotFoundError):
        inventory.delete_item("Burn Dressing")


def test_delete_refuses_while_stock_is_reserved(inventory):
    inventory.reserve_many([("Flashlight", 1)])
    with pytest.raises(ValidationError):
        inventory.delete_item("Flashlight")


def test_daily_refill_clears_holds_and_partial_only_tops_up_the_low(inventory):
    inventory.reserve_many([("Water Bottle 500ml", 55)])  # 5 of 60 left
    inventory.partial_refill()
    row = _row(inventory, "Water Bottle 500ml")
    assert row["available"] == 5  # capacity is committed to the reservation

    inventory.daily_refill()
    row = _row(inventory, "Water Bottle 500ml")
    assert (row["available"], row["reserved"]) == (60, 0)


def test_writes_survive_a_reload(inventory, inventory_csv):
    from aria.services.inventory import InventoryService

    inventory.reserve_many([("Energy Bar", 10)])
    reloaded = InventoryService(inventory_csv, bus=None)

    assert _row(reloaded, "Energy Bar")["reserved"] == 10


def test_history_records_every_movement(inventory):
    inventory.reserve_many([("Glucose Tablets", 2)])
    inventory.settle_return([ItemMovement(item="Glucose Tablets", quantity=2)], [])

    actions = [entry["action"] for entry in inventory.history()]
    assert "reserve" in actions and "consume" in actions


def test_all_or_nothing_reservation_refuses_and_changes_nothing(inventory):
    """With allow_partial=False a shortfall must leave the ledger untouched."""
    from aria.core.errors import InsufficientStockError

    before = _row(inventory, "Oxygen Mask")
    with pytest.raises(InsufficientStockError):
        inventory.reserve_many([("CPR Mask", 1), ("Oxygen Mask", 3)], allow_partial=False)

    assert _row(inventory, "Oxygen Mask") == before
    assert _row(inventory, "CPR Mask")["reserved"] == 0
