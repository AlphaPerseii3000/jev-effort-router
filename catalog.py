"""The provider's model catalog — the grid must never outlive the models it names.

A routing grid is static; a provider catalog is not. Ollama:cloud renamed its Nano tier from
``nemotron-3-nano`` to ``nemotron-3-nano:30b``, and the stale entry in the grid turned a perfectly
confident decision into ``HTTP 404: model "nemotron-3-nano" not found`` — a dead turn, the one
outcome this plugin exists to avoid ("a broken router must be indistinguishable from a router that
is not installed").

This module answers one narrow question: *is this model id provably absent from the provider the
request is about to go to?* It reads what the host already cached — Hermes writes the provider's
model list to ``<HERMES_HOME>/ollama_cloud_models_cache.json`` — so no credential is needed and no
socket is opened. Every failure mode (no cache file, unreadable JSON, an unexpected shape, no host
at all) returns "no evidence", and the grid is trusted as before.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Hermes' per-provider model cache filenames, relative to the active HERMES_HOME.
#: The generic file is keyed by provider; the per-provider file is the older shape and stays as a
#: compatibility fallback (``ollama_cloud_models_cache.json`` is what 0.3.x read).
PROVIDER_CACHE_FILENAME = "provider_models_cache.json"
CACHE_FILENAME = "ollama_cloud_models_cache.json"

#: How long a read of the cache is reused before re-reading. The file is small and rewritten
#: whenever the host refreshes its catalog, so a short window is enough and keeps the routing
#: path free of filesystem work per call.
DEFAULT_TTL_S = 300.0


def _hermes_home() -> Optional[Path]:
    """The active profile's home, from the host when importable, else the environment."""
    try:  # the plugin runs in-process with Hermes, so this is the precise answer
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - an absent host is not an error, it is no evidence
        pass
    home = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(home) if home else None


def _model_ids(payload: Any) -> Tuple[str, ...]:
    """Every model id in a cached catalog payload, in the shapes it is known to take."""
    found = []
    if isinstance(payload, dict):
        entries = payload.get("models") or payload.get("data") or payload.get("model_ids") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        return ()
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            found.append(entry.strip())
        elif isinstance(entry, dict):
            for key in ("id", "model", "name", "model_id"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    found.append(value.strip())
                    break
    return tuple(found)


def _provider_slug(provider: str) -> str:
    """``ollama-cloud`` -> ``ollama_cloud``, the spelling Hermes uses in cache filenames."""
    return (provider or "").strip().lower().replace("-", "_").replace(" ", "_")


def _provider_payload(payload: Any, provider: str) -> Any:
    """The provider's slice of a generic ``provider_models_cache.json``, or the payload itself.

    The generic file is ``{provider: {"models": [...]}}``. A legacy per-provider file is already
    the inner shape, so an absent provider key leaves the payload untouched and ``_model_ids``
    reads it as before.
    """
    if not isinstance(payload, dict):
        return payload
    for key in (provider, provider.replace("-", "_"), provider.replace("_", "-")):
        if key and isinstance(payload.get(key), dict):
            return payload[key]
    return payload


class ModelCatalog:
    """A lazily-read, briefly-cached view of the provider's model list."""

    def __init__(
        self,
        cache_path: Optional[Path] = None,
        *,
        provider: str = "",
        ttl_s: float = DEFAULT_TTL_S,
        clock=time.monotonic,
    ) -> None:
        self._cache_path = cache_path
        self._default_provider = (provider or "").strip().lower()
        self._ttl_s = max(0.0, float(ttl_s))
        self._clock = clock
        # Keyed by provider: one router instance serves every profile, and a status call may ask
        # about a provider other than the one the current turn is using.
        self._models: Dict[str, Optional[Tuple[str, ...]]] = {}
        self._read_at: Dict[str, float] = {}

    # -- reading -----------------------------------------------------------------

    def _paths(self, provider: str) -> Tuple[Path, ...]:
        if self._cache_path is not None:
            return (self._cache_path,)
        home = _hermes_home()
        if home is None:
            return ()
        paths = [home / PROVIDER_CACHE_FILENAME]
        slug = _provider_slug(provider)
        if slug:
            paths.append(home / f"{slug}_models_cache.json")
        return tuple(dict.fromkeys(paths))

    def _fresh(self, provider: str) -> bool:
        if provider not in self._models or provider not in self._read_at:
            return False
        return (self._clock() - self._read_at[provider]) < self._ttl_s

    def models(self, provider: str = "") -> Optional[Tuple[str, ...]]:
        """The provider's model ids, or ``None`` when there is no usable evidence."""
        key = (provider or self._default_provider or "ollama-cloud").strip().lower()
        if self._fresh(key):
            return self._models[key]
        models: Optional[Tuple[str, ...]] = None
        for path in self._paths(key):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                models = _model_ids(_provider_payload(payload, key)) or None
            except Exception as exc:  # noqa: BLE001 - a missing/garbled cache is not a failure
                logger.debug(
                    "jev-effort-router: provider catalog unavailable (%s: %s)",
                    type(exc).__name__,
                    exc,
                )
                models = None
            if models:
                break
        self._models[key] = models
        self._read_at[key] = self._clock()
        return models

    # -- the question the router asks --------------------------------------------

    def is_known(
        self,
        model_id: str,
        provider_prefixes: Sequence[str] = (),
        *,
        provider: str = "",
    ) -> Optional[bool]:
        """``True``/``False`` when the catalog can answer, ``None`` when it cannot.

        A model matches either exactly or through one of ``provider_prefixes`` (a catalog entry
        namespaced as ``vendor/model``), so a grid entry naming the bare id is not judged absent
        for a formatting difference.
        """
        models = self.models(provider)
        if not models:
            return None
        wanted = (model_id or "").strip()
        if not wanted:
            return None
        for entry in models:
            if entry == wanted:
                return True
            for prefix in provider_prefixes:
                if entry == f"{prefix}/{wanted}":
                    return True
        return False
