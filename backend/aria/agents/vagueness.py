"""Step 3b — vagueness resolver.

Real distress calls do not use clinical language.  "My neighbour uncle is not
moving and his legs look wrong" retrieves badly, because none of those words
appear in a first-aid manual.

When the top retrieval score falls below the confidence threshold, this agent
expands the report into concrete hypotheses — one set per severity level — and
retrieves again for each.  The merged, deduplicated chunk set is what triage
then reasons over, so a vague report still lands on the right protocol.

Hypotheses come from the LLM when one is available and from the rule engine's
keyword matches when it is not, which means the expansion step degrades
gracefully instead of disappearing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from aria.agents.retrieval import Chunk, RetrievalResult
from aria.agents.rules import RuleEngine, rule_engine as default_rule_engine
from aria.config import settings
from aria.core.errors import AgentUnavailableError
from aria.core.logging import get_logger
from aria.llm.base import LLMClient
from aria.utils.textutil import normalise

log = get_logger("agents.vagueness")

VAGUENESS_PROMPT = """You are a triage officer at a disaster relief shelter.
The report below is vague. Name the medical or situational conditions it could plausibly be.

REPORT: "{transcript}"

Give two or three candidates per severity level, using standard first-aid terminology.
Reply with JSON only:
{{
  "CRITICAL": ["cardiac arrest", "severe haemorrhage"],
  "HIGH": ["fracture with shock", "spinal injury"],
  "MEDIUM": ["dehydration", "diabetic episode"],
  "LOW": ["exhaustion", "minor wound"]
}}"""

#: Used when neither the LLM nor the rules produce anything: a broad sweep of
#: the most common shelter presentations, so retrieval still has something to
#: work with.
_GENERIC_HYPOTHESES: dict[str, list[str]] = {
    "CRITICAL": ["cardiac arrest", "severe bleeding"],
    "HIGH": ["fracture", "head injury"],
    "MEDIUM": ["dehydration", "infected wound"],
    "LOW": ["minor wound", "supply request"],
}

RetrieveFn = Callable[..., RetrievalResult]


@dataclass
class VaguenessOutcome:
    chunks: list[Chunk] = field(default_factory=list)
    hypotheses: dict[str, list[str]] = field(default_factory=dict)
    queries_run: int = 0
    used_llm: bool = False
    note: str = ""

    @property
    def flat_hypotheses(self) -> list[str]:
        return [item for values in self.hypotheses.values() for item in values]


def generate_hypotheses(
    transcript: str,
    llm: Optional[LLMClient],
    *,
    rules: Optional[RuleEngine] = None,
) -> tuple[dict[str, list[str]], bool, str]:
    """Return ``(hypotheses, used_llm, note)`` — never raises."""
    if llm is not None:
        try:
            raw = llm.complete(
                VAGUENESS_PROMPT.format(transcript=transcript),
                max_tokens=400,
                temperature=0.2,
            )
            parsed = _parse_hypotheses(raw)
            if parsed:
                return parsed, True, ""
            note = "The model's hypotheses could not be parsed."
        except AgentUnavailableError as exc:
            note = f"Language model unavailable: {exc.message}"
        log.info("Vagueness resolver falling back to rules: %s", note)
    else:
        note = "No language model configured."

    engine = rules or default_rule_engine
    from_rules = engine.hypotheses(transcript)
    if from_rules:
        return from_rules, False, note
    return dict(_GENERIC_HYPOTHESES), False, note


def _parse_hypotheses(raw: str) -> dict[str, list[str]]:
    import json

    start, end = raw.find("{"), raw.rfind("}")
    if not (0 <= start < end):
        return {}
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}

    cleaned: dict[str, list[str]] = {}
    for severity, values in payload.items():
        key = str(severity).strip().upper()
        if key not in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
            continue
        if isinstance(values, str):
            values = [values]
        conditions = [str(v).strip() for v in (values or []) if str(v).strip()]
        if conditions:
            cleaned[key] = conditions[:3]
    return cleaned


def resolve_and_retrieve(
    transcript: str,
    llm: Optional[LLMClient],
    retrieve_fn: RetrieveFn,
    *,
    rules: Optional[RuleEngine] = None,
    base_chunks: Sequence[Chunk] = (),
) -> VaguenessOutcome:
    """Expand a vague report and re-retrieve for each hypothesis."""
    hypotheses, used_llm, note = generate_hypotheses(transcript, llm, rules=rules)

    merged: list[Chunk] = list(base_chunks)
    seen: set[str] = {normalise(chunk.text)[:400] for chunk in merged}
    queries = 0
    budget = settings.rag.vagueness_max_queries

    # Walk severity-first so the critical hypotheses always get a query even
    # when the budget is small — missing a cardiac arrest costs more than
    # missing a supply request.
    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        for condition in hypotheses.get(severity, []):
            if queries >= budget:
                break
            result = retrieve_fn(
                f"{condition} first aid emergency treatment", settings.rag.vagueness_top_k
            )
            queries += 1
            for chunk in result.chunks:
                fingerprint = normalise(chunk.text)[:400]
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                chunk.hypothesis = condition
                merged.append(chunk)
        if queries >= budget:
            break

    merged.sort(key=lambda chunk: chunk.score, reverse=True)
    log.info(
        "Vagueness resolver ran %d quer(ies) → %d unique chunk(s)", queries, len(merged)
    )
    return VaguenessOutcome(
        chunks=merged[: settings.rag.max_merged_chunks],
        hypotheses=hypotheses,
        queries_run=queries,
        used_llm=used_llm,
        note=note,
    )
