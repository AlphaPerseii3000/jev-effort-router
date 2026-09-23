"""The status tool's grid-coverage report: which models are actually being applied.

The symptom this exists for: two grid models were correct, available and cheap, and were still
almost never routed on. That is invisible in a single turn — it only shows as a share of the
audit trail — so `status` summarises the routed window and names the models that never survive.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin():
    name = "jev_router_coverage_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _context(tmp_path, records):
    from stubs import StubContext, StubState

    ctx = StubContext(config={}, state=StubState(tmp_path))
    _load_plugin().register(ctx)
    router = ctx.middleware["llm_request"].__self__
    path = tmp_path / "routes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return ctx


def _route(model, *, replayed=False, degraded=False, probabilities=None, choice="1"):
    record = {
        "event": "route",
        "model": model,
        "model_choice": choice,
        "model_probabilities": probabilities or {},
        "replayed": replayed,
        "fallback_reasons": ["low_confidence"] if degraded else [],
    }
    return record


def test_coverage_reports_what_is_applied_and_what_is_never_chosen(tmp_path):
    from grid import DEFAULT_GRID

    ctx = _context(
        tmp_path,
        [
            _route("deepseek-v4.1-flash"),
            _route("deepseek-v4.1-flash"),
            _route("kimi-k3"),
            # A degraded turn is applied as the fallback, so `glm-5.3` must appear under
            # below_threshold, not under applied and not under never_chosen.
            _route(
                "deepseek-v4.1-flash",
                degraded=True,
                probabilities={"1": 0.30, "3": 0.44},
                choice="1",
            ),
        ],
    )

    payload = json.loads(ctx.tools["jev_effort_router_status"]["handler"]({}))

    coverage = payload["grid_coverage"]
    assert coverage["window"] == 4
    assert coverage["applied"] == {"deepseek-v4.1-flash": 2, "kimi-k3": 1}
    assert coverage["below_threshold"] == {"glm-5.3": 1}
    assert coverage["never_chosen"] == [
        entry.model_id
        for entry in DEFAULT_GRID
        if entry.model_id not in {"deepseek-v4.1-flash", "kimi-k3", "glm-5.3"}
    ]


def test_replayed_records_are_not_counted_as_decisions(tmp_path):
    """A turn replays one decision across its whole tool loop; counting them would weight it."""
    ctx = _context(
        tmp_path,
        [
            _route("kimi-k3"),
            *[_route("kimi-k3", replayed=True) for _ in range(30)],
        ],
    )

    coverage = json.loads(ctx.tools["jev_effort_router_status"]["handler"]({}))["grid_coverage"]

    assert coverage["window"] == 1
    assert coverage["applied"] == {"kimi-k3": 1}


def test_coverage_is_omitted_until_a_turn_has_been_routed(tmp_path):
    ctx = _context(tmp_path, [])

    payload = json.loads(ctx.tools["jev_effort_router_status"]["handler"]({}))

    assert "grid_coverage" not in payload


def test_coverage_never_raises_on_a_narrower_router(tmp_path):
    """The status view must tolerate a collaborator without the router's hooks."""
    from config import load_settings
    from tools import _grid_coverage

    class Bare:
        pass

    assert _grid_coverage(Bare(), load_settings(lambda _key, default=None: default)) == {}
