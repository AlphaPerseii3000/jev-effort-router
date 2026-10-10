"""The routing grid: the models Jev may choose between, and the effort vocabulary.

A Choice question's option list is load-bearing. Every extra option measurably dilutes the
decision, so the built-in grid is exactly the six models that were benchmarked for this
profile — see ``docs/routing-grid.md``. Operators can replace it through the ``grid``
setting, which is the supported way to add a model once it has evidence behind it.

**The descriptions below are the criteria strings sent to Jev verbatim, and they are sent in
English.** They are part of the measured payload, not documentation: translating them changes
what Jev weighs, which is why the grid is stated in one language and left alone. Rewording
them (or flipping the language back) is a behaviour change that needs its own measurement.

**A model id here is the string that goes on the wire, verbatim.** ``nemotron-3-nano`` sat in
this table while the provider's catalog named the tier ``nemotron-3-nano:30b``, so a perfectly
confident decision became ``HTTP 404: model ... not found`` and killed the turn. ``catalog.py``
now cross-checks the grid against the provider's own model list before a decision is acted on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Entry:
    """One choice on the grid."""

    #: The model id as it must appear in the provider's ``model`` field.
    model_id: str
    #: Human-readable profile line, sent to Jev as the criterion description.
    description: str

    @property
    def criterion(self) -> str:
        """The exact string Jev sees for this option."""
        return f"{self.model_id}: {self.description}" if self.description else self.model_id


#: The six benchmarked Ollama:cloud models, in the order they are offered to Jev.
#: The descriptions are data: they are sent to Jev verbatim as the criteria, in English.
#:
#: Every profile names a **task family**, never a superlative free of a task ("excellent value
#: for money", "excellent in real use"). Those lines were why `glm-5.3` and `glm-5.3-flash` were
#: almost never chosen: a superlative with no task attached reads as a safe pick on every prompt,
#: and the model carrying one (deepseek, first on the list) absorbed the decisions that belonged to
#: the GLMs. The measured effect of naming task families instead is in the changelog.
DEFAULT_GRID: Tuple[Entry, ...] = (
    Entry(
        "deepseek-v4.1-flash",
        "the usual choice for general work: everyday writing, explanation, summarising, "
        "ordinary coding and tool use; 1M context; cheap for its size",
    ),
    Entry(
        "kimi-k3",
        "strongest at complex code and long agentic tasks: multi-file refactors, deep "
        "debugging, large repositories; slow and the most expensive",
    ),
    Entry(
        "glm-5.3",
        "strongest at rigorous reasoning: mathematics, logic, science, quantitative and "
        "financial analysis, where a wrong answer is costly",
    ),
    Entry(
        "glm-5.3-flash",
        "best reasoning-per-cost on large text: drafting, summarising, translating and "
        "structured extraction over long documents; fast",
    ),
    Entry(
        "minimax-m3",
        "fast tool calling: long sequences of API/CLI actions, repetitive automation, "
        "high throughput",
    ),
    Entry(
        "nemotron-3-nano:30b",
        "highest throughput and lowest cost: trivial single-step requests only; weak at "
        "reasoning and at long context",
    ),
)


def parse_entry(raw: Any) -> Optional[Entry]:
    """Parse ``"model-id: description"`` (or a ``{model_id, description}`` mapping)."""
    if isinstance(raw, dict):
        model_id = str(raw.get("model_id") or raw.get("model") or raw.get("id") or "").strip()
        description = str(raw.get("description") or raw.get("profile") or "").strip()
        return Entry(model_id, description) if model_id else None
    text = str(raw or "").strip()
    if not text:
        return None
    model_id, separator, description = text.partition(":")
    model_id = model_id.strip()
    if not model_id:
        return None
    return Entry(model_id, description.strip() if separator else "")


def _parse_entries(raw: Any) -> List[Entry]:
    """The lenient entry list: drop unparseable items and duplicate ids, never fall back."""
    if raw is None:
        return []
    items: Iterable[Any]
    if isinstance(raw, str):
        items = [line for line in raw.splitlines() if line.strip()]
    elif isinstance(raw, Sequence):
        items = raw
    else:
        return []

    parsed: List[Entry] = []
    seen = set()
    for item in items:
        entry = parse_entry(item)
        if entry is None or entry.model_id in seen:
            continue
        seen.add(entry.model_id)
        parsed.append(entry)
    return parsed


def _looks_like_model_id(text: str) -> bool:
    """Whether ``text`` is a plausible model id (at least one alphanumeric character).

    ``parse_entry`` accepts any non-empty string as a bare model id, which is right for the
    lenient global-grid path. A per-surface policy has to tell a real bare id (``kimi-k3``)
    from nonsense (``"???"``), so strict parsing keeps only ids that could go on the wire.
    """
    return any(character.isalnum() for character in text)


def parse_grid(raw: Any, fallback: Tuple[Entry, ...] = DEFAULT_GRID) -> Tuple[Entry, ...]:
    """Resolve the effective grid, falling back to ``fallback`` on nonsense input.

    A partial or unparseable override is not a reason to route blindly: entries that do not
    parse are dropped, and an override that yields nothing at all falls back to ``fallback``
    (the built-in :data:`DEFAULT_GRID` unless a caller says otherwise).
    """
    parsed = _parse_entries(raw)
    return tuple(parsed) if parsed else fallback


def parse_grid_strict(raw: Any) -> Optional[Tuple[Entry, ...]]:
    """Parse a grid override, or ``None`` when nothing usable parsed.

    Stricter than :func:`parse_grid` two ways: an entry whose id is not a plausible model id
    is dropped, and an override that yields nothing at all is ``None`` rather than a fallback —
    which is what lets a per-surface policy keep the operator's global grid instead of the
    built-in six when its own grid is bad.
    """
    parsed = [entry for entry in _parse_entries(raw) if _looks_like_model_id(entry.model_id)]
    return tuple(parsed) if parsed else None


def criteria(grid: Sequence[Entry]) -> dict:
    """The ``criteria`` map for the model Choice question.

    Keyed by position (``"1"``, ``"2"``, ...) rather than by model id: Jev returns the
    criterion *key*, so a stable key decouples the answer from the description text and a
    description can be rewritten without silently changing which model is selected.
    """
    return {str(index): entry.criterion for index, entry in enumerate(grid, start=1)}


def resolve(criteria_map: dict, choice: Any, grid: Sequence[Entry]) -> Optional[Entry]:
    """Map Jev's ``choice`` back to a grid entry, or ``None`` when it is not on the grid.

    Accepts the positional key (the documented form), a ``"<index>: ..."`` key, or a raw
    model id: a decision endpoint that echoes the option text instead of the key must not
    turn into a silent misroute.
    """
    if choice is None:
        return None
    key = str(choice).strip()
    if not key:
        return None

    by_position = {str(index): entry for index, entry in enumerate(grid, start=1)}
    if key in by_position:
        return by_position[key]

    head = key.split(":", 1)[0].strip()
    if head in by_position:
        return by_position[head]
    for candidate in (key, head):
        for entry in grid:
            if entry.model_id == candidate:
                return entry

    # Last resort: the echoed option string matches a criterion we sent.
    for position, text in (criteria_map or {}).items():
        if text == key:
            return by_position.get(str(position))
    return None
