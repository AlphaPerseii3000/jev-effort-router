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

from .config import EFFORT_LEVELS
from .grid import Entry, criteria

MODEL_QUESTION_ID = "model_route"
EFFORT_QUESTION_ID = "reasoning_effort"

MODEL_INSTRUCTIONS = (
    "Quel modèle est le plus adapté pour traiter la demande de l'utilisateur décrite dans "
    "`user_message`, compte tenu de `recent_context` ?"
)
EFFORT_INSTRUCTIONS = (
    "Quel niveau d'effort de raisonnement la demande décrite dans `user_message` nécessite-t-elle ? "
    "Un effort élevé coûte plus cher et est plus lent : ne le choisir que si la demande exige "
    "réellement une analyse profonde."
)

EFFORT_CRITERIA: Dict[str, str] = {
    "low": "Demande simple, factuelle ou mécanique : réponse courte, pas d'analyse nécessaire.",
    "medium": "Demande courante nécessitant un peu de raisonnement ou de synthèse.",
    "high": "Demande complexe : analyse profonde, débogage difficile, conception ou planification.",
}

#: Per-message cap on the text handed to Jev, to stay well inside the 32K context and keep
#: the state focused. The tail is what matters for routing.
MESSAGE_CHAR_LIMIT = 4000
CONTEXT_CHAR_LIMIT = 12000

#: Appended to a truncated value; the truncated result still fits inside the limit.
_ELLIPSIS = " […]"

_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _clean(text: Any, limit: int) -> str:
    collapsed = _WHITESPACE.sub(" ", str(text or "").replace("\r\n", "\n"))
    collapsed = _BLANK_LINES.sub("\n\n", collapsed).strip()
    if len(collapsed) > limit:
        return collapsed[: limit - len(_ELLIPSIS)] + _ELLIPSIS
    return collapsed


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
    if platform:
        state["surface"] = platform
    if provider:
        state["provider"] = provider
    return state


def build_questions(grid: Sequence[Entry]) -> Dict[str, Any]:
    """The two Choice questions, as the endpoint expects them."""
    return {
        MODEL_QUESTION_ID: {
            "type": "choice",
            "instructions": MODEL_INSTRUCTIONS,
            "criteria": criteria(grid),
        },
        EFFORT_QUESTION_ID: {
            "type": "choice",
            "instructions": EFFORT_INSTRUCTIONS,
            "criteria": dict(EFFORT_CRITERIA),
        },
    }


def build_payload(
    messages: Sequence[Dict[str, Any]],
    grid: Sequence[Entry],
    *,
    jev_model: str,
    context_turns: int,
    platform: str = "",
    provider: str = "",
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
        "questions": build_questions(grid),
    }


def normalise_effort_choice(raw: Any) -> Optional[str]:
    """Coerce Jev's effort answer onto ``low``/``medium``/``high``."""
    text = str(raw or "").strip().lower()
    return text if text in EFFORT_LEVELS else None
