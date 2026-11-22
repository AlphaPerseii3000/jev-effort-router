"""State sent to Jev — the two questions and the context built for them.

Both questions follow TypeSafe's guidance: narrow, atomic, explicit, with criteria that say
what each option means. The model question is asked first and the effort question second, but
they are evaluated **independently and in parallel** by the endpoint, so neither can be
conditioned on the other at the wire level; any coupling is the router's job in code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import EFFORT_LEVELS, redact
from .effort import DEFAULT_OFFERED_LEVELS
from .grid import Entry, criteria

MODEL_QUESTION_ID = "model_route"
EFFORT_QUESTION_ID = "reasoning_effort"

#: What ``model_choice`` reports when the grid carried a single model. With one option the model
#: question is not sent at all (a one-option Choice measures nothing and its answer would be a
#: certainty Jev did not compute), so the audit must say the model was fixed by the grid rather
#: than report a choice key the endpoint was never asked for.
SINGLE_MODEL_CHOICE = "single"

MODEL_INSTRUCTIONS = (
    "Which model is best suited to handle the user's request in `user_message`, given "
    "`recent_context`? Compare every option on the same axes — what the task actually needs, "
    "how much it costs, and how fast it is — and pick the single best fit."
)
EFFORT_INSTRUCTIONS = (
    "Pick the LOWEST reasoning effort that fully covers the work this turn will need "
    "(including the tool steps it implies). Raise the level for uncertainty, risk or "
    "irreversibility — never for message length or politeness. `previous_turn` and "
    "`last_tool_results_excerpt` show whether this is a continuation of ongoing work."
)

#: The four rungs and what each one covers. Concrete examples matter more than adjectives: the
#: measured failure mode of the three vague levels was mass splitting between neighbours and a
#: confidence below the router's threshold, which sent the turn to the host default instead.
EFFORT_CRITERIA: Dict[str, str] = {
    "low": (
        "Single command, lookup, status check, acknowledgement or mechanical edit with a known "
        "answer. E.g. 'show disk usage', 'restart X as agreed', 'ok, continue'."
    ),
    "medium": (
        "Bounded diagnosis combining 2-3 sources, or a localized edit/config change with known "
        "cause. E.g. 'why is unit X failing? check logs', 'fix this function', 'summarize these "
        "results'."
    ),
    "high": (
        "Unknown root cause, service/config change with rollback, multi-step plan, non-trivial "
        "debugging or design. E.g. 'gateway is slow since yesterday, find out why', 'plan "
        "migration with rollback'."
    ),
    "max": (
        "(only offered when the route accepts it) Irreversible or multi-system decision, live "
        "incident, security/data-loss risk, architecture with several interacting constraints."
    ),
}

#: Per-message cap on the text handed to Jev, to stay well inside the 32K context and keep
#: the state focused. The tail is what matters for routing.
MESSAGE_CHAR_LIMIT = 4000
CONTEXT_CHAR_LIMIT = 11000

#: The previous assistant turn's shape: how many tool calls it made and which tools it used.
#: The decision is taken once per turn, so a count of the *current* turn's calls is always zero;
#: what the previous turn did is the signal that actually exists at decision time.
PREVIOUS_TURN_TOOL_LIMIT = 8

#: A tool name is an identifier, not prose: truncating one keeps the payload budget honest even
#: when a plugin registers a pathologically long name.
PREVIOUS_TURN_TOOL_NAME_LIMIT = 64

#: Tail of the last tool results, redacted before it leaves the process. Tool output is what
#: tells an incognito decider whether a turn is mid-diagnosis or starting fresh.
TOOL_RESULTS_CHAR_LIMIT = 1500

#: How much of that output is examined at all. The excerpt is the last 1.500 characters, and
#: redaction has to see the text it is about to keep; a window keeps both the work and the
#: memory bounded no matter how large the turn's tool output was.
TOOL_RESULTS_RAW_LIMIT = 16_000

#: A limit large enough that :func:`redact` only scrubs, never truncates. Valid because the
#: window handed to it is already bounded by :data:`TOOL_RESULTS_RAW_LIMIT`.
_REDACT_UNCAPPED = 1_000_000

#: Appended to a truncated value; the truncated result still fits inside the limit.
_ELLIPSIS = " […]"

_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _normalise(text: Any) -> str:
    """Collapse whitespace and blank lines, without truncating."""
    collapsed = _WHITESPACE.sub(" ", str(text or "").replace("\r\n", "\n"))
    return _BLANK_LINES.sub("\n\n", collapsed).strip()


def _clean(text: Any, limit: int) -> str:
    collapsed = _normalise(text)
    if len(collapsed) > limit:
        return collapsed[: limit - len(_ELLIPSIS)] + _ELLIPSIS
    return collapsed


def _clean_tail(text: Any, limit: int) -> str:
    """The last ``limit`` characters of the cleaned text — what a truncation keeps for routing."""
    collapsed = _normalise(text)
    if len(collapsed) <= limit:
        return collapsed
    return "…" + collapsed[-(limit - 1):]


def flatten_content(content: Any) -> str:
    """Text out of a chat-completions ``content``, string or multimodal parts list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: List[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif part.get("type") == "image_url":
                    chunks.append("[image]")
        return "\n".join(chunk for chunk in chunks if chunk)
    if content is None:
        return ""
    return str(content)


def last_user_message(messages: Sequence[Dict[str, Any]]) -> str:
    """The final user turn's text — the thing being routed."""
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() == "user":
            return flatten_content(message.get("content"))
    return ""


def recent_context(messages: Sequence[Dict[str, Any]], turns: int) -> str:
    """A bounded tail of the conversation, oldest-first, excluding the final user message.

    Assistant reasoning and tool payloads are deliberately not forwarded: they are large, they
    are not what the routing question is about, and the endpoint's context is 32K.
    """
    if turns <= 0:
        return ""
    history = [message for message in (messages or []) if isinstance(message, dict)]

    # Drop the trailing user message (it is `user_message` on its own).
    for index in range(len(history) - 1, -1, -1):
        if str(history[index].get("role") or "").strip().lower() == "user":
            history = history[:index]
            break

    kept: List[str] = []
    user_turns = 0
    for message in reversed(history):
        role = str(message.get("role") or "").strip().lower()
        if role == "user":
            user_turns += 1
            if user_turns > turns:
                break
        elif role not in {"assistant", "system", "user"}:
            continue  # tool results and internal roles carry no routing signal
        text = _clean(flatten_content(message.get("content")), MESSAGE_CHAR_LIMIT)
        if not text:
            continue
        kept.append(f"{role}: {text}")
    kept.reverse()
    return _clean("\n".join(kept), CONTEXT_CHAR_LIMIT)


def _role(message: Dict[str, Any]) -> str:
    return str(message.get("role") or "").strip().lower()


def _tool_call_names(message: Dict[str, Any]) -> List[str]:
    """The names of a message's tool calls, one entry per call (``""`` when unnamed).

    The count is what the state reports, so an unnamed call still counts as a call; the name list
    only carries the ones that have a name.
    """
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    names: List[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        name = ""
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
        if not name:
            name = str(call.get("name") or "").strip()
        names.append(name[:PREVIOUS_TURN_TOOL_NAME_LIMIT])
    return names


def _tail_window(texts: Sequence[str], limit: int) -> str:
    """The last ``limit`` characters of ``texts`` joined, built from the end.

    Walking backwards avoids concatenating the whole turn's tool output just to keep its tail:
    the work and the memory stay bounded by ``limit`` (plus the last chunk), whatever the sizes.
    """
    chunks: List[str] = []
    remaining = limit
    for text in reversed(list(texts)):
        if remaining <= 0:
            break
        if len(text) >= remaining:
            chunks.append(text[-remaining:])
            remaining = 0
            break
        chunks.append(text)
        remaining -= len(text)
    chunks.reverse()
    return "".join(chunks)


def previous_turn_signal(messages: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """What the assistant's previous turn did, as far as the decider may see it.

    The decision is taken once per turn, on the first request, when the current turn has made no
    tool calls yet — so the only usable signal is the *previous* turn's shape: how many calls it
    made, which tools it used (at most :data:`PREVIOUS_TURN_TOOL_LIMIT` distinct names), and the
    tail of its last tool results. Reasoning stays out: it is large, private and not what the
    routing question is about.

    Both fields are omitted when there is nothing to report (a first turn has no previous
    assistant turn and no tool output), so the payload never carries empty scaffolding.
    """
    history = [message for message in (messages or []) if isinstance(message, dict)]

    # Drop the turn being routed, then keep what came after the user message before it: that
    # segment is exactly the assistant turn that answered the previous message, its tool calls
    # and their results.
    for index in range(len(history) - 1, -1, -1):
        if _role(history[index]) == "user":
            history = history[:index]
            break
    start = 0
    for index in range(len(history) - 1, -1, -1):
        if _role(history[index]) == "user":
            start = index + 1
            break
    else:
        # No user message in the history at all (a resumed or synthetic conversation): keep the
        # trailing assistant/tool run instead of reporting every turn the session ever had.
        for index, message in enumerate(history):
            if _role(message) not in {"assistant", "tool"}:
                start = index + 1
    segment = [
        message for message in history[start:] if _role(message) in {"assistant", "tool"}
    ]

    signal: Dict[str, Any] = {}

    assistant_messages = [message for message in segment if _role(message) == "assistant"]
    tool_messages = [message for message in segment if _role(message) == "tool"]
    names = [name for message in assistant_messages for name in _tool_call_names(message)]
    # The count is only reported when the calls are actually visible. The preview projections
    # (Responses wire, a host that stores results but not calls) drop them, and a zero there would
    # claim the previous turn used no tools when it may have used several.
    if names:
        tools: List[str] = []
        for name in names:
            if name and name not in tools:
                tools.append(name)
        signal["previous_turn"] = {
            "tool_calls": len(names),
            "tools": tools[:PREVIOUS_TURN_TOOL_LIMIT],
        }

    results = [flatten_content(message.get("content")) for message in tool_messages]
    joined = _tail_window([text for text in results if text], TOOL_RESULTS_RAW_LIMIT)
    if joined.strip():
        # Redact the window first, then keep the tail: the excerpt is a tail by contract, and a
        # truncation before redaction could cut a credential in half and hide it.
        excerpt = _clean_tail(redact(joined, limit=_REDACT_UNCAPPED), TOOL_RESULTS_CHAR_LIMIT)
        if excerpt:
            signal["last_tool_results_excerpt"] = excerpt

    return signal


def build_state(
    messages: Sequence[Dict[str, Any]],
    *,
    context_turns: int,
    platform: str = "",
    provider: str = "",
) -> Dict[str, Any]:
    """The ``state`` object: only the context the two questions need.

    Deliberately absent: any mention of the model currently configured. Sending it anchors the
    decision on that model — measured against the live endpoint, ``current_model`` flipped a
    code-debugging task from ``kimi-k3`` (option 2, 4/4 calls) to the configured
    ``deepseek-v4.1-flash`` (option 1, 4/4 calls). Jev is told what the task is, not what the
    operator happens to have set; that is the whole point of asking it.
    """
    state: Dict[str, Any] = {
        "user_message": _clean(last_user_message(messages), MESSAGE_CHAR_LIMIT),
        "recent_context": recent_context(messages, context_turns),
    }
    state.update(previous_turn_signal(messages))
    if platform:
        state["surface"] = platform
    if provider:
        state["provider"] = provider
    return state


def rubric_criteria(levels: Optional[Sequence[str]] = None) -> Dict[str, str]:
    """The criteria the effort question shows, limited to the offered levels.

    The built-in rubric is the only source: there is no operator override. ``levels`` is what the
    route can actually be given (see :func:`effort.offered_levels`), so a level the wire would
    clamp away is never presented as an option.
    """
    chosen = dict(EFFORT_CRITERIA)
    if levels is None:
        return chosen
    return {level: chosen[level] for level in levels if level in chosen}


def rubric_instructions() -> str:
    """The effort question's instructions: the built-in rule."""
    return EFFORT_INSTRUCTIONS


def build_questions(
    grid: Sequence[Entry],
    *,
    ask_model: Optional[bool] = None,
    levels: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """The Choice questions, as the endpoint expects them.

    ``ask_model=False`` omits the model question entirely. That is what a one-model grid calls
    for: a Choice over a single option has one possible answer and a confidence of 1.0 by
    construction, so asking it spends tokens and latency to learn nothing. ``None`` means the
    grid decides; a caller with a backend that always expects both questions (the legacy
    OpenRouter Decisions API) passes ``True`` explicitly. The effort question is always sent,
    listing only ``levels`` — the levels the route's effort map accepts.
    """
    if ask_model is None:
        ask_model = len(grid) > 1
    offered = tuple(levels) if levels else DEFAULT_OFFERED_LEVELS
    questions: Dict[str, Any] = {}
    if ask_model:
        questions[MODEL_QUESTION_ID] = {
            "type": "choice",
            "instructions": MODEL_INSTRUCTIONS,
            "criteria": criteria(grid),
        }
    questions[EFFORT_QUESTION_ID] = {
        "type": "choice",
        "instructions": rubric_instructions(),
        "criteria": rubric_criteria(offered),
    }
    return questions


def build_payload(
    messages: Sequence[Dict[str, Any]],
    grid: Sequence[Entry],
    *,
    jev_model: str,
    context_turns: int,
    platform: str = "",
    provider: str = "",
    ask_model: Optional[bool] = None,
    levels: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """The complete Decisions API request body."""
    return {
        "model": jev_model,
        "state": build_state(
            messages,
            context_turns=context_turns,
            platform=platform,
            provider=provider,
        ),
        "questions": build_questions(
            grid,
            ask_model=ask_model,
            levels=levels,
        ),
    }


def normalise_effort_choice(raw: Any) -> Optional[str]:
    """Coerce Jev's effort answer onto the vocabulary ``EFFORT_LEVELS``.

    Well-formed is not the same as *offered*: the question lists only the levels this route's
    ``effort_maps`` entry declares, so a level outside that set is still a level here. Gating it
    is the deciders' job, because only they hold the offered set (``_offered_wire_effort``), and
    ``effort.resolve_effort`` keeps the last word on whether an offered level goes on the wire —
    omitted for a vocabulary contract, clamped onto its bounds for a bounded one — with
    ``effort_requested`` recording what Jev asked for and the audit's ``effort_adjusted`` naming
    the clamp when one happened.
    """
    text = str(raw or "").strip().lower()
    return text if text in EFFORT_LEVELS else None
