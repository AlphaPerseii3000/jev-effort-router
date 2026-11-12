"""Endpoint and key selection: the two Decisions surfaces are not interchangeable.

OpenRouter answers on ``/api/alpha/decisions`` with an ``OPENROUTER_API_KEY``; Requesty answers
on ``/v1/systemone`` with a ``REQUESTY_API_KEY`` and serves ``typesafe/jev-1.14.0``. Sending one
surface's key to the other is a 401, and sending the OpenRouter model spelling to Requesty is a
404. These tests pin the pairing so the 2026-10-20 Requesty cutover cannot silently regress into
"Requesty key, OpenRouter URL".
"""

from __future__ import annotations

import sys
import types

import pytest

from client import JevClient
from config import (
    DEFAULT_ENDPOINT,
    DEFAULT_JEV_MODEL,
    REQUESTY_DEFAULT_JEV_MODEL,
    REQUESTY_ENDPOINT,
    api_key,
    endpoint_host,
    load_settings,
    redact,
    resolve_endpoint,
)
from stubs import StubResponse, StubTransport, decision_payload


def _install_fake_secret_scope(monkeypatch, resolver):
    """Install a stand-in for ``agent.secret_scope`` (the host's profile-scoped resolver)."""
    module = types.ModuleType("agent.secret_scope")
    module.get_secret = resolver
    package = types.ModuleType("agent")
    package.secret_scope = module
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)


MESSAGES = [{"role": "user", "content": "refactore le module de paiement"}]


def settings_for(config=None):
    return load_settings(lambda key, default=None: (config or {}).get(key, default))


# -- endpoint selection ---------------------------------------------------------------


def test_defaults_to_openrouter_without_a_requesty_key():
    assert resolve_endpoint() == DEFAULT_ENDPOINT


def test_requesty_key_selects_the_requesty_endpoint(monkeypatch):
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-key")

    assert resolve_endpoint() == REQUESTY_ENDPOINT


def test_explicit_custom_endpoint_wins_over_the_key(monkeypatch):
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-key")

    assert resolve_endpoint("https://example.invalid/decisions") == "https://example.invalid/decisions"


def test_explicit_endpoint_is_honoured_by_load_settings(monkeypatch):
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-key")
    settings = settings_for({"endpoint": "https://example.invalid/decisions"})

    assert settings.endpoint == "https://example.invalid/decisions"
    # A custom endpoint keeps the OpenRouter model spelling: only the Requesty host gets the
    # Requesty id, so a self-hosted proxy is not rewritten behind the operator's back.
    assert settings.jev_model == DEFAULT_JEV_MODEL


# -- key selection ---------------------------------------------------------------------


def test_each_host_gets_its_own_key(monkeypatch):
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    assert api_key(REQUESTY_ENDPOINT) == "rq-key"
    assert api_key(DEFAULT_ENDPOINT) == "or-key"
    # Unknown endpoints preserve the cutover precedence.
    assert api_key("https://example.invalid/decisions") == "rq-key"


def test_requesty_host_without_a_requesty_key_has_no_key(monkeypatch):
    monkeypatch.delenv("REQUESTY_API_KEY", raising=False)

    assert api_key(REQUESTY_ENDPOINT) == ""


def test_redact_scrubs_both_surfaces(monkeypatch):
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")

    redacted = redact("rq-secret / or-secret")

    assert "rq-secret" not in redacted
    assert "or-secret" not in redacted
    assert redacted.count("[REDACTED]") == 2


# -- the resolved settings and the wire ------------------------------------------------


def test_requesty_key_switches_endpoint_and_model(monkeypatch):
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-key")

    settings = settings_for()

    assert settings.endpoint == REQUESTY_ENDPOINT
    assert settings.jev_model == REQUESTY_DEFAULT_JEV_MODEL
    assert endpoint_host(settings.endpoint) == "router.requesty.ai"


def test_an_explicit_jev_model_is_not_rewritten(monkeypatch):
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-key")

    settings = settings_for({"jev_model": "typesafe/jev-latest"})

    assert settings.jev_model == "typesafe/jev-latest"


def test_client_posts_to_the_pairing_endpoint_with_its_key(monkeypatch):
    monkeypatch.setenv("REQUESTY_API_KEY", "rq-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    transport = StubTransport([StubResponse(decision_payload())])
    settings = settings_for()
    client = JevClient(settings, transport=transport)

    decision, reason = client.decide(MESSAGES, settings.effective(None))

    assert reason is None and decision is not None
    call = transport.calls[0]
    assert call["url"] == REQUESTY_ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer rq-key"
    assert transport.last_payload["model"] == REQUESTY_DEFAULT_JEV_MODEL


def test_client_refuses_a_requesty_endpoint_without_its_key(monkeypatch):
    monkeypatch.delenv("REQUESTY_API_KEY", raising=False)
    transport = StubTransport([StubResponse(decision_payload())])
    settings = settings_for({"endpoint": REQUESTY_ENDPOINT})
    client = JevClient(settings, transport=transport)

    decision, reason = client.decide(MESSAGES, settings.effective(None))

    assert decision is None
    assert reason == "no_api_key"
    assert transport.calls == []


# -- credentials go through the host's profile scope, not os.environ -------------------


def test_api_key_prefers_the_profile_secret_scope(monkeypatch):
    """The multiplexing gateway serves many profiles from one process; os.environ leaks."""
    seen = []

    def get_secret(name, default=None):
        seen.append(name)
        return {"OPENROUTER_API_KEY": "scoped-or"}.get(name, default)

    _install_fake_secret_scope(monkeypatch, get_secret)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-or")

    assert api_key(DEFAULT_ENDPOINT) == "scoped-or"
    assert "OPENROUTER_API_KEY" in seen


def test_api_key_fails_closed_when_the_scope_is_missing(monkeypatch):
    """A multiplexed turn with no scope must not fall back to the process environment."""

    class UnscopedSecretError(RuntimeError):
        pass

    def get_secret(name, default=None):
        raise UnscopedSecretError(name)

    _install_fake_secret_scope(monkeypatch, get_secret)
    monkeypatch.setenv("OPENROUTER_API_KEY", "another-profiles-key")

    assert api_key(DEFAULT_ENDPOINT) == ""


def test_redact_reads_the_same_scope(monkeypatch):
    def get_secret(name, default=None):
        return {"REQUESTY_API_KEY": "scoped-rq", "OPENROUTER_API_KEY": "scoped-or"}.get(name, default)

    _install_fake_secret_scope(monkeypatch, get_secret)

    redacted = redact("scoped-rq / scoped-or")

    assert redacted.count("[REDACTED]") == 2
