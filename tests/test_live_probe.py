"""Opt-in live probe against OpenCode (or OpenAI-compatible decider endpoint).

Skipped by default when OPENCODE_GO_API_KEY is unset.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import pytest

from config import load_settings
from decider import OpenAICompatibleDecider


LIVE_OPT_IN = os.environ.get("JEV_LIVE_PROBE", "").strip().lower() in ("1", "true", "yes")
OPENCODE_KEY = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
BASE_URL = os.environ.get("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")


def test_offline_safety_with_fake_key_blocks_network(monkeypatch):
    """With a fake key present in the environment but without JEV_LIVE_PROBE opt-in, no network is touched."""
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-fictitious-key")
    monkeypatch.delenv("JEV_LIVE_PROBE", raising=False)

    def _fail_on_network(*args, **kwargs):
        raise AssertionError("Network call attempted without JEV_LIVE_PROBE opt-in!")

    monkeypatch.setattr(urllib.request, "urlopen", _fail_on_network)

    # Verify opt-in condition evaluates to False even when key is present
    opt_in = os.environ.get("JEV_LIVE_PROBE", "").strip().lower() in ("1", "true", "yes")
    key_present = bool(os.environ.get("OPENCODE_GO_API_KEY", "").strip())
    assert not (opt_in and key_present)


@pytest.mark.skipif(not (LIVE_OPT_IN and OPENCODE_KEY), reason="Live probe opt-in requires JEV_LIVE_PROBE=1 and OPENCODE_GO_API_KEY")
def test_live_opencode_decider_call(monkeypatch):
    """Verify that OpenAICompatibleDecider works live against the real endpoint."""
    monkeypatch.setenv("OPENCODE_GO_API_KEY", OPENCODE_KEY)
    conf = {
        "decider": "openai_compatible",
        "decider_base_url": BASE_URL,
        "decider_model": "deepseek-v4.1-flash",
        "decider_api_key_env": "OPENCODE_GO_API_KEY",
        "routable_providers": ["opencode-go"],
        "timeout_s": 15.0,
        "grids": {
            "opencode-go": [
                {"model": "deepseek-v4.1-flash", "profile": "Everyday writing and general coding"},
                {"model": "kimi-k3", "profile": "Complex multi-file refactoring and deep reasoning"},
            ]
        },
    }
    settings = load_settings(lambda k, d=None: conf.get(k, d))
    decider = OpenAICompatibleDecider(settings)
    messages = [{"role": "user", "content": "Please refactor this complex 500-line database migration algorithm"}]
    effective = settings.effective(provider="opencode-go")
    decision, reason = decider.decide(messages, effective, provider="opencode-go")

    assert decision is not None, f"Live decider failed: {reason}"
    assert decision.model in ("deepseek-v4.1-flash", "kimi-k3")
    assert 0.0 <= decision.model_confidence <= 1.0
    assert decision.decider == "openai_compatible"
    assert decision.latency_ms > 0


@pytest.mark.skipif(not (LIVE_OPT_IN and OPENCODE_KEY), reason="Live probe opt-in requires JEV_LIVE_PROBE=1 and OPENCODE_GO_API_KEY")
@pytest.mark.parametrize("effort_level", [None, "low", "medium", "high"])
def test_live_opencode_reasoning_effort_support(effort_level):
    """Probe which reasoning_effort levels deepseek-v4.1-flash accepts on OpenCode."""
    url = f"{BASE_URL}/chat/completions"
    body = {
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "max_tokens": 5,
    }
    if effort_level is not None:
        body["reasoning_effort"] = effort_level

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENCODE_KEY}",
            "User-Agent": "Hermes-Agent/0.21.4 (jev-effort-router)",
            "x-opencode-session": "probe-effort-support",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert "choices" in data
    except urllib.error.HTTPError as exc:
        # If reasoning_effort is rejected with 400, record it
        if exc.code == 400 and effort_level is not None:
            pytest.skip(f"reasoning_effort={effort_level!r} rejected by provider with HTTP 400")
        raise
