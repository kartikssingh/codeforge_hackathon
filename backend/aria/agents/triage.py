"""Step 4 — triage.

Turns a transcript plus retrieved protocol passages into a ranked differential:
several plausible situations, each with a severity, the supplies it needs, the
steps a volunteer should follow, and citations back to the source document.

Two engines feed it:

* the LLM, prompted for strict JSON, and
* the deterministic :mod:`aria.agents.rules` engine.

Their outputs are merged and de-duplicated.  If the LLM is missing, slow or
returns garbage, the rule engine alone still produces a usable, cited
differential — the property that makes this system deployable on a laptop with
no models installed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from aria.agents.retrieval import Chunk
from aria.agents.rules import RuleEngine, rule_engine as default_rule_engine
from aria.config import settings
from aria.core.errors import AgentUnavailableError
from aria.core.logging import get_logger
from aria.domain.priority import compute_heap_key
from aria.llm.base import LLMClient
from aria.schemas import Situation, SourceRef
from aria.utils.textutil import normalise, similarity, truncate

log = get_logger("agents.triage")

TRIAGE_PROMPT = """You are the triage officer at a disaster relief shelter.
A distress report has come in and the relevant first-aid protocols have been retrieved for you.

REPORT: "{transcript}"

RETRIEVED PROTOCOLS:
{chunks_text}

List every plausible situation (between 1 and {max_situations}), most likely first.
For each one give:
- label: short name of the condition
- severity: CRITICAL, HIGH, MEDIUM or LOW
- travel_time_min: whole minutes for a volunteer to reach the person
- resolution_time_min: whole minutes to deal with it on site
- confidence: 0.0 to 1.0
- materials: list of {{"item": string, "quantity": integer}} taken from the protocols
- instructions: ordered, concrete steps the volunteer performs on arrival
- reasoning: one sentence on why this severity

Reply with a JSON array and nothing else. No prose, no markdown fences.
[
  {{
    "label": "Cardiac arrest",
    "severity": "CRITICAL",
    "travel_time_min": 6,
    "resolution_time_min": 25,
    "confidence": 0.91,
    "materials": [{{"item": "AED", "quantity": 1}}, {{"item": "CPR Mask", "quantity": 1}}],
    "instructions": ["Check response and breathing", "Start CPR: 30 compressions, 2 breaths"],
    "reasoning": "Unresponsive and not breathing normally"
  }}
]"""

_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


@dataclass
class TriageOutcome:
    situations: list[Situation] = field(default_factory=list)
    used_llm: bool = False
    used_rules: bool = False
    degraded: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def origin_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for situation in self.situations:
            counts[situation.origin] = counts.get(situation.origin, 0) + 1
        return counts


# ── JSON extraction ───────────────────────────────────────────────────────────


def extract_json_objects(raw: str) -> list[dict[str, Any]]:
    """Pull situation dicts out of whatever the model actually said.

    Small models routinely wrap JSON in prose, emit markdown fences, or return
    several arrays in a row.  A bracket-depth scan handles all of those without
    the catastrophic backtracking a regex would hit on a long reply.
    """
    if not raw:
        return []
    cleaned = _FENCE_RE.sub("", raw).strip()

    # Fast path: one well-formed array.
    start, end = cleaned.find("["), cleaned.rfind("]")
    if 0 <= start < end:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
                return parsed
        except json.JSONDecodeError:
            pass

    # Slow path: scan for every balanced [...] or {...} span and keep what parses.
    found: list[dict[str, Any]] = []
    for opener, closer in (("[", "]"), ("{", "}")):
        index = 0
        while index < len(cleaned):
            if cleaned[index] != opener:
                index += 1
                continue
            depth, begin, in_string, escaped = 0, index, False, False
            while index < len(cleaned):
                char = cleaned[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        fragment = cleaned[begin : index + 1]
                        try:
                            parsed = json.loads(fragment)
                        except json.JSONDecodeError:
                            log.debug("Discarded unparseable fragment: %s", truncate(fragment, 120))
                        else:
                            if isinstance(parsed, list):
                                found.extend(i for i in parsed if isinstance(i, dict))
                            elif isinstance(parsed, dict):
                                found.append(parsed)
                        break
                index += 1
            index += 1
        if found:
            break
    return found


# ── LLM path ──────────────────────────────────────────────────────────────────


def _format_chunks(chunks: Sequence[Chunk]) -> str:
    if not chunks:
        return "(no protocol context available)"
    return "\n\n".join(
        f"[Source: {chunk.source} p.{chunk.page} | relevance {chunk.score:.2f}]\n{chunk.text}"
        for chunk in chunks
    )


def _build_prompt(transcript: str, chunks: Sequence[Chunk]) -> str:
    return TRIAGE_PROMPT.format(
        transcript=transcript,
        chunks_text=_format_chunks(chunks),
        max_situations=settings.triage.max_situations,
    )


def _llm_situations(transcript: str, chunks: Sequence[Chunk], llm: LLMClient) -> list[Situation]:
    """Ask the model, trimming context until the prompt actually fits."""
    from aria.llm.base import MIN_GENERATION_TOKENS  # local import avoids a cycle

    available = list(chunks[:6])
    prompt = _build_prompt(transcript, available)
    while available and llm.budget(prompt) < MIN_GENERATION_TOKENS:
        available.pop()
        prompt = _build_prompt(transcript, available)
        log.debug("Prompt too large — retrying with %d chunk(s)", len(available))

    raw = llm.complete(prompt)  # raises AgentUnavailableError on failure
    parsed = extract_json_objects(raw)
    if not parsed:
        log.warning("LLM returned no parseable situations: %s", truncate(raw, 200))
        return []

    situations = [Situation.coerce(item) for item in parsed]
    citations = [
        SourceRef(source=chunk.source, page=chunk.page, score=chunk.score)
        for chunk in available[:3]
    ]
    for situation in situations:
        situation.origin = "llm"
        if not situation.source_chunks:
            situation.source_chunks = citations
    return situations


# ── Merge ─────────────────────────────────────────────────────────────────────


#: Labels this similar describe the same condition ("Cardiac arrest" vs
#: "Cardiac arrest / unresponsive casualty"), and listing both as separate
#: options makes the manager's decision harder rather than better informed.
_LABEL_MATCH_THRESHOLD = 72.0


def _dedupe_key(label: str, merged: dict[str, Situation]) -> str:
    """Key *label* onto an existing entry when they name the same condition."""
    key = normalise(label)
    if not key:
        return ""
    if key in merged:
        return key
    for existing in merged:
        # Substring either way catches the common "X" vs "X / detail" pattern.
        if f" {key} " in f" {existing} " or f" {existing} " in f" {key} ":
            return existing
        if similarity(key, existing) >= _LABEL_MATCH_THRESHOLD:
            return existing
    return key


def merge_situations(*groups: Sequence[Situation]) -> list[Situation]:
    """Union several differentials, keeping the best version of each condition.

    Deduplication is by normalised label.  When both engines propose the same
    condition the more confident one wins, but it inherits the other's materials
    and citations — the rule engine knows the shelf contents, the LLM reads the
    specific report, and the merged entry benefits from both.
    """
    merged: dict[str, Situation] = {}
    for group in groups:
        for situation in group:
            key = _dedupe_key(situation.label, merged)
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = situation
                continue
            winner, loser = (
                (existing, situation)
                if existing.confidence >= situation.confidence
                else (situation, existing)
            )
            if not winner.materials:
                winner.materials = loser.materials
            if not winner.instructions:
                winner.instructions = loser.instructions
            if not winner.source_chunks:
                winner.source_chunks = loser.source_chunks
            if winner.origin != loser.origin:
                winner.origin = "llm+rules"
                winner.reasoning = (
                    f"{winner.reasoning} Corroborated by the {loser.origin} engine."
                ).strip()
            # Never let a merge lower the assessed severity.
            if loser.severity.rank < winner.severity.rank:
                winner.severity = loser.severity
                winner.severity_score = loser.severity.score
            merged[key] = winner

    ranked = sorted(merged.values(), key=lambda s: (s.severity.rank, -s.confidence))
    return ranked[: settings.triage.max_situations]


def _fallback_situation(chunks: Sequence[Chunk]) -> Situation:
    """Last resort: cite the closest protocol and tell the volunteer to assess.

    Deliberately HIGH — with no information at all, assuming the worst that can
    still be handled by a dispatched volunteer is the safe default.
    """
    if not chunks:
        return Situation(
            label="Unclassified emergency",
            severity="HIGH",
            confidence=0.3,
            instructions=[
                "Attend the reported location and assess the casualty on arrival.",
                "Radio base with what you find before starting treatment.",
                "Take the general first-aid kit and a thermal blanket.",
            ],
            reasoning="No model and no protocol match were available; dispatched for human assessment.",
            origin="fallback",
        )

    top = chunks[0]
    label = re.sub(r"\.pdf$", "", top.source, flags=re.IGNORECASE)
    label = re.sub(r"^[A-Z]+-\d+_?", "", label).replace("_", " ").replace("-", " ").strip()
    steps = [
        re.sub(r"^(\d+[.)]\s*|[-•]\s*)", "", line).strip()
        for line in top.text.splitlines()
        if re.match(r"^\s*(\d+[.)]\s|[-•]\s)", line)
    ]
    if not steps:
        steps = [
            sentence.strip() + "."
            for sentence in re.split(r"[.!]\s+", top.text)
            if len(sentence.strip()) > 15
        ][:5]

    return Situation(
        label=(label.title() or "Protocol match"),
        severity="HIGH",
        confidence=min(0.6, max(0.3, top.score)),
        instructions=steps[:6] or ["Follow the cited protocol and assess on arrival."],
        reasoning=f"Derived from the closest protocol match ({top.label}, relevance {top.score:.2f}).",
        source_chunks=[SourceRef(source=top.source, page=top.page, score=top.score)],
        origin="fallback",
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def run_triage(
    transcript: str,
    chunks: Sequence[Chunk],
    llm: Optional[LLMClient] = None,
    *,
    rules: Optional[RuleEngine] = None,
) -> TriageOutcome:
    """Produce the differential for one report.  Never raises."""
    engine = rules or default_rule_engine
    outcome = TriageOutcome()

    rule_situations: list[Situation] = []
    if settings.triage.rules_enabled and len(engine):
        rule_situations = engine.situations(
            transcript, retrieved_sources=[chunk.source for chunk in chunks]
        )
        outcome.used_rules = bool(rule_situations)

    llm_situations: list[Situation] = []
    if llm is not None:
        try:
            llm_situations = _llm_situations(transcript, chunks, llm)
            outcome.used_llm = bool(llm_situations)
            if not llm_situations:
                outcome.notes.append("The language model returned no usable situations.")
        except AgentUnavailableError as exc:
            outcome.degraded = True
            outcome.notes.append(f"Language model unavailable: {exc.message}")
            log.warning("Triage LLM unavailable: %s", exc.message)
    else:
        outcome.degraded = True
        outcome.notes.append("No language model configured — used the protocol rule engine.")

    if settings.triage.merge_rules_with_llm:
        situations = merge_situations(llm_situations, rule_situations)
    else:
        situations = merge_situations(llm_situations or rule_situations)

    if not situations:
        situations = [_fallback_situation(chunks)]
        outcome.degraded = True
        outcome.notes.append("Fell back to a protocol-derived assessment.")

    for situation in situations:
        situation.heap_key = compute_heap_key(
            situation.severity.score,
            situation.travel_time_min,
            situation.resolution_time_min,
            scale_factor=settings.queue.scale_factor,
        )

    outcome.situations = situations
    log.info(
        "Triage produced %d situation(s) %s",
        len(situations),
        outcome.origin_counts,
    )
    return outcome
