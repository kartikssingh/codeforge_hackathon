"""Shared fixtures.

Every test gets an isolated board: its own inventory CSV in a temp directory,
persistence off, and the LLM backend forced to ``none``.  The whole suite
therefore runs on a machine with no models, no Ollama and no internet — which is
also the environment ARIA itself has to survive.

The environment is set at *import* time rather than in a fixture: pytest imports
test modules (and therefore ``aria.config``) during collection, before any
fixture runs, and :data:`aria.config.settings` is resolved once at import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("ARIA_LLM_BACKEND", "none")
# Point the protocol library at an empty directory so no test ever loads the
# embedding model. Retrieval degrades to "unavailable", which is exactly the
# offline path the suite is meant to cover — and keeps the run under a second.
os.environ.setdefault("ARIA_PROTOCOLS_DIR", str(BACKEND_DIR / "tests" / ".no-protocols"))
os.environ.setdefault("ARIA_PERSISTENCE_ENABLED", "0")
os.environ.setdefault("ARIA_ESCALATION_ENABLED", "0")
os.environ.setdefault("ARIA_LOG_TO_FILE", "0")
os.environ.setdefault("ARIA_LOG_LEVEL", "WARNING")

import pytest  # noqa: E402

SAMPLE_INVENTORY = """Item,Available,Reserved,Total,Bin Location,Category
AED,2,0,2,A-01,Medical
CPR Mask,5,0,5,A-01,Medical
Leg Splint,4,0,4,A-02,Medical
Bandage Roll,15,5,20,A-03,Medical
Sterile Gauze,10,0,10,A-03,Medical
Nitrile Gloves (pair),40,0,40,A-04,PPE
Thermal Blanket,8,2,10,B-01,Comfort
Water Bottle 500ml,60,0,60,B-02,Nutrition
Energy Bar,40,0,40,B-02,Nutrition
Oxygen Mask,1,1,2,C-02,Medical
Glucose Tablets,20,0,20,C-01,Medical
Flashlight,6,2,8,D-03,Equipment
"""


@pytest.fixture
def inventory_csv(tmp_path: Path) -> Path:
    path = tmp_path / "inventory.csv"
    path.write_text(SAMPLE_INVENTORY, encoding="utf-8")
    return path


@pytest.fixture
def inventory(inventory_csv: Path):
    from aria.services.inventory import InventoryService

    return InventoryService(inventory_csv, bus=None)


@pytest.fixture
def hub(inventory):
    """A fully wired hub on an isolated ledger, with no background threads."""
    from aria.services.hub import Hub

    instance = Hub(inventory=inventory)
    yield instance
    instance.stop()


@pytest.fixture
def api_client(hub):
    """FastAPI TestClient wired to the isolated hub (skipped without httpx)."""
    pytest.importorskip("httpx", reason="fastapi.testclient needs httpx")
    from fastapi.testclient import TestClient

    from aria.api import create_app
    from aria.api.deps import hub as hub_dep

    app = create_app()
    app.dependency_overrides[hub_dep] = lambda: hub
    with TestClient(app) as client:
        yield client
