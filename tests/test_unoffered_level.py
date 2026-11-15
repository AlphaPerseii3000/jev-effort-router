"""An answer the question never offered never reaches the wire.

``max`` is an option only where the running route declares it — in its operator ``effort_maps``
entry or, since 0.7.0, in its own built-in policy. The question follows the option set, while
``resolve_effort`` still consults the route's policy: validating the answer against the vocabulary
alone once let a well-formed but unoffered ``max`` reach the wire — the question offered three
levels and the turn ran the priciest rung.

The rule these tests pin is the one the branch applies to every declaration: an unoffered level is
*recorded* (``effort_requested``) and dropped from the wire (``effort: null``), never clamped to a
neighbour and never a reason to lose the model decision. Pinned on all three deciders, on the memo
replay, and on the ``default_effort`` knob.

The route used to demonstrate it is ``opencode-go`` with an operator map that drops ``max``: its
*built-in* policy now offers ``max`` too, so the map is the only way to narrow the question.
"""

from __future__ import annotations

import json

NARROWED_GO_MAP = {
    "opencode-go": {"deepseek-v4.1-flash": {"accepted": ["low", "medium", "high"]}}
}


def test_an_unoffered_max_is_dropped_from_the_live_console_wire(tmp_path, monkeypatch):
    from stubs import StubResponse, StubTransport
    from test_effort_rubric import GO_CONFIG, _go_router, _max_answer
    from test_router import route

    config = dict(GO_CONFIG, effort_maps=NARROWED_GO_MAP)
    transport = StubTransport([StubResponse(_max_answer())])
    router, settings = _go_router(tmp_path, monkeypatch, transport, config)

    result = route(router, provider="opencode-go", turn_id="turn-unoffered")

    from state import EFFORT_QUESTION_ID

    criteria = transport.last_payload["questions"][EFFORT_QUESTION_ID]["criteria"]
    assert "max" not in criteria
    # The model decision survives; the effort field does not.
    assert result is not None
    assert "reasoning_effort" not in result["request"]
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["effort_requested"] == "max"
    assert record["effort"] is None


def test_an_unoffered_max_is_not_applied_by_the_openai_compatible_decider(monkeypatch):
    from client import JevClient
    from config import load_settings
    from stubs import StubResponse, StubTransport

    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test")
    transport = StubTransport(
        [
            StubResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "model": "deepseek-v4.1-flash",
                                        "effort": "max",
                                        "confidence": 0.95,
                                    }
                                )
                            }
                        }
                    ]
                }
            )
        ]
    )
    settings = load_settings(
        lambda key, default=None: {
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_model": "deepseek-v4.1-flash",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "routable_providers": ["opencode-go"],
        }.get(key, default)
    )
    client = JevClient(settings, transport=transport)

    decision, reason = client.decide(
        [{"role": "user", "content": "plan the migration"}],
        settings.effective(),
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    assert decision.effort_requested == "max"
    assert decision.effort is None


def test_an_unoffered_max_is_not_applied_on_the_legacy_wire(monkeypatch):
    from config import load_settings
    from decider import JevDecider
    from stubs import StubResponse, StubTransport, decision_payload

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    settings = load_settings(
        lambda key, default=None: {"decider": "jev", "effort_maps": {}}.get(key, default)
    )
    transport = StubTransport(
        [
            StubResponse(
                decision_payload(
                    model_choice="deepseek-v4.1-flash",
                    model_confidence=0.9,
                    effort_choice="max",
                    effort_confidence=1.0,
                    effort_probabilities={"max": 1.0},
                )
            )
        ]
    )
    decider = JevDecider(settings, transport=transport)

    decision, reason = decider.decide(
        [{"role": "user", "content": "plan the migration"}],
        settings.effective(),
        platform="cli",
        provider="opencode-go",
    )

    assert reason is None
    assert decision is not None
    # ``opencode-go``'s built-in policy accepts ``max``; the gate is the *question*, not the policy.
    assert decision.effort_requested == "max"
    assert decision.effort is None


def test_a_replayed_level_the_route_no_longer_offers_is_omitted(tmp_path, monkeypatch):
    """A memo written while the map declared ``max`` must not outlive the declaration."""
    from stubs import StubResponse, StubTransport
    from test_effort_rubric import GO_CONFIG, _go_router, _max_answer, settings_for
    from test_router import route

    transport = StubTransport([StubResponse(_max_answer())])
    router, settings = _go_router(tmp_path, monkeypatch, transport, GO_CONFIG)
    first = route(router, provider="opencode-go", turn_id="turn-map-dropped")
    assert first is not None
    assert first["request"]["reasoning_effort"] == "max"

    # The operator narrows the route between the turn's two requests: the memo still carries
    # ``max``, and the replay re-resolves it against the declaration that will actually run.
    narrowed = dict(GO_CONFIG, effort_maps=NARROWED_GO_MAP)
    settings2 = settings_for(narrowed)
    monkeypatch.setattr(router, "_get_settings", lambda: settings2)
    second = route(router, provider="opencode-go", turn_id="turn-map-dropped", api_call_count=1)

    assert second is not None
    assert "reasoning_effort" not in second["request"]
    assert transport.call_count == 1  # the replay never asks the decider again
    record = router.tail(settings2, limit=1)[0]
    assert record["replayed"] is True
    assert record["effort_requested"] == "max"
    assert record["effort"] is None
