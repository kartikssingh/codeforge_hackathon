"""ARIA — Autonomous Relief Intelligence Agent.

An offline, CPU-only multi-agent triage and dispatch system for disaster relief
shelters.  Audio or typed distress reports go in; a ranked, human-approved,
inventory-aware dispatch queue comes out — with no internet connection.

Layout::

    aria/
      config.py     every tunable, resolved from env + .env
      schemas.py    pydantic contracts shared by all layers
      domain/       pure logic: severity, heap key, escalation policy
      core/         infrastructure: errors, logging, heap, event bus
      utils/        small dependency-free helpers
      llm/          pluggable model backends (Ollama, OpenVINO, ONNX)
      agents/       the pipeline steps
      services/     stateful services and the composition root (hub)
      api/          FastAPI routes — thin adapters over the services
"""

from __future__ import annotations

import time

__version__ = "2.0.0"
__all__ = ["__version__", "STARTED_AT", "uptime_secs"]

#: Process start, used by the health endpoint.
STARTED_AT = time.time()


def uptime_secs() -> float:
    return round(time.time() - STARTED_AT, 1)
