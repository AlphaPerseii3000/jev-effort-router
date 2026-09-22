# Integration surface — where the router hooks into Hermes Agent

Companion of SPEC.md. Everything here was read from the Hermes Agent source installed in this profile
(`hermes-agent` 0.21.4) on 2026-09-22, with file and symbol names, so the build does not have to
rediscover the surface.

## The insertion point

`agent/turn_api_request.py::build_api_request()` assembles each attempt's provider kwargs and then, at
line ~141, runs the registered `llm_request` middleware:

```python
from hermes_cli.middleware import apply_llm_request_middleware
_llm_request_mw = apply_llm_request_middleware(
    api_kwargs, task_id=..., turn_id=..., api_request_id=..., session_id=..., platform=...,
    model=agent.model, provider=agent.provider, base_url=agent.base_url, api_mode=agent.api_mode,
    api_call_count=api_call_count,
)
api_kwargs = _llm_request_mw.payload
```

That call is the "just before the network call" point the brief asked for. It is the *only* place the
final `model` and reasoning options are frozen; everything upstream (`agent._build_api_kwargs`,
`_build_api_kwargs_for_mode`, `_build_chat_completions_kwargs`, the provider profile's
`build_api_kwargs_extras`) runs before it, and the observer hooks plus the debug dump run after it.

Middleware chain semantics (`hermes_cli/middleware.py`):

- `apply_llm_request_middleware` no-ops unless `has_middleware("llm_request")`; it passes `request`
  (a deep copy), `original_request`, `original_payload` and the context kwargs above.
- Each callback may return `{"request": {...}}` to replace the payload, optionally with `source`,
  `reason`, `name` strings that land in the trace the downstream observer hooks receive as
  `middleware_trace`.
- Payloads are copied between callbacks; a mutated dict must still be returned to take effect.
- A callback that raises is logged and skipped; the chain continues. **Middleware can never break the
  base runtime path**, which is exactly the guarantee CAP-4 depends on.
- Kinds: `tool_request`, `llm_request`, `tool_execution`, `llm_execution` (`VALID_MIDDLEWARE`).
  `*_execution` wrap the real call with a single-use `next_call`; `llm_execution` is *not* needed here.

## What the payload contains

`api_kwargs` for the `ollama-cloud` (`chat_completions`) route carries at least:

| Key | Note for the router |
|---|---|
| `model` | the string to rewrite |
| `messages`, `tools` | the conversation; do not touch (prompt-cache stability) |
| `max_tokens`, `timeout` | leave alone |
| `reasoning_config` | Hermes' internal `{enabled, effort}` dict — the *entry* clamp already ran |
| `reasoning_effort` | present only when the provider profile emitted it (Ollama:cloud: only for models resolved as reasoning-capable) |
| `extra_body` | leave alone; Ollama:cloud ignores `extra_body.thinking` |

The profile `plugins/model-providers/ollama-cloud/__init__.py` is the reference for how the effort reaches
the wire: it reads `reasoning_config["effort"]`, maps `xhigh → max`, clamps onto
`OLLAMA_CLOUD_EFFORTS = ("none","low","medium","high","max")`, and returns it as a top-level
`reasoning_effort` — or omits the field entirely when the model has no thinking capability. So a router
that writes both `reasoning_config: {"enabled": true, "effort": <level>}` and a top-level
`reasoning_effort: <level>` is coherent with the host: the profile re-derives the top-level field from the
config anyway, and writing both guarantees the field survives a profile change.

## Per-turn vs per-request

`api_call_count` counts API calls inside the conversation loop
(`agent/conversation_loop.py`: `s.api_request_id = f"{s.turn_id}:api:{s.api_call_count}"`), and
`turn_id` is stable for the whole user turn. The router keys its memo on `turn_id`, so the first request
of a turn does the routing and every follow-up request inside the same tool loop replays it. That is what
makes CAP-2 and CAP-3 compatible: the model can be swapped between turns without disturbing the system
prompt or the prompt cache mid-turn.

`pre_llm_call` (`agent/turn_context.py`) fires once per turn *before* the loop and would let the plugin
route earlier, but it cannot change the model — its only output is text injected into the user message.
It is therefore used only if the plugin ever needs to tell the model that it was routed (not in scope).

## Context available per request

| Field | Use |
|---|---|
| `session_id`, `task_id`, `turn_id` | audit record keys, per-turn memo |
| `platform` | audit record; also a routing state signal (`cli`, `telegram`, `cron`, …) |
| `model`, `provider`, `base_url`, `api_mode` | the skip gate (CAP-7) and the audit record |
| `api_call_count` | 0 marks the first request of the turn |

`platform` and `session_id` are the only state signals available for routing other than the message
itself; the conversation is not in the payload as text unless the plugin reads `request["messages"]`,
which it does for the recent-context window (a bounded tail, never the whole transcript).

## Plugin packaging surface

- Directory plugin (`HERMES_HOME/plugins/jev-router/`) with `plugin.yaml`, `__init__.py`,
  optional `pyproject.toml` for `httpx`.
- Manifest `provides_hooks: [...]`, `provides_tools: [...]`, `config_schema` for the tunables
  (endpoint is fixed; settings are threshold, Jev model id, timeout, enable/disable switches, grid
  override, context turns, audit on/off). The manifest **does** declare `provides_middleware:
  [llm_request]`, knowingly outside the host's manifest schema: `hermes plugins validate` fails a
  plugin whose registered middleware is undeclared, and that check is the plugin-catalog admission
  gate, so an undeclared middleware costs the catalog entry. The price is one `unknown manifest
  field(s) ignored` warning per load — accepted. The middleware is wired in code either way
  (`ctx.register_middleware("llm_request", ...)`).
  `llm_execution` is deliberately not declared: it wraps the real call with a single-use `next_call`
  and nothing in this plugin needs to observe or retry the call itself.
- `ctx.register_middleware(kind, callback)` → `PluginRegistration`
- `ctx.register_hook(name, callback)` → observer hooks (`post_llm_call` for turn outcome correlation)
- `ctx.register_tool(name, toolset, schema, handler)` → tools
- `ctx.register_command(name, handler, description, args_hint)` → slash commands
- `ctx.register_cli_command(name, help, setup_fn, handler_fn)` → `hermes jev-router …`
- `ctx.get_config(key, default)` / `ctx.set_config(key, value)` → `plugins.entries.<id>.settings.<key>`
- `ctx.state` → profile-scoped durable JSON under `<HERMES_HOME>/plugin-data/<id>/`, atomic, 10 MiB quota
- `ctx.emit(event, payload)` / `ctx.subscribe(event, callback)` → plugin-to-plugin events
- `ctx.llm` → out-of-band model access; **not used** here (Jev is a plain HTTP call, and using `ctx.llm`
  to call a decision endpoint would be the wrong abstraction)

The API key is read from the process environment at call time and never from `config.yaml`; it is never
logged and never included in an audit record. `OPENROUTER_API_KEY` is treated as an ambient requirement,
and its absence is surfaced once at registration.

## Verification surface

- `hermes plugins doctor <dir> --ci` — real discovery, manifest parsing, `register(ctx)`, hook/middleware
  and tool registries, and drift between declared and registered surfaces; blocks direct socket
  connections during registration, so the plugin's network calls must all be lazy (inside callbacks), not
  at import or registration time.
- `hermes plugins validate <dir>` — catalog admission gate (`plugin.yaml` needs `__init__.py` beside it).
- `hermes plugins list` / `hermes plugins capabilities` — declared vs granted capabilities.
- Tests: plain `pytest` in the repository, driving the middleware callback directly with a fabricated
  request dict and a stubbed HTTP transport; plus one integration test that loads the plugin through a
  temporary `HERMES_HOME` and asserts the registry wiring.
