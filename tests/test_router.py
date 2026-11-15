"""The router end to end: routing, replay, degradation, and the skip gates.

These exercise the real middleware callback — the same callable Hermes invokes — against a
stub Decisions API, so the contract under test is the one that ships.
"""

from __future__ import annotations

import copy
import json

import pytest

from client import JevClient
from decider import REASON_LOW_CONFIDENCE, REASON_SKIPPED_UNKNOWN_MODEL, Decision
from stubs import (
    ReadTimeout,
    StubContext,
    StubResponse,
    StubState,
    StubTransport,
    codex_request,
    decision_payload,
    ollama_request,
)
from catalog import ModelCatalog
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


class _FixedDecision:
    """A decider whose answer is fixed — used to aim a routing decision at one model."""

    def __init__(self, decision) -> None:
        self._decision = decision

    def decide(self, messages, effective, *, platform="", provider=""):
        return self._decision, ""


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
        # Not in ``routable_providers`` (the default is ollama-cloud only): the anthropic route
        # is opt-in, so an undeclared provider stays untouched even though the router now owns
        # its payload shape — see ``tests/test_anthropic_route.py``.
        {"provider": "anthropic"},
        {"api_mode": "bedrock_converse"},
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


# -- fork tests: routable_providers, openai_compatible decider, per-provider grids/efforts --


def test_routable_providers_allowlist(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("1", 0.9, "medium", 0.9))])
    router, _ = build(
        tmp_path,
        transport,
        config={
            "routable_providers": ["ollama-cloud", "opencode-go"],
        },
    )

    # opencode-go is in routable_providers -> routes
    result = route(
        router,
        ollama_request(),
        provider="opencode-go",
        model="deepseek-v4.1-flash",
    )
    assert result is not None
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    assert transport.call_count == 1

    # openrouter is NOT in routable_providers -> skipped
    assert route(router, ollama_request(), provider="openrouter") is None
    assert transport.call_count == 1


def test_provider_specific_grid_and_effort_map(tmp_path):
    transport = StubTransport([StubResponse(decision_payload("1", 0.95, "high", 0.95, grid_size=2))])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "routable_providers": ["opencode-go"],
            "grids": {"opencode-go": ["deepseek-v4.1-flash: fast", "kimi-k3: code"]},
            "effort_maps": {"opencode-go": {"deepseek-v4.1-flash": ["low", "medium"]}},
        },
    )

    result = route(
        router,
        ollama_request(),
        provider="opencode-go",
        model="deepseek-v4.1-flash",
    )
    assert result is not None
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    # `high` is not in the declared map, so the field is removed rather than clamped to `medium`.
    assert "reasoning_effort" not in result["request"]
    record = router.tail(settings, limit=1)[0]
    assert record["effort_requested"] == "high"
    assert record["effort"] is None

    # A declared level lands verbatim on the same requested model.
    transport_declared = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "medium", 0.95, grid_size=2))]
    )
    router_declared, settings_declared = build(
        tmp_path,
        transport_declared,
        config={
            "routable_providers": ["opencode-go"],
            "grids": {"opencode-go": ["deepseek-v4.1-flash: fast", "kimi-k3: code"]},
            "effort_maps": {"opencode-go": {"deepseek-v4.1-flash": ["low", "medium"]}},
        },
    )
    declared = route(
        router_declared,
        ollama_request(),
        provider="opencode-go",
        model="deepseek-v4.1-flash",
        turn_id="turn-2",
        session_id="session-2",
    )
    assert declared is not None
    assert declared["request"]["reasoning_effort"] == "medium"
    assert router_declared.tail(settings_declared, limit=1)[0]["effort"] == "medium"


def test_openai_compatible_decider_routing_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-secret")
    resp_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"model": "deepseek-v4.1-flash", "effort": "medium", "confidence": 0.92}
                    )
                }
            }
        ]
    }
    transport = StubTransport([StubResponse(resp_payload)])
    audit_dir = tmp_path / "audit"
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_model": "deepseek-v4.1-flash",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "routable_providers": ["opencode-go"],
            "effort_maps": {"opencode-go": {"deepseek-v4.1-flash": ["low", "medium", "high"]}},
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(audit_dir))

    result = route(
        router,
        ollama_request(),
        provider="opencode-go",
        model="deepseek-v4.1-flash",
    )
    assert result is not None
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    assert result["request"]["reasoning_effort"] == "medium"

    # Verify audit record has decider
    records = router.tail(settings, limit=10)
    assert len(records) == 1
    assert records[0]["event"] == "route"
    assert records[0]["decider"] == "openai_compatible"
    assert "latency_ms" in records[0]


def test_confidence_threshold_per_decider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-secret")
    resp_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"model": "kimi-k3", "effort": "medium", "confidence": 0.75}
                    )
                }
            }
        ]
    }
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "confidence_threshold_per_decider": {"openai_compatible": 0.85},
            "default_model": "deepseek-v4.1-flash",
            "routable_providers": ["opencode-go"],
        },
    )

    req = ollama_request(model="glm-5.3")
    result = route(
        router,
        req,
        provider="opencode-go",
        model="glm-5.3",
    )
    # Fail-open: low confidence leaves original request untouched
    assert result is None
    records = router.tail(settings, limit=1)
    assert len(records) == 1
    assert records[0]["event"] == "skip"
    assert records[0]["reason"] == "low_confidence"


def test_openai_compatible_off_grid_model_leaves_original_model_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-secret")
    resp_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"model": "not-in-grid", "effort": "high", "confidence": 0.99}
                    )
                }
            }
        ]
    }
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "default_model": "deepseek-v4.1-flash",
            "routable_providers": ["opencode-go"],
        },
    )

    req = ollama_request(model="glm-5.3")
    result = route(
        router,
        req,
        provider="opencode-go",
        model="glm-5.3",
    )
    assert result is None
    records = router.tail(settings, limit=1)
    assert len(records) == 1
    assert records[0]["event"] == "skip"
    assert records[0]["reason"] == "unknown_choice"


def test_openai_compatible_invalid_confidence_leaves_original_model_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-secret")
    for bad_conf in ("garbage", -0.1, 1.5):
        resp_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"model": "kimi-k3", "effort": "low", "confidence": bad_conf}
                        )
                    }
                }
            ]
        }
        transport = StubTransport([StubResponse(resp_payload)])
        router, settings = build(
            tmp_path,
            transport,
            config={
                "decider": "openai_compatible",
                "decider_base_url": "https://opencode.ai/zen/go/v1",
                "decider_api_key_env": "OPENCODE_GO_API_KEY",
                "default_model": "deepseek-v4.1-flash",
                "routable_providers": ["opencode-go"],
            },
        )
        req = ollama_request(model="glm-5.3")
        result = route(
            router,
            req,
            provider="opencode-go",
            model="glm-5.3",
        )
        assert result is None
        records = router.tail(settings, limit=1)
        assert len(records) == 1
        assert records[0]["event"] == "skip"
        assert records[0]["reason"] == "malformed_answer"


def test_openai_compatible_replay_preserves_decider_in_turn_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-secret")
    resp_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"model": "kimi-k3", "effort": "low", "confidence": 0.95}
                    )
                }
            }
        ]
    }
    # 1. Turn mode: 2 calls in same turn share 1 transport call and both audit decider=openai_compatible
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path / "turn_mode",
        transport,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "routable_providers": ["opencode-go"],
            "route_per_turn": True,
        },
    )
    res1 = route(router, provider="opencode-go", api_call_count=1)
    res2 = route(router, provider="opencode-go", api_call_count=2, api_request_id="turn-1:api:2")
    assert res1 is not None and res2 is not None
    assert transport.call_count == 1
    records = router.tail(settings, limit=10)
    assert len(records) == 2
    assert records[0]["decider"] == "openai_compatible"
    assert records[1]["decider"] == "openai_compatible"
    assert records[1]["replayed"] is True

    # 2. Session mode: 2 turns in same session share 1 transport call and both audit decider=openai_compatible
    transport_session = StubTransport([StubResponse(resp_payload)])
    router_sess, settings_sess = build(
        tmp_path / "session_mode",
        transport_session,
        config={
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "routable_providers": ["opencode-go"],
            "route_per_turn": False,
        },
    )
    sres1 = route(router_sess, provider="opencode-go", turn_id="turn-A", session_id="sess-1")
    sres2 = route(router_sess, provider="opencode-go", turn_id="turn-B", session_id="sess-1")
    assert sres1 is not None and sres2 is not None
    assert transport_session.call_count == 1
    sess_records = router_sess.tail(settings_sess, limit=10)
    assert len(sess_records) == 2
    assert sess_records[0]["decider"] == "openai_compatible"
    assert sess_records[1]["decider"] == "openai_compatible"
    assert sess_records[1]["replayed"] is True


def test_cross_provider_memo_replay_isolation_and_effort_map_guard(tmp_path, monkeypatch):
    """A decision made for provider-a must not replay an incompatible model or leak effort maps to provider-b."""
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-secret")
    resp_payload = {
        "choices": [{"message": {"content": json.dumps({
            "model": "only-a",
            "effort": "high",
            "confidence": 0.95,
        })}}]
    }
    transport = StubTransport([StubResponse(resp_payload)])
    conf = {
        "decider": "openai_compatible",
        "decider_base_url": "https://example.com/v1",
        "decider_api_key_env": "OPENCODE_GO_API_KEY",
        "routable_providers": ["provider-a", "provider-b"],
        "grids": {
            "provider-a": [{"model": "only-a"}, {"model": "shared"}],
            "provider-b": [{"model": "shared"}, {"model": "only-b"}],
        },
        "effort_maps": {
            "provider-a": {"only-a": ["high"]},
            "provider-b": {"shared": []},
        },
    }

    class _CustomCatalog:
        def is_known(self, model_id, provider_prefixes=(), *, provider=""):
            if provider == "provider-a":
                return model_id in ("shared", "only-a")
            if provider == "provider-b":
                return model_id in ("shared", "only-b")
            return None

    router, settings = build(
        tmp_path,
        transport,
        config=conf,
        catalog=_CustomCatalog(),
    )
    req = {"model": "shared", "messages": [{"role": "user", "content": "hi"}]}

    # 1. Provider-a routes to only-a with high effort
    r1 = route(
        router,
        request=dict(req),
        model="shared",
        provider="provider-a",
        session_id="s1",
        turn_id="t1",
    )
    assert r1 is not None
    assert r1["request"]["model"] == "only-a"
    assert r1["request"].get("reasoning_effort") == "high"
    assert transport.call_count == 1

    # 2. Provider-b in same turn: provider mismatch fails open, no leakage of model or effort
    r2 = route(
        router,
        request=dict(req),
        model="shared",
        provider="provider-b",
        session_id="s1",
        turn_id="t1",
    )
    assert r2 is None
    assert transport.call_count == 1


def test_cross_provider_session_replay_isolation(tmp_path, monkeypatch):
    """In session mode (route_per_turn=False), a decision made for provider-a must not replay to provider-b."""
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-secret")
    resp_payload = {
        "choices": [{"message": {"content": json.dumps({
            "model": "only-a",
            "effort": "high",
            "confidence": 0.95,
        })}}]
    }
    transport = StubTransport([StubResponse(resp_payload)])
    conf = {
        "decider": "openai_compatible",
        "decider_base_url": "https://example.com/v1",
        "decider_api_key_env": "OPENCODE_GO_API_KEY",
        "route_per_turn": False,
        "routable_providers": ["provider-a", "provider-b"],
        "grids": {
            "provider-a": [{"model": "only-a"}, {"model": "shared"}],
            "provider-b": [{"model": "shared"}, {"model": "only-b"}],
        },
        "effort_maps": {
            "provider-a": {"only-a": ["high"]},
            "provider-b": {"shared": []},
        },
    }

    class _CustomCatalog:
        def is_known(self, model_id, provider_prefixes=(), *, provider=""):
            if provider == "provider-a":
                return model_id in ("shared", "only-a")
            if provider == "provider-b":
                return model_id in ("shared", "only-b")
            return None

    router, settings = build(
        tmp_path,
        transport,
        config=conf,
        catalog=_CustomCatalog(),
    )
    req = {"model": "shared", "messages": [{"role": "user", "content": "hi"}]}

    # Turn 1 with provider-a: decides only-a
    r1 = route(
        router,
        request=dict(req),
        model="shared",
        provider="provider-a",
        session_id="s1",
        turn_id="t1",
    )
    assert r1 is not None
    assert r1["request"]["model"] == "only-a"
    assert transport.call_count == 1

    # Turn 2 with provider-b in the same session: provider mismatch fails open
    r2 = route(
        router,
        request=dict(req),
        model="shared",
        provider="provider-b",
        session_id="s1",
        turn_id="t2",
    )
    assert r2 is None
    assert transport.call_count == 1


def test_jev_console_decider_routing_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "sk-console-secret")
    resp_payload = decision_payload(model_choice="1", model_confidence=0.95, grid_size=1)
    transport = StubTransport([StubResponse(resp_payload)])
    audit_dir = tmp_path / "audit"
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "jev_console",
            "routable_providers": ["opencode-console"],
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(audit_dir))

    result = route(
        router,
        ollama_request(),
        provider="opencode-console",
        model="deepseek-v4.1-flash",
    )
    assert result is not None
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"] == "https://opencode.ai/inference/systemone/v1/systemone"


def test_jev_console_decider_generation_route_go_uses_the_single_model_catalog(tmp_path, monkeypatch):
    """Jev Console decides; generation lands on ``opencode-go`` with its one-model catalog.

    The grid is the *effective* one — no per-provider grid is injected here — so this exercises
    what an operator actually gets: the opt-in Go route caps itself to ``deepseek-v4.1-flash``,
    and no Ollama model can be selected or offered as an alternative.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "sk-console-secret")
    resp_payload = decision_payload(
        model_choice="1",
        model_confidence=0.95,
        effort_choice="high",
        effort_confidence=0.9,
        grid_size=1,
    )
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={"decider": "jev_console", "routable_providers": ["opencode-go"]},
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    effective = settings.effective(None, provider="opencode-go")
    assert [e.model_id for e in effective.grid] == ["deepseek-v4.1-flash"]
    assert effective.default_model == "deepseek-v4.1-flash"

    # Two independent, distinguishable payloads: the router must rewrite neither in place.
    request = ollama_request(model="deepseek-v4.1-flash")
    request["injected_by_an_earlier_middleware"] = True
    original = ollama_request(model="deepseek-v4.1-flash", reasoning_config={"mode": "on"})
    request_before = json.dumps(request, sort_keys=True)
    original_before = json.dumps(original, sort_keys=True)
    original_snapshot = copy.deepcopy(original)

    result = router.on_llm_request(
        request,
        original,
        turn_id="gowire-1",
        session_id="gowire",
        platform="cli",
        model="deepseek-v4.1-flash",
        provider="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="gowire-1:api:0",
    )

    assert result is not None
    # The generation model comes from the one-model Go catalog, never from the Ollama six.
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    # The decider call goes to Console SystemOne — never to the Go generation base URL.
    assert transport.call_count == 1
    assert transport.calls[0]["url"] == "https://opencode.ai/inference/systemone/v1/systemone"
    assert transport.calls[0]["json"]["model"] == "jev-1.13-free"
    # One model is not a choice: the payload carries no model question at all, and the audit
    # records the model as fixed by the grid rather than as a choice key.
    questions = transport.calls[0]["json"]["questions"]
    assert "model_route" not in questions
    # The route's own policy declares the effort range, so `max` is offered without the operator
    # having to repeat it in `effort_maps` (the simplification this release ships).
    assert set(questions["reasoning_effort"]["criteria"]) == {"low", "medium", "high", "max"}
    # The probe-confirmed top-level reasoning_effort carries the decided level on the Go wire.
    assert result["request"]["reasoning_effort"] == "high"
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["model"] == "deepseek-v4.1-flash"
    assert record["model_choice"] == "single"
    assert record["model_confidence"] == 1.0
    assert record["effort_requested"] == "high"
    assert record["effort"] == "high"
    assert record["fallback_reasons"] == []
    assert record["alternatives"] is None
    # Neither payload was mutated in place, and the pre-middleware payload is intact.
    assert json.dumps(request, sort_keys=True) == request_before
    assert json.dumps(original, sort_keys=True) == original_before
    assert original == original_snapshot

    # Replay in the same turn reuses the decision instead of calling the decider again.
    replayed = route(
        router,
        provider="opencode-go",
        model="deepseek-v4.1-flash",
        session_id="gowire",
        turn_id="gowire-1",
        api_call_count=1,
    )
    assert replayed is not None
    assert replayed["request"]["model"] == "deepseek-v4.1-flash"
    assert transport.call_count == 1


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_opencode_go_route_lands_every_decidable_level_on_the_wire(tmp_path, monkeypatch, level):
    """Each level Jev can decide goes out verbatim as reasoning_effort, and is audited as sent."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "***")
    resp_payload = decision_payload(effort_choice=level, effort_confidence=0.9, grid_size=1)
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={"decider": "jev_console", "routable_providers": ["opencode-go"]},
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = route(
        router,
        provider="opencode-go",
        model="deepseek-v4.1-flash",
        base_url="https://opencode.ai/zen/go/v1",
        turn_id=f"golevel-{level}",
        session_id="golevel",
    )

    assert result is not None
    assert result["request"]["reasoning_effort"] == level
    record = router.tail(settings, limit=1)[0]
    assert record["effort_requested"] == level
    assert record["effort"] == level
    assert record["fallback_reasons"] == []
    assert transport.call_count == 1


def test_opencode_go_route_cap_leaves_one_model_and_no_question(tmp_path, monkeypatch):
    """A widened operator grid cannot widen the wire.

    The cap leaves a one-model grid, so the model question is not sent at all; an answer naming
    an id the route does not serve has nowhere to land, and the request keeps the catalog's only
    model.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "sk-console-secret")
    resp_payload = decision_payload(model_choice="unauthorized-model", model_confidence=0.95, grid_size=1)
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "jev_console",
            "routable_providers": ["opencode-go"],
            "grids": {
                "opencode-go": [
                    "deepseek-v4.1-flash: routine",
                    "unauthorized-model: invented",
                ],
            },
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    # The operator's grid is capped to the catalog before it ever becomes the question.
    assert [
        e.model_id for e in settings.effective(None, provider="opencode-go").grid
    ] == ["deepseek-v4.1-flash"]

    original = ollama_request(model="deepseek-v4.1-flash")
    original_before = json.dumps(original, sort_keys=True)
    result = route(router, dict(original), provider="opencode-go", model="deepseek-v4.1-flash")

    assert result is not None
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    assert json.dumps(original, sort_keys=True) == original_before
    # No model question was sent, so the invented id never had a channel to the wire.
    assert "model_route" not in transport.calls[0]["json"]["questions"]
    assert transport.call_count == 1
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["model"] == "deepseek-v4.1-flash"
    assert record["model_choice"] == "single"


def test_jev_console_decider_generation_route_commandcode_uses_the_namespaced_catalog(
    tmp_path, monkeypatch
):
    """Jev Console decides; generation lands on ``commandcode`` with its one namespaced id.

    Effective configuration only — no grid is injected — so this is what an operator gets:
    the opt-in CommandCode route is capped to ``deepseek/deepseek-v4.1-flash``, the decider is
    still Console SystemOne, and the decided level reaches this wire as the top-level
    ``reasoning_effort`` (the toggle the provider profile derives stays untouched).
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "sk-console-secret")
    resp_payload = decision_payload(
        model_choice="1",
        model_confidence=0.95,
        effort_choice="high",
        effort_confidence=0.9,
        grid_size=1,
    )
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={"decider": "jev_console", "routable_providers": ["commandcode"]},
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    effective = settings.effective(None, provider="commandcode")
    assert [e.model_id for e in effective.grid] == ["deepseek/deepseek-v4.1-flash"]
    assert effective.default_model == "deepseek/deepseek-v4.1-flash"

    request = ollama_request(model="deepseek/deepseek-v4.1-flash")
    request["injected_by_an_earlier_middleware"] = True
    original = ollama_request(
        model="deepseek/deepseek-v4.1-flash", reasoning_config={"mode": "on"}
    )
    request_before = json.dumps(request, sort_keys=True)
    original_before = json.dumps(original, sort_keys=True)
    original_snapshot = copy.deepcopy(original)

    result = router.on_llm_request(
        request,
        original,
        turn_id="ccwire-1",
        session_id="ccwire",
        platform="cli",
        model="deepseek/deepseek-v4.1-flash",
        provider="commandcode",
        base_url="https://api.commandcode.ai/provider/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="ccwire-1:api:0",
    )

    assert result is not None
    assert result["request"]["model"] == "deepseek/deepseek-v4.1-flash"
    # The bare OpenCode id of the same model must never reach this wire.
    assert result["request"]["model"] != "deepseek-v4.1-flash"
    # The decision came from Console SystemOne, never from the generation base URL.
    assert transport.call_count == 1
    assert transport.calls[0]["url"] == "https://opencode.ai/inference/systemone/v1/systemone"
    assert transport.calls[0]["json"]["model"] == "jev-1.13-free"
    # One model is not a choice: the namespaced id is fixed by the grid and the payload carries
    # no model question at all.
    questions = transport.calls[0]["json"]["questions"]
    assert "model_route" not in questions
    # The level Jev decided is on the wire as the top-level field this endpoint accepts.
    assert result["request"]["reasoning_effort"] == "high"
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["model"] == "deepseek/deepseek-v4.1-flash"
    assert record["model_choice"] == "single"
    assert record["effort_requested"] == "high"
    assert record["effort"] == "high"
    assert record["alternatives"] is None
    # Neither payload was mutated in place, and the pre-middleware payload is intact.
    assert json.dumps(request, sort_keys=True) == request_before
    assert json.dumps(original, sort_keys=True) == original_before
    assert original == original_snapshot

    # Replay in the same turn reuses the decision instead of calling the decider again.
    replayed = route(
        router,
        provider="commandcode",
        model="deepseek/deepseek-v4.1-flash",
        session_id="ccwire",
        turn_id="ccwire-1",
        api_call_count=1,
    )
    assert replayed is not None
    assert replayed["request"]["model"] == "deepseek/deepseek-v4.1-flash"
    assert replayed["request"]["reasoning_effort"] == "high"
    assert transport.call_count == 1


def test_commandcode_lands_the_jev_level_beside_the_profile_thinking_toggle(
    tmp_path, monkeypatch
):
    """The real profile shape for this id: ``extra_body.thinking`` **and** ``reasoning_effort``.

    CommandCode's profile delegates ``deepseek/*`` to the DeepSeek profile, whose
    ``thinking_toggle_extras(..., always_emit_toggle=True)`` emits the toggle **and** its own
    top-level ``reasoning_effort`` derived from the params-level ``reasoning_config``. The
    transport builds that payload before the middleware and the routed payload replaces it, so
    the plugin's level lands *after* the profile's derivation: it overrides the level the
    profile would have sent and leaves the toggle alone. Both then coexist on the wire.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    resp_payload = decision_payload(
        model_choice="1",
        model_confidence=0.95,
        effort_choice="high",
        effort_confidence=0.9,
        grid_size=1,
    )
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={"decider": "jev_console", "routable_providers": ["commandcode"]},
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    request = ollama_request(
        model="deepseek/deepseek-v4.1-flash", reasoning_config={"effort": "low"}
    )
    # Exactly what the provider profile puts on this wire for this id, derived from that config.
    request["extra_body"] = {"thinking": {"type": "enabled"}}
    request["reasoning_effort"] = "low"
    request_before = json.dumps(request, sort_keys=True)
    original = copy.deepcopy(request)
    original_before = json.dumps(original, sort_keys=True)

    result = router.on_llm_request(
        request,
        original,
        turn_id="ccnative-1",
        session_id="ccnative",
        platform="cli",
        model="deepseek/deepseek-v4.1-flash",
        provider="commandcode",
        base_url="https://api.commandcode.ai/provider/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="ccnative-1:api:0",
    )

    assert result is not None
    routed = result["request"]
    # The Jev level replaces the profile-derived one (low -> high), which is the documented
    # precedence: the middleware payload is what the call is made with.
    assert routed["reasoning_effort"] == "high"
    # The toggle and the params-level input owned by the provider layer survive untouched.
    assert routed["extra_body"] == {"thinking": {"type": "enabled"}}
    assert routed["reasoning_config"] == {"effort": "low"}
    assert routed["model"] == "deepseek/deepseek-v4.1-flash"
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["effort_requested"] == "high"
    assert record["effort"] == "high"
    # Neither input payload was mutated in place.
    assert json.dumps(request, sort_keys=True) == request_before
    assert json.dumps(original, sort_keys=True) == original_before

    # Replay rebuilds the same payload from the same snapshot, without a second decider call.
    replayed = route(
        router,
        request,
        provider="commandcode",
        model="deepseek/deepseek-v4.1-flash",
        base_url="https://api.commandcode.ai/provider/v1",
        api_mode="chat_completions",
        session_id="ccnative",
        turn_id="ccnative-1",
        api_call_count=1,
        api_request_id="ccnative-1:api:1",
    )
    assert replayed is not None
    assert json.dumps(replayed["request"], sort_keys=True) == json.dumps(routed, sort_keys=True)
    assert transport.call_count == 1


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_commandcode_route_lands_every_decidable_level_on_the_wire(tmp_path, monkeypatch, level):
    """Each level Jev can decide goes out verbatim as reasoning_effort, and is audited as sent."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "***")
    resp_payload = decision_payload(effort_choice=level, effort_confidence=0.9, grid_size=1)
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={"decider": "jev_console", "routable_providers": ["commandcode"]},
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = route(
        router,
        provider="commandcode",
        model="deepseek/deepseek-v4.1-flash",
        base_url="https://api.commandcode.ai/provider/v1",
        turn_id=f"cclevel-{level}",
        session_id="cclevel",
    )

    assert result is not None
    assert result["request"]["reasoning_effort"] == level
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["provider"] == "commandcode"
    assert record["decider"] == "jev_console"
    assert record["effort_requested"] == level
    assert record["effort"] == level
    assert record["fallback_reasons"] == []
    assert transport.call_count == 1


def test_commandcode_route_cap_leaves_one_model_and_no_question(tmp_path, monkeypatch):
    """A widened operator grid cannot widen the wire (see the Go counterpart).

    The cap leaves one model, the model question is not sent, and the bare OpenCode id named by
    the answer never reaches this wire.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "sk-console-secret")
    resp_payload = decision_payload(
        model_choice="unauthorized-model", model_confidence=0.95, grid_size=1
    )
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "jev_console",
            "routable_providers": ["commandcode"],
            "grids": {
                "commandcode": [
                    "deepseek/deepseek-v4.1-flash: routine",
                    "deepseek-v4.1-flash: bare OpenCode id",
                ],
            },
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    # The operator's grid is capped to the catalog before it ever becomes the question.
    assert [
        e.model_id for e in settings.effective(None, provider="commandcode").grid
    ] == ["deepseek/deepseek-v4.1-flash"]

    original = ollama_request(model="deepseek/deepseek-v4.1-flash")
    original_before = json.dumps(original, sort_keys=True)
    result = route(
        router, dict(original), provider="commandcode", model="deepseek/deepseek-v4.1-flash"
    )

    assert result is not None
    assert result["request"]["model"] == "deepseek/deepseek-v4.1-flash"
    assert result["request"]["model"] != "deepseek-v4.1-flash"
    assert json.dumps(original, sort_keys=True) == original_before
    assert "model_route" not in transport.calls[0]["json"]["questions"]
    assert transport.call_count == 1
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["model"] == "deepseek/deepseek-v4.1-flash"
    assert record["model_choice"] == "single"


def test_console_route_no_longer_applies_normal_mimo(tmp_path, monkeypatch):
    """Normal MiMo-V2.6-Flash is a Go model: the Console route must fail open on it."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "sk-console-secret")
    resp_payload = decision_payload(model_choice="2", model_confidence=0.99, grid_size=1)
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={"decider": "jev_console", "routable_providers": ["opencode-console"]},
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    assert [e.model_id for e in settings.effective(None, provider="opencode-console").grid] == [
        "deepseek-v4.1-flash"
    ]

    original = ollama_request(model="deepseek-v4.1-flash")
    original_before = json.dumps(original, sort_keys=True)
    result = route(router, dict(original), provider="opencode-console", model="deepseek-v4.1-flash")

    # The route carries one model, so there is no model question to answer: an answer naming a
    # model the Console does not serve has nowhere to land, and the wire keeps the only model.
    assert result is not None
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    assert json.dumps(original, sort_keys=True) == original_before
    assert "model_route" not in transport.calls[0]["json"]["questions"]


def test_opencode_go_legacy_decider_keeps_its_configured_grid(tmp_path, monkeypatch):
    """The Go catalog cap is opt-in: a legacy decider keeps what the operator configured."""
    resp_payload = decision_payload(model_choice="2", model_confidence=0.95, grid_size=2)
    transport = StubTransport([StubResponse(resp_payload)])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "routable_providers": ["opencode-go"],
            "grids": {"opencode-go": ["deepseek-v4.1-flash: fast", "kimi-k3: code"]},
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    assert [e.model_id for e in settings.effective(None, provider="opencode-go").grid] == [
        "deepseek-v4.1-flash",
        "kimi-k3",
    ]


def test_cross_decider_memo_replay_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "sk-console-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    resp_payload = decision_payload(model_choice="1", model_confidence=0.95, grid_size=1)
    transport = StubTransport([StubResponse(resp_payload)])
    audit_dir = tmp_path / "audit"
    current_config = {
        "decider": "jev_console",
        "routable_providers": ["opencode-console"],
    }
    audit = StubState(tmp_path)
    router = Router(
        lambda: load_settings(lambda k, d=None: current_config.get(k, d)),
        get_state=lambda: audit,
        client_factory=lambda s: _ClientWith(transport, s),
        catalog=_NoCatalog(),
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(audit_dir))

    # Turn 1: routes with jev_console
    res1 = route(
        router,
        ollama_request(),
        provider="opencode-console",
        model="deepseek-v4.1-flash",
        session_id="s-dec",
        turn_id="t1",
    )
    assert res1 is not None
    assert res1["request"]["model"] == "deepseek-v4.1-flash"
    assert transport.call_count == 1

    # Switch active decider to legacy "jev" (which has no OPENROUTER_API_KEY in env)
    current_config["decider"] = "jev"

    # Turn 1 retry / replay in same turn with different decider: fails open due to decider mismatch
    res2 = route(
        router,
        ollama_request(),
        provider="opencode-console",
        model="deepseek-v4.1-flash",
        session_id="s-dec",
        turn_id="t1",
    )
    assert res2 is None


def test_opencode_console_distinguishes_logical_effort_from_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    # Jev returns model choice "1" (deepseek-v4.1-flash) and effort choice "high" with confidence 0.95
    resp_payload = decision_payload(
        model_choice="1",
        model_confidence=0.95,
        effort_choice="high",
        effort_confidence=0.95,
        grid_size=1,
    )
    transport = StubTransport([StubResponse(resp_payload)])
    audit_dir = tmp_path / "audit"
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "jev_console",
            "routable_providers": ["opencode-console"],
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(audit_dir))

    result = route(
        router,
        ollama_request(),
        provider="opencode-console",
        model="deepseek-v4.1-flash",
    )
    assert result is not None
    assert result["request"]["model"] == "deepseek-v4.1-flash"
    # Wire payload MUST omit reasoning_effort because opencode-console support is unconfirmed
    assert "reasoning_effort" not in result["request"]

    # Audit record must distinguish logical effort (high) from wire (None / omitted)
    records = router.tail(settings, limit=10)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "route"
    assert record["model"] == "deepseek-v4.1-flash"
    assert record["effort_requested"] == "high"
    assert record["effort_choice"] == "high"
    assert record["effort"] is None  # Never claimed as sent on wire


def test_memo_replay_invalidated_by_confidence_threshold_elevation(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    # Model confidence 0.95, effort confidence 0.70
    resp_payload = decision_payload(
        model_choice="1",
        model_confidence=0.95,
        effort_choice="medium",
        effort_confidence=0.70,
        grid_size=1,
    )
    transport = StubTransport([StubResponse(resp_payload)])
    current_config = {
        "decider": "jev_console",
        "routable_providers": ["opencode-console"],
        "confidence_threshold": 0.50,
    }
    audit = StubState(tmp_path)
    router = Router(
        lambda: load_settings(lambda k, d=None: current_config.get(k, d)),
        get_state=lambda: audit,
        client_factory=lambda s: _ClientWith(transport, s),
        catalog=_NoCatalog(),
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    # First call: applies memo
    res1 = route(router, ollama_request(), provider="opencode-console", session_id="s1", turn_id="t1")
    assert res1 is not None
    assert res1["request"]["model"] == "deepseek-v4.1-flash"
    assert transport.call_count == 1

    # Elevate threshold above effort confidence (0.80 > 0.70)
    current_config["confidence_threshold"] = 0.80

    # Second call in same turn: fail open because memo effort confidence is below active threshold
    res2 = route(router, ollama_request(), provider="opencode-console", session_id="s1", turn_id="t1", api_call_count=1)
    assert res2 is None
    assert transport.call_count == 1  # No extra decider call was made


def test_memo_replay_invalidated_by_fallback_backend_or_key_env_change(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    monkeypatch.setenv("CUSTOM_CONSOLE_KEY", "custom-console-key")
    resp_payload = decision_payload(model_choice="1", model_confidence=0.95, grid_size=1)
    transport = StubTransport([StubResponse(resp_payload)])
    current_config = {
        "decider": "jev_console",
        "routable_providers": ["opencode-console"],
        "console_jev_fallback_model": "jev-1.13",
        "console_api_key_env": "OPENCODE_CONSOLE_API_KEY",
    }
    audit = StubState(tmp_path)
    router = Router(
        lambda: load_settings(lambda k, d=None: current_config.get(k, d)),
        get_state=lambda: audit,
        client_factory=lambda s: _ClientWith(transport, s),
        catalog=_NoCatalog(),
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    res1 = route(router, ollama_request(), provider="opencode-console", session_id="s2", turn_id="t1")
    assert res1 is not None

    # Change fallback backend model -> invalidates replay
    current_config["console_jev_fallback_model"] = "other-fallback-model"
    res2 = route(router, ollama_request(), provider="opencode-console", session_id="s2", turn_id="t1", api_call_count=1)
    assert res2 is None

    # Reset fallback model, verify replay with key env change invalidation
    transport2 = StubTransport([StubResponse(resp_payload)])
    current_config["console_jev_fallback_model"] = "jev-1.13"
    router2 = Router(
        lambda: load_settings(lambda k, d=None: current_config.get(k, d)),
        get_state=lambda: audit,
        client_factory=lambda s: _ClientWith(transport2, s),
        catalog=_NoCatalog(),
    )
    monkeypatch.setattr(router2, "_data_dir", lambda: str(tmp_path / "audit"))
    res3 = route(router2, ollama_request(), provider="opencode-console", session_id="s3", turn_id="t1")
    assert res3 is not None

    # Change key env -> invalidates replay
    current_config["console_api_key_env"] = "CUSTOM_CONSOLE_KEY"
    res4 = route(router2, ollama_request(), provider="opencode-console", session_id="s3", turn_id="t1", api_call_count=1)
    assert res4 is None


def test_middleware_rejects_invented_criteria_or_outsider_choice(tmp_path, monkeypatch, caplog):
    secret_key = "test-secret-console-key-probe1"
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", secret_key)
    # A multi-model grid is the only setup where a model choice exists to be validated.
    p = decision_payload(model_choice="2", model_confidence=0.99, effort_choice="high", effort_confidence=0.8, grid_size=6)
    p["answers"]["model_route"]["choice"] = "outsider"
    p["answers"]["model_route"]["criteria"] = {"2": "outsider"}

    transport = StubTransport([StubResponse(p)])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "jev_console",
            "routable_providers": ["ollama-cloud"],
            "console_jev_fallback_model": "",
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    original_req = ollama_request(model="deepseek-v4.1-flash")
    req_copy = copy.deepcopy(original_req)
    req_json_before = json.dumps(original_req, sort_keys=True)

    result = route(router, original_req, provider="ollama-cloud")
    assert result is None
    assert json.dumps(original_req, sort_keys=True) == req_json_before
    assert original_req == req_copy
    assert secret_key not in caplog.text


def test_middleware_zero_effort_confidence_replay_policy_and_no_effort_response(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key-probe2")
    # Case A: Probe 2 - zero effort confidence distinguishes presence from value
    p_zero_effort = decision_payload(model_choice="1", model_confidence=0.99, effort_choice="high", effort_confidence=0.0, grid_size=1)
    transport = StubTransport([StubResponse(p_zero_effort)])
    current_config = {
        "decider": "jev_console",
        "routable_providers": ["opencode-console"],
        "console_jev_fallback_model": "",
        "confidence_threshold": 0.0,
    }
    audit = StubState(tmp_path)
    router = Router(
        lambda: load_settings(lambda k, d=None: current_config.get(k, d)),
        get_state=lambda: audit,
        client_factory=lambda s: _ClientWith(transport, s),
        catalog=_NoCatalog(),
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    req1 = ollama_request(model="deepseek-v4.1-flash")
    res1 = route(router, req1, provider="opencode-console", session_id="s_zero", turn_id="t1")
    assert res1 is not None
    assert res1["request"]["model"] == "deepseek-v4.1-flash"
    assert transport.call_count == 1

    # Elevate threshold to 0.5 in same turn: replay must FAIL because effort_confidence 0.0 < 0.5
    current_config["confidence_threshold"] = 0.5
    res2 = route(router, req1, provider="opencode-console", session_id="s_zero", turn_id="t1", api_call_count=1)
    assert res2 is None
    assert transport.call_count == 1  # No extra decider call

    # Fresh decision with threshold 0.5 also returns None (low_confidence)
    fresh_decision, fresh_reason = JevClient(
        load_settings(lambda k, d=None: current_config.get(k, d)),
        transport=StubTransport([StubResponse(p_zero_effort)]),
    ).decide(req1["messages"], load_settings(lambda k, d=None: current_config.get(k, d)).effective(None, provider="opencode-console"), provider="opencode-console")
    assert fresh_decision is None
    assert fresh_reason == REASON_LOW_CONFIDENCE

    # Case B: Response without effort question (include_effort=False)
    p_no_effort = decision_payload(model_choice="1", model_confidence=0.99, grid_size=1, include_effort=False)
    transport_no_eff = StubTransport([StubResponse(p_no_effort)])
    current_config["confidence_threshold"] = 0.5
    router_no_eff = Router(
        lambda: load_settings(lambda k, d=None: current_config.get(k, d)),
        get_state=lambda: audit,
        client_factory=lambda s: _ClientWith(transport_no_eff, s),
        catalog=_NoCatalog(),
    )
    monkeypatch.setattr(router_no_eff, "_data_dir", lambda: str(tmp_path / "audit"))

    req2 = ollama_request(model="deepseek-v4.1-flash")
    res3 = route(router_no_eff, req2, provider="opencode-console", session_id="s_no_eff", turn_id="t1")
    assert res3 is not None
    assert res3["request"]["model"] == "deepseek-v4.1-flash"
    # Second call in same turn: replay should succeed because response has no effort confidence
    res4 = route(router_no_eff, req2, provider="opencode-console", session_id="s_no_eff", turn_id="t1", api_call_count=1)
    assert res4 is not None
    assert res4["request"]["model"] == "deepseek-v4.1-flash"
    assert transport_no_eff.call_count == 1


_CONSOLE_TEST_SECRET = "test-console-secret-key-999888"


@pytest.mark.parametrize(
    "case_name,resp_or_exc",
    [
        ("http_401", StubResponse({"error": f"unauthorized bearer {_CONSOLE_TEST_SECRET}"}, status_code=401)),
        ("http_403", StubResponse({"error": f"forbidden key {_CONSOLE_TEST_SECRET}"}, status_code=403)),
        ("http_429", StubResponse({"error": f"rate limited on {_CONSOLE_TEST_SECRET}"}, status_code=429)),
        ("http_500", StubResponse({"error": f"internal error key={_CONSOLE_TEST_SECRET}"}, status_code=500)),
        ("http_503", StubResponse({"error": f"unavailable server key {_CONSOLE_TEST_SECRET}"}, status_code=503)),
        ("timeout", ReadTimeout(f"upstream timeout connecting with Bearer {_CONSOLE_TEST_SECRET}")),
        ("real_non_json_html", StubResponse(payload=None, status_code=200, text=f"<html><body>Gateway Timeout {_CONSOLE_TEST_SECRET}</body></html>")),
        ("real_non_json_raw", StubResponse(payload=None, status_code=200, text=f"Plain error text with auth {_CONSOLE_TEST_SECRET}")),
        ("empty_dict", StubResponse({})),
        ("missing_answers_envelope", StubResponse({"result": "ok"})),
        ("answers_not_dict", StubResponse({"answers": ["not", "a", "dict"]})),
        ("confidence_bool", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": True, "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("confidence_string", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": "0.95", "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("confidence_nan", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": float("nan"), "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("confidence_inf", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": float("inf"), "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("confidence_negative", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": -0.5, "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("confidence_above_one", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 1.5, "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("confidence_below_threshold", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.3, "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("probabilities_null", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": None}}})),
        ("probabilities_not_dict", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": [0.5, 0.5]}}})),
        ("probabilities_outside_domain", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"outsider": 1.0}}}})),
        ("probabilities_bool_val", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"1": True, "2": False}}}})),
        ("probabilities_nan_val", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"1": float("nan"), "2": 0.5}}}})),
        ("probabilities_sum_mismatch", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"1": 0.2, "2": 0.2}}}})),
        ("choice_missing", StubResponse({"answers": {"model_route": {"type": "choice", "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("choice_empty", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "   ", "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("choice_outside_grid", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "outsider", "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("choice_invented_criteria", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "outsider", "criteria": {"2": "outsider"}, "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}}})),
        ("effort_confidence_bool", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}, "reasoning_effort": {"type": "choice", "choice": "high", "confidence": True, "probabilities": {"low": 0.0, "medium": 0.0, "high": 1.0}}}})),
        ("effort_confidence_nan", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}, "reasoning_effort": {"type": "choice", "choice": "high", "confidence": float("nan"), "probabilities": {"low": 0.0, "medium": 0.0, "high": 1.0}}}})),
        ("effort_probabilities_bad_key", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}, "reasoning_effort": {"type": "choice", "choice": "high", "confidence": 0.95, "probabilities": {"extreme": 1.0}}}})),
        ("effort_choice_invalid", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}, "reasoning_effort": {"type": "choice", "choice": "maximum", "confidence": 0.95, "probabilities": {"low": 0.0, "medium": 0.0, "high": 1.0}}}})),
        ("effort_criteria_remapped", StubResponse({"answers": {"model_route": {"type": "choice", "choice": "2", "confidence": 0.95, "probabilities": {"1": 0.0, "2": 1.0}}, "reasoning_effort": {"type": "choice", "choice": "high", "criteria": {"3": "extreme"}, "confidence": 0.95, "probabilities": {"low": 0.0, "medium": 0.0, "high": 1.0}}}})),
    ],
)
def test_middleware_fail_open_console_negative_matrix(tmp_path, monkeypatch, caplog, case_name, resp_or_exc):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", _CONSOLE_TEST_SECRET)
    transport = StubTransport([resp_or_exc])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "jev_console",
            # Multi-model grid: the negative matrix exercises the model *and* effort answers.
            "routable_providers": ["ollama-cloud"],
            "console_jev_fallback_model": "",  # fallback explicitly disabled
            "confidence_threshold": 0.70,
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    working_req = {
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "analyze problem step"}],
        "temperature": 0.7,
        "working_stage": "pre_routing",
        "metadata": {"caller": "orchestrator", "hop": 1, "nested": {"active": True}},
    }
    original_req = {
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "analyze problem step"}],
        "temperature": 0.7,
        "pristine_stage": "entrypoint",
        "metadata": {"caller": "client", "hop": 0, "nested": {"active": False}},
    }
    working_snapshot = json.dumps(working_req, sort_keys=True)
    original_snapshot = json.dumps(original_req, sort_keys=True)
    working_copy = copy.deepcopy(working_req)
    original_copy = copy.deepcopy(original_req)

    result = router.on_llm_request(
        working_req,
        original_req,
        turn_id=f"turn-{case_name}",
        session_id=f"session-{case_name}",
        platform="cli",
        model="deepseek-v4.1-flash",
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id=f"turn-{case_name}:api:0",
    )
    assert result is None, f"Expected fail-open (None) for case {case_name}"
    # Verify working request unchanged (both top-level and nested)
    assert json.dumps(working_req, sort_keys=True) == working_snapshot
    assert working_req == working_copy
    # Verify original request unchanged (both top-level and nested)
    assert json.dumps(original_req, sort_keys=True) == original_snapshot
    assert original_req == original_copy

    # Verify audit trail recorded a skip without leaking secret
    audit_file = tmp_path / "audit" / "routes.jsonl"
    assert audit_file.exists(), f"Expected audit file for case {case_name}"
    records = router.tail(settings, limit=10)
    assert len(records) >= 1
    assert records[-1]["event"] == "skip"
    audit_text = audit_file.read_text(encoding="utf-8")
    assert _CONSOLE_TEST_SECRET not in audit_text
    # Verify secret is never logged in caplog and redaction is exercised when present
    assert _CONSOLE_TEST_SECRET not in caplog.text
    if case_name in ("http_401", "http_403", "http_429", "http_500", "http_503", "real_non_json_html", "real_non_json_raw"):
        assert "[REDACTED]" in caplog.text


def test_middleware_fail_open_both_models_fail(tmp_path, monkeypatch, caplog):
    secret_key = "test-console-secret-both-fail"
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", secret_key)
    transport = StubTransport([
        StubResponse({"error": f"primary failed with {secret_key}"}, status_code=500),
        StubResponse({"error": f"fallback failed with {secret_key}"}, status_code=500),
    ])
    router, settings = build(
        tmp_path,
        transport,
        config={
            "decider": "jev_console",
            "routable_providers": ["opencode-console"],
            "console_jev_fallback_model": "jev-1.13",
        },
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    working_req = {
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "analyze problem step"}],
        "temperature": 0.7,
        "working_stage": "pre_routing",
        "metadata": {"caller": "orchestrator", "hop": 1, "nested": {"active": True}},
    }
    original_req = {
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "analyze problem step"}],
        "temperature": 0.7,
        "pristine_stage": "entrypoint",
        "metadata": {"caller": "client", "hop": 0, "nested": {"active": False}},
    }
    working_snapshot = json.dumps(working_req, sort_keys=True)
    original_snapshot = json.dumps(original_req, sort_keys=True)
    working_copy = copy.deepcopy(working_req)
    original_copy = copy.deepcopy(original_req)

    result = router.on_llm_request(
        working_req,
        original_req,
        turn_id="turn-both-fail",
        session_id="session-both-fail",
        platform="cli",
        model="deepseek-v4.1-flash",
        provider="opencode-console",
        base_url="https://opencode.ai/inference/openai/v1",
        api_mode="chat_completions",
        api_call_count=0,
        api_request_id="turn-both-fail:api:0",
    )
    assert result is None
    # Verify working request unchanged (both top-level and nested)
    assert json.dumps(working_req, sort_keys=True) == working_snapshot
    assert working_req == working_copy
    # Verify original request unchanged (both top-level and nested)
    assert json.dumps(original_req, sort_keys=True) == original_snapshot
    assert original_req == original_copy

    audit_file = tmp_path / "audit" / "routes.jsonl"
    assert audit_file.exists()
    records = router.tail(settings, limit=10)
    assert len(records) >= 1
    assert records[-1]["event"] == "skip"
    audit_text = audit_file.read_text(encoding="utf-8")
    assert secret_key not in audit_text
    assert secret_key not in caplog.text
    assert "[REDACTED]" in caplog.text


# -- the Codex Responses route -------------------------------------------------------


CODEX_CONFIG = {"decider": "jev_console", "routable_providers": ["openai-codex"]}


def codex_route(router, request=None, **overrides):
    """``route`` with the Codex route's payload shape and provider."""
    kwargs = dict(
        model="gpt-6-luna",
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
    )
    kwargs.update(overrides)
    return route(router, request if request is not None else codex_request(), **kwargs)


def test_codex_route_is_not_skipped_and_lands_the_level_under_reasoning(tmp_path, monkeypatch):
    """The Responses body gets ``reasoning.effort``; a top-level ``reasoning_effort`` never appears.

    That key is not a Responses field at all — the SDK call would raise ``TypeError`` before a
    byte was sent — so the router must write the nested one and leave nothing at the top level.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("3", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=CODEX_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = codex_route(router, turn_id="codex-1", session_id="codex")

    assert result is not None
    routed = result["request"]
    assert routed["model"] == "gpt-6.1-sol"
    assert routed["reasoning"] == {"effort": "high", "summary": "auto"}
    # ``include``/``store``/``instructions`` are the transport's and survive untouched.
    assert routed["include"] == ["reasoning.encrypted_content"]
    assert routed["store"] is False
    assert routed["instructions"] == "you are a test"
    assert "reasoning_effort" not in routed
    # The params-level input the transport already consumed is not resurrected either.
    assert "reasoning_config" not in routed

    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["provider"] == "openai-codex"
    assert record["model"] == "gpt-6.1-sol"
    assert record["effort"] == "high"
    assert record["effort_requested"] == "high"
    assert record["fallback_reasons"] == []
    assert transport.call_count == 1


@pytest.mark.parametrize("level", ["low", "medium", "high"])
@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol"])
def test_codex_route_lands_every_decidable_level_on_the_wire(tmp_path, monkeypatch, level, model):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, level, 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=CODEX_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = codex_route(
        router,
        codex_request(model=model),
        model=model,
        turn_id=f"codex-{model}-{level}",
        session_id=f"codex-{model}-{level}",
    )

    assert result is not None
    assert result["request"]["model"] == "gpt-6-luna"
    assert result["request"]["reasoning"]["effort"] == level
    record = router.tail(settings, limit=1)[0]
    assert record["effort"] == level
    assert record["effort_requested"] == level


def test_codex_picker_alias_never_reaches_the_wire(tmp_path, monkeypatch):
    """Jev may pick ``gpt-6-luna-900k``; the wire id is the base slug the backend serves.

    The transport strips the suffix while *building* the body, before this middleware runs, so a
    router that wrote the picker id back would send a model the endpoint does not know. The audit
    keeps the two apart: ``model`` is the id Jev chose, ``wire_model`` is the id that was sent —
    the alias buys no capability here, and the record must not imply one.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("2", 0.95, "medium", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=CODEX_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = codex_route(router, turn_id="codex-alias", session_id="codex-alias")

    assert result is not None
    assert result["request"]["model"] == "gpt-6-luna"
    record = router.tail(settings, limit=1)[0]
    assert record["model"] == "gpt-6-luna-900k"
    assert record["wire_model"] == "gpt-6-luna"


def test_codex_route_never_offers_an_id_the_backend_does_not_serve(tmp_path, monkeypatch):
    """``gpt-6.1-sol-900k`` is not a model here, so it is not an option the route may offer.

    Reproduced against the real ``Router``, ``Settings`` and ``ModelCatalog`` (no network): the
    default catalog carries three ids, the route allowlist answers ``False`` for the dead one
    instead of "no evidence", an operator grid naming it is capped away, and a decision that
    names it anyway leaves the request byte-identical — an unrouted turn, not a 400.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    settings = load_settings(lambda key, default=None: CODEX_CONFIG.get(key, default))
    catalog = ModelCatalog(cache_path=tmp_path / "absent-cache.json")

    expected = ["gpt-6-luna", "gpt-6-luna-900k", "gpt-6.1-sol"]
    assert [entry.model_id for entry in settings.grid_for("openai-codex")] == expected
    assert (
        catalog.is_known("gpt-6.1-sol-900k", provider="openai-codex", decider="jev_console")
        is False
    )

    # An operator grid naming the dead id cannot reintroduce it: the allowlist cap drops it, and
    # a grid the cap empties falls back to the route's own catalog.
    capped = load_settings(
        lambda key, default=None: {
            "decider": "jev_console",
            "routable_providers": ["openai-codex"],
            "grids": {"openai-codex": ["gpt-6.1-sol-900k: big reasoning"]},
        }.get(key, default)
    )
    assert [
        entry.model_id for entry in capped.effective(None, provider="openai-codex").grid
    ] == expected

    # And a decision that names it anyway is refused before it can reach the provider.
    dead = Decision(
        model="gpt-6.1-sol-900k",
        effort="medium",
        effort_requested="medium",
        model_confidence=0.99,
        effort_confidence=0.9,
        model_choice="4",
        effort_choice="medium",
    )
    router = Router(
        lambda: settings,
        get_state=lambda: StubState(tmp_path),
        client_factory=lambda _settings: _FixedDecision(dead),
        catalog=catalog,
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))
    request = codex_request(model="gpt-6-luna")

    assert (
        router.on_llm_request(
            request,
            dict(request),
            turn_id="codex-dead",
            session_id="codex-dead",
            platform="cli",
            model="gpt-6-luna",
            provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            api_mode="codex_responses",
            api_call_count=0,
            api_request_id="codex-dead:api:0",
        )
        is None
    )
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "skip"
    assert record["reason"] == REASON_SKIPPED_UNKNOWN_MODEL
    assert record["chosen_model"] == "gpt-6.1-sol-900k"


def test_codex_picker_alias_from_an_operator_grid_still_sends_the_base_slug(
    tmp_path, monkeypatch
):
    """An operator ``grids.openai-codex`` entry carries a description, not a wire id.

    Caught live: with the grid pinned to ``gpt-6-luna-900k`` the routed payload carried the
    picker id itself, the backend answered HTTP 400 "model entitlement", and Hermes fell back to
    another model *after* the audit had recorded the route. The catalog is what knows the id the
    endpoint serves, so the lookup must not depend on the configured grid entry.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    config = {
        "decider": "jev_console",
        "routable_providers": ["openai-codex"],
        "grids": {"openai-codex": ["gpt-6-luna-900k: big context"]},
    }
    transport = StubTransport(
        [StubResponse(decision_payload("1", 1.0, "low", 1.0, grid_size=1))]
    )
    router, settings = build(tmp_path, transport, config=config)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = codex_route(
        router,
        codex_request(model="gpt-6-luna-900k"),
        model="gpt-6-luna-900k",
        turn_id="codex-grid-alias",
        session_id="codex-grid-alias",
    )

    assert result is not None
    assert router.tail(settings, limit=1)[0]["model"] == "gpt-6-luna-900k"
    assert result["request"]["model"] == "gpt-6-luna"


def test_codex_reads_the_conversation_from_input_not_messages(tmp_path, monkeypatch):
    """A Responses body has no ``messages``: the state must be built from ``input``."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "medium", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=CODEX_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    request = codex_request(
        input_items=[
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "an answer"}],
            },
            {"type": "function_call", "name": "read_file", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call_output", "call_id": "c1", "output": "file body"},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "refactor the parser"}],
            },
        ]
    )

    result = codex_route(router, request, turn_id="codex-input", session_id="codex-input")

    assert result is not None
    state = transport.last_payload["state"]
    assert state["user_message"] == "refactor the parser"
    # Tool items carry no role and stay out of the routing state, exactly as on chat_completions.
    assert "read_file" not in state["recent_context"]
    assert "file body" not in state["recent_context"]
    assert "assistant: an answer" in state["recent_context"]
    assert state["provider"] == "openai-codex"


def test_codex_payload_without_a_reasoning_object_keeps_the_audit_honest(tmp_path, monkeypatch):
    """The transport decides whether a route takes a reasoning field; the router never invents one.

    A route it judged field-less has no ``reasoning`` key, and adding one is the 400 class that
    guard exists to avoid — so the level is simply not sent, and the audit says so.
    """
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=CODEX_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    request = codex_request()
    del request["reasoning"]

    result = codex_route(router, request, turn_id="codex-noreason", session_id="codex-noreason")

    assert result is not None
    assert "reasoning" not in result["request"]
    record = router.tail(settings, limit=1)[0]
    # The wire carried no level; ``effort_requested`` still names Jev's choice.
    assert record["effort"] is None
    assert record["effort_requested"] == "high"


def test_codex_route_replays_the_same_body_without_a_second_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("3", 0.95, "high", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=CODEX_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    request = codex_request()
    first = codex_route(router, request, turn_id="codex-replay", session_id="codex-replay")
    second = codex_route(
        router, request, turn_id="codex-replay", session_id="codex-replay", api_call_count=1
    )

    assert first is not None and second is not None
    assert json.dumps(second["request"], sort_keys=True) == json.dumps(first["request"], sort_keys=True)
    assert transport.call_count == 1


def test_codex_alias_provider_names_the_same_route(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "test-console-key")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "low", 0.9, grid_size=3))]
    )
    router, _ = build(tmp_path, transport, config=CODEX_CONFIG)

    result = codex_route(
        router, provider="codex", turn_id="codex-alias-provider", session_id="codex-alias-provider"
    )

    assert result is not None
    assert result["request"]["reasoning"]["effort"] == "low"


def test_chat_completions_still_writes_the_top_level_field_only(tmp_path):
    """The widened api-mode gate must not change the chat-completions payload shape."""
    transport = StubTransport([StubResponse(decision_payload("2", 0.9, "high", 0.8))])
    router, _ = build(tmp_path, transport)

    result = route(router)

    assert result is not None
    assert result["request"]["reasoning_effort"] == "high"
    assert "reasoning" not in result["request"]



# -- the declarative contract, end to end -------------------------------------------

#: The maps the ``code`` profile is being migrated to, trimmed to what these tests route.
CHAT_ROUTE_MAP_CONFIG = {
    "decider": "jev_console",
    "routable_providers": ["opencode-go", "commandcode"],
    "effort_maps": {
        "opencode-go": {
            "deepseek-v4.1-flash": {
                "accepted": ["low", "medium", "high", "max"],
                "overrides": {"xhigh": "max"},
            }
        },
        "commandcode": {
            "deepseek/deepseek-v4.1-flash": {
                "accepted": ["low", "medium", "high", "max"],
                "overrides": {"xhigh": "max"},
            }
        },
    },
}

CODEX_ROUTE_MAP_CONFIG = {
    "decider": "jev_console",
    "routable_providers": ["openai-codex"],
    "effort_maps": {
        "openai-codex": {
            "gpt-6-luna": {"accepted": ["none", "low", "medium", "high", "xhigh", "max"]},
            "gpt-6-luna-900k": {"accepted": ["none", "low", "medium", "high", "xhigh", "max"]},
            "gpt-6.1-sol": {"accepted": ["low", "medium", "high", "xhigh", "max"]},
        }
    },
}


@pytest.mark.parametrize(
    "provider,model",
    [("opencode-go", "deepseek-v4.1-flash"), ("commandcode", "deepseek/deepseek-v4.1-flash")],
)
@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_a_declared_map_lands_the_jev_level_on_the_chat_routes(
    tmp_path, monkeypatch, provider, model, level
):
    """Every level Jev can decide reaches the wire verbatim, through the operator's map."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "***")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, level, 0.9, grid_size=1))]
    )
    router, settings = build(tmp_path, transport, config=CHAT_ROUTE_MAP_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = route(
        router,
        provider=provider,
        model=model,
        turn_id=f"{provider}-{level}",
        session_id=f"{provider}-{level}",
    )

    assert result is not None
    # The route really ran: the model Jev chose is the one on the wire.
    assert result["request"]["model"] == model
    assert result["request"]["reasoning_effort"] == level
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["provider"] == provider
    assert record["model"] == model
    assert record["effort_requested"] == level
    assert record["effort"] == level
    assert record["fallback_reasons"] == []
    assert transport.call_count == 1


def test_a_declared_map_removes_a_preexisting_effort_field_when_it_omits(tmp_path, monkeypatch):
    """A level the map does not declare is omitted from a payload that already carried one."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "***")
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "high", 0.9, grid_size=1))]
    )
    config = {
        "decider": "jev_console",
        "routable_providers": ["opencode-go"],
        "effort_maps": {"opencode-go": {"deepseek-v4.1-flash": ["low"]}},
    }
    router, settings = build(tmp_path, transport, config=config)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    # Exactly what the profile's derivation would have put there before the middleware ran.
    request = ollama_request(model="deepseek-v4.1-flash", reasoning_config={"effort": "low"})
    request["reasoning_effort"] = "low"

    result = route(
        router,
        request,
        provider="opencode-go",
        model="deepseek-v4.1-flash",
        turn_id="omit-map",
        session_id="omit-map",
    )

    assert result is not None
    assert "reasoning_effort" not in result["request"]
    record = router.tail(settings, limit=1)[0]
    # The wire carried nothing; the audit keeps Jev's choice as a separate field.
    assert record["effort"] is None
    assert record["effort_requested"] == "high"


@pytest.mark.parametrize(
    "choice,model,wire_model",
    [
        ("1", "gpt-6-luna", "gpt-6-luna"),
        ("2", "gpt-6-luna-900k", "gpt-6-luna"),
        ("3", "gpt-6.1-sol", "gpt-6.1-sol"),
    ],
)
def test_codex_selects_each_declared_model_and_writes_only_reasoning_effort(
    tmp_path, monkeypatch, choice, model, wire_model
):
    """Each of the three served ids is really selected, and only ``reasoning.effort`` changes."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "***")
    transport = StubTransport(
        [StubResponse(decision_payload(choice, 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=CODEX_ROUTE_MAP_CONFIG)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    result = codex_route(
        router, turn_id=f"codex-map-{choice}", session_id=f"codex-map-{choice}"
    )

    assert result is not None
    routed = result["request"]
    assert routed["model"] == wire_model
    # The level is written inside the object the transport already built, nothing else.
    assert routed["reasoning"] == {"effort": "high", "summary": "auto"}
    assert routed["include"] == ["reasoning.encrypted_content"]
    assert "reasoning_effort" not in routed
    record = router.tail(settings, limit=1)[0]
    assert record["event"] == "route"
    assert record["model"] == model
    assert record["wire_model"] == wire_model
    assert record["effort_requested"] == "high"
    assert record["effort"] == "high"
    assert transport.call_count == 1


def test_codex_omits_a_level_the_map_does_not_declare(tmp_path, monkeypatch):
    """A level outside the declared set removes only ``effort`` — summary/include stay."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "***")
    config = {
        "decider": "jev_console",
        "routable_providers": ["openai-codex"],
        "effort_maps": {
            "openai-codex": {
                "gpt-6-luna": {"accepted": ["low", "medium", "high"]},
                "gpt-6-luna-900k": {"accepted": ["low", "medium", "high"]},
                "gpt-6.1-sol": {"accepted": ["low"]},
            }
        },
    }
    transport = StubTransport(
        [StubResponse(decision_payload("3", 0.95, "high", 0.9, grid_size=3))]
    )
    router, settings = build(tmp_path, transport, config=config)
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    request = codex_request(model="gpt-6.1-sol", effort="high")
    result = codex_route(
        router, request, model="gpt-6.1-sol", turn_id="codex-none", session_id="codex-none"
    )

    assert result is not None
    assert result["request"]["reasoning"] == {"summary": "auto"}
    assert result["request"]["include"] == ["reasoning.encrypted_content"]
    record = router.tail(settings, limit=1)[0]
    assert record["effort"] is None
    assert record["effort_requested"] == "high"


def test_replay_re_resolves_the_level_after_the_map_changes(tmp_path, monkeypatch):
    """The memo stores Jev's choice, not the level: the second call re-resolves it."""
    monkeypatch.setenv("OPENCODE_CONSOLE_API_KEY", "***")
    config = {
        "decider": "jev_console",
        "routable_providers": ["opencode-go"],
        "effort_maps": {"opencode-go": {"deepseek-v4.1-flash": ["low", "medium", "high"]}},
    }
    transport = StubTransport(
        [StubResponse(decision_payload("1", 0.95, "high", 0.9, grid_size=1))]
    )
    router = Router(
        lambda: load_settings(lambda key, default=None: config.get(key, default)),
        get_state=lambda: StubState(tmp_path),
        client_factory=lambda _settings: JevClient(
            load_settings(lambda key, default=None: config.get(key, default)), transport=transport
        ),
        catalog=_NoCatalog(),
    )
    monkeypatch.setattr(router, "_data_dir", lambda: str(tmp_path / "audit"))

    first = route(
        router,
        provider="opencode-go",
        model="deepseek-v4.1-flash",
        turn_id="replay-map",
        session_id="replay-map",
    )
    assert first is not None
    assert first["request"]["reasoning_effort"] == "high"

    # The operator narrows the map between the two calls of the same turn.
    config["effort_maps"] = {"opencode-go": {"deepseek-v4.1-flash": ["low"]}}
    second = route(
        router,
        dict(first["request"]),
        provider="opencode-go",
        model="deepseek-v4.1-flash",
        turn_id="replay-map",
        session_id="replay-map",
        api_call_count=1,
        api_request_id="replay-map:api:1",
    )

    assert second is not None
    assert "reasoning_effort" not in second["request"]
    # No second decision: the memo supplied the model and Jev's requested level.
    assert transport.call_count == 1
