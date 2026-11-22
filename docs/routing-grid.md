# Routing grid — models, profiles, and effort mapping

Companion of SPEC.md. Every consumer reads this to know what Jev may choose, what each option means, and
how a chosen level lands on each model family's wire.

## Models offered to Jev

The Choice options are exactly these six, in this order. The option string is
`"<model-id>: <one-line profile>"`; the model id is everything before the first `:`.

Every model id in this default grid belongs to the **Ollama:Cloud** provider — by default this plugin routes `ollama-cloud`. When custom providers are configured under `routable_providers`, their corresponding provider-specific grids configured in `grids.<provider>` are used instead. The
profile text is sent to Jev verbatim, **in English**, and is part of the measured payload, so treat it
as data rather than documentation: it is not a display string, and rewriting or re-translating it
changes what Jev is choosing between. The criteria were moved from French to English in this pass; the
before/after measurement is recorded in the changelog.

| # | Model id (option prefix) | Profile line sent to Jev | Context | Benchmark evidence |
|---|---|---|---|---|
| 1 | `deepseek-v4.1-flash` | the usual choice for general work: everyday writing, explanation, summarising, ordinary coding and tool use; 1M context; cheap for its size | 1M | Terminal-Bench near paid flagships; young model (Sep 2026), watch in production |
| 2 | `kimi-k3` | strongest at complex code and long agentic tasks: multi-file refactors, deep debugging, large repositories; slow and the most expensive | 1M | best open-weight on SWE-bench and GPQA across independent benches |
| 3 | `glm-5.3` | strongest at rigorous reasoning: mathematics, logic, science, quantitative and financial analysis, where a wrong answer is costly | — | GPQA Diamond 91.7 |
| 4 | `glm-5.3-flash` | best reasoning-per-cost on large text: drafting, summarising, translating and structured extraction over long documents; fast | — | highest measured Intelligence Index among flash models in real usage (41.8) |
| 5 | `minimax-m3` | fast tool calling: long sequences of API/CLI actions, repetitive automation, high throughput | — | 210 tok/s, good agentic score |
| 6 | `nemotron-3-nano:30b` | highest throughput and lowest cost: trivial single-step requests only; weak at reasoning and at long context | — | 346 TPS; avoid for reasoning |

**A profile line must name a task family, never praise the model in the abstract.** The two rules a
rewrite has to keep: no superlative with no task attached ("excellent value for money", "excellent in
real use for everyday tasks") and no cost adjective standing in for a task ("fast and economical"). A
line like that is a safe pick on *every* prompt, so the model carrying one swallows decisions that
belong to the others — the mechanism that kept `glm-5.3` and `glm-5.3-flash` off the route until the
grid was rewritten to name task families. `tests/test_grid.py::test_no_profile_is_a_task_free_superlative`
enforces it. Cost and speed may appear as a secondary clause, always beside the task the model is for;
pricing belongs in this file, not in the criterion text.

**The id here is the wire id, verbatim — tag included.** The tier was written as
`nemotron-3-nano` while the provider's catalog names it `nemotron-3-nano:30b`; the bare name
returned `HTTP 404: model "nemotron-3-nano" not found` and killed the turn outright. Verify every
grid entry against the provider's model list (the host caches it at
`<HERMES_HOME>/ollama_cloud_models_cache.json`) rather than against memory or pricing pages — a
provider-side rename is the one drift a passing offline suite cannot see.

Pricing (reference only — the plugin does not use it for decisions): deepseek-v4.1-flash $0.15/$0.60
off-peak first-party ($0.12/$0.48 on OpenRouter, doubled during UTC 01–04h and 06–10h on weekdays);
kimi-k3 $3/$15; glm-5.3 $1/$4; glm-5.3-flash $0.15/$0.50; minimax-m3 $0.60/$2; nemotron-3-nano:30b $0.06/$0.24.

The rest of the profile's Ollama:cloud catalog (`glm-5.1`, `glm-5.2`, `deepseek-v4-flash`,
`deepseek-v4-pro`, `kimi-k2.6`, `kimi-k2.7-code`, `nemotron-3-ultra`, `nemotron-3-super`, `gemma4`,
`qwen3.5`, `minimax-m2.7`, `mistral-large-3`, `gpt-oss`) is deliberately excluded: not benchmarked in
this pass, and a longer Choice list dilutes decision quality. Adding an option is a reviewed change to
this file plus a README note, not a config-only act.

## Effort options

Second Choice question, three options, in this order: `low`, `medium`, `high`. Default when confidence is
below threshold: `medium`.

## Provider-specific grids and effort maps

In v0.4.0+, the router supports multiple providers declared under `routable_providers`.

- **Default (`ollama-cloud`)**: Uses the built-in six-model grid and the built-in effort table below.
- **OpenCode Console (`opencode-console`)**: Pinned to its own catalog (`OPENCODE_CONSOLE_GRID`): the one normal generation id the Console list carries, `deepseek-v4.1-flash`.
- **OpenCode Go generation (`opencode-go`)**: While `decider: jev_console` is active, the route is capped to its one-model catalog (`OPENCODE_GO_GRID`: `deepseek-v4.1-flash`). With a legacy decider (`jev`, `openai_compatible`) the route keeps whatever `grids.<provider>` the operator declared, exactly as before.
- **CommandCode (`commandcode`)**: Opt-in OpenAI-compatible route with its own one-model catalog (`COMMANDCODE_GRID`: `deepseek/deepseek-v4.1-flash`, the provider's namespaced id). Capped the same way — only while `decider: jev_console` is active; a legacy decider keeps the operator's grid.
- **OpenAI Codex (`openai-codex`, aliases `codex` / `openai_codex`)**: The ChatGPT OAuth route, which speaks `codex_responses` (the Responses API) rather than `chat_completions`. Its catalog is `CODEX_GRID`, three picker ids across two families. Same opt-in cap as Go and CommandCode.
- **Anthropic (`anthropic`, aliases `claude` / `claude-code`)**: The Claude route, which speaks `anthropic_messages` (the Anthropic Messages API). Its catalog is `ANTHROPIC_GRID`, one id per family (Haiku, Sonnet, Opus) and the newest version of each the provider's model list carries. Same opt-in cap; its effort caps are per family (see below). This is the only Claude route: the `claude-subscription-directsdk-experimental` route (Claude Pro/Max through the Claude Code CLI, `extra_body.reasoning.effort`, `[1m]` ids) shipped **off** and was removed in 0.8.0 with its plugin, so nothing in `config.py` refers to it any more.

### OpenCode Console catalog (`opencode-console`)

| # | Model id | Profile line sent to Jev | Scope / Notes |
|---|---|---|---|
| 1 | `deepseek-v4.1-flash` | the usual choice for general work: everyday writing, explanation, summarising, ordinary coding and tool use; 1M context | The Console's own generation id |

Every OpenCode/CommandCode generation catalog in this plugin names **one** model. A decision naming any
other model on `opencode-console` fails open — the request keeps the model the operator configured — and
the same holds for the opt-in Go and CommandCode routes below. Three routes are the exception, and all
of them carry several families whose own ladders differ: the Codex route (three ids, two families), the
Anthropic route (one id per family, three families) and the Claude subscription route (the same three
families, on a different wire).

### OpenCode Go generation catalog (`opencode-go`, opt-in)

| # | Model id | Profile line sent to Jev | Scope / Notes |
|---|---|---|---|
| 1 | `deepseek-v4.1-flash` | the usual choice for general work: everyday writing, explanation, summarising, ordinary coding and tool use; 1M context | Normal, per-token |

The cap is a *cap*: a configured `grids.opencode-go` entry outside the catalog is dropped, and a grid
the cap empties falls back to the one-model catalog. Choice, fallback, alternatives and memo replay all
read their options from the capped grid, so no path can select a second model.

### CommandCode generation catalog (`commandcode`, opt-in)

| # | Model id | Profile line sent to Jev | Scope / Notes |
|---|---|---|---|
| 1 | `deepseek/deepseek-v4.1-flash` | the usual choice for general work: everyday writing, explanation, summarising, ordinary coding and tool use; 1M context | The provider's namespaced id, verified against its public unauthenticated `/models` list |

The id is namespaced on purpose: `deepseek/deepseek-v4.1-flash` is CommandCode's own contract, while the
bare `deepseek-v4.1-flash` used by the OpenCode routes is a different one. Neither the bare id nor the
`deepseek/deepseek-v4.1-flash-fast` variant is in the allowlist, so neither can be selected, offered as an
alternative or replayed on this route. The cap mechanics are identical to Go: configured entries outside
the allowlist are dropped from the effective grid, and a surface policy grid cannot widen it.

### OpenAI Codex generation catalog (`openai-codex`, opt-in)

| # | Model id | Profile line sent to Jev | Scope / Notes |
|---|---|---|---|
| 1 | `gpt-6-luna` | general work, coding, explanation and tool use | GPT-6 Luna; ladder `none..max` |
| 2 | `gpt-6-luna-900k` | general work and coding over a 900k context window (large repos, long documents) | Picker alias of `gpt-6-luna`; wire id is the base slug |
| 3 | `gpt-6.1-sol` | harder reasoning, architecture, planning and long-context analysis | GPT-6.1 Sol; ladder `low..max` (no `none`) |

Two things about this catalog are easy to get wrong, and both are enforced in code rather than assumed:

- **`-900k` is a Hermes-side picker suffix, not a backend id.** The transport strips it while *building*
  the body (`agent/transports/codex.py`, `strip_codex_context_variant_suffix`), and the large window it
  opts into is resolved from `agent.model` when the session is built — a middleware-time route cannot
  grant it. So `Entry.wire_id` records the id the endpoint actually serves and the router writes **that**,
  never the alias; an operator `grids.openai-codex` entry carries a description only, which is why the
  lookup is answered by the catalog (`router.wire_model_for`) and not by the configured grid entry. A grid
  pinned to `gpt-6-luna-900k` therefore sends `model: gpt-6-luna` — verified live.
- **`gpt-6.1-sol-900k` is not an id this backend lists, so it is not in the catalog.** It is absent from
  the account's model cache (`provider_models_cache.json` → `openai-codex`) and from the core's `-900k`
  eligible bases (`agent/model_metadata.py::_CODEX_900K_ELIGIBLE_BASES`, which lists `gpt-6-luna`,
  `gpt-6-sol`, `gpt-6-astra`, the `gpt-5.6-*` tier bases, `gpt-5.4` and `gpt-daybreak-blue-latest` —
  not `gpt-6.1-sol`). A live turn against it returns HTTP 400 "model entitlement" and Hermes falls back
  to another model, so the route would be recorded in the audit while the request that answered belonged
  to a different model. It is deliberately absent from both `CODEX_GRID` and `CODEX_ALLOWLIST`: an
  operator grid naming it is capped away by the allowlist (a grid the cap empties falls back to the
  route's own catalog), and a decision that names it anyway is refused as
  `model_not_in_provider_catalog`, leaving the request untouched. A grid must not name a model the
  provider does not serve.

Two families, two effort vocabularies:

| Model | Accepted levels | Source |
|---|---|---|
| `gpt-6-luna`, `gpt-6-luna-900k` | `none`, `low`, `medium`, `high`, `xhigh`, `max` | `CODEX_GPT56_EFFORTS` (`agent/reasoning_effort.py`) |
| `gpt-6.1-sol` | `low`, `medium`, `high`, `xhigh`, `max` — **no `none`** | `CODEX_ASTRA_EFFORTS`, reached through `NO_DISABLE_TIER_PREFIXES` |

A `none` request on the Sol family is **omitted**, never sent: that route rejects a disable with a 400, and
an omitted `reasoning` field re-enables the model's default effort instead of switching it off. This is the
same rule the rest of this plugin follows — only a declared level goes out, and nothing is invented or
clamped onto another rung.

### Anthropic generation catalog (`anthropic`, opt-in)

| # | Model id | Profile line sent to Jev | Scope / Notes |
|---|---|---|---|
| 1 | `claude-haiku-5-5` | routine and well-scoped agentic coding, quick edits, summaries, extraction, classification and other high-volume chores | Haiku; effort up to `max` |
| 2 | `claude-sonnet-5-5` | human-facing messages and writing, careful single-file or refined code changes, code review | Sonnet; effort up to `high`; the route's default model |
| 3 | `claude-opus-5-5` | planning, orchestration, architecture, multi-step debugging and deep analysis | Opus; effort up to `high` |

The ids are copied verbatim from the provider's own model list — the Hermes provider-model cache for
`anthropic` (`<HERMES_HOME>/provider_models_cache.json` → `anthropic.models`), which is the cached answer
of the profile's `/v1/models` call (`core/plugins/model-providers/anthropic/__init__.py::fetch_models`).
One entry per family, newest version: no id is composed from a family name, and the older `claude-*-4-*`
ids the same list carries are deliberately not offered (a longer Choice list dilutes the decision, and
those families are superseded).

**Effort caps are per family, and they are this plugin's decision, not a wire limit:**

| Model | Offered / accepted levels | Why |
|---|---|---|
| `claude-sonnet-5-5`, `claude-opus-5-5` | `low`, `medium`, `high` | a `max`/`xhigh` turn on these families costs several times a `high` one, and this deployment has no measured need for it |
| `claude-haiku-5-5` | `low`, `medium`, `high`, `max` | the high-volume family: the wire accepts `max` (`ADAPTIVE_EFFORT_MAP` maps it to itself) and the cap is what keeps the other two down |

`max` is only **offered** where the route declares it, so an operator `effort_maps` entry is still the
supported way to widen Opus/Sonnet. Three consequences worth stating:

- The effort question is asked once, *before* the model is known, and `offered_levels_for_grid` intersects
  the families' sets **when no model in the grid declares bounds**. On the natural three-model grid the
  question therefore offers `low`/`medium`/`high` even though Haiku would accept `max`; a Haiku-only grid
  can be asked for `max`. `default_effort` still excludes `max`, so a degraded turn is never the expensive
  one.
- A level outside a family's cap (`max` on Opus/Sonnet, `xhigh`, `minimal`, `ultra`, an unknown string) is
  **omitted**, never clamped onto `high`, when the family's contract is the built-in vocabulary.
  `effort_requested` keeps Jev's answer; `effort` records what the wire carried.
- **Unless the operator declares bounds for the model** (`min`/`max`, see below): then the pair decides,
  the question offers the union of the bounded grid, and a level outside a model's pair is clamped onto
  its bound instead of being dropped.

#### Per-model effort bounds (`min`/`max`)

An `effort_maps` entry can declare a **range** instead of a level list. This is the shape the owner chose
for this route (card t_b477e209): `opus` `low..medium`, `sonnet` `medium..high`, `haiku` `medium..max`.

```yaml
effort_maps:
  anthropic:
    claude-haiku-5-5:  {min: medium, max: max}
    claude-sonnet-5-5: {min: medium, max: high}
    claude-opus-5-5:   {min: low,    max: medium}
```

- **The pair *is* the contract.** `accepted` is derived from it — the askable levels between the bounds,
  inclusive (`effort.levels_within`) — so it is not declared twice. One open side is filled from the
  askable vocabulary (`low` for a missing `min`, `max` for a missing `max`); a bound outside that
  vocabulary, an inverted pair, or an `accepted` list that disagrees with the derived set makes the entry
  declare nothing (the deliberate-shadow branch) instead of letting one key win silently.
- **The question offers the union.** With at least one bounded model in the grid,
  `offered_levels_for_grid` offers every model's offered set (`low`, `medium`, `high`, `max` here) instead
  of their intersection. That is safe precisely because the applied level is clamped per model: the
  alternative was a one-option question (`medium`) that would have flattened every routed turn.
- **The applied level is clamped, never omitted** (`effort.resolve_bounded_effort`): inside the pair it
  goes verbatim; above the ceiling it lands on the ceiling (`opus`+`high` → `medium`, `sonnet`+`max` →
  `high`); below the floor it lands on the floor (`haiku`+`low` → `medium`); a rung inside the pair that
  the vocabulary does not carry (`xhigh` between `high` and `max`) lands on the nearest accepted level at
  or below it. `none` and any word outside the ladder are still omitted — a disable is not raised onto the
  floor and a bespoke word has no position to clamp from. A bounded model is also exempt from the
  "unoffered level" gate in the three deciders and in the memo replay: the clamp, not the drop, is what
  applies.
- **The audit proves it.** The route record keeps `effort_requested` (what the decider asked for) and
  `effort` (what reached the wire), and a bounded model adds `effort_bounds: [min, max]` plus, only when
  the pair bit, `effort_adjusted: "clamped_max"` / `"clamped_min"` (`"nearest_accepted"` for the `xhigh`
  case). Without those keys a clamped turn is indistinguishable from a turn that simply ran the requested
  level.
- **A level the question never offered is still taken, by clamping.** `_offered_wire_effort` drops
  an unoffered answer on a vocabulary contract, but a bounded model is exempt: dropping it would hand
  the turn to the transport's default, which sits above (or below) the pair. The level is recorded as
  asked and clamped, on a fresh decision and on a memo replay alike — and on all three decider backends
  (`jev`, `openai_compatible`, `jev_console`).
- **A mixed grid is still two contracts.** A grid that mixes a bounded model with an `accepted`-only one
  offers the union, because the bounded model needs it — but the `accepted`-only model keeps the omit rule:
  a level outside its list is dropped from the wire for that model, which then runs the transport's default,
  and its record carries no `effort_bounds`. An operator who wants a ceiling to hold on every model of a
  route declares bounds for every model.
- **A route with no bounds is untouched.** The declarative omit rule, the strict built-in policies and the
  legacy `ollama-cloud` clamp behave exactly as before, and a route record for such a model does not gain
  either key.

#### The wire: `output_config.effort`

This route speaks the Anthropic Messages API, so the level's home is neither a top-level
`reasoning_effort` (not a Messages field) nor the Responses `reasoning` object: the transport turns the
params-level `reasoning_config` into `thinking: {type: adaptive, display: summarized}` plus
`output_config: {effort: …}` **while it builds the body**
(`core/agent/anthropic_adapter.py::_thinking_kwargs`, reached from
`core/agent/transports/anthropic.py::build_kwargs`), and the `llm_request` middleware runs *after* that
build (`core/agent/turn_api_request.py::build_api_request`). So the plugin rewrites **the effort value
inside an `output_config` the transport already built**, and nothing else:

- a body the transport left without an `output_config` — a Claude the core classifies as
  pre-adaptive-thinking, or a session with reasoning disabled (`thinking: {type: disabled}`) — gets
  **none invented**. The audit then reports `effort: null` with `effort_requested` keeping Jev's choice.
- an `output_config` that carries other keys (`format`, for structured output) keeps them; the plugin
  never adds the `effort` key to a container the transport did not put it in.
- `thinking`, `system`, `max_tokens` and the tool block are the transport's and survive untouched.
- an omitted level removes the `effort` key (an `output_config` left empty is dropped instead of sent as
  `{}`), so a `null` `effort` in the audit always means "no level on the wire".

Contract evidence, 2026-11-10: three real routed turns (one per family) returned HTTP 200. The
Messages body was **reproduced**, not captured live — `agent.anthropic_adapter.build_anthropic_kwargs`
for the same model and level yields `"thinking": {"type": "adaptive", "display": "summarized"}` and
`"output_config": {"effort": "low"}`, with no `reasoning_effort` / `reasoning_config` key;
`HERMES_DUMP_REQUESTS=1` does not reach the managed worker that performs the call on this host. The
offline suite proves the shape, the live turns prove the endpoint accepts it
(`docs/EVIDENCIAS_OPERACIONAIS.md` §11).

#### Gate & Privacy Decision (Owner Directive)
- **Owner Directive & Separation of Routes**: The owner's route-reconciliation directive (Comment 108) separated the products and endpoints: Jev SystemOne decides, and generation runs on a dedicated provider. The later single-model directive removed the normal MiMo variant from every generation catalog — no route serves MiMo, and `mimo-v2.6-flash-free` is never substituted. As clarified by official documentation and the owner orientation:
  - **Decider Route**: Jev Console operates via SystemOne (`https://opencode.ai/inference/systemone/v1/systemone`) using primary `jev-1.13-free` with automatic fallback to paid `jev-1.13`.
  - **Generation Route**: Generation of `deepseek-v4.1-flash` is routed to the `opencode-go` provider; the id comes from the official OpenCode Go model list (`https://opencode.ai/v2/docs/console/go`). The opt-in `commandcode` route generates `deepseek/deepseek-v4.1-flash`, an id confirmed from the provider's public unauthenticated `/models` endpoint. The Console decider model is a separate SystemOne id (`jev-1.13-free` / `jev-1.13`). No credential was read to establish either mapping and no paid inference was performed.
  - **Isolation and capped catalogs**: The OpenCode/CommandCode generation catalogs carry exactly one model each, so choosing a model is degenerate there and the decider effectively chooses only the reasoning level; the Codex route is the exception, with three ids across two families. A model outside a route's catalog is never sent, offered as an alternative or replayed; the turn fails open with its payload intact. All catalogs are enforced offline.
- **Explicit Reasoning Policy per Model**: The router distinguishes (1) the logical effort level chosen and returned by Jev (`effort_requested`), and (2) the reasoning parameter/mode accepted by the generation endpoint (`effort`). Each opt-in route states its own contract, and only a probe-confirmed parameter is ever sent:
  - `opencode-console` / `deepseek-v4.1-flash`: unconfirmed on the Console generation endpoint. Support on OpenCode Go (`/zen/go/v1`) or Ollama:cloud cannot be inferred.
  - `opencode-go` / `deepseek-v4.1-flash`: **sends** the decided level as the top-level `reasoning_effort`, on a probe of `POST https://opencode.ai/zen/go/v1/chat/completions` (the `x-opencode-session` header is mandatory) run on 2026-10-20: no field → HTTP 200, and `reasoning_effort=low`, `medium`, `high`, `xhigh` and `max` → HTTP 200, while `reasoning={"effort":"high"}` → HTTP 200 and `thinking=high` → HTTP 400 (`thinking must be an object`). Every accepted response carried `reasoning_content` and `usage.completion_tokens_details.reasoning_tokens` (~25–61 tokens across the probe calls), so the model does emit reasoning on this endpoint. The declared set is the one the Hermes core already drives on this same wire for `deepseek-v4*` ids (`core/plugins/model-providers/opencode-zen` → `DEEPSEEK_V4_EFFORTS = (low, medium, high, max)`, `DEEPSEEK_V4_OVERRIDES = {xhigh: max}`): `low`/`medium`/`high`/`max` pass verbatim, `xhigh` is mapped onto `max` by the same declared override, and anything else (`minimal`, `none`, an unknown string) is omitted instead of being clamped or invented. Two limitations stay on the record: the route does **not** validate the value at all (an out-of-vocabulary string also returned HTTP 200, and one sample per level showed no monotonic reasoning-token scaling), so acceptance is not calibration; and the `thinking` object form is not used at all, because the Go route rejects a scalar there.
  - `commandcode` / `deepseek/deepseek-v4.1-flash`: **sends** the decided level as the top-level `reasoning_effort`, on a probe of `POST https://api.commandcode.ai/provider/v1/chat/completions` run on 2026-10-20: `reasoning_effort=low`, `medium`, `high`, `xhigh` and `max` all returned HTTP 200. The route has **two** reasoning components and they now have **two** authors. The `extra_body.thinking` toggle stays with the provider layer: `build_api_kwargs_extras` (`core/plugins/model-providers/commandcode/__init__.py:42-59`) delegates `deepseek/*` ids to the DeepSeek profile (`deepseek/__init__.py:28-43`), whose `thinking_toggle_extras(..., always_emit_toggle=True)` (`core/agent/reasoning_effort.py:197-219`) emits it from the params-level `reasoning_config`. The top-level `reasoning_effort` is the **plugin's**, with the level Jev decided: the Hermes core builds `api_kwargs` from the profile *before* the `llm_request` middleware runs and then uses the middleware payload for the call (`core/agent/turn_api_request.py::build_api_request`), and the routed payload is built from the pre-middleware `original_request` and rewrites only `model` and `reasoning_effort`. The plugin's level therefore lands **after** the profile's derivation — it overrides the level the profile would have sent and leaves the toggle untouched, so the wire carries `thinking: {"type": "enabled"}` **plus** `reasoning_effort: <level chosen by Jev>`, never one at the expense of the other. The declared set is the same DeepSeek V4 contract the core declares (`low`/`medium`/`high`/`max`, `xhigh`→`max`); anything else (`minimal`, `none`, an unknown string) is omitted rather than clamped or invented. The limitation stays on the record: a 200 proves the endpoint **accepts** the field, not that it grades the level or calibrates depth to it.
  - `openai-codex` / the three `gpt-6*` ids: **sends** the decided level as **`reasoning.effort`**, not as a top-level `reasoning_effort`. This route speaks the Responses API, so the shape is the opposite of the two above: the transport decodes the params-level `reasoning_config` into `reasoning: {"effort": ..., "summary": "auto"}` **while it builds the body** (`core/agent/transports/codex.py::_reasoning_fields`, called from `build_kwargs`), and the `llm_request` middleware runs *after* that build (`core/agent/turn_api_request.py::build_api_request`) — so the payload the plugin receives is already the Responses body, and a top-level `reasoning_effort` in it is not a Responses field at all (the SDK call raises `TypeError: unexpected keyword argument` before a byte is sent). Captured live with `HERMES_DUMP_REQUESTS=1` (`--provider openai-codex`): the body carried `"reasoning": {"effort": "high", "summary": "auto"}`, `"include": ["reasoning.encrypted_content"]` and no `reasoning_effort` key. The plugin rewrites only `reasoning.effort`; `summary`, `include` and the whole object stay the transport's, and a route the transport judged as taking no reasoning field gets none invented (the audit then reports `effort: null`, with `effort_requested` keeping Jev's choice). Vocabularies are per family — `CODEX_GPT56_EFFORTS` (`none`/`low`/`medium`/`high`/`xhigh`/`max`) for the Luna ids, `CODEX_ASTRA_EFFORTS` (`low`/`medium`/`high`/`xhigh`/`max`, **no `none`**) for the Sol ids — and `gpt-6-luna-900k` inherits its base's set through the same slug prefix, exactly as the core's `codex_supported_efforts` does (it is the only `-900k` id on this route). A `none` request on the Sol family is omitted rather than sent: that route 400s on a disable, and an omitted field re-enables the default effort instead of switching it off.
  - `anthropic` / the three `claude-*` ids: **sends** the decided level as **`output_config.effort`**, for the same structural reason as Codex — the transport builds the field, the middleware runs after. `core/agent/anthropic_adapter.py::_thinking_kwargs` maps `reasoning_config.effort` onto `output_config.effort` (through `ADAPTIVE_EFFORT_MAP`: `ultra`→`max`, `max`→`max`, `xhigh`→`xhigh`, `high`/`medium`/`low` verbatim), and the plugin rewrites only that value inside an `output_config` the transport already built; it never creates the object and never adds the `effort` key to a container built without it (pre-adaptive-thinking Claude, reasoning disabled, or an `output_config` carrying only `format`). The caps are per family — Haiku up to `max`, Sonnet/Opus up to `high` — and a level outside a cap is omitted rather than clamped **when the built-in vocabulary governs**; an operator entry declaring `min`/`max` for the model clamps onto the pair instead (§ per-model effort bounds above). Evidence on 2026-11-10 is a **reproduction, not a live capture**: `build_anthropic_kwargs` yields `"thinking": {"type": "adaptive", "display": "summarized"}` and `"output_config": {"effort": "low"}`, with no `reasoning_effort` and no `reasoning_config`, while three real routed turns (one per family) returned HTTP 200 with the decided level in the audit (`docs/EVIDENCIAS_OPERACIONAIS.md` §11).
  - **Audit Semantics**: Logical effort evaluated by Jev is recorded in audit logs under `effort_requested` and `effort_choice`. Wire `effort` records the value actually put on the wire — the decided level on `opencode-go`, `commandcode`, `openai-codex` and `anthropic`, and `null` on the routes that omit the field (`opencode-console`, any Codex route the transport judged as taking no `reasoning` field, and any Anthropic route the transport built without an `output_config.effort`) — so a `null` is a statement about the wire, never about what the model executed. A route record also names its `provider`, the same key a skip record carries, and `wire_model` beside `model`: `model` is the id the decision chose (possibly a `-900k` picker alias) and `wire_model` is the id written into the provider's `model` field, so a record for `gpt-6-luna-900k` cannot be read as a 900k-capable route. On `opencode-go`, `commandcode` and `openai-codex` an HTTP 200 proves the parameter was accepted, not that the level was graded. A model with declared effort **bounds** adds `effort_bounds: [min, max]`, and when the pair actually bit, `effort_adjusted: "clamped_max" | "clamped_min" | "nearest_accepted"` — the keys together are the proof that the ceiling held, since `effort` alone cannot say whether the decider asked for the level that ran.
  - **Custom Override**: Operators can declare `effort_maps` for any routable provider, and the declaration *is* the route's vocabulary — the same rule applies whether or not the route also has a built-in policy. A requested level that `accepted` names goes out verbatim; otherwise `overrides[requested]` is used, but only when its target is itself a declared level; otherwise the field is omitted. There is no ordering, no clamp and no anti-escalation rule, so `xhigh → max` works because it is written down and a level nobody declared is never sent. A model entry that declares `min`/`max` instead of a list is the bounded shape (see the `anthropic` section): it derives `accepted` from the pair and clamps onto it, and it is the only operator declaration that clamps. `"none"` is an ordinary level when the map declares it (Luna's set includes it, Sol's does not), while an absent request — `null` or empty — is always an omission: a YAML `null` is never promoted to the level `"none"`. An entry for a model **replaces** that model's built-in policy wholesale (levels, overrides and the omission decision), so a map can narrow or even disable a route, but it never silently inherits a level; a provider map that does not mention a model leaves the built-in fallback in place, and an entry that matches with an empty spec omits rather than falling back. Model keys are matched by full id, then bare slug, then the longest `-`-delimited prefix — never by a substring — so `gpt-6-luna-900k` can carry its own vocabulary beside `gpt-6-luna`. Providers are looked up by exact name, then by the canonical name of a known alias (`config._GRID_ALIASES`). Maps written for the old clamp semantics change behaviour: a level that used to be clamped to the nearest weaker accepted one is now omitted — declare the override or accept the omission (see `CHANGELOG.md`).
- **Isolation from the Ollama cache**: The Ollama catalog never inspects or filters models of any opt-in route; and no opt-in route can inherit an Ollama grid entry.


### Effort-to-wire mapping (Ollama:Cloud built-in)

For `ollama-cloud`, every route lands on the `ollama-cloud` provider profile, whose own profile code
(`plugins/model-providers/ollama-cloud/__init__.py`) already clamps to
`none | low | medium | high | max` and maps `xhigh → max`, and which emits the field only when the model
is resolved as reasoning-capable. The plugin's table exists to (a) state the family's real vocabulary
where Hermes' generic clamp would be wrong, and (b) omit the field instead of risking a 400.

| Model family | Wire parameter | Accepted levels | Mapping of `low` / `medium` / `high` | Source |
|---|---|---|---|---|
| DeepSeek (V4 / V4.1) | top-level `reasoning_effort` | low, medium, high, max | `low` → low, `medium` → medium, `high` → high | `DEEPSEEK_V4_EFFORTS`, `DEEPSEEK_V4_OVERRIDES` |
| Kimi K3 | top-level `reasoning_effort` | low, high, max (medium → **high** is the declared override; server default is high) | `low` → low, `medium` → high, `high` → high | `KIMI_K3_EFFORTS`, `KIMI_K3_OVERRIDES` |
| GLM-5.3 / GLM-5.3 Flash | top-level `reasoning_effort` | low, medium, high, max (graded, live-verified, monotonic) | passthrough | `GLM53_EFFORTS` |
| MiniMax M3 | top-level `reasoning_effort` | via the Ollama:cloud profile set (none, low, medium, high, max) | passthrough | `OLLAMA_CLOUD_EFFORTS` |
| Nemotron 3 Nano | top-level `reasoning_effort` | via the Ollama:cloud profile set | passthrough; the model is offered for simple tasks, not reasoning, so a `high` answer is recorded and honoured | `OLLAMA_CLOUD_EFFORTS` |

Rules that follow from the table:

1. **Never escalate.** When the requested level is not accepted, clamp to the nearest *weaker* accepted
   level; only send the weakest accepted level when nothing weaker exists. Hermes' `clamp_effort` already
   implements exactly this and is the reference implementation for the plugin's own table.
2. **Never invent a level.** A level outside `low | medium | high` (and the family's override targets) is
   dropped, and the request goes out with no `reasoning_effort` field at all rather than a 400.
3. **`none` is a disable, not a rung.** The plugin never routes to a reasoning-off state.
4. **`max` is not reachable by Jev's three-level question.** It is a valid target only as an override
   destination (`medium → high` for Kimi K3 is; `xhigh → max` is not reachable at all here, and where a
   route declares it — as the `opencode-go` policy does — only a direct API call can ask for `xhigh`).

This table is the record of what has been verified. A family added later gets its row filled in from a
live probe before it joins the grid; until then the generic clamp applies and the audit record marks the
effort as `clamped`.

### Effort mapping for custom providers (e.g. OpenCode)

For providers other than `ollama-cloud` the level comes from the model's **declaration** — an operator `effort_maps` entry when one matches, otherwise the route's built-in per-model policy — and both paths run the **same** algorithm (`effort.resolve_declared_effort`). There is one rule, not one per provider:

1. **Accepted level, verbatim.** If the requested level is in `accepted`, it goes out unchanged (after `strip().lower()`). It wins even when an override names a different level. No positional translation: a declared `medium` is `medium`, never remapped to a neighbouring rung.
2. **Declared override.** Otherwise `overrides[requested]` is used, but only when the target is itself an accepted level. Overrides are never chained, and the target may be *stronger* than the request — `xhigh → max` is honoured because it is written down, not inferred. A target outside `accepted` is ignored.
3. **Omission.** Anything else is omitted: the request goes out with no field rather than a level the route never declared. `minimal`, `ultra` and an unknown string are omitted, and so is a level the map's `accepted` narrows away. **One exception: a bounded entry** (`min`/`max`) clamps such a level onto the nearest bound instead of dropping it (rule 4 below) — an omitted level would fall back to the transport's own default and a declared ceiling would hold nothing.
4. **Bounds clamp, they do not omit.** An entry that declares `min`/`max` for a model derives `accepted` from the pair and resolves through `effort.resolve_bounded_effort`: inside the pair verbatim, above the ceiling the ceiling (`clamped_max`), below the floor the floor (`clamped_min`), and a rung inside the pair the vocabulary does not carry lands on the nearest accepted level at or below it. The audit carries `effort_bounds` and `effort_adjusted` so the clamp is provable, not inferred. `none` and words outside the ladder are still omitted.
5. **`none` is a level, `null` is not.** `"none"` is sent when (and only when) it is declared — `accepted: ["none"]` is a valid contract and reports `is_omitted=False`. Python `None`, an empty string, whitespace, YAML `null`, numbers and booleans are never promoted to a level; the parser drops them from `accepted`, from override keys and from override targets, so a null entry cannot masquerade as the level `"none"`.
6. **Declaration replaces, never merges.** A matched model entry replaces the built-in policy's accepted levels, overrides and omission decision. A provider map that does not mention a model leaves that model's built-in policy in place; an entry that matches with an empty or null spec (`[]`, `{}`, `null`) is a deliberate shadow and omits instead of falling back.
7. **Matching is deterministic.** Model keys resolve by full id exact, then a namespace-free key equal to the slug, then the longest true prefix (the id continues with `-`); a key merely contained in the middle of an id never matches, and a qualified key never claims another namespace's model. Providers resolve by exact name, then the canonical name of a registered alias. Ties go to the longer model part, then qualified before bare, then lexically — never insertion order.
8. **The wire shape is still the transport's.** `wire_parameter` on a policy is metadata only: `reasoning` for Codex and its aliases, `output_config.effort` for the Anthropic route, `reasoning_effort` for the other custom contracts, `None` when the contract is omitted. `router.py` keeps choosing the payload shape from `api_mode`, so a map can add a level inside an existing `reasoning` object or an existing `output_config.effort`, but can never create either. A bounded model's level is resolved even when the container is absent — the record then reports `effort: null` and `effort_adjusted` says why the request was changed, because the transport's ownership of the container outranks the bounds.
9. **Two clamps, both declared.** The legacy `ollama-cloud` family fallback (**no** map entry) keeps its historical nearest-weaker clamp, and an operator entry with `min`/`max` clamps onto its own bounds. Every other declared policy — a map with an `accepted` list, or a strict built-in — omits instead.

Operating rules that hold on every route:

- **No speculation / No unverified levels:** Never declare reasoning effort levels without empirical verification via live probe or official provider specifications.
- **Never escalate by guesswork:** A level is never rounded up or down on a declared route; it is sent or omitted. The only clamps in the codebase are the two *declared* ones — the legacy `ollama-cloud` family fallback for a route with no map, and an operator entry that declares `min`/`max` for a model.
- **Omission on unverified or empty maps:** If an unverified model or an empty accepted list (`accepted: []`) is encountered, the router omits `reasoning_effort` from the request altogether. It never falls back to guessing `low`/`medium`/`high` on custom providers.
- **Live probe evidence:** The opt-in live probe suite (`tests/test_live_probe.py`) verified against OpenCode (`https://opencode.ai/zen/go/v1`):
   - `deepseek-v4.1-flash`: accepts `null` (omitted), `low`, `medium`, and `high` with HTTP 200. The 2026-10-20 direct probe recorded in the Go reasoning policy above is the evidence the built-in policy cites.

### Confidence semantics across deciders

Confidence scoring differs fundamentally between the two supported decider backends:

- **TypeSafe Jev (`jev`)**: Produces mathematically calibrated probability distributions across all choices in the question. The `confidence` value represents the concentration of probability on the winning option.
- **OpenAI-Compatible (`openai_compatible`)**: Produces an LLM self-reported heuristic score parsed from the JSON output. While constrained to `[0.0, 1.0]`, this score is uncalibrated and subjective to the model's self-assessment.

## Levers left, in the order they are worth trying

Measured findings and the order of expected return, so the next pass does not re-derive them.

1. **The confidence threshold is the second-order cause of an unused model.** A model can win the
   decision and still never serve a turn: below the 0.5 threshold the answer is discarded and the
   configured fallback model is applied instead. On the pre-rewrite grid, 3 of the 8 task families sat
   at mean confidence 0.40-0.42 and were degraded 5/5 — the grid was deciding and the threshold was
   throwing it away. A per-task threshold, or a threshold that only applies when the leading
   probability is not a clear margin over the runner-up, recovers those turns without loosening
   anything on the tasks that are already confident. Use `grid_coverage.below_threshold` in `status`
   to see how often it fires before changing the number.
2. **Effort can depend on the chosen model, but the endpoint cannot do it.** The two questions are
   evaluated independently and in parallel, so a `high` effort answer reaches a model chosen for
   throughput. Coupling them in code — asking for the effort *after* the model is known, or mapping
   `(model, effort)` through the per-family table instead of only clamping the level — is a behaviour
   change that needs its own measurement. There is no evidence yet that it pays.
3. **`route_per_turn: false` (once per session) is the wrong default for a mixed session.** A session
   that opens with a greeting and then asks for a refactor is served by one model the whole way. Routing
   per turn is the default because of this; the cost is one decision call per user turn, at p50 0.27 s.
4. **Do not add models, and do not reorder the grid, without a measurement.** Every extra option
   measurably dilutes a Choice decision, and the first-listed option has an advantage that is visible
   in the numbers (the generalist holds its position partly through wording, not only through fit).
   Both were measured here; changing either without re-running the 8-task × 5-call protocol makes the
   effect unattributable.
5. **Re-measure after any provider-side change.** Two independent sources of drift: the model ids
   themselves (`nemotron-3-nano` → `nemotron-3-nano:30b`) and the decision model's own calibration
   (`jev_model` pinned to `typesafe/jev-1.13` for exactly this reason). The measurement script pattern
   is: import the plugin package through `importlib`, swap the criterion text, POST to the live
   endpoint with the profile's key, 5 calls per task family, then compare chosen model, mean
   confidence and calls above the threshold.
