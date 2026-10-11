"""The router end to end: routing, replay, degradation, and the skip gates.

These exercise the real middleware callback — the same callable Hermes invokes — against a
stub Decisions API, so the contract under test is the one that ships.
"""

from __future__ import annotations

import json

import pytest

from stubs import StubContext, StubResponse, StubState, StubTransport, decision_payload, ollama_request
from config import load_settings
from router import Router


def build(tmp_path, transport, config=None, state=None, catalog=None):
    """A real router wired the way ``register(ctx)`` wires it."""
    settings = load_settings(lambda key, default=None: (config or {}).get(key, default))
    audit = StubState(tmp_path)
    router = Router(
        lambda: settings,
        get_state=lambda: audit,
        client_factory=lambda _settings: _ClientWith(transport, settings),
        # No provider catalog: these tests are about routing, and an absent catalog is the
        # "no evidence" answer, so a chosen model is never refused for a missing entry.
        catalog=_NoCatalog() if catalog is None else catalog,
    )
    return router, settings


class _NoCatalog:
    def is_known(self, model_id, provider_prefixes=()):
        return None


def _ClientWith(transport, settings):
    from client import JevClient

    return JevClient(settings, transport=transport)


def route(router, request=None, **overrides):
    kwargs = dict(
        turn_id="turn-1",
        session_id="session-1",
        platform="cli",
        model="deepseek-v4.1-flash",
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-1:api:0",
    )
    kwargs.update(overrides)
    request = request if request is not None else ollama_request()
    return router.on_llm_request(request, dict(request), **kwargs)


# -- routing -----------------------------------------------------------------------


def test_routes_model_and_effort(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, _ = build(tmp_path, transport)

    result = route(router)

    assert result is not None
    assert result["request"]["model"] == "kimi-k3"
    assert result["request"]["reasoning_effort"] == "high"
    # `reasoning_config` must NOT be in the payload: it is a params-level input the provider
    # profile consumes to derive the top-level field, and Ollama rejects the whole call if it
    # reaches the wire.
    assert "reasoning_config" not in result["request"]
    assert result["source"] == "jev-effort-router"


def test_request_is_built_from_the_pre_middleware_payload(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("1", 0.9))])
    router, _ = build(tmp_path, transport)
    original = ollama_request(model="glm-5.3")
    modified = dict(original, injected_by_an_earlier_middleware=True)

    result = router.on_llm_request(
        modified,
        original,
        turn_id="t",
        session_id="s",
        model="glm-5.3",
        provider="ollama-cloud",
        api_mode="chat_completions",
        api_call_count=0,
    )

    assert result["request"]["model"] == "deepseek-v4.1-flash"
    assert result["request"].get("injected_by_an_earlier_middleware") is None
    assert result["request"]["messages"] == original["messages"]


def test_effort_is_omitted_when_the_decision_carries_none(tmp_path):
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.9, "medium", 0.9, include_effort=False))]
    )
    router, _ = build(tmp_path, transport)
    request = ollama_request(reasoning_config={"enabled": True, "effort": "low"})

    result = route(router, request, )

    assert result["request"]["model"] == "deepseek-v4.1-flash"
    # The model changed, so a stale effort from the old model must not survive.
    assert "reasoning_effort" not in result["request"]


# -- one decision per turn ---------------------------------------------------------


def test_follow_up_requests_replay_the_turns_first_decision(tmp_path):
    transport = StubTransport(
        [
            StubResponse(decision_payload("2", 0.9, "high", 0.9)),
            StubResponse(decision_payload("5", 0.9, "low", 0.9)),
        ]
    )
    router, _ = build(tmp_path, transport)

    first = route(router, api_call_count=0, api_request_id="turn-1:api:0")
    second = route(router, api_call_count=1, api_request_id="turn-1:api:1")

    assert first["request"]["model"] == "kimi-k3"
    assert second["request"]["model"] == "kimi-k3"
    assert second["request"]["reasoning_effort"] == "high"
    assert transport.call_count == 1


def test_replay_works_with_the_call_counter_hermes_really_sends(tmp_path):
    """Hermes passes the *incremented* counter, so a turn's first call arrives as api_call_count=1.

    Keying the memo store off `api_call_count == 0` therefore never stored anything and every
    call of a turn re-decided. This reproduces the real values.
    """
    transport = StubTransport(
        [
            StubResponse(decision_payload("2", 0.9, "high", 0.9)),
            StubResponse(decision_payload("5", 0.9, "low", 0.9)),
        ]
    )
    router, _ = build(tmp_path, transport)

    first = route(router, api_call_count=1, api_request_id="turn-1:api:1")
    second = route(router, api_call_count=2, api_request_id="turn-1:api:2")
    third = route(router, api_call_count=3, api_request_id="turn-1:api:3")

    assert first["request"]["model"] == "kimi-k3"
    assert [second["request"]["model"], third["request"]["model"]] == ["kimi-k3", "kimi-k3"]
    assert transport.call_count == 1


def test_a_new_turn_routes_again(tmp_path):
    transport = StubTransport(
        [
            StubResponse(decision_payload("2", 0.9, "high", 0.9)),
            StubResponse(decision_payload("5", 0.9, "low", 0.9)),
        ]
    )
    router, _ = build(tmp_path, transport)

    first = route(router, turn_id="turn-1")
    second = route(router, turn_id="turn-2", api_request_id="turn-2:api:0")

    assert first["request"]["model"] == "kimi-k3"
    assert second["request"]["model"] == "minimax-m3"
    assert transport.call_count == 2


def test_route_per_session_decides_once(tmp_path):
    transport = StubTransport(
        [
            StubResponse(decision_payload("2", 0.9, "high", 0.9)),
            StubResponse(decision_payload("5", 0.9, "low", 0.9)),
        ]
    )
    router, _ = build(tmp_path, transport, config={"route_per_turn": False})

    first = route(router, turn_id="turn-1")
    second = route(router, turn_id="turn-2", api_request_id="turn-2:api:0")

    assert first["request"]["model"] == "kimi-k3"
    assert second["request"]["model"] == "kimi-k3"
    assert transport.call_count == 1


# -- replayed audit records keep the measured confidences --------------------------


def test_replayed_route_reemits_both_measured_confidences(tmp_path):
    """The model and effort confidences are measured independently and must survive a replay.

    Storing ``max(model, effort)`` in the memo and re-emitting it as both fields made a fresh
    ``0.34/0.65`` decision re-report as ``0.65/0.65`` — the effort confidence masquerading as a
    model confidence Jev never gave — and destroyed which side was the weak one.
    """
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.34, "medium", 0.65, model_probabilities={"1": 0.46, "5": 0.23}))]
    )
    router, _ = build(tmp_path, transport)

    route(router, turn_id="turn-1", api_request_id="turn-1:api:1")
    route(router, turn_id="turn-1", api_request_id="turn-1:api:2")

    lines = (tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()
    fresh = json.loads(lines[0])
    replayed = json.loads(lines[1])

    assert fresh["replayed"] is False
    assert replayed["replayed"] is True
    assert (replayed["model_confidence"], replayed["effort_confidence"]) == (0.34, 0.65)


def test_replayed_route_never_copies_unmeasured_fields(tmp_path):
    """What was not stored at replay is reported as ``null`` — never copied from a sibling field.

    The per-criterion distributions, the full alternatives and the effort-choice string are only
    present on the fresh decision. A replayed record that filled them in would let a consumer of
    the raw tail compute over values that were never measured.
    """
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.9, "medium", 0.9, model_probabilities={"1": 0.9, "2": 0.1}))]
    )
    router, _ = build(tmp_path, transport)

    route(router, turn_id="turn-1", api_request_id="turn-1:api:1")
    route(router, turn_id="turn-1", api_request_id="turn-1:api:2")

    lines = (tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()
    fresh = json.loads(lines[0])
    replayed = json.loads(lines[1])

    # The fresh decision carries the measured surfaces.
    assert fresh["model_probabilities"] == {"1": 0.9, "2": 0.1}
    assert fresh["effort_choice"] == "medium"
    assert fresh["alternatives"]

    # The replay does not invent them.
    assert replayed["model_probabilities"] is None
    assert replayed["effort_probabilities"] is None
    assert replayed["effort_choice"] is None
    assert replayed["alternatives"] is None


def test_decision_from_memo_preserves_the_failing_dimension(tmp_path):
    """`_decision_from_memo` re-emits the pair of confidences as stored — not their max."""
    from memo import Memo
    from router import _decision_from_memo

    memo = Memo(
        model="deepseek-v4.1-flash",
        effort="medium",
        effort_requested="medium",
        model_confidence=0.12,
        effort_confidence=0.93,
        choice="1",
    )
    decision = _decision_from_memo(memo)

    assert decision.model_confidence == 0.12
    assert decision.effort_confidence == 0.93


# -- fail open ---------------------------------------------------------------------


def test_timeout_leaves_the_request_untouched(tmp_path):
    transport = StubTransport(handler=lambda *args: _raise_read_timeout())
    router, _ = build(tmp_path, transport)
    request = ollama_request()

    assert route(router, request, ) is None


def test_http_error_leaves_the_request_untouched(tmp_path):

    transport = StubTransport([StubResponse(status_code=402, text='{"error":"insufficient credits"}')])
    router, _ = build(tmp_path, transport)

    assert route(router, ollama_request()) is None


def test_malformed_body_leaves_the_request_untouched(tmp_path):

    transport = StubTransport([StubResponse(payload=None, status_code=200, text="not json")])
    router, _ = build(tmp_path, transport)

    assert route(router, ollama_request()) is None


def test_low_confidence_falls_back_to_the_configured_defaults(tmp_path):

    transport = StubTransport([StubResponse(decision_payload("2", 0.10, "high", 0.12))])
    router, _ = build(tmp_path, transport, config={"default_model": "deepseek-v4.1-flash", "default_effort": "low"})

    result = route(router, ollama_request())

    # The turn still goes out routed — on the fallback, and flagged as degraded.
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    assert result["request"]["reasoning_effort"] == "low"
    record = json.loads((tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    # The model and the effort degrade independently, so a weak answer on both is recorded
    # twice — the audit trail has to show how many of the two decisions were distrusted.
    assert record["fallback_reasons"] == ["low_confidence", "low_confidence"]


def test_unknown_choice_falls_back(tmp_path):

    transport = StubTransport([StubResponse(decision_payload("99", 0.99, "medium", 0.99))])
    router, _ = build(tmp_path, transport)

    result = route(router, ollama_request())

    assert result["request"]["model"] == "deepseek-v4.1-flash"
    record = json.loads((tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "unknown_choice" in record["fallback_reasons"]


def test_missing_api_key_is_inert(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    transport = StubTransport([])
    router, _ = build(tmp_path, transport)

    assert route(router, ollama_request()) is None
    assert transport.call_count == 0


def test_a_raising_client_never_breaks_the_turn(tmp_path):
    class Exploding:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def decide(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    settings = load_settings(lambda _key, default=None: default)
    router = Router(
        lambda: settings,
        get_state=lambda: StubState(tmp_path),
        client_factory=lambda _settings: Exploding(),
        catalog=_NoCatalog(),
    )

    assert route(router, ollama_request()) is None


# -- skip gates --------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "openrouter"},
        {"provider": "anthropic"},
        {"api_mode": "anthropic_messages"},
        {"model": "gpt-9-ultra"},
    ],
)
def test_untouched_on_surfaces_the_router_does_not_own(tmp_path, overrides):
    transport = StubTransport([])
    router, _ = build(tmp_path, transport)

    assert route(router, ollama_request(), **overrides) is None
    assert transport.call_count == 0


def test_disabled_switch_is_inert(tmp_path):
    transport = StubTransport([])
    router, _ = build(tmp_path, transport, config={"enabled": False})

    assert route(router, ollama_request()) is None
    assert transport.call_count == 0


def test_a_surface_disabled_by_its_policy_is_skipped_naming_the_surface(tmp_path):
    transport = StubTransport([])
    router, _ = build(tmp_path, transport, config={"platform_overrides": {"cron": {"enabled": False}}})

    assert route(router, ollama_request(), platform="cron") is None
    assert transport.call_count == 0

    records = (tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["event"] == "skip"
    assert record["reason"] == "surface_disabled"
    assert record["platform"] == "cron"


def test_the_policy_is_per_surface(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("1", 0.9))])
    router, _ = build(
        tmp_path,
        transport,
        config={"platform_overrides": {"cron": {"enabled": False}}},
    )

    # A cli turn on the same router still routes; only the cron surface is disabled.
    result = route(router, ollama_request(), platform="cli")

    assert result is not None
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    assert transport.call_count == 1


# -- the policy drives the whole decision path --------------------------------------


CRON_POLICY = {
    "cron": {
        "grid": "glm-5.3: rigorous reasoning",
        "confidence_threshold": 0.7,
        "max_effort": "medium",
    }
}


def test_a_cron_turn_gets_the_cron_grid_and_a_capped_effort(tmp_path):
    """CAP-8: the surface grid is the one sent to Jev, and `max_effort` caps the emitted level."""
    transport = StubTransport([StubResponse(decision_payload("3", 0.9, "high", 0.9))])
    router, _ = build(tmp_path, transport, config={"platform_overrides": CRON_POLICY})

    result = route(router, ollama_request(), platform="cron", model="glm-5.3")

    # The one-entry cron grid is what Jev was offered — position `3` is off it, so the answer
    # is unknown and falls back inside the cron grid.
    criteria = transport.last_payload["questions"]["model_route"]["criteria"]
    assert criteria == {"1": "glm-5.3: rigorous reasoning"}
    assert result["request"]["model"] == "glm-5.3"
    assert result["request"]["reasoning_effort"] == "medium"
    record = json.loads((tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "unknown_choice" in record["fallback_reasons"]


def test_an_off_grid_cron_answer_never_leaves_the_cron_grid(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("2", 0.99, "high", 0.9))])
    router, _ = build(tmp_path, transport, config={"platform_overrides": CRON_POLICY})

    result = route(router, ollama_request(), platform="cron", model="glm-5.3")

    assert result["request"]["model"] == "glm-5.3"
    record = json.loads((tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "unknown_choice" in record["fallback_reasons"]


def test_a_cron_fallback_below_the_policy_threshold_stays_in_the_cron_grid(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("1", 0.2, "high", 0.9, grid_size=1))])
    router, _ = build(tmp_path, transport, config={"platform_overrides": CRON_POLICY})

    result = route(router, ollama_request(), platform="cron", model="glm-5.3")

    assert result["request"]["model"] == "glm-5.3"
    assert result["request"]["reasoning_effort"] == "medium"
    record = json.loads((tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "low_confidence" in record["fallback_reasons"]


def test_a_cli_turn_on_the_same_router_routes_on_the_global_grid(tmp_path):
    """The cron policy narrows cron only; a cli turn still uses the six-entry global grid."""
    transport = StubTransport(
        [
            StubResponse(decision_payload("3", 0.9, "high", 0.9)),  # cron: off the one-entry grid
            StubResponse(decision_payload("2", 0.9, "high", 0.8)),  # cli: global position 2
        ]
    )
    router, _ = build(tmp_path, transport, config={"platform_overrides": CRON_POLICY})

    cron = route(router, ollama_request(), platform="cron", model="glm-5.3", turn_id="t-cron")
    cli = route(
        router, ollama_request(), platform="cli", model="deepseek-v4.1-flash", turn_id="t-cli",
        api_request_id="t-cli:api:0",
    )

    assert cron["request"]["model"] == "glm-5.3"
    assert cli["request"]["model"] == "kimi-k3"
    # The cli turn's payload was the six-entry global grid, not the cron surface's one entry.
    criteria = transport.calls[1]["json"]["questions"]["model_route"]["criteria"]
    assert set(criteria) == {"1", "2", "3", "4", "5", "6"}


def test_max_effort_caps_the_default_effort_fallback_on_a_surface(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, "high", 0.1))])
    router, _ = build(
        tmp_path,
        transport,
        config={
            "default_effort": "high",
            "platform_overrides": {"cron": {"grid": "glm-5.3: rigorous reasoning", "max_effort": "low"}},
        },
    )

    result = route(router, ollama_request(), platform="cron", model="glm-5.3")

    assert result["request"]["model"] == "glm-5.3"
    assert result["request"]["reasoning_effort"] == "low"



def test_end_to_end_cron_turn_is_capped_and_cli_turn_is_not_in_one_run(tmp_path):
    """Proves CAP-8 through one run of the real middleware callback across four turns.

    1. cron turn: stub Jev answers off the cron grid at high; goes out on cron-grid model at capped effort (medium).
    2. cli turn: in the same run, routes on the global grid (kimi-k3 at high).
    3. disabled cron turn: cron policy with enabled: false leaves turn byte-identical to plugin-disabled run.
    4. unmapped turn: platform absent from the map with off-grid model leaves turn byte-identical to plugin-disabled run.

    Captures provider kwargs and audit records for all four turns:
    - Never a model or effort outside the cron policy on cron turns.
    - Exactly one skip entry naming the disabled surface (reason == 'surface_disabled', platform == 'cron').
    - Byte-identical payloads on the disabled and unmapped surfaces.
    """
    transport = StubTransport(
        [
            StubResponse(decision_payload("3", 0.9, "high", 0.9)),  # turn 1 (cron): off-grid answer at high
            StubResponse(decision_payload("2", 0.9, "high", 0.8)),  # turn 2 (cli): global position 2 at high
        ]
    )

    current_config = {"platform_overrides": dict(CRON_POLICY)}
    audit = StubState(tmp_path)
    router = Router(
        lambda: load_settings(lambda k, default=None: current_config.get(k, default)),
        get_state=lambda: audit,
        client_factory=lambda settings: _ClientWith(transport, settings),
        catalog=_NoCatalog(),
    )

    disabled_router, _ = build(tmp_path / "disabled", StubTransport([]), config={"enabled": False})

    def dispatch(r, req, **kwargs):
        res = r.on_llm_request(req, dict(req), **kwargs)
        return res, (res["request"] if res is not None else req)


    # Turn 1: cron turn under CRON_POLICY (grid: glm-5.3, max_effort: medium)
    req_cron = ollama_request(model="glm-5.3")
    res_cron, cron_kwargs = dispatch(
        router,
        req_cron,
        turn_id="turn-cron",
        session_id="session-cron",
        platform="cron",
        model="glm-5.3",
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-cron:api:0",
    )
    assert res_cron is not None
    assert cron_kwargs["model"] == "glm-5.3"
    assert cron_kwargs["reasoning_effort"] == "medium"

    # Turn 2: cli turn in the same run (routes on global grid)
    req_cli = ollama_request(model="deepseek-v4.1-flash")
    res_cli, cli_kwargs = dispatch(
        router,
        req_cli,
        turn_id="turn-cli",
        session_id="session-cli",
        platform="cli",
        model="deepseek-v4.1-flash",
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-cli:api:0",
    )
    assert res_cli is not None
    assert cli_kwargs["model"] == "kimi-k3"
    assert cli_kwargs["reasoning_effort"] == "high"

    # Turn 3: cron policy with enabled: false
    current_config["platform_overrides"] = {"cron": {"enabled": False}}
    req_cron_disabled = ollama_request(model="glm-5.3")
    res_cron_disabled, disabled_cron_kwargs = dispatch(
        router,
        req_cron_disabled,
        turn_id="turn-cron-disabled",
        session_id="session-cron-disabled",
        platform="cron",
        model="glm-5.3",
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-cron-disabled:api:0",
    )
    res_ref_disabled, ref_disabled_cron_kwargs = dispatch(
        disabled_router,
        req_cron_disabled,
        turn_id="turn-ref-cron-disabled",
        session_id="session-ref-cron-disabled",
        platform="cron",
        model="glm-5.3",
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-ref-cron-disabled:api:0",
    )
    assert res_cron_disabled is None
    assert res_ref_disabled is None
    assert disabled_cron_kwargs == ref_disabled_cron_kwargs == req_cron_disabled

    # Turn 4: platform absent from the map with off-grid model
    req_unmapped = ollama_request(model="gpt-9-ultra")
    res_unmapped, unmapped_kwargs = dispatch(
        router,
        req_unmapped,
        turn_id="turn-unmapped",
        session_id="session-unmapped",
        platform="telegram",
        model="gpt-9-ultra",
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-unmapped:api:0",
    )
    res_ref_unmapped, ref_unmapped_kwargs = dispatch(
        disabled_router,
        req_unmapped,
        turn_id="turn-ref-unmapped",
        session_id="session-ref-unmapped",
        platform="telegram",
        model="gpt-9-ultra",
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-ref-unmapped:api:0",
    )
    assert res_unmapped is None
    assert res_ref_unmapped is None
    assert unmapped_kwargs == ref_unmapped_kwargs == req_unmapped

    # Audit records check
    lines = (tmp_path / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    records = [json.loads(line) for line in lines]

    # Turn 1 audit: routed on cron grid at or below ceiling
    assert records[0]["event"] == "route"
    assert records[0]["platform"] == "cron"
    assert records[0]["model"] == "glm-5.3"
    assert records[0]["effort"] == "medium"

    # Turn 2 audit: routed on global grid
    assert records[1]["event"] == "route"
    assert records[1]["platform"] == "cli"
    assert records[1]["model"] == "kimi-k3"
    assert records[1]["effort"] == "high"

    # Turn 3 audit: skip entry naming the disabled surface
    assert records[2]["event"] == "skip"
    assert records[2]["reason"] == "surface_disabled"
    assert records[2]["platform"] == "cron"

    # Turn 4 audit: unmapped surface skipped model
    assert records[3]["event"] == "skip"
    assert records[3]["reason"] == "model_not_in_grid"
    assert records[3]["platform"] == "telegram"

    # Invariant: exactly one skip entry naming the disabled surface
    disabled_skips = [r for r in records if r.get("reason") == "surface_disabled"]
    assert len(disabled_skips) == 1
    assert disabled_skips[0]["platform"] == "cron"

    # Jev transport calls: only the two routed turns reached Jev
    assert transport.call_count == 2
    assert set(transport.calls[0]["json"]["questions"]["model_route"]["criteria"]) == {"1"}
    assert set(transport.calls[1]["json"]["questions"]["model_route"]["criteria"]) == {
        "1", "2", "3", "4", "5", "6",
    }


def test_a_turn_without_a_user_message_is_skipped(tmp_path):
    transport = StubTransport([])
    router, _ = build(tmp_path, transport)
    request = ollama_request(messages=[{"role": "system", "content": "only a system message"}])

    assert route(router, request, ) is None
    assert transport.call_count == 0


def _raise_read_timeout():

    raise ReadTimeout("timed out")
