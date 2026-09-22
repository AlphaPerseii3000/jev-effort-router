# jev-router

Per-turn model and reasoning-effort routing for [Hermes Agent](https://github.com/NousResearch/hermes-agent),
decided by [TypeSafe Jev](https://openrouter.ai/typesafe/jev-1.13) — a "System One" decision model, not an LLM.

Hermes normally runs one model at one reasoning-effort for a whole session. `jev-router` asks Jev — on every
user turn, in ~270 ms and at $0.042/M input tokens — which of six benchmarked Ollama:cloud models and which
effort level fit the task, then rewrites the outgoing provider request accordingly.

Jev writes nothing. The selected model still does all the reasoning and all the generation; Jev only steers.

```
User input
   │
   ▼
llm_request middleware  ──►  ask Jev (choice: model_route, choice: reasoning_effort)
   │                              │
   │                              ▼
   │                       confidence guard (default 0.5)
   ▼                              │
provider request rewritten: model + reasoning_effort
   │
   ▼
main model reasons and answers
```

## Install

```bash
hermes plugins install AlphaPerseii3000/jev-router
hermes plugins enable jev-router
```

Hermes scans a community plugin on install and may refuse one that rewrites outgoing provider requests
without an explicit override — the scan returns a CAUTION verdict here by design, because routing
necessarily sends a bounded slice of conversation context to an external decision API. If the install is
refused, review the findings and re-run with `--force`:

```bash
hermes plugins install AlphaPerseii3000/jev-router --force
```

To skip the scan entirely for this repository, set `plugins.scan_on_install` in your Hermes config.

Then set the key Jev is reached with — OpenRouter serves the Decisions API, so no separate TypeSafe
credential is needed:

```bash
# in the Hermes .env file
OPENROUTER_API_KEY=sk-or-...
```

Restart the session. That is the whole setup: the six-model grid ships as the default.

Requirements: Hermes Agent with the `llm_request` middleware kind (0.21.4 or newer), Python 3.11+,
and prepaid OpenRouter credits.

## Verify it is working

In a session:

```
/jev-router status
/jev-router route refactor the payment module
```

From the shell:

```bash
hermes jev-router status
hermes jev-router route "summarise this changelog"
hermes jev-router grid
hermes jev-router tail 20      # last audit records as JSON
hermes jev-router reset        # drop memoised decisions
```

`status` reports whether routing is enabled, whether the key is present, the grid, and the most recent
decisions. `route` exercises Jev end-to-end without running a turn, and exits non-zero when routing fails.

Two agent-facing tools are registered as well: `jev_router_status` and `jev_router_route`.

## Configuration

All settings live under `plugins.entries.jev-router.settings` and are editable from the Desktop settings
form generated from `plugin.yaml`.

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch. Off registers the plugin but never rewrites a request. |
| `jev_model` | `typesafe/jev-1.13` | Decision model. `typesafe/jev-latest` follows the newest release. |
| `confidence_threshold` | `0.5` | Below this, the turn keeps the configured model and effort. |
| `timeout_s` | `2.0` | Budget for the decision call. Any timeout leaves the request untouched. |
| `default_model` | `deepseek-v4.1-flash` | Model used when the decision is below threshold. |
| `default_effort` | `medium` | Effort used when the decision is below threshold. |
| `context_turns` | `4` | Preceding turns sent to Jev as recent context. |
| `route_per_turn` | `true` | Off routes once per session instead of once per user turn. |
| `audit_enabled` | `true` | Append one JSONL record per turn under the plugin data directory. |
| `log_skips` | `true` | Record why a turn was left unrouted. |
| `include_user_message_in_audit` | `false` | Off keeps conversation content out of the audit. |
| `grid` | `null` | Optional `["model-id: description", ...]` replacing the built-in grid. |

## How it behaves when things go wrong

The router is built to be invisible when it fails. A timeout, an HTTP error, a malformed answer, a
confidence below the threshold, an unknown provider, or a model outside the grid all leave the request
**exactly** as it was — a broken router is indistinguishable from a router that is not installed.

Notably, it touches only what it owns:

- **Provider** — only `ollama-cloud` is routed; any other provider passes through untouched.
- **API mode** — only `chat_completions`; a Responses or Anthropic-Messages route is left alone.
- **Model** — a model outside the configured grid is not routed.
- **Auxiliary calls** — titling, compression, MoA and vision calls are not routed, only the main turn.

### One decision per user turn

The decision is taken at the first provider request of a turn and replayed for the rest of that turn's tool
loop, so the system prompt and the prompt cache are never disturbed mid-turn. The next user turn routes
afresh, which is what lets a session move from a one-line question to a code refactor and be served by
different models without anyone reconfiguring anything.

## The routing grid

Six models, in this order. The list is deliberately short: every extra option measurably dilutes a Choice
decision.

| # | Model | Profile |
|---|---|---|
| 1 | `deepseek-v4.1-flash` | généraliste, excellent rapport qualité/prix, contexte 1M, à privilégier par défaut |
| 2 | `kimi-k3` | code et tâches agentiques haut de gamme, coûteux, à réserver au développement complexe |
| 3 | `glm-5.3` | raisonnement scientifique et logique poussé, coûteux, forte exigence analytique |
| 4 | `glm-5.3-flash` | rapide et économique, excellent en usage réel pour les tâches courantes |
| 5 | `minimax-m3` | bon compromis vitesse/agentique pour le tool calling et les actions séquentielles |
| 6 | `nemotron-3-nano:30b` | très haut débit, tâches simples uniquement, à éviter pour du raisonnement |

Adding a model is a reviewed change to [`docs/routing-grid.md`](docs/routing-grid.md) with benchmark
evidence behind it — not a config-only act. See that file for the benchmark sources and pricing.

### Reasoning effort per model family

Model families do not accept the same effort vocabulary, so the plugin carries a per-family table rather
than sending a generic parameter. It never escalates a level, never invents one, and omits the field rather
than risk a 400. The notable mapping: Kimi K3's documented set is `low | high | max`, so a `medium` request
lands on `high`.

## Audit trail

One JSONL record per routing attempt, under `<HERMES_HOME>/plugin-data/jev-router/routes.jsonl`:

```json
{
  "ts": "2026-09-22T19:09:21+00:00",
  "event": "route",
  "model": "kimi-k3",
  "effort": "high",
  "effort_requested": "high",
  "model_choice": "2",
  "model_confidence": 0.9,
  "model_probabilities": {"2": 0.9},
  "effort_confidence": 0.8,
  "alternatives": ["deepseek-v4.1-flash", "glm-5.3", "..."],
  "latency_ms": 271,
  "fallback_reasons": [],
  "jev_model": "typesafe/jev-1.13",
  "replayed": false,
  "turn_id": "turn-1",
  "session_id": "s",
  "platform": "cli"
}
```

The file is the point: it is what lets you judge over weeks whether the grid routes well, without
re-running benchmarks by hand. Skipped turns (`event: "skip"`, with a `reason`) are recorded too. The API
key is never written and the user message only when `include_user_message_in_audit` is explicitly on.

## Development

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q tests
```

`pytest` must be pointed at `tests/` explicitly. The plugin's entry point is `__init__.py` at the
repository root (Hermes requires it beside `plugin.yaml`), which makes that root a package; if pytest
takes the root as its rootdir it tries to import that `__init__.py` as the package of the collected test
modules and fails with *"attempted relative import with no known parent package"*. That is why the ini
lives in `tests/pytest.ini` and CI runs `pytest -q tests`.

The suite drives the real middleware callback against a stub Decisions API — no network, no API key
required (an autouse fixture supplies a dummy one; tests asserting the unconfigured path delete it).

```
tests/test_grid.py          the grid and choice→model mapping
tests/test_effort.py        per-family effort translation
tests/test_client.py        wire contract, answer interpretation, failure containment
tests/test_router.py        routing end-to-end, replay, degradation, skip gates
tests/test_audit.py         audit trail and the per-turn memo
tests/test_registration.py  register(ctx) surface, manifest drift, no socket I/O
```

`tests/test_registration.py` is the one that matters most for packaging: it loads `__init__.py` the way
Hermes' loader does and asserts the manifest declares exactly what `register(ctx)` registers, and that
registration opens no socket (`hermes plugins doctor` blocks sockets while `register` runs).

## Design notes

- **No Hermes core change.** The plugin rides the documented `llm_request` middleware point in
  `agent/turn_api_request.py::build_api_request`. See
  [`docs/integration-surface.md`](docs/integration-surface.md).
- **Choices are keyed, not text.** Jev returns the criterion *key*, so criteria are built as
  `{"1": "deepseek-v4.1-flash: …", …}` and mapped back by key. Profile text can be rewritten without
  silently changing which model is selected. See [`docs/jev-decisions-api.md`](docs/jev-decisions-api.md).
- **The key question is asked first.** Both questions go in one request; the endpoint evaluates them
  independently and in parallel, so any coupling between them is resolved in code, not on the wire.
- **`httpx` is imported lazily**, inside the callback — never at import or registration time.

The full contract is in [`docs/SPEC.md`](docs/SPEC.md) (capabilities, constraints, non-goals) with its
companions `routing-grid.md`, `jev-decisions-api.md` and `integration-surface.md`.

## License

MIT — see [LICENSE](LICENSE).
