"""Behaviour that has to stay byte-identical to the stack's base commit.

This module is both a library and a script:

* ``pytest`` imports it and compares the working tree against
  ``tests/fixtures/legacy_stack_base.json``;
* the fixture itself was produced by running this same module **against the reference commit**, so
  a difference means the change moved legacy behaviour — it can never be "fixed" by regenerating the
  expectation from the new code.

The reference moved from ``d42230b`` (0.4.0) to ``12aaeec`` because of two *deliberate* decisions
reviewed in this stack, neither of which can be byte-identical to 0.4.0:

1. ``t_8cac6c92`` stopped asking the model question on a one-model grid, so the opt-in Console
   route's payload and its low-confidence decision changed by design;
2. ``t_0998d382`` replaced the effort rubric with the four-step one, so the rubric's *wording* is
   masked in the projection below while its shape and option keys stay pinned.

Everything else — URLs, headers, timeouts, decision fields, reason strings, audit records — is
still compared byte-for-byte against the reference.

Two rules keep the comparison honest:

1. Only *observable* legacy behaviour is projected: the calls (URL, headers, timeout, serialized
   payload bytes), the decision fields 0.4.0 already carried, the reason strings and the audit
   records with ``ts`` dropped. New fields (``decider_endpoint``, the opt-in audit columns) are
   excluded, because 0.4.0 could not have written them.
2. The clock is frozen, so ``latency_ms`` is 0 on both sides and a scheduling hiccup cannot make
   the comparison flaky.

Usage (fixture generation, run from a checkout of the reference commit)::

    python tests/legacy_fixture_scenarios.py --root /path/to/reference/checkout > tests/fixtures/legacy_stack_base.json
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

#: The commit the fixture was generated from: this stack's base (the parent card's head), not
#: 0.4.0 — the one-model grid rule and the rubric rewrite are deliberate and reviewed changes,
#: so a literal 0.4.0 byte expectation is no longer achievable (see the module docstring).
BASE_COMMIT = "12aaeec55713b55d08a791c856cb0ebbf22dec4a"

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "legacy_stack_base.json"

#: The conversation every decider case routes. Same text as the rest of the suite.
MESSAGES = [
    {"role": "user", "content": "première question"},
    {"role": "assistant", "content": "première réponse"},
    {"role": "user", "content": "refactore le module de paiement"},
]

#: Environment every case starts from. ``COMMANDCODE_API_KEY`` is deliberately *removed*: none of
#: these cases may read the opt-in secondary credential, and its absence is part of the fixture.
BASE_ENV = {
    "OPENROUTER_API_KEY": "sk-or-test",
    "OPENCODE_CONSOLE_API_KEY": "console-key",
    "OPENCODE_GO_API_KEY": "go-key",
    "COMMANDCODE_API_KEY": None,
}

#: Settings whose explicit empty string must mean exactly what an absent key means.
LEGACY_EMPTY_STRING_CONFIG = {
    "decider": "",
    "decider_base_url": "",
    "decider_model": "",
    "decider_api_key_env": "",
    "console_endpoint": "",
    "console_jev_model": "",
    "console_api_key_env": "",
    "jev_model": "",
    "routable_providers": "",
}


class FrozenClock:
    """A clock that never advances: ``latency_ms`` is 0 everywhere, deterministically.

    ``time`` is frozen too: the ``openai_compatible`` decider stamps a session header with it, and
    an advancing wall clock would make the recorded header (and therefore the fixture) unstable.
    """

    @staticmethod
    def monotonic() -> float:
        return 1_000_000.0

    @staticmethod
    def time() -> float:
        return 1_700_000_000.0


def _console_payload(choice: str = "1", confidence: float = 0.95, effort: str = "medium", effort_confidence: float = 0.9):
    from stubs import decision_payload

    return decision_payload(choice, confidence, effort, effort_confidence, grid_size=1)


def _jev_payload(choice: str = "1", confidence: float = 0.9, effort: str = "medium", effort_confidence: float = 0.8):
    """The legacy decider's grid has six options, so its distribution spans six keys."""
    from stubs import decision_payload

    return decision_payload(choice, confidence, effort, effort_confidence, grid_size=6)


def _console_config(**overrides):
    config = {"decider": "jev_console", "routable_providers": ["opencode-console"]}
    config.update(overrides)
    return config


#: Decider-level cases: one ``client.decide`` call each, captured call-by-call.
DECIDER_CASES = (
    {
        "name": "jev_success",
        "config": {},
        "provider": "ollama-cloud",
        "script": [{"jev": {}}],
    },
    {
        "name": "jev_success_empty_string_config",
        "config": LEGACY_EMPTY_STRING_CONFIG,
        "provider": "ollama-cloud",
        "script": [{"jev": {}}],
    },
    {
        "name": "jev_http_error",
        "config": {},
        "provider": "ollama-cloud",
        "script": [{"status": 429, "body": "quota exhausted"}],
    },
    {
        "name": "console_primary_ok",
        "config": _console_config(),
        "provider": "opencode-console",
        "script": [{"console": {}}],
    },
    {
        "name": "console_paid_fallback",
        "config": _console_config(),
        "provider": "opencode-console",
        "script": [
            {"status": 500, "body": "free quota exhausted"},
            {"console": {}},
        ],
    },
    {
        "name": "console_both_models_fail",
        "config": _console_config(),
        "provider": "opencode-console",
        "script": [{"status": 500, "body": "one"}, {"status": 502, "body": "two"}],
    },
    {
        "name": "console_low_confidence",
        "config": _console_config(console_jev_fallback_model=""),
        "provider": "opencode-console",
        "script": [{"console": {"confidence": 0.2}}],
    },
    {
        "name": "console_unknown_choice",
        "config": _console_config(console_jev_fallback_model=""),
        "provider": "opencode-console",
        "script": [{"console": {"choice": "9"}}],
    },
    {
        "name": "console_timeout",
        "config": _console_config(console_jev_fallback_model=""),
        "provider": "opencode-console",
        "script": [{"raise": "timeout"}],
    },
    {
        "name": "console_non_json_body",
        "config": _console_config(console_jev_fallback_model=""),
        "provider": "opencode-console",
        "script": [{"status": 200, "text": "<html>bad gateway</html>"}],
    },
    {
        "name": "console_missing_key",
        "config": _console_config(),
        "provider": "opencode-console",
        "env": {"OPENCODE_CONSOLE_API_KEY": None},
        "script": [],
    },
    {
        "name": "openai_compatible_success",
        "config": {
            "decider": "openai_compatible",
            "decider_base_url": "https://opencode.ai/zen/go/v1",
            "decider_model": "deepseek-v4.1-flash",
            "decider_api_key_env": "OPENCODE_GO_API_KEY",
            "routable_providers": ["opencode-go"],
            "effort_maps": {"opencode-go": {"deepseek-v4.1-flash": ["low", "medium", "high"]}},
        },
        "provider": "opencode-go",
        "script": [{"chat": {"model": "deepseek-v4.1-flash", "effort": "medium", "confidence": 0.92}}],
    },
)

#: Router-level cases: the real middleware callback, so audit records and fail-open are covered too.
ROUTER_CASES = (
    {
        "name": "router_jev_route_then_replay",
        "config": {},
        "provider": "ollama-cloud",
        "script": [{"jev": {"choice": "2", "effort": "high"}}],
        "calls": [{}, {"api_call_count": 1, "api_request_id": "turn-1:api:1"}],
    },
    {
        "name": "router_jev_upstream_error_skips",
        "config": {},
        "provider": "ollama-cloud",
        "script": [{"status": 500, "body": "boom"}],
        "calls": [{}],
    },
    {
        "name": "router_console_429_then_paid",
        "config": _console_config(),
        "provider": "opencode-console",
        "script": [{"status": 429, "body": "quota"}, {"console": {}}],
        "calls": [{}, {"api_call_count": 1, "api_request_id": "turn-1:api:1"}],
    },
    {
        "name": "router_console_low_confidence_skips",
        "config": _console_config(console_jev_fallback_model=""),
        "provider": "opencode-console",
        "script": [{"console": {"confidence": 0.2}}],
        "calls": [{}],
    },
    {
        "name": "router_console_missing_key_skips",
        "config": _console_config(),
        "provider": "opencode-console",
        "env": {"OPENCODE_CONSOLE_API_KEY": None},
        "script": [],
        "calls": [{}],
    },
    {
        "name": "router_provider_not_routed",
        "config": {},
        "provider": "commandcode",
        "script": [],
        "calls": [{}],
    },
)


def _build_script(script):
    """Turn a declarative script into the responses a ``StubTransport`` hands back."""
    from stubs import ReadTimeout, StubResponse

    built = []
    for step in script:
        if "raise" in step:
            if step["raise"] != "timeout":
                raise ValueError(f"unknown scripted exception {step['raise']!r}")
            built.append(ReadTimeout("timed out"))
        elif "chat" in step:
            built.append(
                StubResponse(
                    {"choices": [{"message": {"content": json.dumps(step["chat"])}}]}
                )
            )
        elif "jev" in step:
            answer = step["jev"]
            built.append(
                StubResponse(
                    _jev_payload(
                        answer.get("choice", "1"),
                        answer.get("confidence", 0.9),
                        answer.get("effort", "medium"),
                        answer.get("effort_confidence", 0.8),
                    )
                )
            )
        elif "text" in step:
            built.append(StubResponse(None, step.get("status", 200), step["text"]))
        elif "console" in step:
            answer = step["console"]
            built.append(
                StubResponse(
                    _console_payload(
                        answer.get("choice", "1"),
                        answer.get("confidence", 0.95),
                        answer.get("effort", "medium"),
                        answer.get("effort_confidence", 0.9),
                    )
                )
            )
        else:
            built.append(StubResponse(step.get("payload"), step.get("status", 200), step.get("body", "")))
    return built


def _apply_env(env):
    saved = {name: os.environ.get(name) for name in env}
    for name, value in env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    return saved


def _restore_env(saved):
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _decision_projection(decision):
    """Only the fields 0.4.0 already carried — new provenance fields are out of scope here."""
    return {
        "model": decision.model,
        "effort": decision.effort,
        "effort_requested": decision.effort_requested,
        "model_confidence": round(decision.model_confidence, 6),
        "effort_confidence": round(decision.effort_confidence, 6),
        "model_choice": decision.model_choice,
        "effort_choice": decision.effort_choice,
        "alternatives": list(decision.alternatives),
        "latency_ms": decision.latency_ms,
        "fallback_reasons": list(decision.fallback_reasons),
        "decider": decision.decider,
        "decider_model": decision.decider_model,
        "decider_fallback_reason": decision.decider_fallback_reason,
        "has_effort_confidence": getattr(decision, "has_effort_confidence", None),
    }


#: The effort question's text was deliberately replaced by card ``t_0998d382`` (the four-step
#: rubric, and criteria filtered to the levels a route offers). Its *presence*, its ``type`` and
#: its option keys are still part of the gate; only the wording is out of scope, because pinning
#: it here would make every rubric iteration a "legacy break". Everything else in the payload is
#: hashed byte-for-byte.
_RUBRIC_MASK = "<rubric>"
_CHAT_RUBRIC_PREFIXES = ("- low:", "- medium:", "- high:", "- max:")
_CHAT_RULE_PREFIX = "- The effort MUST be "
_CHAT_INSTRUCTIONS_PREFIX = "Pick the LOWEST reasoning effort"
_CHAT_EFFORT_HEADER = "Reasoning Effort Levels:"


def _mask_chat_prompt(text, *, system):
    """The ``openai_compatible`` prompt with the rubric's wording masked, shape preserved.

    Masking is confined to where the rubric actually lives: the level lines inside the
    ``Reasoning Effort Levels:`` block, and (in the system message) the rule line and the
    instructions paragraph. A user or context line that happens to start with ``- low:`` is left
    alone, so a regression in the conversation cannot hide behind the mask.

    The criteria lines keep their level key — the offered set is still pinned — and lose their
    text. The instructions paragraph is new in this card, so removing it is what lets a base
    payload without it compare equal to a current one that carries it.
    """
    kept = []
    in_effort_block = False
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped == _CHAT_EFFORT_HEADER:
            in_effort_block = True
            kept.append(line)
            continue
        if in_effort_block:
            if not stripped:
                in_effort_block = False
            elif stripped.startswith(_CHAT_RUBRIC_PREFIXES):
                kept.append(f"{stripped.split(':', 1)[0]}: {_RUBRIC_MASK}")
                continue
            kept.append(line)
            continue
        if system and stripped.startswith(_CHAT_RULE_PREFIX):
            kept.append(_RUBRIC_MASK)
            continue
        if system and stripped.startswith(_CHAT_INSTRUCTIONS_PREFIX):
            continue
        kept.append(line)
    return "\n".join(kept)


def _masked_payload(payload):
    """``payload`` with the deliberately reworded effort rubric's text replaced."""
    import copy

    masked = copy.deepcopy(payload)
    if not isinstance(masked, dict):
        return masked
    questions = masked.get("questions")
    effort = questions.get("reasoning_effort") if isinstance(questions, dict) else None
    if isinstance(effort, dict):
        if "instructions" in effort:
            effort["instructions"] = _RUBRIC_MASK
        if isinstance(effort.get("criteria"), dict):
            effort["criteria"] = {key: _RUBRIC_MASK for key in effort["criteria"]}
    messages = masked.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                system = str(message.get("role") or "").strip().lower() == "system"
                message["content"] = _mask_chat_prompt(message["content"], system=system)
    return masked


def _call_projection(call):
    serialized = json.dumps(_masked_payload(call["json"]), ensure_ascii=False)
    return {
        "url": call["url"],
        "headers": dict(call["headers"] or {}),
        "timeout": call["timeout"],
        "payload_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "payload_length": len(serialized),
    }


def _with_frozen_clock(fn):
    import decider

    original = decider.time
    decider.time = FrozenClock
    try:
        return fn()
    finally:
        decider.time = original


def _run_decider(case):
    from client import JevClient
    from config import load_settings
    from stubs import StubTransport

    settings = load_settings(lambda key, default=None: case["config"].get(key, default))
    transport = StubTransport(_build_script(case["script"]))
    client = JevClient(settings, transport=transport)
    decision, reason = client.decide(
        MESSAGES,
        settings.effective(provider=case["provider"]),
        provider=case["provider"],
    )
    return {
        "calls": [_call_projection(call) for call in transport.calls],
        "decision": None if decision is None else _decision_projection(decision),
        "reason": reason,
    }


def _run_router(case):
    from client import JevClient
    from config import load_settings
    from router import Router
    from stubs import StubState, StubTransport, ollama_request

    class _NoCatalog:
        def is_known(self, *args, **kwargs):
            return None

    settings = load_settings(lambda key, default=None: case["config"].get(key, default))
    transport = StubTransport(_build_script(case["script"]))
    state = StubState(tempfile.mkdtemp(prefix="legacy-fixture-"))
    router = Router(
        lambda: settings,
        get_state=lambda: state,
        client_factory=lambda _settings: JevClient(settings, transport=transport),
        catalog=_NoCatalog(),
    )

    results = []
    for index, call in enumerate(case["calls"]):
        request = ollama_request()
        result = router.on_llm_request(
            request,
            dict(request),
            turn_id="turn-1",
            session_id="session-1",
            platform="cli",
            model="deepseek-v4.1-flash",
            provider=case["provider"],
            base_url="https://ollama.com/v1",
            api_mode="chat_completions",
            api_call_count=call.get("api_call_count", 0),
            api_request_id=call.get("api_request_id", f"turn-1:api:{index}"),
        )
        results.append(
            {
                "request": result["request"] if result else None,
                "source": result["source"] if result else None,
            }
        )

    records = [
        {key: value for key, value in record.items() if key != "ts"}
        for record in router.tail(settings, limit=50)
    ]
    return {
        "calls": [_call_projection(call) for call in transport.calls],
        "results": results,
        "records": records,
    }


def _run_case(case, runner):
    env = dict(BASE_ENV)
    env.update(case.get("env", {}))
    saved = _apply_env(env)
    try:
        return _with_frozen_clock(lambda: runner(case))
    finally:
        _restore_env(saved)


def collect_decider_cases() -> dict:
    return {case["name"]: _run_case(case, _run_decider) for case in DECIDER_CASES}


def collect_router_cases() -> dict:
    return {case["name"]: _run_case(case, _run_router) for case in ROUTER_CASES}


def collect() -> dict:
    return {
        "base_commit": BASE_COMMIT,
        "generated_by": "tests/legacy_fixture_scenarios.py",
        "regenerate": (
            "git archive <base_commit> | tar -x -C /tmp/base && "
            "python tests/legacy_fixture_scenarios.py --root /tmp/base > tests/fixtures/legacy_stack_base.json"
        ),
        "decider": collect_decider_cases(),
        "router": collect_router_cases(),
    }


def _load_plugin_modules(root: Path) -> None:
    """Import the plugin's modules from ``root`` the way ``tests/conftest.py`` does under pytest.

    The plugin is a *directory* plugin: its modules import each other relatively, while the tests
    import them by bare name. A synthetic package whose ``__path__`` is the checkout root (plus a
    bare alias per module) is what makes both work without an install.
    """
    import importlib
    import types

    package_name = "jev_legacy_fixture_plugin"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package
    for child in sorted(root.glob("*.py")):
        if child.stem == "__init__":
            continue  # its registration side effects are not wanted here
        module = importlib.import_module(f"{package_name}.{child.stem}")
        sys.modules.setdefault(child.stem, module)
        setattr(package, child.stem, module)


def _main(argv) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="checkout whose plugin modules are imported")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    for entry in (str(root), str(root / "tests")):
        if entry not in sys.path:
            sys.path.insert(0, entry)

    _load_plugin_modules(root)

    import config

    loaded = Path(config.__file__).resolve()
    if root not in loaded.parents and root != loaded.parent:
        print(
            f"refusing to run: config was imported from {loaded}, not from {root}",
            file=sys.stderr,
        )
        return 2

    json.dump(collect(), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
