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
from typing import Any, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Hermes' provider-model cache, relative to the active HERMES_HOME.
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


class ModelCatalog:
    """A lazily-read, briefly-cached view of the provider's model list."""

    def __init__(
        self,
        cache_path: Optional[Path] = None,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        clock=time.monotonic,
    ) -> None:
        self._cache_path = cache_path
        self._ttl_s = max(0.0, float(ttl_s))
        self._clock = clock
        self._models: Optional[Tuple[str, ...]] = None
        self._read_at: Optional[float] = None

    # -- reading -----------------------------------------------------------------

    def _path(self) -> Optional[Path]:
        if self._cache_path is not None:
            return self._cache_path
        home = _hermes_home()
        return home / CACHE_FILENAME if home else None

    def _fresh(self) -> bool:
        if self._models is None or self._read_at is None:
            return False
        return (self._clock() - self._read_at) < self._ttl_s

    def models(self) -> Optional[Tuple[str, ...]]:
        """The provider's model ids, or ``None`` when there is no usable evidence."""
        if self._fresh():
            return self._models
        path = self._path()
        models: Optional[Tuple[str, ...]] = None
        if path is not None:
            try:
                models = _model_ids(json.loads(path.read_text(encoding="utf-8"))) or None
            except Exception as exc:  # noqa: BLE001 - a missing/garbled cache is not a failure
                logger.debug("jev-effort-router: provider catalog unavailable (%s: %s)", type(exc).__name__, exc)
                models = None
        self._models = models
        self._read_at = self._clock()
        return models

    # -- the question the router asks --------------------------------------------

    def is_known(self, model_id: str, provider_prefixes: Sequence[str] = ()) -> Optional[bool]:
        """``True``/``False`` when the catalog can answer, ``None`` when it cannot.

        A model matches either exactly or through one of ``provider_prefixes`` (a catalog entry
        namespaced as ``vendor/model``), so a grid entry naming the bare id is not judged absent
        for a formatting difference.
        """
        models = self.models()
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
