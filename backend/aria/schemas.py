"""Pydantic data contracts — the single source of truth for every payload.

Rules of the house:

* Routers and services never invent an inline dict shape; they import from here.
* Anything that crosses the LLM boundary goes through a ``coerce`` classmethod,
  because a 1B model *will* return ``"severity": "critical!"`` or a string where
  an int belongs, and a 500 from a triage endpoint costs someone their life.
* Timestamps are timezone-aware datetimes.  They serialise to ISO-8601 with an
  offset, which ``new Date(...)`` in the renderer parses natively — that is what
  makes the countdown timers correct across a midnight rollover.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from aria.domain.enums import RequestStatus, Severity, VolunteerStatus
from aria.utils.timeutil import now

# ─────────────────────────────────────────────────────────────────────────────
# Core domain objects
# ─────────────────────────────────────────────────────────────────────────────


class MaterialItem(BaseModel):
    """One supply or piece of equipment a situation calls for."""

    model_config = ConfigDict(populate_by_name=True)

    item: str
    quantity: int = Field(default=1, ge=0)
    # Filled in by the logistics agent against live inventory.
    available: bool = False
    available_qty: int = 0
    bin: str = "?"
    #: Canonical inventory name this line matched, when it differs from ``item``.
    matched_item: Optional[str] = None

    @field_validator("item", mode="before")
    @classmethod
    def _clean_item(cls, value: Any) -> str:
        return str(value or "").strip() or "Unspecified item"

    @field_validator("quantity", mode="before")
    @classmethod
    def _coerce_quantity(cls, value: Any) -> int:
        try:
            qty = int(float(value))
        except (TypeError, ValueError):
            return 1
        return max(0, min(qty, 999))


class SourceRef(BaseModel):
    """A citation back into the offline protocol library."""

    source: str
    page: str = "?"
    score: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.source} p.{self.page}"


class Situation(BaseModel):
    """One plausible emergency scenario for a single distress report.

    A request carries several of these; the shelter manager confirms one or more
    before anything is dispatched or reserved.
    """

    model_config = ConfigDict(use_enum_values=False)

    label: str
    severity: Severity = Severity.HIGH
    severity_score: int = 75
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    travel_time_min: int = Field(default=10, ge=0, le=600)
    resolution_time_min: int = Field(default=20, ge=0, le=600)
    heap_key: float = 0.0
    materials: list[MaterialItem] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    reasoning: str = ""
    source_chunks: list[SourceRef] = Field(default_factory=list)
    #: Which engine produced this hypothesis — "llm", "rules", "manual" or
    #: "fallback".  Surfaced in the UI so the manager knows what they are reading.
    origin: str = "llm"
    selected: bool = False

    @field_validator("label", mode="before")
    @classmethod
    def _clean_label(cls, value: Any) -> str:
        return str(value or "").strip() or "Unspecified emergency"

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value: Any) -> Severity:
        return Severity.from_any(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: Any) -> float:
        try:
            conf = float(value)
        except (TypeError, ValueError):
            return 0.5
        # Models sometimes answer 91 when they mean 0.91.
        if conf > 1.0:
            conf = conf / 100.0
        return min(max(conf, 0.0), 1.0)

    @field_validator("instructions", mode="before")
    @classmethod
    def _coerce_instructions(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [line for line in value.splitlines() if line.strip()]
        return [str(step).strip() for step in value if str(step).strip()]

    @field_validator("source_chunks", mode="before")
    @classmethod
    def _coerce_sources(cls, value: Any) -> list[Any]:
        """Accept both the structured form and bare ``"file.pdf p.4"`` strings."""
        if not value:
            return []
        out: list[Any] = []
        for entry in value:
            if isinstance(entry, str):
                source, _, page = entry.partition(" p.")
                out.append({"source": source.strip(), "page": (page or "?").strip()})
            else:
                out.append(entry)
        return out

    def model_post_init(self, _context: Any) -> None:
        # Keep the numeric score consistent with the label; the label wins,
        # because that is what the human in the loop actually reads.
        self.severity_score = self.severity.score

    @classmethod
    def coerce(cls, raw: dict[str, Any]) -> "Situation":
        """Build a Situation from untrusted (LLM) output, never raising."""
        if not isinstance(raw, dict):
            return cls(label="Unspecified emergency", origin="fallback")
        payload = dict(raw)
        # Tolerate the handful of alternative key names small models emit.
        for alias, canonical in (
            ("name", "label"),
            ("condition", "label"),
            ("situation", "label"),
            ("steps", "instructions"),
            ("actions", "instructions"),
            ("items", "materials"),
            ("equipment", "materials"),
            ("travel_time", "travel_time_min"),
            ("resolution_time", "resolution_time_min"),
        ):
            if canonical not in payload and alias in payload:
                payload[canonical] = payload.pop(alias)
        materials = payload.get("materials") or []
        if isinstance(materials, str):
            materials = [m.strip() for m in materials.split(",") if m.strip()]
        payload["materials"] = [
            {"item": m, "quantity": 1} if isinstance(m, str) else m for m in materials
        ]
        try:
            return cls.model_validate(payload)
        except Exception:  # noqa: BLE001 — never let bad model output 500 a triage
            return cls(
                label=str(payload.get("label") or "Unspecified emergency"),
                origin="fallback",
                reasoning="Situation could not be parsed and was rebuilt defensively.",
            )


class HandoffLog(BaseModel):
    """One agent-to-agent transition, shown as the explainability timeline."""

    step: str
    from_agent: str = ""
    to_agent: str = ""
    reason: str = ""
    at: datetime = Field(default_factory=now)
    duration_ms: Optional[int] = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ItemMovement(BaseModel):
    """A quantity of one item moving in or out of stock."""

    item: str
    quantity: int = Field(ge=0)


class EmergencyRequest(BaseModel):
    """Full request lifecycle object — created by intake, closed on return."""

    request_id: str
    created_at: datetime = Field(default_factory=now)
    transcript: str = ""
    intake_mode: str = "audio"  # audio | text | override
    is_vague: bool = False
    retrieval_top_score: float = 0.0
    situations: list[Situation] = Field(default_factory=list)

    status: RequestStatus = RequestStatus.AWAITING_REVIEW
    severity: Severity = Severity.HIGH
    heap_key: float = 0.0
    escalation_stage: int = 0
    promoted_at: Optional[datetime] = None

    approved_at: Optional[datetime] = None
    assigned_volunteer: Optional[str] = None
    assigned_at: Optional[datetime] = None
    expected_return: Optional[datetime] = None
    actual_return: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    closed_reason: Optional[str] = None

    items_taken: list[ItemMovement] = Field(default_factory=list)
    items_returned: list[ItemMovement] = Field(default_factory=list)
    items_consumed: list[ItemMovement] = Field(default_factory=list)

    handoff_logs: list[HandoffLog] = Field(default_factory=list)
    degraded: bool = False
    notes: list[str] = Field(default_factory=list)

    # ── Derived helpers (not stored, computed on demand) ──────────────────────

    @property
    def selected_situations(self) -> list[Situation]:
        chosen = [s for s in self.situations if s.selected]
        return chosen or self.situations[:1]

    @property
    def primary(self) -> Optional[Situation]:
        chosen = self.selected_situations
        return chosen[0] if chosen else None

    @computed_field  # serialised, so the UI never re-derives the headline itself
    @property
    def summary(self) -> str:
        primary = self.primary
        return primary.label if primary else "Emergency request"

    def waited_minutes(self, at: Optional[datetime] = None) -> float:
        reference = at or now()
        return max(0.0, (reference - self.created_at).total_seconds() / 60.0)


class Volunteer(BaseModel):
    """Live state of one volunteer on the roster."""

    volunteer_id: str
    name: str = ""
    status: VolunteerStatus = VolunteerStatus.AVAILABLE
    request_id: Optional[str] = None
    request_summary: Optional[str] = None
    assigned_at: Optional[datetime] = None
    expected_return: Optional[datetime] = None
    items_taken: list[ItemMovement] = Field(default_factory=list)
    missions_completed: int = 0
    on_roster_since: datetime = Field(default_factory=now)

    @property
    def is_free(self) -> bool:
        return self.status == VolunteerStatus.AVAILABLE


class InventoryRow(BaseModel):
    """One line of ``data/inventory.csv``, in API (snake_case) form."""

    item: str
    available: int = 0
    reserved: int = 0
    total: int = 0
    bin: str = ""
    category: str = ""

    @property
    def committed(self) -> int:
        """Units physically present: what is on the shelf plus what is held."""
        return self.available + self.reserved

    @property
    def capacity_pct(self) -> int:
        return round(self.available / self.total * 100) if self.total > 0 else 0

    def as_api(self, low_stock_threshold: float) -> dict[str, Any]:
        pct = self.capacity_pct
        if self.total <= 0:
            status = "UNTRACKED"
        elif self.available == 0 and self.reserved > 0:
            status = "ALL_RESERVED"
        elif self.available == 0:
            status = "OUT_OF_STOCK"
        elif pct <= low_stock_threshold * 100:
            status = "LOW"
        else:
            status = "OK"
        return {
            **self.model_dump(),
            "committed": self.committed,
            "capacity_pct": pct,
            "status": status,
        }


class InventoryMovementLog(BaseModel):
    """Audit line for every stock change — reserve, release, consume, refill."""

    at: datetime = Field(default_factory=now)
    action: str
    item: str
    quantity: int
    request_id: Optional[str] = None
    note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# API request bodies
# ─────────────────────────────────────────────────────────────────────────────


class AudioIntakeRequest(BaseModel):
    """POST /pipeline — incoming distress audio as base64."""

    audio_b64: str = Field(min_length=16)
    filename: Optional[str] = None
    npu_mode: bool = False


class TextIntakeRequest(BaseModel):
    """POST /pipeline/text — dispatcher types the report (radio, runner, SMS)."""

    text: str = Field(min_length=3, max_length=8000)
    npu_mode: bool = False

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Report text cannot be empty")
        return cleaned


class MaterialSelection(BaseModel):
    """A manager-adjusted quantity for one material line."""

    item: str
    quantity: int = Field(ge=0, le=999)


class ApproveRequest(BaseModel):
    """POST /requests/{id}/approve — confirm situations and reserve stock."""

    selected_indices: list[int] = Field(default_factory=list)
    #: Optional per-item overrides applied on top of the situations' materials.
    material_overrides: list[MaterialSelection] = Field(default_factory=list)
    note: str = ""


class OverrideRequest(BaseModel):
    """POST /requests/{id}/override — manager replaces the AI assessment."""

    condition: str = Field(min_length=1, max_length=200)
    severity: Severity = Severity.HIGH
    resources: list[MaterialSelection] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    travel_time_min: int = Field(default=10, ge=0, le=600)
    resolution_time_min: int = Field(default=20, ge=0, le=600)
    notes: str = ""


class CancelRequest(BaseModel):
    """POST /requests/{id}/cancel — false alarm, duplicate, resolved elsewhere."""

    reason: str = Field(default="Cancelled by shelter manager", max_length=300)


class VolunteerReturnRequest(BaseModel):
    """POST /volunteers/{id}/return — back at base with whatever came back."""

    returned_items: list[ItemMovement] = Field(default_factory=list)
    note: str = ""


class VolunteerCountRequest(BaseModel):
    """POST /volunteers/count — resize the roster."""

    count: int = Field(ge=0, le=200)


class VolunteerCreateRequest(BaseModel):
    """POST /volunteers — add a named volunteer to the roster."""

    name: str = Field(default="", max_length=80)


class VolunteerStatusRequest(BaseModel):
    """PATCH /volunteers/{id} — take someone off shift without losing them."""

    status: VolunteerStatus


class InventoryRefillRequest(BaseModel):
    """POST /inventory/refill — daily reset or top up what dipped below 60 %."""

    mode: str = Field(default="partial", pattern="^(partial|daily)$")


class InventoryUpdateRequest(BaseModel):
    """POST /inventory/{item}/stock — add units to an existing item."""

    quantity: int = Field(ge=1, le=10000)


class InventoryCreateRequest(BaseModel):
    """POST /inventory — register a new item with a capacity."""

    item: str = Field(min_length=1, max_length=80)
    capacity: int = Field(ge=1, le=100000)
    bin: str = Field(default="NEW", max_length=32)
    category: str = Field(default="General", max_length=40)


# ─────────────────────────────────────────────────────────────────────────────
# API response shapes
# ─────────────────────────────────────────────────────────────────────────────


class IntakeResponse(BaseModel):
    """What the renderer gets back from either intake endpoint."""

    request: EmergencyRequest
    timings_ms: dict[str, int] = Field(default_factory=dict)
    degraded: bool = False
    notes: list[str] = Field(default_factory=list)


class BoardResponse(BaseModel):
    """The whole live board — returned by every mutating endpoint.

    One round trip instead of three keeps the dashboard consistent: the queue,
    the roster and the stock levels always describe the same instant.
    """

    queue: list[EmergencyRequest] = Field(default_factory=list)
    volunteers: list[Volunteer] = Field(default_factory=list)
    inventory: list[dict[str, Any]] = Field(default_factory=list)
    buffer: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ActionResponse(BaseModel):
    """Envelope for every mutating endpoint.

    Returning the whole board with the mutation means the renderer never has to
    re-fetch three endpoints and briefly show a state that never existed.
    """

    request: Optional[EmergencyRequest] = None
    board: BoardResponse
    detail: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = ""
    uptime_secs: float = 0.0


class ComponentHealth(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class HealthDetailResponse(BaseModel):
    status: str
    version: str
    uptime_secs: float
    components: list[ComponentHealth] = Field(default_factory=list)
