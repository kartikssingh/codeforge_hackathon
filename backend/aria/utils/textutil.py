"""Text normalisation and matching.

``fuzzy_best_match`` prefers :mod:`rapidfuzz` when it is installed and falls back
to the standard library's :mod:`difflib`, so unit tests (and a stripped-down
field deployment) run with no third-party packages at all.

The matching policy matters more than it looks.  The previous implementation
used ``partial_ratio`` with a threshold of 55, which matched *any* short string
against *any* longer one — "kit" scored 100 against "First Aid Kit", and a
request for "gloves" could reserve "Glucose Tablets".  Reserving the wrong item
in a disaster shelter means a volunteer arrives without what they need, so the
default here is deliberately strict: exact match, then normalised match, then
token-aware fuzzy match above a high threshold, then *no match at all*.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Optional, Sequence

try:  # pragma: no cover - exercised implicitly by whichever branch is installed
    from rapidfuzz import fuzz as _rf_fuzz

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _rf_fuzz = None
    _HAS_RAPIDFUZZ = False


_APOSTROPHE_RE = re.compile(r"[’'`´]")
_PUNCT_RE = re.compile(r"[^a-z0-9\s]+")
_SPACE_RE = re.compile(r"\s+")


def normalise(text: object) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    Apostrophes are *deleted* rather than replaced by a space, so "can't
    breathe" and "cant breathe" both normalise to ``cant breathe`` and a single
    keyword matches however the dispatcher typed it.
    """
    raw = str(text or "")
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = _APOSTROPHE_RE.sub("", ascii_text.lower())
    cleaned = _PUNCT_RE.sub(" ", lowered)
    return _SPACE_RE.sub(" ", cleaned).strip()


# Words ending in these are not plurals ("shortness", "status", "crisis", "gas").
_NOT_PLURAL = ("ss", "us", "is", "as", "os")


def _singularise(word: str) -> str:
    """Strip a trailing plural/third-person 's' when it is safe to do so."""
    if len(word) >= 4 and word.endswith("s") and not word.endswith(_NOT_PLURAL):
        return word[:-1]
    return word


def stem_text(text: object) -> str:
    """Normalise and crudely singularise every word.

    Real reports say "she needs blankets" where a rule says "need blanket".
    Applying the same reduction to both sides catches that whole class of miss
    without a stemming library — and because both sides get the same treatment,
    it cannot introduce a mismatch that exact matching would have found.
    """
    return " ".join(_singularise(word) for word in normalise(text).split())


def _score(a: str, b: str) -> float:
    """Token-aware similarity in 0-100."""
    if _HAS_RAPIDFUZZ:
        return float(_rf_fuzz.token_sort_ratio(a, b))
    return SequenceMatcher(None, a, b).ratio() * 100.0


def similarity(a: object, b: object) -> float:
    return _score(normalise(a), normalise(b))


def fuzzy_best_match(
    query: object,
    candidates: Sequence[str],
    *,
    min_score: int = 82,
) -> Optional[tuple[int, str, float]]:
    """Return ``(index, candidate, score)`` for the best match, or None.

    Resolution order — exact, normalised-exact, containment of a full token
    sequence, then fuzzy above *min_score*.  Ties resolve to the shortest
    candidate, which favours "Bandage Roll" over "Bandage Roll Elastic Wide"
    when the query was just "bandage roll".
    """
    if not candidates:
        return None

    raw_query = str(query or "").strip()
    if not raw_query:
        return None

    for index, candidate in enumerate(candidates):
        if candidate == raw_query:
            return index, candidate, 100.0

    norm_query = normalise(raw_query)
    if not norm_query:
        return None

    norm_candidates = [normalise(c) for c in candidates]

    exact = [i for i, c in enumerate(norm_candidates) if c == norm_query]
    if exact:
        best = min(exact, key=lambda i: len(candidates[i]))
        return best, candidates[best], 100.0

    # Whole-phrase containment in either direction ("aed" ⊂ "aed defibrillator").
    contained = [
        i
        for i, c in enumerate(norm_candidates)
        if c and (f" {norm_query} " in f" {c} " or f" {c} " in f" {norm_query} ")
    ]
    if contained:
        best = min(contained, key=lambda i: len(candidates[i]))
        return best, candidates[best], 95.0

    scored = [(_score(norm_query, c), i) for i, c in enumerate(norm_candidates)]
    score, index = max(scored, key=lambda pair: (pair[0], -len(candidates[pair[1]])))
    if score < min_score:
        return None
    return index, candidates[index], score


def keyword_hits(text: object, keywords: Iterable[str]) -> list[str]:
    """Which *keywords* appear in *text* as whole words or phrases.

    Matching is done on normalised text with space padding, so "cpr" matches
    "start cpr now" but not "cprogram", and multi-word keys like "not breathing"
    match as a phrase.

    A keyword may also be a conjunction written with ``+``: ``"face+drooping"``
    matches when both parts appear anywhere in the report, in any order.  Real
    reports say "her face is drooping" as often as "face drooping", and a strict
    phrase match misses half of them.

    Both sides are singularised (see :func:`stem_text`), so "needs blankets"
    matches the keyword "need blanket".
    """
    haystack = f" {stem_text(text)} "
    found: list[str] = []
    for keyword in keywords:
        if "+" in str(keyword):
            parts = [stem_text(part) for part in str(keyword).split("+")]
            if all(part and f" {part} " in haystack for part in parts):
                found.append(keyword)
            continue
        needle = stem_text(keyword)
        if needle and f" {needle} " in haystack:
            found.append(keyword)
    return found


def truncate(text: object, limit: int = 160) -> str:
    """Shorten for log lines without splitting mid-word where avoidable."""
    raw = str(text or "").strip()
    if len(raw) <= limit:
        return raw
    cut = raw[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"
