# jev-effort-router

> **Fork note:** This fork by [RVerdiF](https://github.com/RVerdiF/jev-effort-router) (upstream: [AlphaPerseii3000/jev-effort-router](https://github.com/AlphaPerseii3000/jev-effort-router), MIT license) adds pluggable decider backends (`jev` via OpenRouter Decisions API, `openai_compatible` via OpenAI chat completions like OpenCode Go, or `jev_console` via OpenCode Console SystemOne API) and selectable routable providers with per-provider grids and effort maps.

**Per-turn model *and* reasoning-effort routing for [Hermes Agent](https://github.com/NousResearch/hermes-agent)** —
both halves decided by [TypeSafe Jev](https://openrouter.ai/typesafe/jev-1.13), OpenCode Console SystemOne, or OpenAI-compatible decision endpoints.

Hermes lets you set a reasoning effort. It does not let you set a *different* one on the next turn — the
level is a session setting, so a two-line question and a gnarly refactor both pay whichever effort you
last chose. This plugin makes it per-turn: Jev reads the task, picks the model and the effort for that
turn, and the request goes out rewritten.

**That combination is the whole point, and it is why this plugin exists separately from the others.**
Every other routing entry in the catalog picks a **model** and stops there. The only entries that touch
reasoning effort make you move it **by hand** from the status bar. Nothing else decides both
automatically, per turn.

**Scope and defaults:** By default (preserving v0.3.0 legacy compatibility), this plugin targets Hermes running on `ollama-cloud` with the six benchmarked models and OpenRouter Decisions. In this v0.4.0 fork, operators can configure `routable_providers` allowlists, per-provider grids, custom `effort_maps`, pluggable deciders (such as `openai_compatible` for OpenCode), and session-level routing (`route_per_turn: false`). Any provider not listed in `routable_providers` passes through untouched (fail-open).

Hermes normally runs one model at one reasoning-effort for a whole session. `jev-effort-router` asks Jev —
on every user turn, in ~270 ms and at $0.042/M input tokens — which of six benchmarked Ollama:cloud models
and which effort level fit the task, then rewrites the outgoing provider request accordingly.

Jev writes nothing. The selected model still does all the reasoning and all the generation; Jev only steers.

```
User input
   │
   ▼
llm_request middleware  ──►  ask Jev (choice: model_route when the grid has ≥2 models, choice: reasoning_effort)
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
hermes plugins install RVerdiF/jev-effort-router --force
hermes plugins enable jev-effort-router
```

Hermes scans a community plugin on install and **blocks this one by default** — verified on 0.21.4, not
assumed:

```text
Decision: BLOCKED — Blocked (community source + caution verdict, 4 findings). Use --force to override.
```

There are two layers and they disagree, which is worth knowing before you file a bug:

- `hermes plugins validate <dir>` reports **one** finding: `caution`, `context_exfil`
  (`docs/jev-decisions-api.md:46`).
- `hermes plugins install` reports **four**: that same `HIGH exfiltration` finding, plus three
  `MEDIUM supply_chain` findings for `pip install` lines in `.github/workflows/tests.yml` and the
  README's own install snippet.

Neither the three MEDIUM findings nor the HIGH one are a defect to fix here: the supply-chain ones are
text matches on the words `pip install` in documentation and CI — the install path itself never executes
them — and the exfiltration finding is the plugin's documented purpose, because routing necessarily
sends a bounded slice of conversation context to an external decision API. The scanner does not
distinguish a docstring describing a network call from a hostile one, so the verdict is expected.

Review the findings above and re-run with `--force` to accept them:

```bash
hermes plugins install RVerdiF/jev-effort-router --force
```

To skip the scan entirely for this repository, set `plugins.scan_on_install` in your Hermes config.

Then set the key Jev is reached with — OpenRouter serves the Decisions API, so no separate TypeSafe
credential is needed:

```bash
# in the Hermes .env file
OPENROUTER_API_KEY=sk-or-...
```

Restart the session. That is the whole setup: the six-model grid ships as the default.

Requirements: **Hermes Agent** with the `llm_request` middleware kind (0.21.4 or newer) and Python 3.11+. By default, targets `provider: ollama-cloud` with `OPENROUTER_API_KEY`. When configured for custom providers (`routable_providers`) or alternative backends (such as `decider: jev_console` for OpenCode Console or `decider: openai_compatible` for OpenCode Go), the corresponding API key (e.g. `OPENCODE_CONSOLE_API_KEY`, `OPENCODE_GO_API_KEY`) and endpoint are used instead.

### Decider Backends: OpenRouter vs OpenCode Console vs OpenCode Go

1. **`decider: jev` (default)**:
   - Uses OpenRouter Decisions API (`https://openrouter.ai/api/alpha/decisions`) with `OPENROUTER_API_KEY`.
   - Decider model: `typesafe/jev-1.13`.
   - Routes default `ollama-cloud` provider across the six benchmarked models.
2. **`decider: jev_console` (OpenCode Console)**:
   - Uses OpenCode Console SystemOne API (`https://opencode.ai/inference/systemone/v1/systemone`) with `OPENCODE_CONSOLE_API_KEY`.
   - Decider model: primary `console_jev_model` (`jev-1.13-free`) with automatic fallback iteration to `console_jev_fallback_model` (`jev-1.13`, standard/paid Jev).
   - Quota breaker: a 429 from SystemOne means the free quota is spent (`FreeUsageLimitError`), not a transient hiccup. After `console_jev_breaker_threshold` consecutive 429s (default `3`) the decider opens a breaker for `console_jev_breaker_ttl_s` (default `600`) and sends **no request at all** to the endpoint while it is open; any non-429 answer clears the counter. The audit records exactly one `decider_unavailable_quota` line per window (with `audit_detail: "decider unavailable (quota)"`, the opening timestamp and the counter), and `jev_effort_router_status` exposes the state as `quota_breaker` (`state`, `reason`, `consecutive_429`, `threshold`, `ttl_s`, `opened_at`). When the TTL expires the next turn asks the endpoint again.
   - Optional third host: `console_jev_commandcode_base_url` + `console_jev_commandcode_api_key_env` + `console_jev_commandcode_model` add a CommandCode SystemOne fallback, consulted once after the Console chain fails *technically*. All three must be set; empty means off. That upstream has no ZDR agreement, so read `docs/jev-decisions-api.md` (§ Wire 4) before enabling it — it changes where routing context may be retained.
   - Routes generation models: per the owner's route-reconciliation directive (Comment 108), Jev SystemOne operates as decider, while downstream generation of `deepseek-v4.1-flash` is routed via `opencode-go`. While this decider is active the `opencode-go` route is capped to that single model (configured grid entries outside the catalog are dropped, and no choice, fallback, alternative or replay can escape it), and the `opencode-console` route serves only `deepseek-v4.1-flash`. The same opt-in cap applies to the `commandcode` route, whose catalog is the one namespaced id `deepseek/deepseek-v4.1-flash`, and to `openai-codex`, whose catalog is the three `gpt-6*` picker ids. Those three OpenCode/CommandCode catalogs name a single model, so the model question is degenerate there and the decider effectively chooses only the reasoning level; the Codex catalog carries two families with different effort ladders. The opt-in `anthropic` route carries three families (Haiku, Sonnet, Opus) with a per-family effort cap: Haiku up to `max`, Sonnet and Opus up to `high`. See `docs/routing-grid.md` and `docs/EVIDENCIAS_OPERACIONAIS.md` for details.
3. **`decider: openai_compatible` (OpenCode Go / Generic)**:
   - Uses OpenAI-compatible chat completions (`https://opencode.ai/zen/go/v1/chat/completions`) with `OPENCODE_GO_API_KEY`.
   - Decider model: `decider_model` (e.g. `deepseek-v4.1-flash`).

## Verify it is working

In a session:

```
/jev-effort-router status
/jev-effort-router route refactor the payment module
```

From the shell:

```bash
hermes jev-effort-router status
hermes jev-effort-router route "summarise this changelog"
hermes jev-effort-router grid
hermes jev-effort-router tail 20      # last audit records as JSON
hermes jev-effort-router reset        # drop memoised decisions
```

`status` reports whether routing is enabled, whether the key is present, the grid, and the most recent
decisions. `route` exercises Jev end-to-end without running a turn, and exits non-zero when routing fails.

It also reports `grid_coverage` over the last 200 routed turns, which is how you notice a model the
router is *not* using:

```
"grid_coverage": {
  "window": 38,
  "applied": { "deepseek-v4.1-flash": 5, "kimi-k3": 3, "glm-5.3-flash": 1 },
  "never_chosen": ["glm-5.3", "minimax-m3", "nemotron-3-nano:30b"],
  "below_threshold": { "kimi-k3": 8, "minimax-m3": 7 }
}
```

`never_chosen` means Jev never picks that model on this workload — fix the criterion wording in the grid.
`below_threshold` means Jev picks it but the answer does not clear `confidence_threshold`, so the turn is
served by the fallback model instead — fix the wording or the threshold. Only first-call records count, so
a turn that replays one decision across a long tool loop is not counted many times.

Two agent-facing tools are registered as well: `jev_effort_router_status` and `jev_effort_router_route`.

## Configuration

All settings live under `plugins.entries.jev-effort-router.settings` and are editable from the Desktop settings
form generated from `plugin.yaml`.

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch. Off registers the plugin but never rewrites a request. |
| `decider` | `jev` | Decision backend: `jev` (OpenRouter Decisions API) or `openai_compatible` (chat completions API). |
| `decider_base_url` | `""` | Base URL for `openai_compatible` decider (e.g. `https://opencode.ai/zen/go/v1`). |
| `decider_model` | `""` | Model for `openai_compatible` decider (e.g. `deepseek-v4.1-flash`). |
| `decider_api_key_env` | `""` | Env var name storing the API key for `openai_compatible` decider. |
| `routable_providers` | `["ollama-cloud"]` | Allowlist of provider IDs that the router is permitted to rewrite. |
| `jev_model` | `typesafe/jev-1.13` | Decision model for `jev` backend. `typesafe/jev-latest` follows the newest release. |
| `endpoint` | `https://openrouter.ai/api/alpha/decisions` | TypeSafe Jev endpoint. Only the legacy `jev` decider reads it. |
| `confidence_threshold` | `0.5` | Below this, the turn keeps the configured model and effort. One threshold for the whole deployment, whichever decider answers. |
| `timeout_s` | `2.0` | Budget for the decision call. Any timeout leaves the request untouched. |
| `default_model` | `deepseek-v4.1-flash` | Model used when the decision is below threshold. |
| `default_effort` | `medium` | Effort used when the decision is below threshold. `low`/`medium`/`high` only: `max` is a vocabulary level, never the fallback of a degraded turn. |
| `context_turns` | `4` | Preceding turns sent to the decider as recent context. |
| `route_per_turn` | `true` | Off routes once per session instead of once per user turn. |
| `audit_enabled` | `true` | Append one JSONL record per turn under the plugin data directory. |
| `log_skips` | `true` | Record why a turn was left unrouted. |
| `console_jev_breaker_threshold` | `3` | Consecutive HTTP 429 answers from the `jev_console` endpoint that open the quota breaker. |
| `console_jev_breaker_ttl_s` | `600.0` | How long the quota breaker stays open. While open, no request reaches SystemOne; the audit records one `decider_unavailable_quota` line per window. |
| `include_user_message_in_audit` | `false` | Off keeps conversation content out of the audit. |
| `grid` | `null` | Optional `["model-id: description", ...]` replacing the default grid. |
| `grids` | `null` | Optional operator override of a route's choice set. Empty uses the route's built-in catalog (`grid.py`), which is the single source of the model ids and their descriptions. |
| `effort_maps` | `null` | Optional operator override of a route's effort contract (`accepted` levels, `overrides`). Every route ships a built-in policy that already declares its accepted levels *and* which levels the effort question offers; declare a map only to narrow or widen one. The effort question's own text (the four rungs and the rule) is built into `state.py` and is not configurable. |
| `session_selection` | `null` | Optional reconciliation of the webui session's own model/effort pick on a fail-open turn: `mode` (`off`/`warn`/`apply`), `platforms` and `provider_map`. Defaults to `off`, which never reads the session row. |

### Minimal profile block

Nothing the code already knows has to be repeated in a profile. With `decider: jev_console` the
route catalogs come from `grid.py` and the effort contracts from `effort.py`, so the whole
`plugins.entries.jev-effort-router` block is nine lines:

```yaml
plugins:
  entries:
    jev-effort-router:
      settings:
        decider: jev_console
        console_api_key_env: OPENCODE_GO_API_KEY
        console_jev_fallback_model: ''
        console_jev_commandcode_base_url: https://api.commandcode.ai/provider/v1/systemone
        timeout_s: 3.0
        routable_providers: [opencode-go, commandcode, openai-codex, anthropic]
        session_selection: {mode: apply}
```

Each remaining line is an operator choice, not a default worth publishing: the backend (`decider`),
the credential (`console_api_key_env`), the cost guard (`console_jev_fallback_model: ''` turns the
paid `jev-1.13` leg **off**; the code default is `jev-1.13`), the privacy opt-in for the
non-ZDR CommandCode upstream (`console_jev_commandcode_base_url`), the decision budget
(`timeout_s`), the routes this deployment may touch (`routable_providers`) and the webui
reconciliation mode (`session_selection`). `platforms` defaults to `[webui]`.

`console_jev_commandcode_model` and `console_jev_commandcode_api_key_env` do **not** need to be
written: once the endpoint is declared they default to `typesafe/jev` and `COMMANDCODE_API_KEY`.
An empty `console_jev_commandcode_base_url` disables the fallback silently — the URL is the opt-in.

`tests/test_config_simplification_equivalence.py` proves this block routes exactly like the
long one, provider by provider and level by level.

### OpenCode & Pluggable Decider Configuration

To route requests for another provider (such as `opencode-go`) using an OpenAI-compatible decider endpoint (such as OpenCode's `deepseek-v4.1-flash`):

```yaml
plugins:
  entries:
    jev-effort-router:
      settings:
        decider: openai_compatible
        decider_base_url: https://opencode.ai/zen/go/v1
        decider_model: deepseek-v4.1-flash
        decider_api_key_env: OPENCODE_GO_API_KEY
        routable_providers:
          - opencode-go
        grids:
          opencode-go:
            - "deepseek-v4.1-flash: everyday writing, explanation, ordinary coding"
            - "kimi-k3: complex code, multi-file refactoring, deep reasoning"
        effort_maps:
          opencode-go:
            deepseek-v4.1-flash:
              accepted: [low, medium, high, max]
```

#### Key differences between backends:
- **Route catalogs under `jev_console`**: the example above uses the legacy `openai_compatible` decider, so its `grids.opencode-go` is taken verbatim. With `decider: jev_console` the same route is capped to `deepseek-v4.1-flash`, and the opt-in `commandcode` route is capped to `deepseek/deepseek-v4.1-flash`; any other entry is dropped rather than offered to the decider.
- **The Responses route (`openai-codex`)**: the ChatGPT OAuth provider speaks `codex_responses`, not `chat_completions`, so it is one of the routes whose payload shape is not a chat-completions body. It is declared like any other:
  ```yaml
          routable_providers:
            - openai-codex
          grids:
            openai-codex:
              - "gpt-6-luna: general work, coding, explanation and tool use"
              - "gpt-6-luna-900k: general work and coding over a 900k context window (large repos, long documents)"
              - "gpt-6.1-sol: harder reasoning, architecture, planning and long-context analysis"
  ```
  Its catalog (`CODEX_GRID`) carries exactly the three ids shown above. `gpt-6.1-sol-900k` is deliberately absent: the ChatGPT backend answers HTTP 400 "model entitlement" for it and the core does not treat `gpt-6.1-sol` as a valid `-900k` base, and a grid must not name a model the provider does not serve — an offering the endpoint rejects is a dead turn. An operator grid that names it is capped away by `CODEX_ALLOWLIST`, so Jev can never select it. `-900k` ids are Hermes-side picker aliases — the router sends the base slug and the large window is a session-level setting, not something a middleware route can grant. Route records now carry both `model` (the id Jev chose) and `wire_model` (the id that went into the provider's `model` field), so the audit never claims the alias ran.
- **The Anthropic route (`anthropic`, aliases `claude` / `claude-code`)**: the provider speaks `anthropic_messages`, so it is the second route whose payload shape differs. Declare it like any other:
  ```yaml
          routable_providers:
            - anthropic
          grids:
            anthropic:
              - "claude-haiku-5-5: routine and well-scoped agentic coding, quick edits, summaries, extraction, classification and other high-volume chores"
              - "claude-sonnet-5-5: human-facing messages and writing, careful single-file or refined code changes, code review"
              - "claude-opus-5-5: planning, orchestration, architecture, multi-step debugging and deep analysis"
  ```
  Its catalog (`ANTHROPIC_GRID`) carries one id per family, copied from the provider's own model list (the Hermes cache at `<HERMES_HOME>/provider_models_cache.json` → `anthropic`), and the effort caps are per family: `low`/`medium`/`high` on Sonnet and Opus, plus `max` on Haiku. `claude-sonnet-5-5` is the route's default model, i.e. what a degraded turn keeps. The level is written to `output_config.effort` inside the `output_config` the transport already built — never to a top-level `reasoning_effort`, which is not a Messages field, and never by adding the container itself.
- **Effort verification**: Reasoning effort levels are never guessed. Each opt-in route declares its own contract in `effort.py`: `opencode-go` and `commandcode` both send the decided level as a top-level `reasoning_effort` (`low`/`medium`/`high`/`max`, with `xhigh` mapped onto `max` by the same declared override the Hermes core uses, probe-confirmed on 2026-10-20 against `POST https://opencode.ai/zen/go/v1/chat/completions` and `POST https://api.commandcode.ai/provider/v1/chat/completions` respectively), while `opencode-console` keeps omitting the plugin's own field. On `commandcode` that level rides beside the `extra_body.thinking` toggle the provider profile derives, never instead of it. A level outside a route's declared set (`minimal`, `none`, an unknown string) is omitted rather than clamped, and an operator `effort_maps` entry still overrides the built-in policy. That override is the custom path, not a merge: a matched model entry **replaces** the built-in policy's vocabulary, level for level — declare everything the route should accept, including any `xhigh → max` mapping, because nothing is inherited from the built-in policy. Add an entry only when deliberately narrowing a route; for `commandcode` the reference profile carries none. The entry also decides what the effort question may ask for: `max` is offered only where the route declares it — the operator's `effort_maps` entry, or (on `anthropic`) the route's own per-family cap, which is Haiku's `max` and nobody else's.

### Session selection reconciliation (`session_selection`)

The webui records the model and effort the operator picks in the session's own row
(`sessions.model_config` in the profile's `state.db`) *and* hands them to the agent it builds. When
the pick arrives while a pre-warmed build is already running, only the row gets it: the turn runs on
the profile's default, this plugin sees that default, and the operator's choice never reaches the
wire. `session_selection` decides what the plugin does with the row when its own routing **fails
open** — a successful decision always wins, and nothing here is a ceiling on the decider.

```yaml
plugins:
  entries:
    jev-effort-router:
      settings:
        session_selection:
          mode: apply                    # off (default) | warn | apply
          platforms: [webui]             # surfaces whose row is authoritative
          provider_map: {}               # default: identity only
```

- `mode: off` is the default and the pre-feature behaviour, byte for byte: the row is not read at
  all, so `sqlite3.connect` is never called.
- `mode: warn` reads the row, records `selection_divergence` in the audit and logs a `WARNING`, and
  leaves the request untouched.
- `mode: apply` does the same and additionally rewrites a **fail-open** turn to the session's model
  and effort: the model through `wire_model_for`, the level through the route's own effort policy,
  so caps still omit a level they do not accept and a container the transport did not build is never
  invented. The `skip` record then carries `reconciled_model`, `reconciled_effort` and
  `selection_source: session_row`.
- `provider_map` adds a **second** route a selection is also valid on; identity (the selection's own
  route, aliases included) always is. Its default is `{}`, i.e. identity only: since 0.8.0 the
  plugin no longer translates a pick onto a route the row did not name unless an operator declares
  that translation.
- Nothing is reconciled when the router may not override the request: `enabled: false`, a
  `provider_not_routed` (`selection_native_provider` is recorded instead — out of the build race the
  plugin can only report the turn) or `api_mode_not_routed`.
- The session row is read `mode=ro` with a 50 ms lock timeout, once per turn. A missing, locked or
  unfamiliar `model_config` (the `api_server`'s `browser_model_lock`, a `follow_profile_config` flag,
  invalid JSON) is "no evidence": the turn fails open exactly as it does without this key.

Two limits are worth stating plainly, because they are properties of doing this in a plugin:

- **The agent is not moved — only the wire is.** `agent.model` keeps whatever the build chose, so
  `agent.log` still reports the profile's default on a reconciled turn (it already does on every
  Jev-routed turn). The proof that a turn ran on the session's model is this plugin's audit
  (`reconciled_model`, `selection_source`) and the provider's own `--model`, not `agent.log`. The
  context window is not a factor: the three Claude families all carry 1M.
- **Out of the race, *repo-only* means warning only.** If the agent build finishes before the pick
  arrives and the route the agent landed on is not in `routable_providers`, no middleware can move
  it: it cannot change a client, a `base_url` or a credential. The plugin records
  `selection_native_provider` and leaves the request untouched. What that turn costs is the core's
  answer, not this plugin's: with Claude Code's OAuth it spends the subscription quota, with an
  `sk-ant-api*` key it bills per token. Declaring the route under `routable_providers` removes the
  state entirely — the turn is then routed like any other.

**Rollback:** set `session_selection.mode: off` and reload the plugin — with `off` the row is never
read and the router is byte-for-byte its pre-feature self — or revert the commit and reinstall the
previous version.

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

Six Ollama:Cloud models, in this order. The list is deliberately short: every extra option measurably
dilutes a Choice decision.

The **Profile** column is the criterion string sent to Jev, verbatim, in English. It lives in
[`grid.py`](grid.py) as an English one-liner and is part of the measured payload rather than a display
string: rewriting it changes what Jev is choosing between, so treat it as data. To use your own wording,
override `grid`.

Every profile names a **task family**. A task-free superlative — "excellent value for money",
"excellent in real use for everyday tasks" — reads as a safe pick on every prompt, and the model
carrying one absorbs decisions that belong to the others. That is what kept `glm-5.3` and
`glm-5.3-flash` out of the route; see the changelog for the before/after measurement.

| # | Model | Profile (as sent to Jev) |
|---|---|---|
| 1 | `deepseek-v4.1-flash` | the usual choice for general work: everyday writing, explanation, summarising, ordinary coding and tool use; 1M context; cheap for its size |
| 2 | `kimi-k3` | strongest at complex code and long agentic tasks: multi-file refactors, deep debugging, large repositories; slow and the most expensive |
| 3 | `glm-5.3` | strongest at rigorous reasoning: mathematics, logic, science, quantitative and financial analysis, where a wrong answer is costly |
| 4 | `glm-5.3-flash` | best reasoning-per-cost on large text: drafting, summarising, translating and structured extraction over long documents; fast |
| 5 | `minimax-m3` | fast tool calling: long sequences of API/CLI actions, repetitive automation, high throughput |
| 6 | `nemotron-3-nano:30b` | highest throughput and lowest cost: trivial single-step requests only; weak at reasoning and at long context |

Adding a model is a reviewed change to [`docs/routing-grid.md`](docs/routing-grid.md) with benchmark
evidence behind it — not a config-only act. See that file for the benchmark sources and pricing.

Model ids are checked against Ollama:Cloud's own catalog before they go on the wire: `status` reports any
grid entry the provider no longer serves under `grid_unavailable`, and a decision naming one is refused
rather than sent. A provider catalog naming a model the grid does not offer is not added automatically —
extending the grid stays a reviewed change.

### Reasoning effort per model family

Model families do not accept the same effort vocabulary, so the plugin carries a per-family table rather
than sending a generic parameter. It never escalates a level, never invents one, and omits the field rather
than risk a 400. The notable mapping: Kimi K3's documented set is `low | high | max`, so a `medium` request
lands on `high`.

The opt-in OpenCode/CommandCode routes do not use that family table: each one has an explicit per-model
policy, and its declared set is matched exactly — no nearest-weaker clamp, because the fallback family
table would answer a level the route never declared. `opencode-go` and `commandcode` send the level as
`reasoning_effort` in the same vocabulary the Hermes core drives on those wires (`low`, `medium`, `high`,
`max`, with `xhigh` mapped onto `max`); on `commandcode` it is the top-level field, so it lands beside the
`extra_body.thinking` toggle the provider profile derives rather than replacing it. The question offers
those four levels by default — each built-in policy declares its own `offered` set — so `effort_maps` is
only needed to narrow or widen a route, never to make `max` reachable. `opencode-console`
omits the plugin's own field for the reasons stated in [`docs/routing-grid.md`](docs/routing-grid.md).

`openai-codex` is the exception on both axes. Its field is **`reasoning.effort`**, nested in the Responses
body the transport has already built — a top-level `reasoning_effort` is not a Responses field at all, so
the plugin rewrites the nested one and leaves `summary`/`include` alone. And its vocabulary is not one set
but two: the Luna ids keep the full `none`/`low`/`medium`/`high`/`xhigh`/`max` ladder while the Sol ids take
`low`/`medium`/`high`/`xhigh`/`max`, with no `none` — a `none` request there is omitted, because that route
rejects a disable and an omitted field re-enables the model's default effort instead of switching it off.

`anthropic` is the same shape of exception one level deeper: the Messages transport derives
`thinking: {type: adaptive, display: summarized}` plus `output_config: {effort: …}` and the plugin rewrites
only that effort value, inside the object the transport built. A body the transport left without an
`output_config` — a pre-adaptive-thinking Claude, reasoning disabled, or a container carrying only
`format` — gets no `effort` invented, and the audit then reports `effort: null`. Its caps are per family:
Haiku `low`–`max`, Sonnet and Opus `low`–`high`, and a level outside a family's cap is omitted, not clamped.

## Audit trail

One JSONL record per routing attempt, under `<HERMES_HOME>/plugin-data/jev-effort-router/routes.jsonl`:

```json
{
  "ts": "2026-09-22T19:09:21+00:00",
  "event": "route",
  "model": "kimi-k3",
  "wire_model": "kimi-k3",
  "effort": "high",
  "effort_requested": "high",
  "model_choice": "2",
  "model_confidence": 0.9,
  "model_probabilities": {"2": 0.9},
  "effort_confidence": 0.8,
  "alternatives": ["deepseek-v4.1-flash", "glm-5.3", "..."],
  "latency_ms": 271,
  "fallback_reasons": [],
  "provider": "ollama-cloud",
  "jev_model": "typesafe/jev-1.13",
  "decider": "jev",
  "decider_fallback_reason": null,
  "replayed": false,
  "turn_id": "turn-1",
  "session_id": "s",
  "platform": "cli"
}
```

The file is the point: it is what lets you judge over weeks whether the grid routes well, without
re-running benchmarks by hand. Skipped turns (`event: "skip"`, with a `reason`) are recorded too. The API
key is never written and the user message only when `include_user_message_in_audit` is explicitly on.

`jev_model` names the decider model that actually produced the record — the Console id (`jev-1.13-free`)
when `decider` is `jev_console`, the OpenAI-compatible id when it is `openai_compatible` — never the legacy
`typesafe/jev-1.13`, which only appears on `decider: jev` records. `decider_fallback_reason` is `null`
unless the backend's fallback model answered, in which case it names why the primary was abandoned
(`upstream_error`, `timeout`, `malformed_answer`). `provider` names the route the record belongs to, the
same value a skip record carries; route and skip records are told apart by `event`, never by the shape.

`model` is the id the decision chose and `wire_model` is the id actually written into the provider's
`model` field. They differ only on the Codex `-900k` picker aliases (`model: gpt-6-luna-900k`,
`wire_model: gpt-6-luna`), so a record can never be read as claiming the alias ran.

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
tests/test_catalog.py       provider-catalog check: a model the provider lacks is refused
tests/test_catalog_status.py the grid_unavailable warning on the status surface
tests/test_audit.py         audit trail and the per-turn memo
tests/test_registration.py  register(ctx) surface, manifest drift, tool dispatch shape, no socket I/O
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
