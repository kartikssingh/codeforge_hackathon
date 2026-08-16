"""Deterministic triage engine — the offline safety net.

A 1B model on a shelter laptop will sometimes be unreachable, sometimes slow,
and occasionally will answer with prose where JSON was asked for.  None of those
are acceptable reasons to hand a volunteer nothing.

This engine matches the report against a curated rule set
(``data/triage_rules.json``), each rule tied to a real protocol document in
``data/protocols/``.  It is fast (microseconds), fully explainable ("matched:
*not breathing*, *collapsed*"), and produces exactly the same
:class:`~aria.schemas.Situation` shape the LLM path produces — so the UI, the
heap and the inventory checks cannot tell the difference.

It runs in one of two modes (``ARIA_TRIAGE_MERGE_RULES``):

* **merge** (default) — rule hypotheses are unioned with the LLM's, giving the
  manager a broader differential and catching the case where a small model
  fixates on one diagnosis.
* **fallback** — rules are used only when the LLM produced nothing usable.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from aria.config import settings
from aria.core.logging import get_logger
from aria.domain.enums import Severity
from aria.schemas import MaterialItem, Situation, SourceRef
from aria.utils.textutil import keyword_hits, normalise

log = get_logger("agents.rules")

#: A strong keyword is worth this many ordinary ones.
_STRONG_WEIGHT = 3
#: Confidence starts here on a single ordinary keyword and climbs with evidence.
_CONFIDENCE_BASE = 0.30
_CONFIDENCE_STEP = 0.09
_CONFIDENCE_CAP = 0.95
#: Bonus when RAG retrieved the very protocol this rule cites.
_PROTOCOL_BONUS = 0.05
_PROTOCOL_BONUS_CAP = 0.10
#: A rule needs either one strong keyword or two ordinary ones before it is
#: offered as a diagnosis.  One incidental word ("generator" in a sentence about
#: paperwork) is evidence of nothing, and a spurious option costs the manager
#: time they do not have.
_MIN_SCORE_FOR_SITUATION = 2


@dataclass(frozen=True)
class TriageRule:
    id: str
    label: str
    severity: Severity
    keywords: tuple[str, ...] = ()
    strong_keywords: tuple[str, ...] = ()
    travel_time_min: int = 10
    resolution_time_min: int = 20
    materials: tuple[dict[str, Any], ...] = ()
    instructions: tuple[str, ...] = ()
    protocols: tuple[str, ...] = ()
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["TriageRule"]:
        try:
            return cls(
                id=str(payload["id"]),
                label=str(payload["label"]),
                severity=Severity.from_any(payload.get("severity")),
                keywords=tuple(str(k) for k in payload.get("keywords", ())),
                strong_keywords=tuple(str(k) for k in payload.get("strong_keywords", ())),
                travel_time_min=int(payload.get("travel_time_min", settings.triage.default_travel_time_min)),
                resolution_time_min=int(
                    payload.get("resolution_time_min", settings.triage.default_resolution_time_min)
                ),
                materials=tuple(payload.get("materials", ())),
                instructions=tuple(str(s) for s in payload.get("instructions", ())),
                protocols=tuple(str(p) for p in payload.get("protocols", ())),
                notes=str(payload.get("notes", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed triage rule %r: %s", payload.get("id"), exc)
            return None


@dataclass
class RuleMatch:
    rule: TriageRule
    score: int
    matched: list[str] = field(default_factory=list)
    confidence: float = 0.0


class RuleEngine:
    """Loads the rule catalogue and scores reports against it."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path or settings.paths.triage_rules)
        self._rules: list[TriageRule] = []
        self._lock = threading.RLock()
        self.load()

    # ── Catalogue ─────────────────────────────────────────────────────────────

    def load(self) -> int:
        rules: list[TriageRule] = []
        if self._path.exists():
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                for entry in payload.get("rules", []):
                    rule = TriageRule.from_dict(entry)
                    if rule is not None:
                        rules.append(rule)
            except (OSError, json.JSONDecodeError) as exc:
                log.error("Could not read triage rules from %s: %s", self._path, exc)
        else:
            log.warning("Triage rule catalogue %s not found", self._path)

        with self._lock:
            self._rules = rules
        log.info("Loaded %d triage rule(s)", len(rules))
        return len(rules)

    @property
    def rules(self) -> list[TriageRule]:
        with self._lock:
            return list(self._rules)

    def __len__(self) -> int:
        with self._lock:
            return len(self._rules)

    # ── Matching ──────────────────────────────────────────────────────────────

    def match(
        self,
        text: str,
        *,
        retrieved_sources: Sequence[str] = (),
        limit: Optional[int] = None,
    ) -> list[RuleMatch]:
        """Score every rule against *text*, best first."""
        haystack = normalise(text)
        if not haystack:
            return []

        retrieved = {normalise(source) for source in retrieved_sources}
        matches: list[RuleMatch] = []

        for rule in self.rules:
            strong = keyword_hits(haystack, rule.strong_keywords)
            ordinary = keyword_hits(haystack, rule.keywords)
            score = len(strong) * _STRONG_WEIGHT + len(ordinary)
            if score == 0:
                continue

            confidence = min(_CONFIDENCE_CAP, _CONFIDENCE_BASE + _CONFIDENCE_STEP * score)
            bonus = 0.0
            for protocol in rule.protocols:
                needle = normalise(protocol)
                if any(needle and needle in source for source in retrieved):
                    bonus = min(_PROTOCOL_BONUS_CAP, bonus + _PROTOCOL_BONUS)
            confidence = min(_CONFIDENCE_CAP, confidence + bonus)

            matches.append(
                RuleMatch(
                    rule=rule,
                    score=score,
                    matched=strong + ordinary,
                    confidence=round(confidence, 2),
                )
            )

        # Most evidence first; ties go to the more severe rule, because
        # over-triage costs a wasted trip and under-triage costs a life.
        matches.sort(key=lambda m: (-m.score, m.rule.severity.rank))
        return matches[: limit or settings.triage.max_situations]

    def situations(
        self,
        text: str,
        *,
        retrieved_sources: Sequence[str] = (),
        limit: Optional[int] = None,
    ) -> list[Situation]:
        """Rule matches rendered as Situations the rest of the app understands."""
        out: list[Situation] = []
        for match in self.match(text, retrieved_sources=retrieved_sources, limit=limit):
            if match.score < _MIN_SCORE_FOR_SITUATION:
                continue
            if match.confidence < settings.triage.min_rule_confidence:
                continue
            rule = match.rule
            out.append(
                Situation(
                    label=rule.label,
                    severity=rule.severity,
                    confidence=match.confidence,
                    travel_time_min=rule.travel_time_min,
                    resolution_time_min=rule.resolution_time_min,
                    materials=[MaterialItem.model_validate(m) for m in rule.materials],
                    instructions=list(rule.instructions),
                    reasoning=(
                        f"Protocol rule '{rule.id}' matched: "
                        + ", ".join(f"“{hit}”" for hit in match.matched[:6])
                        + (f". {rule.notes}" if rule.notes else "")
                    ),
                    source_chunks=[SourceRef(source=p, page="—", score=match.confidence) for p in rule.protocols],
                    origin="rules",
                )
            )
        return out

    def hypotheses(self, text: str, limit: int = 6) -> dict[str, list[str]]:
        """Severity → candidate conditions, for the vagueness resolver."""
        buckets: dict[str, list[str]] = {}
        for match in self.match(text, limit=limit):
            buckets.setdefault(match.rule.severity.value, []).append(match.rule.label)
        return buckets


#: Process-wide engine.  Cheap to construct, but there is no reason to reload
#: the catalogue on every request.
rule_engine = RuleEngine()
