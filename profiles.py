"""Provider profiles: the model a provider puts behind each capability slot.

The routing question has two halves that used to be fused into one list:

* *which kind of task is this?* — the six capability slots, whose criteria text is the measured
  payload sent to Jev;
* *which model on the active provider serves that slot?* — a mapping that changes with the
  provider, and must not change what Jev is asked.

A profile is that mapping. The built-in profile maps the six slots to the six Ollama:Cloud models
in the same order, with the same descriptions as before, so the default install sends a
byte-identical request. An operator can add a profile for another provider; a provider with no
profile is left untouched by the router.

No provider-specific profile ships here without benchmark evidence. The parser is deliberately
lenient about *shape* and strict about *identity*: unparseable entries are dropped, duplicates keep
their first slot, and a fallback that is not one of the profile's models resolves to the profile's
general slot (or its first entry) rather than putting an off-grid id on the wire.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .grid import SLOTS, SLOT_DESCRIPTIONS, Entry

#: The provider the plugin is benchmarked for, and the profile that ships built in.
DEFAULT_PROVIDER = "ollama-cloud"

#: Spellings the host has used for the default provider. Normalised before lookup.
PROVIDER_ALIASES: Dict[str, str] = {
    "ollama_cloud": DEFAULT_PROVIDER,
    "ollama": DEFAULT_PROVIDER,
}


def normalize_provider(name: Any) -> str:
    """The canonical provider key, or ``""`` for an empty/nameless provider."""
    text = str(name or "").strip().lower()
    return PROVIDER_ALIASES.get(text, text)


def slots_for_count(count: int) -> Tuple[str, ...]:
    """Slot names for a grid of ``count`` entries, in the measured order.

    A custom grid or an operator profile may have fewer than six entries. More than six is
    allowed by the existing ``grid`` setting; those extra entries get neutral ``slot-N`` names
    because they have no measured capability meaning.
    """
    limit = max(0, int(count))
    return tuple(
        SLOTS[index] if index < len(SLOTS) else f"slot-{index + 1}"
        for index in range(limit)
    )


@dataclass(frozen=True)
class ProviderProfile:
    """One provider's slot → model mapping, plus the fallback served on a weak decision."""

    name: str
    entries: Tuple[Entry, ...]
    slots: Tuple[str, ...]
    fallback_model: str
    #: ``builtin`` for the shipped Ollama:Cloud profile, ``config`` for an operator's.
    source: str = "config"

    def __post_init__(self) -> None:
        if not self.entries:
            raise ValueError("a provider profile needs at least one entry")
        if len(self.entries) != len(self.slots):
            raise ValueError(
                f"profile {self.name!r} has {len(self.entries)} entries but {len(self.slots)} slots"
            )

    @property
    def grid(self) -> Tuple[Entry, ...]:
        """The entries in slot order — the list Jev is offered."""
        return self.entries

    def entry_for(self, model_id: str) -> Optional[Entry]:
        wanted = (model_id or "").strip()
        for entry in self.entries:
            if entry.model_id == wanted:
                return entry
        return None

    def entry_for_slot(self, slot: str) -> Optional[Entry]:
        key = str(slot or "").strip().lower()
        for name, entry in zip(self.slots, self.entries):
            if name == key:
                return entry
        return None

    def slot_for(self, model_id: str) -> str:
        wanted = (model_id or "").strip()
        for slot, entry in zip(self.slots, self.entries):
            if entry.model_id == wanted:
                return slot
        return ""


def _model_id_from_any(raw: Any) -> str:
    """The model id in a string or mapping, ignoring any description after ``": "``."""
    if isinstance(raw, dict):
        return str(raw.get("model_id") or raw.get("model") or raw.get("id") or "").strip()
    text = str(raw or "").strip()
    if not text:
        return ""
    model_id, separator, _ = text.partition(": ")
    return model_id.strip() if separator else text


def _parse_entry(raw: Any, slot: str) -> Optional[Entry]:
    """One profile entry: ``"model-id"``, ``"model-id: description"`` or a mapping.

    The separator is ``": "`` rather than ``":"`` on purpose: ids such as
    ``nemotron-3-nano:30b`` carry a colon that is part of the id, and splitting on the first
    colon would silently truncate it into a model the provider does not serve.
    """
    default_description = SLOT_DESCRIPTIONS.get(slot, "")
    if isinstance(raw, Entry):
        return raw
    if isinstance(raw, dict):
        model_id = _model_id_from_any(raw)
        description = str(
            raw.get("description") or raw.get("profile") or default_description
        ).strip()
        return Entry(model_id, description) if model_id and any(c.isalnum() for c in model_id) else None
    text = str(raw or "").strip()
    if not text:
        return None
    model_id, separator, description = text.partition(": ")
    model_id = model_id.strip()
    if not model_id or not any(character.isalnum() for character in model_id):
        return None
    return Entry(model_id, description.strip() if separator else default_description)


def _parse_entries(raw: Any) -> Tuple[Tuple[Entry, ...], Tuple[str, ...]]:
    """Parse a profile's ``models`` value into ``(entries, slots)``, dropping duplicates."""
    pairs: list[tuple[str, Entry]] = []
    if isinstance(raw, dict):
        for slot in SLOTS:
            if slot in raw:
                entry = _parse_entry(raw[slot], slot)
                if entry is not None:
                    pairs.append((slot, entry))
    elif isinstance(raw, str):
        lines = [line for line in raw.splitlines() if line.strip()]
        names = slots_for_count(len(lines))
        for slot, line in zip(names, lines):
            entry = _parse_entry(line, slot)
            if entry is not None:
                pairs.append((slot, entry))
    elif isinstance(raw, Iterable):
        items = list(raw)
        names = slots_for_count(len(items))
        for index, item in enumerate(items):
            slot = names[index]
            if isinstance(item, dict) and item.get("slot"):
                candidate = str(item.get("slot")).strip().lower()
                if candidate in SLOTS:
                    slot = candidate
            entry = _parse_entry(item, slot)
            if entry is not None:
                pairs.append((slot, entry))

    entries: list[Entry] = []
    slots: list[str] = []
    seen: set[str] = set()
    seen_slots: set[str] = set()
    for slot, entry in pairs:
        if entry.model_id in seen or slot in seen_slots:
            continue
        seen.add(entry.model_id)
        seen_slots.add(slot)
        entries.append(entry)
        slots.append(slot)
    return tuple(entries), tuple(slots)


def parse_provider_profile(name: Any, spec: Any) -> Optional[ProviderProfile]:
    """Parse one provider's profile, or ``None`` when nothing usable parsed. Never raises."""
    provider = normalize_provider(name)
    if not provider:
        return None

    if isinstance(spec, dict):
        models = spec.get("models", spec.get("grid", spec.get("model_ids")))
        if models is None and any(slot in spec for slot in SLOTS):
            # A bare slot map is accepted too: {general: "...", deep-code: "..."}.
            models = spec
        fallback_raw = spec.get("fallback")
    else:
        models = spec
        fallback_raw = None

    try:
        entries, slots = _parse_entries(models)
    except Exception:  # noqa: BLE001 - a bad profile is "no profile", never a crash
        return None
    if not entries:
        return None

    fallback = _model_id_from_any(fallback_raw)
    if not fallback or not any(entry.model_id == fallback for entry in entries):
        general = next(
            (entry for slot, entry in zip(slots, entries) if slot == "general"),
            entries[0],
        )
        fallback = general.model_id

    try:
        return ProviderProfile(
            name=provider,
            entries=entries,
            slots=slots,
            fallback_model=fallback,
            source="config",
        )
    except ValueError:
        return None


def parse_provider_profiles(raw: Any) -> Dict[str, ProviderProfile]:
    """Parse the ``provider_profiles`` setting into a provider-keyed dict.

    A profile that does not parse is dropped, exactly like an unlisted provider: the router
    skips that provider's turns rather than guessing a model. Never raises.
    """
    if not isinstance(raw, dict):
        return {}
    profiles: Dict[str, ProviderProfile] = {}
    for name, spec in raw.items():
        profile = parse_provider_profile(name, spec)
        if profile is not None:
            profiles[profile.name] = profile
    return profiles


def builtin_profile(entries: Iterable[Entry], fallback_model: str) -> ProviderProfile:
    """The shipped default profile: the measured grid, zipped to the measured slots."""
    grid = tuple(entries)
    return ProviderProfile(
        name=DEFAULT_PROVIDER,
        entries=grid,
        slots=slots_for_count(len(grid)),
        fallback_model=fallback_model,
        source="builtin",
    )
