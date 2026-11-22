# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.1] - 2026-11-22

### Added

- **Per-model effort bounds (`min`/`max`) in an operator `effort_maps` entry.** A model entry may
  now declare a pair instead of a level list:

  ```yaml
  effort_maps:
    anthropic:
      claude-haiku-5-5:  {min: medium, max: max}
      claude-sonnet-5-5: {min: medium, max: high}
      claude-opus-5-5:   {min: low,    max: medium}
  ```

  The pair *is* the contract: `accepted` is derived from it (`effort.levels_within`, the askable
  levels between the bounds inclusive), a request inside the pair goes out verbatim, and a request
  outside it is **clamped to the nearest bound** (`effort.resolve_bounded_effort`). One open side
  is filled from the askable vocabulary (`low` / `max`), a bound outside it is refused, and an
  `accepted` list that disagrees with the bounds makes the entry declare nothing rather than
  letting one key silently win.
- **The effort question offers the union of a bounded grid.** `offered_levels_for_grid` kept
  intersecting the models' sets, so the three-model `anthropic` grid with the bounds above would
  have offered one option (`medium`) and every routed turn would have landed there. A grid with at
  least one bounded model now offers the **union** (`low`, `medium`, `high`, `max`), because a
  bounded model clamps whatever it cannot take. A grid with no bounds keeps the intersection.
- **The audit proves the clamp.** A route record for a bounded model now carries `effort_bounds:
  [min, max]` and, only when the pair actually bit, `effort_adjusted: "clamped_max"` /
  `"clamped_min"` (or `"nearest_accepted"` for a rung inside the bounds the vocabulary does not
  carry, e.g. `xhigh`). `effort_requested` keeps the request and `effort` the applied value, so a
  capped turn can no longer be read as if the stronger level had run. Records for routes without
  bounds keep exactly their previous shape.

### Changed

- **"Omitted, never clamped" now has one documented exception.** An operator entry with bounds
  clamps instead of omitting, because an omitted level falls back to the transport's own default
  and a declared ceiling that is dropped holds nothing. Everything else is unchanged: a map with an
  `accepted` list keeps the declarative omit rule, the strict built-in policies keep their own
  vocabularies, and the legacy `ollama-cloud` family table keeps its nearest-weaker clamp. An entry
  for a provider with no bounded model changes nothing at all.
- **An answer the question did not offer is clamped, not dropped, on a bounded model.**
  `decider._offered_wire_effort` keeps its gate for a vocabulary contract — an unoffered level must
  not reach the wire — but a model with bounds takes any level by clamping, so the level is resolved
  instead of being replaced by the transport's own default (on a fresh decision and on a memo replay).
- **All three decider backends take the bounds.** `decider: jev` (the legacy Decisions API) was still
  resolving the level without the model, provider and map the exemption needs, so an answer its
  question did not offer was dropped on a bounded model — and the transport's own default then ran,
  which is exactly what the ceiling exists to prevent. `jev`, `openai_compatible` and `jev_console`
  now share the same clamp.
- **A bounded model is exempt from that gate** in all three deciders and in the memo replay. Every
  call site passes `model`/`provider`/`effort_maps` to `_offered_wire_effort`.
- `docs/SPEC.md`, `docs/routing-grid.md`, `docs/jev-decisions-api.md`, `README.md` and
  `plugin.yaml` document the bounded contract, the union question and the two audit keys, including
  the mixed-grid caveat (a grid with bounded *and* `accepted`-only models offers the union, but the
  unbounded model still omits a level outside its list).

### Added (tests)

- `tests/test_effort_bounds.py`: the declaration (derivation, one open side, contradictory and
<<<<<<< HEAD
  unorderable pairs, per-model lookup), the union question, all twelve model×level clamps with their
  audit reason, the "a bounded model never drops the field" invariant, the `xhigh` gap case, the
  three deciders applying the same clamp, the compatibility paths (accepted-only map still omits,
  built-in caps still omit, legacy Ollama clamp untouched), and the wire/audit end to end —
  including a replay that re-applies the declaration to a memo written before the bounds existed.

### Notes

- **Rebased onto 0.8.0 (`399f34c`).** This release is the bounds work replayed on top of the 0.8.0
  removal commit, so the removals stand: there is no `claude-subscription-directsdk-experimental`
  route, no route-specific wire-style layer, no `[1m]` context variant and none of the four knobs
  (`confidence_threshold_per_decider`, `effort_criteria`, `effort_instructions`,
  `platform_overrides`). With `platform_overrides` gone there is no per-surface `max_effort` cap any
  more, so the bounds are applied to the level the decider produced — the cap-versus-bounds
  attribution this work carried on its pre-rebase 0.7.1 line (`Decision.effort_capped`, the
  `effort_capped` audit key) is not part of this release because its subject no longer exists.

## [0.8.0] - 2026-11-15

### Removed (breaking)

- **The `claude-subscription-directsdk-experimental` route is gone.** Its provider plugin left the host
  on 2026-11-15, no profile listed it under `routable_providers` (0/9 configs), and the operator's
  standing rule is not to reintroduce the dependency. Everything the route owned was deleted, not
  merely disabled:
  - `config.py`: `CLAUDE_SUBSCRIPTION_PROVIDER/_ALIASES/_MODELS/_ALLOWLIST`, its entries in
    `_GRID_ALIASES` and `ROUTE_DEFAULT_MODELS`, and its branches in `route_allowlist` and
    `route_catalog`.
  - `grid.py`: `SUBSCRIPTION_GRID`. `effort.py`: `SUBSCRIPTION_MODEL_LEVELS`,
    `SUBSCRIPTION_REASONING_POLICIES`, its `WIRE_PARAMETERS` entry and the subscription branch of
    `model_reasoning_policy`.
  - `router.py`: `_apply_extra_body_reasoning` and every `extra_body_reasoning` branch.
- **The route-specific wire-style layer.** `WIRE_STYLE_EXTRA_BODY_REASONING` and `ROUTE_WIRE_STYLES` are
  removed: with its only entry gone the table could no longer change a payload, so `config.wire_style`
  now answers from `API_MODE_WIRE_STYLES` alone and `wire_style`/`wire_effort`/`Router._apply`/
  `Router._rewrite`/`Router._applied_decision` no longer take a provider argument they cannot use.
- **The `[1m]` context variant.** `CONTEXT_VARIANT_SUFFIXES`, `context_variant_suffixes`,
  `split_context_variant` and their call sites are removed, together with the audit keys that only
  existed for them (`normalized_model`, and `configured_model` on a `route` record). Model ids are now
  compared and sent exactly as configured. The native `anthropic` route never needed this: its 1M
  window is the `context-1m-2025-08-07` beta header, and an unexpected `[1m]` there stays a
  `model_not_in_grid` skip.
- `tests/test_subscription_route.py`, the `[1m]`/both-routes cases of
  `tests/test_anthropic_native_and_variant.py` (now `tests/test_anthropic_native.py`) and
  `tests.stubs.subscription_request`.
- **Four settings no deployment ever used, and the code that only existed for them.** None of the
  nine profiles declares any of them, and 26 days of audit never show them in effect:
  - `confidence_threshold_per_decider` — with one decider per profile, `confidence_threshold` is the
    only threshold. `Settings.confidence_threshold_for` is gone; `effective()` reads the single
    value, and `status` reports it.
  - `effort_criteria` and `effort_instructions` — the built-in rubric in `state.py` is now the sole
    source of the effort question's text and rule. `state.build_questions`/`build_payload` no longer
    take the override parameters, and the tools that helped (`parse_effort_criteria`,
    `parse_effort_instructions`, their length caps, and the `override` parameters of
    `state.effort_criteria`/`state.effort_instructions`, now `rubric_criteria`/`rubric_instructions`)
    are removed with them.
  - `platform_overrides` — the per-surface policy layer (`PlatformPolicy`, `Settings.policy_for`,
    `parse_platform_overrides`, the `policy` branch of `Settings.effective`,
    `EffectiveSettings.max_effort`/`grid_from_policy`, the `surface_disabled` skip reason and the
    `max_effort` field of both the memo record and the `route` audit record). `Settings.effective()`
    now takes only the provider, so a surface can no longer be disabled, re-gridded, re-thresholded
    or capped on its own.
- The tests that only exercised those knobs (`tests/test_config_policy.py`'s policy half, the rubric
  and cap cases of `tests/test_effort_rubric.py`, `tests/test_client.py`'s per-surface section, the
  `CRON_POLICY` block of `tests/test_router.py`, and the surface-cap case in
  `tests/test_session_selection.py`). `tests/test_config_policy.py` gains the compatibility case
  that matters after a removal: an old `config.yaml` still carrying the four keys loads unchanged.

### Changed

- **`session_selection.provider_map` defaults to `{}`** (identity only) instead of
  `anthropic -> claude-subscription-directsdk-experimental`. A selection is still eligible on its own
  route, aliases included; the map only *adds* a second route a pick is also valid on, and an operator
  who wants that translation now declares it. `plugin.yaml` and the README say so.
- The session-selection suite runs on the native `anthropic` route (`output_config.effort`), and
  `plugin.yaml`/`pyproject.toml` say `0.8.0`.
=======
  unorderable pairs, per-model lookup), the union question, all twelve model×level clamps with
  their audit reason, the "a bounded model never drops the field" invariant, the `xhigh` gap case,
  the compatibility paths (accepted-only map still omits, built-in caps still omit, legacy Ollama
  clamp untouched), and the wire/audit end to end — including a replay that re-applies the
  declaration to a memo written before the bounds existed, the same clamp through the
  `openai_compatible` and `jev` deciders, and the two cap-vs-bounds attribution cases.
>>>>>>> baac58c (fix(effort): bound the legacy jev decider and attribute the cap honestly)

## [0.7.0] - 2026-11-15

### Changed

- **A profile's `plugins.entries.jev-effort-router` block drops from 50 to 9 lines with no change in
  routing** (`docs/SPEC-simplificacao-config.md`). Nothing about `grids` or `effort_maps` was
  operator-specific: the route catalogs in `grid.py` and the effort contracts in `effort.py` already
  held the same declarations. The four pieces that had to move into the code first:
  - `effort.OPENCODE_GO_REASONING_POLICIES`, `COMMANDCODE_REASONING_POLICIES` and
    `CODEX_REASONING_POLICIES` now declare `offered=("low","medium","high","max")`. Until now
    `max` was offered only because the *operator* declared it in `effort_maps`
    (`offered_levels` deliberately ignored a built-in policy's `accepted_levels`), so deleting the
    map without this change would have quietly removed `max` from three routes' effort question.
    `ANTHROPIC_MODEL_LEVELS` is untouched: Opus/Sonnet still stop at `high`, Haiku keeps `max`, and
    a level above a cap is omitted, never clamped.
  - `console_jev_commandcode_base_url` is now the whole opt-in for the SystemOne fallback. With the
    URL declared, an empty `console_jev_commandcode_model` resolves to `typesafe/jev` and an empty
    `console_jev_commandcode_api_key_env` to `COMMANDCODE_API_KEY`
    (`config.resolve_commandcode_fallback`). With the URL empty the fallback is disabled **without a
    warning**: the non-ZDR upstream is never reached by a default, and a leftover model/key is inert
    rather than an error. Both keys had the same value in every profile anyway.
  - `plugin.yaml` documents `endpoint`, which `load_settings` has always read (the legacy `jev`
    decider POSTs to it) and the schema was missing, and its descriptions now say what the code
    does: `console_jev_fallback_model: ''` **disables** the paid `jev-1.13` leg, `grids` and
    `effort_maps` are overrides of the built-in declaration, and the CommandCode model/key default
    only once the URL is named.
  - Corrected the `config.py` docstring: `ctx.get_config` returns the caller's default, it does
    **not** merge `plugin.yaml`'s `config_schema` defaults, so the manifest documents the settings
    and the effective defaults live in `load_settings`.

### Added

- **`tests/test_config_simplification_equivalence.py`**: the block from before the simplification
  (`tests/fixtures/jev_settings_legacy.json`, transcribed from a real profile) and the 9-line block
  (`tests/fixtures/jev_settings_target_minimal.json`) are compared setting by setting for
  `opencode-go`, `commandcode`, `openai-codex` and `anthropic`: grid ids, descriptions, the wire id
  each model reaches, the degraded model, the offered levels, and `resolve_effort` for every model ×
  level of the ladder. The only permitted difference is the `description` of the two DeepSeek
  entries, which is a criterion Jev reads and never reaches the wire. Fixed assertions pin the
  anthropic caps (`opus`/`sonnet` up to `high`, `haiku` up to `max`; `xhigh`/`max` on Opus resolve to
  `None`). Dropping an `offered` declaration makes the suite fail, which is the point: the code is
  now the single source.

- **The native `anthropic` route is routable, and the subscription route's `[1m]` context variant is
  normalised.** The owner's decision (card t_ec2453b1): the Jev routes *both* Claude routes, because
  what a turn costs is decided by the credential the core resolves, not by the provider id — Claude
  Code's OAuth spends the subscription quota, an `sk-ant-api*` key bills per token.
  - `session_selection`'s `provider_map` became **additive** ("also valid in"): a selection is always
    eligible on its own route (`config.same_provider_route`, the alias table), and the map adds the
    second route the same choice applies to. The default `anthropic →
    claude-subscription-directsdk-experimental` entry stays, so the t_9bda5849 race keeps reconciling
    on the subscription route, while a native `anthropic` pick no longer turns into
    `selection_unmappable/provider_mismatch` on a native request.
  - `config.CONTEXT_VARIANT_SUFFIXES` declares the context-window suffixes per route (`[1m]` on
    `claude-subscription-directsdk-experimental`). The grid, catalog, memo, divergence and selection
    comparisons use the bare id, and the write paths re-append the suffix, so a
    `claude-sonnet-5-5[1m]` request is routed (it used to skip as `model_not_in_grid`) and the wire
    carries, for example, `claude-haiku-5-5[1m]` — the id the provider's own `native_model` accepts
    and resolves back to itself. The audit keeps `configured_model` verbatim and adds
    `normalized_model` when a suffix was normalised. The native `anthropic` route declares no variant:
    its 1M window is the `context-1m-2025-08-07` beta header's, so an unexpected `[1m]` there is still
    a skip.
  - Corrected the billing claim in the code and in the docs: the plugin no longer states that the
    native route is "billed per token against the API key". `_note_native_selection` still fires when
    the route is not in `routable_providers` (out of the build race there is no lever), and its
    warning now names the unresolved credential instead of a price.
  - Tests: spec §5 `N1`–`N5`, `M1`–`M4`, `B1`, `B2`, `F1`–`F3` in
    `tests/test_anthropic_native_and_variant.py`, plus the D2 test in `test_session_selection.py`
    rewritten to the new semantics.

- **Opt-in reconciliation of the webui session's own model/effort pick (`session_selection`).** The
  webui writes the operator's choice into the session row (`sessions.model_config`) and in the same
  breath hands it to the agent being built; when the pick lands while the pre-warmed build is
  already running, only the row gets it and the turn silently runs on the profile's default (the
  diagnosed job-search turn: UI `claude-haiku-5-5`/`medium` on `anthropic`, agent
  `claude-sonnet-5-5`/`high` on the subscription route). The new `selection.py` reads that row —
  stdlib only, `file:<profile>/state.db?mode=ro` with a 50 ms lock timeout, cached per turn — and
  `Router._fail_open` is now the single exit of every **post-gate** fail-open: `low_confidence`
  (fresh and replayed), `timeout`, `no_api_key`, `exception`, `skipped_unknown_model`,
  `skipped_no_message`, `skipped_model` and the memo mismatches. In `mode: apply` that exit returns
  the request rewritten to the session's model (`wire_model_for`) and effort (`resolve_effort`, then
  the route's own writer, so caps omit rather than clamp and `reasoning.enabled: false` survives),
  and the `skip` record carries `reconciled_model`, `reconciled_effort` and
  `selection_source: session_row`. `mode: warn` records `selection_divergence` (plus a `WARNING`)
  and rewrites nothing; `mode: off` is the default and never touches the database. A selection that
  is not applicable to the request is reported as `selection_unmappable` and applied to nothing; an
  agent already on the native `anthropic` route only produces `selection_native_provider` while that
  route is not in `routable_providers` (out of the build race there is no lever, and no middleware can
  move an agent). A successful decision still wins:
  this is a fallback for the fail-open path, never a ceiling on the decider. The gates that may not
  be overridden (`disabled`, `provider_not_routed`, `api_mode_not_routed`, `surface_disabled`,
  `user_data`) keep returning an untouched request.

## [0.6.0] - 2026-11-10

### Added

- **Opt-in `claude-subscription-directsdk-experimental` generation route, wired on
  `extra_body.reasoning.effort`.** A **different provider** from the native `anthropic` route of
  0.5.0: Claude Pro/Max driven through the official Claude Code CLI, `api_mode=chat_completions`,
  and a body whose reasoning object sits one level below the kwargs. Its catalog is
  `SUBSCRIPTION_GRID` — the same three native ids the provider's own `model_catalog.py` pins
  (`claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`), copied verbatim rather than composed
  from a family name or taken from the short `opus`/`sonnet`/`haiku` aliases. The effort caps are
  per family and are this plugin's decision, not a wire limit (`SUBSCRIPTION_MODEL_LEVELS`):
  `low`/`medium`/`high` on Opus and Sonnet, plus `max` on Haiku; a level outside a cap is
  **omitted**, never clamped. The route's default model is `claude-opus-5-5` — a degraded turn keeps
  the planning family, because this route exists to serve planner/reviewer work — and the cap is the
  same opt-in mechanism as Go/CommandCode/Codex/Anthropic (`route_allowlist` / `route_catalog` under
  `decider: jev_console`, `_GRID_ALIASES`, `ROUTE_DEFAULT_MODELS`).
- **A declared wire style, so the router stops inferring the container from the api mode.** `config`
  gained `WIRE_STYLE_*`, `API_MODE_WIRE_STYLES`, `ROUTE_WIRE_STYLES` and `wire_style(provider,
  api_mode)`: the route declares **where** the level goes (`top_level`, `responses`,
  `output_config`, `extra_body_reasoning`) and `Router._apply` / `_applied_decision` dispatch on that
  name, receiving the `provider`. Adding the subscription route therefore changed no other route's
  payload, and nothing in the router tests a provider id. The new branch
  (`Router._apply_extra_body_reasoning`) writes **only** `extra_body.reasoning.effort` inside a
  `reasoning` object the provider profile already created inside an `extra_body` the core already
  created: it preserves `reasoning.enabled` and every other key, creates neither container, never
  writes a top-level `reasoning_effort` (the provider's kwarg allowlist rejects the field with
  `ValueError: Unsupported request parameters`), and leaves a `reasoning` object the transport
  disabled (`enabled: false`) exactly as built — the audit then reports `effort: null` while
  `effort_requested` keeps Jev's choice.
- **Evidence §12, reproduced through the provider's own parser.** `docs/EVIDENCIAS_OPERACIONAIS.md`
  §12 records the three sources of the contract (`build_api_kwargs_extras`, this plugin's rewrite,
  `directsdk.py::request_body`), the resulting `thinking` / `output_config.effort` for the three ids
  (`high`, `medium`, `max`), the provider's `ValueError` for a top-level `reasoning_effort`, and what
  a disabled `reasoning` object becomes on the wire. It is a **reproduction, not a live capture**:
  the card delivers the route only, so no routed turn ran and no HTTP status is claimed.

### Changed

- **`_custom_wire_parameter` is a table, not a chain of `if`s.** The field a custom `effort_maps`
  contract rides in is now `effort.WIRE_PARAMETERS` keyed by canonical route name (`reasoning` for
  Codex, `output_config.effort` for Anthropic Messages, `extra_body.reasoning.effort` for the
  subscription route, `reasoning_effort` for everything else). Metadata only — the payload branch is
  still chosen by `config.wire_style`.
- **`ModelReasoningPolicy.offered` is no longer a one-route exception.** Both Claude routes declare
  the levels their own question asks for; every other route still leaves the field empty and keeps
  the three-level default, so no existing question widens.

## [0.5.0] - 2026-11-10

### Added

- **Opt-in `anthropic` generation route with per-family effort caps.** A new provider (`anthropic`,
  aliases `claude` / `claude-code`, mirroring the core's own provider table) with its own catalog,
  `ANTHROPIC_GRID`: `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5` — one id per family,
  copied verbatim from the provider's model list (the Hermes cache at
  `<HERMES_HOME>/provider_models_cache.json` → `anthropic`, i.e. `/v1/models`), never composed from a
  family name. It is the second route whose payload shape is not `chat_completions`: the provider
  speaks `api_mode="anthropic_messages"`, so `ROUTED_API_MODES` now carries that mode and `_apply()`
  writes the level as **`output_config.effort`** — inside the `output_config` the transport already
  built from the params-level `reasoning_config`
  (`core/agent/anthropic_adapter.py::_thinking_kwargs`). The plugin adds neither the container nor the
  key: a body the transport left without an `output_config` (a pre-adaptive-thinking Claude, a session
  with reasoning disabled, or a container carrying only `format` for structured output) gets no field
  invented, and the audit then reports `effort: null` while keeping `effort_requested`. `thinking`,
  `system`, `max_tokens` and the tool block are the transport's and survive untouched; an omitted
  level removes the `effort` key (an `output_config` left empty is dropped, not sent as `{}`). The
  caps are per family and are this plugin's decision, not a wire limit: `low`/`medium`/`high` on
  Sonnet and Opus (a `max`/`xhigh` turn there costs several times a `high` one and this deployment has
  no measured need for it), plus `max` on Haiku. `ModelReasoningPolicy` gained an `offered` field for
  exactly this: a route may declare the levels its question asks for, and only `anthropic` ships one,
  so no other route's question widens. A level outside a family's cap (`max` on Opus/Sonnet, `xhigh`,
  `minimal`, `ultra`, an unknown string) is **omitted**, never clamped onto `high`. The cap is the same
  opt-in mechanism as Go/CommandCode/Codex (`route_allowlist` / `route_catalog` under
  `decider: jev_console`), the route's default model is `claude-sonnet-5-5` (a degraded turn keeps a
  general-purpose family, not the cheapest one), and both aliases are routable through `_GRID_ALIASES`.
  Three real routed turns on 2026-11-10 (one per family, `--provider anthropic`) returned HTTP 200
  with the decided level in the audit; the Messages body was **reproduced** from
  `agent.anthropic_adapter.build_anthropic_kwargs`, not captured live — `HERMES_DUMP_REQUESTS=1`
  does not reach the managed worker that performs the call on this host
  (`docs/EVIDENCIAS_OPERACIONAIS.md` §11).

- **Four-step effort rubric, configurable, plus `max` offered only where the route declares it.**
  `EFFORT_LEVELS` now carries `low`/`medium`/`high`/`max`, and the built-in rubric says what each
  rung covers with an example, under the rule "pick the lowest level that covers the work; raise it
  for uncertainty, risk or irreversibility — never for message length". The measured failure mode of
  the old three vague levels was mass splitting between neighbours and a confidence below the
  router's threshold, which sent the turn to the host default. Operators can reword the rubric
  without a commit: `effort_criteria` (level → text) and `effort_instructions` (string) in
  `plugins.entries.jev-effort-router.settings`. A knob with a level outside the vocabulary, a
  non-string or empty text is discarded whole and the built-in rubric applies; a partial map
  overrides only the levels it names.
- **`max` as a real level, scoped to the route that accepts it.** `effort.offered_levels` (and
  `offered_levels_for_grid`) derives the levels a route may be asked for from `EFFORT_LEVELS` ∩ the
  accepted levels of the provider's `effort_maps` entry, for every model on the grid. Without a map
  the question keeps `low`/`medium`/`high`; `max` is never asked for on a route whose declaration
  does not carry it. The payload question, the `openai_compatible` prompt and schema, and the
  Console's probability-key and echoed-criteria checks all derive from that one set. The answer is
  still validated as a level and then resolved against the declaration — omitted, never clamped —
  with the asked level in `effort_requested` and the applied one in `effort`.
  `max_effort` in a platform policy now accepts `max`, and it is still the operator's ceiling on what
  runs.
- **The previous turn reaches the decider.** `state` carries `previous_turn`
  (`{tool_calls: n, tools: [≤8 distinct names]}`) and `last_tool_results_excerpt` (tail ≤ 1.500
  chars of the previous assistant turn's tool output, after `_clean` and `redact`). The decision is
  taken once per turn, when the current turn has made no tool calls yet, so the previous turn is the
  signal that exists; assistant reasoning remains outside the payload. Tool names are capped at 64
  characters and the excerpt is taken from a bounded tail window, so the payload stays under 20K
  characters with a full context and the built-in rubric.
- **Opt-in `commandcode` generation route.** A new OpenAI-compatible provider (`commandcode`, aliases
  `commandcode-chat` / `commandcode_chat`) carries its own one-model catalog, `COMMANDCODE_GRID`:
  `deepseek/deepseek-v4.1-flash`. The namespaced id was confirmed from the provider's public,
  unauthenticated `/models` endpoint (`https://api.commandcode.ai/provider/v1/models`); the bare
  OpenCode id of the same model is a different contract and is rejected on this route. The cap is
  applied by the existing `route_allowlist` / `route_catalog` mechanism and is opt-in under
  `decider: jev_console`, like the Go route; legacy deciders keep the operator's grid.
- **Opt-in `openai-codex` (ChatGPT OAuth) generation route.** A new provider (`openai-codex`, aliases
  `codex` / `openai_codex`) with its own catalog, `CODEX_GRID`: `gpt-6-luna`, `gpt-6-luna-900k`,
  `gpt-6.1-sol`. It is the first route whose payload is a **Responses API** body
  (`api_mode="codex_responses"`), so `ROUTED_API_MODES` now carries that mode and the router grew the two
  functions that shape implies: `conversation_messages()` reads the conversation from `input` when there
  is no `messages`, and `_apply()` writes the level as **`reasoning.effort`** inside the `reasoning`
  object the transport built — a top-level `reasoning_effort` is not a Responses field at all, so the SDK
  call would raise `TypeError: unexpected keyword argument` before a byte was sent. Only `effort` is
  rewritten; `summary`/`include` stay the transport's, and a route the transport judged as taking no
  reasoning field gets no object invented (the audit then reports `effort: null` and keeps
  `effort_requested`, because a null effort is a statement about the wire). The cap is the same opt-in
  mechanism as Go and CommandCode: `route_allowlist` / `route_catalog` under `decider: jev_console`, and
  `_GRID_ALIASES` gives both aliases the same routability. Declared vocabularies are the core's:
  `CODEX_GPT56_EFFORTS` (`none`/`low`/`medium`/`high`/`xhigh`/`max`) for the Luna ids and
  `CODEX_ASTRA_EFFORTS` (`low`/`medium`/`high`/`xhigh`/`max`, no `none`) for the Sol ids; a `none` request
  on Sol is omitted rather than sent, because that route rejects a disable with a 400. `gpt-6.1-sol-900k`
  is deliberately absent from that catalog: the ChatGPT backend answers HTTP 400 "model entitlement"
  for it and the core does not treat `gpt-6.1-sol` as an eligible `-900k` base, and an option the
  endpoint rejects is a dead turn — the exact bug a catalog exists to prevent. An operator grid naming
  it is capped away by `CODEX_ALLOWLIST`, and a decision that names it anyway is refused with
  `model_not_in_provider_catalog`, leaving the request untouched.
- **`wire_model` on route audit records.** Every `event: route` record now carries `wire_model` beside
  `model`: `model` is the id the decision chose (on the Codex route it may be a `-900k` picker alias)
  and `wire_model` is the id that actually went into the provider's `model` field. They differ only for
  those aliases, so a record for `gpt-6-luna-900k` can no longer be read as a 900k-capable route.
- **`Entry.wire_id`.** The grid can now record the id the provider actually serves when it differs from
  the picker id. The Codex `-900k` variants are Hermes-side aliases whose suffix the transport strips
  while building the body, so the router must write the base slug back rather than the alias. The lookup
  is answered by the route catalog (`router.wire_model_for`) — an operator `grids.<provider>` entry
  carries a description only, and trusting it was a live-caught bug: a grid pinned to `gpt-6-luna-900k`
  sent the alias, the backend answered HTTP 400 "model entitlement", and Hermes fell back to another
  model after the audit had already recorded the route.
- **Provider alias normalisation in `is_provider_routable`.** One table (`_GRID_ALIASES`) now drives
  both the grid lookups and the routable check, so a canonical name and any alias of the same route
  match in both directions. This also fixes a pre-existing asymmetry: a declared `opencode-go` never
  matched the `opencode_go` the host reports, so the route was skipped silently.

### Fixed

- **An effort level the question never offered no longer reaches the wire.** `max` became a
  vocabulary level, and `resolve_effort` still consults the *built-in* policy of a route with no
  `effort_maps` entry — where `opencode-go`, `commandcode` and `codex` do declare `max`. A well-formed
  but unoffered answer (`max` on a route the question asked with `low`/`medium`/`high`) therefore
  went out as `reasoning_effort: max`: the question offered three rungs and the turn ran the fourth.
  The applied level is now gated on the offered set in all three deciders, and on the memo replay —
  the map may have been narrowed after the decision was written. An unoffered level is recorded in
  `effort_requested` and omitted from the wire (`effort: null`), which is the rule the declared-map
  path already followed: omitted, never clamped, and never a reason to lose the model decision.
- **`default_effort: max` is refused.** The knob is the level a degraded turn falls back to, and it
  was validated against `EFFORT_LEVELS` — which now includes `max`, so a hand-written config could
  make the decider's failure the most expensive turn. It is validated against
  `DEFAULT_OFFERED_LEVELS` instead, matching the `choices` the plugin manifest advertises.
- **A failed turn still remembers its failure when the opt-in SystemOne fallback is enabled.** The
  negative memo's provenance did not include the CommandCode trio, so on a deployment with the
  fallback configured the marker looked foreign to the very configuration that wrote it, was
  dropped, and the decider was asked again on every provider request of the turn — exactly the cost
  the marker removed. `failure_marker` now carries the trio and `_remember_failure` fills it from
  the active settings.
- **`max_effort: max` is a ceiling, not a target.** With `max` now a legal cap, the no-effort-answer
  path (which uses the cap as its level, the existing contract) would have run the most expensive
  level on the turn the decider failed to classify. An explicit cap of `max` now falls back to the
  configured `default_effort`; every other cap value keeps the existing behaviour.
- **The turn-signal excerpt also scrubs the plugin's own CommandCode key.** `config.redact` covered
  the OpenRouter, Go and Console variables but not `COMMANDCODE_API_KEY`, which the excerpt would
  have forwarded verbatim to the decider. Other credential formats remain the operator's call; the
  excerpt passes through the same `redact` the audit uses.

### Changed

- **`effort_maps` now mean one thing, on every route.** The map path and the strict built-in routes
  (`opencode-console`, `opencode-go`, `commandcode`, `openai-codex`) run a single algorithm,
  `effort.resolve_declared_effort`: a requested level the declaration accepts goes out verbatim;
  otherwise a *declared* override whose target is itself accepted is used; otherwise the field is
  omitted. The custom path's nearest-weaker clamp, its `LADDER` position comparison and its
  anti-escalation rule for overrides are gone — an override is honoured because it is written down,
  which is what makes `xhigh → max` work on a map exactly as it did on a built-in policy. `"none"` is
  an ordinary level when declared (`accepted: ["none"]` is a valid contract), while Python `None`, an
  empty request and a YAML `null` mean "omit". A matched model entry **replaces** the built-in policy
  (levels, overrides and the omission decision) instead of merging with it; a provider map that does
  not mention a model leaves the built-in fallback in place, and an entry that matches with an empty
  spec omits rather than revealing the built-in policy. `ModelReasoningPolicy.overrides` now carries
  the effective declaration and `wire_parameter` stays metadata (`reasoning` for Codex and its
  aliases, `reasoning_effort` for the other custom contracts). Map keys now resolve deterministically:
  full id exact, then a namespace-free key equal to the slug, then the longest true `-`-delimited
  prefix (never a substring), with the provider looked up by exact name and then by the canonical
  name of a registered alias. `ollama-cloud` **without** a map entry keeps its legacy family clamp
  unchanged. **This is a deliberate behaviour break for maps that relied on the clamp:** a level that
  used to be clamped to the nearest weaker accepted level is now omitted. Migration: declare the
  override (`overrides: {xhigh: max}`) or accept the omission; a map that wants the built-in
  vocabulary must now repeat it.
- **`effort_requested` keeps what Jev asked for; `effort` keeps what ran.** Every decider used to
  overwrite `effort_requested` with the value produced by the *cap* (`max_effort`), so a capped turn
  was audited as if the stronger level had been applied. The requested level now stays in
  `effort_requested` and the one actually handed to the wire in `effort`, on fresh decisions and on
  replays: the replay path keeps the stored request and re-applies the cap to the wire value.
  `recent_context`'s cap drops from 12.000 to 11.000 characters so a full payload (context, the
  four-step rubric and the turn-signal fields) stays under 20K with the built-in rubric.
- **A failed turn remembers the failure instead of re-asking in its own tool loop.** With
  `route_per_turn: true` a decision is memoised on the turn, but a *failure* was not: `low_confidence`
  and `timeout` returned `None` without storing anything, so every provider request of that turn's
  tool loop asked the decider again — the same state, the same question, the same unusable answer
  (the live audit showed ~9 calls per failing turn). `router._decide` now stores a **negative memo**
  (`memo.failure_marker`) carrying the reason plus the provenance the replay guards compare, and the
  replay path records a skip with `replayed: true` without calling the decider. Only `low_confidence`
  and `timeout` are memoised: a 500, a malformed body or a missing key is worth retrying or is a
  configuration fact the guards already handle. The marker is per turn only — `route_per_turn: false`
  keeps routing once per session, and suppressing a session for 24h because one request timed out
  would be a much larger change than the waste it fixes. The remembered reason survives the replay,
  so a timeout is not relabelled `low_confidence` by the zeroed marker confidences. The marker is also
  scoped to the provenance that recorded it: when the next request of the turn belongs to a different
  provider, session, decider or grid, the marker is dropped and that request decides normally — a
  marker holds no decision to lose, and failing the request open would let one provider's timeout cost
  another provider's turn. A *decision* from another provenance still fails open, as before.
- **A one-model grid no longer asks the model question.** A Choice over a single option has one
  possible answer and a confidence of 1.0 by construction, so the question only cost tokens and
  latency. `state.build_questions` omits it when `len(grid) == 1` (explicit `ask_model=True` keeps the
  legacy OpenRouter payload byte-identical), `jev_console` does not send it, and `openai_compatible`
  drops the model block, its rule and the `model` JSON field from the prompt. The decider then fixes
  `model=grid[0]`, `model_confidence=1.0` and `model_choice="single"` in the audit, and ignores a
  model answer the endpoint volunteers for a question that was not asked. Multi-model grids are
  unchanged, including the strict validation of the model answer.
- **`opencode-go` now applies the decided level as `reasoning_effort`.** The built-in policy for
  `deepseek-v4.1-flash` on that route stops claiming "no confirmed contract": a probe of
  `POST https://opencode.ai/zen/go/v1/chat/completions` (mandatory `x-opencode-session` header) on
  2026-10-20 returned HTTP 200 for `reasoning_effort` at `low`, `medium`, `high`, `xhigh` and `max`,
  with `reasoning_content` and `usage.completion_tokens_details.reasoning_tokens` in the response,
  while `thinking=high` returned HTTP 400 (`thinking must be an object`). The declared vocabulary is
  the one the Hermes core already drives on this same wire for `deepseek-v4*` ids —
  `DEEPSEEK_V4_EFFORTS = ("low","medium","high","max")` with `DEEPSEEK_V4_OVERRIDES = {"xhigh":"max"}`
  — so the policy declares `wire_parameter="reasoning_effort"`,
  `accepted_levels=("low","medium","high","max")`, `overrides={"xhigh":"max"}` and `omitted=False`.
  `resolve_effort` passes a declared level verbatim, honours the declared override, and omits anything
  else (`minimal`, `none`, an unknown string) instead of clamping it, because the family fallback would
  have answered a level this route never declared. Wire `effort` is no longer `null` on this route;
  `effort_requested` keeps recording the Jev choice. `opencode-console` keeps omitting the plugin's own
  field (the `commandcode` route did too until the entry below), and an operator `effort_maps` entry still
  takes precedence. What the probe proves is acceptance (HTTP 200 — even for an out-of-vocabulary level, so
  the route does not validate the value), not calibrated depth: the audit claim stays at "the field was
  sent".
- **Every generation catalog now names a single model.** `OPENCODE_GO_MODELS` is
  `("deepseek-v4.1-flash",)`; `mimo-v2.6-flash` was removed from all grids, allowlists and catalogs.
  With one model per route the model question is degenerate by construction, and it is no longer
  sent at all (see the single-model entry above); the decider effectively chooses only the reasoning
  level.
- **`commandcode` now applies the decided level as `reasoning_effort`, beside the profile's `thinking`
  toggle.** The route's reasoning contract has two components with two authors: the provider profile keeps
  emitting `extra_body.thinking` (CommandCode delegates `deepseek/*` ids to the DeepSeek profile, whose
  `thinking_toggle_extras(..., always_emit_toggle=True)` derives it from `reasoning_config`), while the
  plugin now authors the **top-level** `reasoning_effort` with the level Jev decided. The Hermes core builds
  `api_kwargs` from the profile before the `llm_request` middleware runs and uses the middleware payload for
  the call (`agent/turn_api_request.py::build_api_request`), and the routed payload is built from the
  pre-middleware `original_request` and rewrites only `model` and `reasoning_effort` — so the plugin's level
  lands after the profile's derivation, overrides the level the profile would have sent, and leaves the
  toggle alone. The wire therefore carries `thinking: {"type": "enabled"}` **plus** `reasoning_effort: <level
  chosen by Jev>`. A probe of `POST https://api.commandcode.ai/provider/v1/chat/completions` on 2026-10-20
  returned HTTP 200 for `reasoning_effort` at `low`, `medium`, `high`, `xhigh` and `max`, so the policy
  declares `wire_parameter="reasoning_effort"`, `accepted_levels=("low","medium","high","max")`,
  `overrides={"xhigh":"max"}` and `omitted=False`, mirroring `DEEPSEEK_V4_EFFORTS`/`DEEPSEEK_V4_OVERRIDES`.
  Anything the policy does not declare is omitted, never clamped or invented. The probe proves the field is
  accepted, not that the endpoint grades the level. `opencode-console` still omits the plugin's own field,
  and its OpenCode policy is not reused here.

### Fixed

- **A YAML `null` level no longer becomes the level `"none"`.** `config._freeze_effort_spec` built
  every declaration with `str(x).strip().lower()`, so `None` became `"none"` and a null entry looked
  like a declared disable — the same string the Luna policy legitimately sends. Parsing and
  resolution now share one normaliser (`effort.normalise_level`): only non-empty strings are levels,
  and nulls, numbers and booleans are dropped from `accepted`, from override keys and targets, and
  from provider/model keys. A spec that matched the model but declares nothing is still a matched
  entry — it omits instead of falling back to the built-in policy. The structures stay immutable.
- **The plugin is a virtual uv workspace member, so it installs in more than one profile.** With a
  `[build-system]` block, `pm/workspace.py` kept the declared project name
  (`hermes-plugin-jev-effort-router`) for every profile copy. Two profiles enabling the same plugin
  then produced two workspace members with one name and `uv lock` refused the environment:
  `Two workspace members are both named hermes-plugin-jev-effort-router`. The package builds no
  modules (`py-modules = []`), so the build backend was removed: the member is now metadata-only and
  uv keys it by path (`hermes-plugin-jev-effort-router-<hash>`), which is the layout the plugin
  manager documents for virtual members. Dependencies are unchanged.
- **A profile entry for a route that already has a built-in policy no longer hides its contract.**
  `resolve_effort` resolves a provider that appears in `effort_maps` through the custom path, which
  takes precedence over the built-in policy and clamps to the nearest weaker accepted level. A
  `commandcode` entry declaring `accepted: [low, medium, high, max]` therefore answered `xhigh` with
  `high` (the declared `xhigh`→`max` override was rejected as an escalation) and `ultra` with `max`
  instead of omitting it, so the delivered configuration contradicted the policy it was meant to
  enable. The reference profile now declares only `routable_providers` and `grids` for that route — the
  built-in policy is its single authority — while operator maps of other routes keep their existing
  clamp semantics. Regression: `test_the_delivered_code_profile_config_holds_the_commandcode_contract`
  loads the profile settings through `load_settings` and asserts `low`/`medium`/`high`/`max` verbatim,
  `xhigh`→`max`, omit for `minimal`/`none`/`bogus`/`ultra`/empty, and the untouched `opencode-go`
  semantics; an opt-in companion test reads a real profile file via `JEV_PROFILE_CONFIG`.
- **Route audit records name their `provider`.** A `route` record carried the model, the effort and the
  decider but not the route the decision was applied to, while a `skip` record always named it. With
  `opencode-go` and `commandcode` both sending `reasoning_effort` and both serving
  `deepseek/deepseek-v4.1-flash` (under different ids), a route record could no longer be attributed to a
  route by inspection. The record now carries `provider` at the top level, the same key skip records use.
- **The audit record names the decider model that actually answered.** `router.py` stamped every route
  with `settings.jev_model`, the legacy OpenRouter id (`typesafe/jev-1.13`), so a `decider: jev_console`
  turn — which is answered by `console_jev_model` (`jev-1.13-free`) — was recorded as a model that was
  never consulted. The record now carries the decision's own `decider_model`, and the new
  `decider_fallback_reason` names why the Console fallback (`console_jev_fallback_model`) answered when
  the primary failed over. Provenance survives a memo replay: it is stored with the memo instead of being
  re-derived from the (unconsulted) active configuration.

## [0.4.0] - 2026-10-15

### Added

- **Pluggable Decider Backends:** Introduces pluggable decider architectures supported by `decider.py`:
  - `jev` (**default**): Preserves legacy TypeSafe Jev via OpenRouter Decisions API (`https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`).
  - `openai_compatible`: Generic OpenAI-compatible chat completions endpoint (e.g. OpenCode `https://opencode.ai/zen/go/v1` with `deepseek-v4.1-flash`), with strict JSON output parsing and multi-candidate extraction.
- **Selectable Routable Providers (`routable_providers`):** Replaces hardcoded single-provider gate with a configurable list (default `["ollama-cloud"]`).
- **Per-Provider Grids and Effort Maps:** Optional `grids` and `effort_maps` configuration allowing distinct model grids and reasoning effort translations for each routable provider.
- **Per-Decider Confidence Thresholds:** `confidence_threshold_per_decider` mapping allowing fine-tuning thresholds per backend.
- **Fail-open Effort Guarantees:** Unknown or unmapped providers omit `reasoning_effort` on the wire rather than guessing or escalating.
- **Audit Records:** Enhanced with `decider` and `latency_ms` fields for every decision.

## [0.3.0] - 2026-10-11

### Added

- **Per-surface routing policy (`platform_overrides`).** An operator can declare standing routing policies
  for individual surfaces (e.g. `cron`, `telegram`, `cli`) without affecting any other surface.
  Configured in `plugin.yaml` under `platform_overrides`, each platform entry accepts overrides in
  precedence order: `enabled`, `grid`, `confidence_threshold`, and `max_effort`.
  - `enabled: false`: Leaves requests on that surface untouched, recording a skip entry naming the surface (`surface_disabled`).
  - `grid`: Restricts routing and fallback to a subset of the global grid as a structural cap; Jev cannot choose off-grid models, and low-confidence fallback resolves within the surface grid.
  - `confidence_threshold`: Per-surface decision threshold.
  - `max_effort`: Clamps the reasoning effort level emitted by the router (`low`, `medium`, `high`) before per-family effort translation, applied whether chosen by Jev or from `default_effort`.
  - Absent, `null`, unlisted platforms, or unparseable entries reproduce global 0.2.2 behavior byte for byte.

## [0.2.2] - 2026-09-23

### Fixed

- **Replayed audit records now re-emit the two measured confidences instead of a collapsed copy.**
  The per-turn memo stored a single `confidence = max(model_confidence, effort_confidence)` and the
  replay path recopied that value into *both* confidence fields of every follow-up record in the turn.
  Verified in production: a fresh `0.34 / 0.65` decision re-reported as `0.65 / 0.65` on every replay,
  so the effort confidence was disguised as a model confidence Jev never gave. On a tool-heavy turn
  (≈1 fresh record for ≈11 replays) the tail was dominated by that invented pair, so anyone tuning the
  threshold off the tail calibrated against a number that was not the decision. The memo now keeps both
  confidences, and the replay re-emits that pair. This is an audit/observability fix — routing and
  replay behaviour are unchanged, and the `grid_coverage` report already filtered replayed records.

- **Replayed records report unmeasured fields as `null`, never as a copied value.** A replay only
  persisted the model, effort and confidences, so its record used to carry `effort_choice: ""`,
  `model_probabilities: {}`, `effort_probabilities: {}` and `alternatives: []`. Any aggregate over the
  raw tail silently read those as "this decision measured nothing". They are now `null` (and the confidences
  above are no longer the collapsed max), because an audit record must record what was measured and say
  `null` — omit — for what was not. Collapsing the confidences to a max also destroyed *which* of the two
  answers was weak; the pair is kept so a consumer can tell a model-side from an effort-side fallback.

## [0.2.1] - 2026-09-23

### Changed

- **The six grid profiles were rewritten to name task families, and the two questions are now
  English. This is a change of payload and therefore of behaviour.** `glm-5.3` and `glm-5.3-flash`
  were almost never chosen. The cause was the criterion text, not the models: `glm-5.3-flash` was
  described as *"fast and economical, excellent in real use for everyday tasks"* and `glm-5.3` as
  *"deep scientific and logical reasoning, expensive, for tasks with high analytical demands"*,
  while `deepseek-v4.1-flash` was *"generalist, excellent value for money, 1M context, the default
  choice"*. A superlative with no task attached reads as a safe pick on **every** prompt, and
  "*excellent in real use for everyday tasks*" describes nearly every request a user makes — so
  `glm-5.3-flash` competed with the generalist for ordinary work and lost, while `glm-5.3` was
  reserved for "high analytical demands" and never fired on an ordinary analysis.
  Every profile now names the task family it is for (`glm-5.3-flash` → *"best reasoning-per-cost on
  large text: drafting, summarising, translating and structured extraction over long documents"*,
  `glm-5.3` → *"strongest at rigorous reasoning: mathematics, logic, science, quantitative and
  financial analysis, where a wrong answer is costly"*), the model instructions ask Jev to compare
  every option on the same axes (task, cost, speed) instead of leaving cost adjectives to break ties,
  and the two question instructions and the three effort criteria move from French to English so the
  whole payload is stated in one language. Ids, order and the six-entry count are untouched;
  `tests/test_grid.py::test_no_profile_is_a_task_free_superlative` now fails a profile that praises
  a model without naming a task.

- Measured against the live endpoint, 5 calls × 8 task families = 40 calls per variant (the decision
  is not deterministic, so one call proves nothing). Applied model = the choice after the 0.5
  confidence threshold and the configured fallback, i.e. what actually serves the turn.

  | Task family | Before: chosen → applied | After: chosen → applied |
  |---|---|---|
  | trivial (`2+2`-class) | `glm-5.3-flash` 0.40 → **fallback** (0/5 confident) | `nemotron-3-nano:30b` 0.92 → `nemotron-3-nano:30b` (5/5) |
  | everyday ERP entry | `minimax-m3` 0.69 → `minimax-m3` | `minimax-m3` 0.62 → `minimax-m3` |
  | code refactor | `kimi-k3` 0.64 → `kimi-k3` | `kimi-k3` 0.64 → `kimi-k3` |
  | hard debugging | `deepseek-v4.1-flash` 0.42 → **fallback** (0/5) | `kimi-k3` 0.39 → **fallback** (0/5) |
  | demanding analysis | `glm-5.3` **1/5**, else `deepseek-v4.1-flash`, 0.40 → **fallback** | `glm-5.3` **5/5**, 0.95 → `glm-5.3` |
  | long-text extraction | `deepseek-v4.1-flash` 0.90 → `deepseek-v4.1-flash` | `glm-5.3-flash` **5/5**, 0.98 → `glm-5.3-flash` |
  | sequential tool calling | `minimax-m3` 0.90 → `minimax-m3` | `minimax-m3` 0.96 → `minimax-m3` |
  | web/design copy | `deepseek-v4.1-flash` 0.91 → `deepseek-v4.1-flash` | `deepseek-v4.1-flash` 0.61 → `deepseek-v4.1-flash` |

  Aggregate over the 40 calls: `glm-5.3` 5 → 5 and `glm-5.3-flash` 5 → 5 (each was 1 or 0 before, on
  a distribution that scattered across four models with mean confidence ~0.40 and 0/5 above the
  threshold on the two tasks they now win 5/5); mean model confidence 0.658 → 0.749; calls above the
  0.5 threshold 25/40 → 30/40. The generalist is untouched where it should be — it still takes
  ordinary writing and web copy — and two previously degraded-and-scattered tasks now route
  confidently. Median decision latency 311 ms in both variants.

- The English-instruction change alone was measured separately first (40 calls): it moved
  `analysis` from `glm-5.3` 1/5 to `deepseek-v4.1-flash` 5/5 at 0.50 — i.e. it removed the only GLM
  signal without adding another. It is the grid rewrite that produces the effect; the instructions
  change rides along for consistency of payload language and is not claimed as a gain.

- A control run with two differently-worded variants of the first profile (with/without "the usual
  choice", and "cheap for its size" vs "cheapest of the full-size models") confirmed the effect does
  not depend on that phrase: 40 calls each, identical distributions on 7 of 8 task families, and the
  design prompt at 0.61/0.63 (applied, routed) versus 0.45/0.47 (below threshold, degraded to the
  fallback). The phrase is kept for the generalist, deliberately, so a plain writing turn does not
  fall through to the fallback.

- Evidence quality: 120 live calls across five variants. This measures the mechanism and the
  before/after route distribution; it is not a quality benchmark of the models themselves, and the
  0.95+ confidences on the two GLM tasks are high enough to re-check after any future edit to the
  grid or the instructions.

### Added

- `jev_effort_router_status` now reports `grid_coverage` over the last 200 routed turns: what each
  model was actually **applied** to, which grid entries are **never_chosen**, and which ones Jev picks
  but the confidence threshold then throws away (`below_threshold`). This is the finding that was
  invisible before: a model can be right for the job, present in the provider's catalog and cheap,
  and still never serve a turn — and no single turn shows it. Only first-call records are counted, so
  a turn that replays one decision across a long tool loop is not weighted as if it had decided many
  times. Applied against the real audit trail of this profile, the report names `glm-5.3` under
  `never_chosen` and `glm-5.3-flash` under `below_threshold` — exactly the symptom.

- The same block is rendered on the **text** surfaces (`hermes jev-effort-router status` and the
  `/jev-effort-router status` slash command), not only in the agent tool's JSON: the shell is where an
  operator reads routing state, and a finding that only exists in the tool would stay invisible there.

### Fixed

- The **audit trail restarts** after the `jev-router` → `jev-effort-router` rename: the plugin data
  directory is namespaced by the plugin id *and* a content hash, so the new id opened a new empty
  `routes.jsonl` next to the 880-record history of the old one. `grid_coverage` therefore reports a
  window of a handful of turns until the new trail fills. The old directory is left in place and is
  the source of truth for the pre-rename history.

## [0.2.0] - 2026-09-23

### Changed

- **Renamed: `jev-router` → `jev-effort-router`** (plugin id, repository, CLI command, slash command,
  toolset, tools, Python package and pyproject distribution name).

  The old name was both **taken and less accurate**. On GitHub, `jev-router` is a 340-star project
  (`gargpratyush/jev-router`) that also picks the cheapest model, created six days before this one, and
  `jev-router in:name` returns 155 repositories — searching the old name finds someone else. More
  importantly, "router" alone under-sells what this plugin does: it decides the **model** *and* the
  **reasoning effort**, per turn. Measured across the 241-entry catalog, no other entry does that —
  every other routing entry picks a model and stops, and the only two entries that touch reasoning effort
  (`reasoning-switch`, `compact-reasoning-label`) require you to move the level by hand. The name now
  leads with the capability that has no competitor.

  Renaming the id changes what a user installs, so this is a breaking change for anyone who installed
  `jev-router` directly: remove it and install the new name. There were no catalog or released users at
  the time of the rename.

| Old | New |
|---|---|
| `hermes plugins install …/jev-router` | `hermes plugins install …/jev-effort-router` |
| `/jev-router status` | `/jev-effort-router status` |
| `hermes jev-router status` | `hermes jev-effort-router status` |
| `jev_router_status` / `jev_router_route` | `jev_effort_router_status` / `jev_effort_router_route` |
| `plugins.entries.jev-router.settings.*` | `plugins.entries.jev-effort-router.settings.*` |
| `hermes-plugin-jev-router` (pip) | `hermes-plugin-jev-effort-router` |

  The README also now leads with the model **and effort** combination instead of opening on the
  Zürich→London 7.1-second benchmark, which sold speed rather than the capability this plugin is alone in
  offering.

## [0.1.1] - 2026-09-23

### Changed

- **The grid profiles are now English, which is a change of payload and therefore of behaviour.**
  The six criterion strings in `DEFAULT_GRID` are sent to Jev verbatim and are what it weighs; they
  were French, they are now English (`grid.py`, mirrored in `README.md`, `docs/routing-grid.md`,
  `docs/jev-decisions-api.md` and `tests/test_grid.py`). The ids, the order and the six-entry count are
  untouched, and the docstrings now say the criteria are sent in English and that rewording them is a
  behaviour change rather than an editorial one.
- Measured against the live endpoint, 4 calls per task before and 4 after (16 calls per variant, the
  decision is not deterministic so one call proves nothing):

  | Task | Chosen model, French → English | Mean confidence | Calls at/above the 0.5 threshold |
  |---|---|---|---|
  | trivial (`2+2`) | `glm-5.3-flash` → `glm-5.3-flash` | 0.477 → 0.350 | 1/4 → 0/4 |
  | code refactor | `kimi-k3` → `kimi-k3` | 0.520 → 0.550 | 3/4 → 4/4 |
  | demanding analysis | `glm-5.3` → `glm-5.3` | 0.893 → 0.893 | 4/4 → 4/4 |
  | sequential tool calling | `minimax-m3` → `minimax-m3` | 0.962 → 0.980 | 4/4 → 4/4 |

  All 16 choices are identical between the two variants: discrimination between the four task
  categories is unchanged. The only material difference is confidence on the trivial task, which drops
  from ~0.48 to ~0.35 — further below the 0.5 threshold than it already was. That task was already
  degraded in 3 of 4 French calls, so the applied model (the configured default, option 1
  `deepseek-v4.1-flash`) is unchanged in practice; but the English wording makes the tie between
  `glm-5.3-flash` and `nemotron-3-nano:30b` for a trivial prompt slightly harder to break (0.46/0.36
  versus 0.56/0.31). One task × 4 calls is thin evidence: this measures the mechanism and one
  category-level regression in confidence, not a general quality verdict.

### Fixed

- `jev_router_status` and `jev_router_route` now accept the arguments dict the host's tool registry
  passes positionally (`handler(args, **context)`). Both were declared `handler(recent=5)` /
  `handler(task="", context="")`, so the arguments dict was bound to the first parameter and every
  call that carried a parameter failed with
  `TypeError: int() argument must be a string, a bytes-like object or a real number, not 'dict'`.
  Reproduced through `tools/registry.py::dispatch`: the empty-arguments call worked, which is how it
  hid. Covered by tests that dispatch through a host-shaped call.
- **`provides_middleware:` is declared again — a deliberate reversal of the previous entry.** It had
  been removed because the manifest schema has no such field and the host warned about it on every
  load. That trade was wrong: `hermes plugins validate` diffs the manifest against what
  `register(ctx)` wires and fails the plugin with `undeclared middleware registered (not in
  provides_middleware): llm_request`. That check *is* the plugin-catalog admission gate
  (`plugin-catalog-ci.yml` runs `hermes plugins validate --install-deps` at the pinned sha), so
  omitting the key silently disqualified the plugin from the catalog while looking like harmless
  cleanup — the warning it "fixed" was cosmetic and the failure it caused was not. Reproduced and
  confirmed on Hermes 0.21.4; with the key restored, `hermes plugins validate` reports
  `declared middleware :: matches registrations` and the overall verdict is `ok: true`. The one
  load-time warning is accepted as the price of admission. `test_manifest_fields_are_known_to_the_host`
  now asserts exactly `["provides_middleware"]` as the single tolerated unknown key instead of
  demanding an empty set, and the README/manifest comments and `docs/integration-surface.md` state
  the trade-off rather than denying the field exists.
- Documentation states the plugin's scope explicitly: it is for Hermes running on **Ollama:Cloud**, and
  no other provider is routed (README, manifest description, SPEC non-goals). The README's grid table
  now gives the profiles as they are actually sent to Jev, plus an English gloss beside each.
- The grid entry `nemotron-3-nano` is now `nemotron-3-nano:30b`, the id the provider's own catalog
  uses. The bare name produced `HTTP 404: model "nemotron-3-nano" not found` and failed the whole
  turn — the one outcome the fail-open design exists to prevent.
- New `catalog.py` cross-checks a decision against the provider's cached model list (read from the
  host's `ollama_cloud_models_cache.json`, no credential and no socket) before the chosen model is
  put on the wire. A model the catalog proves absent is refused: the turn keeps the configured model
  and the refusal is recorded as `model_not_in_provider_catalog` with the model Jev picked. No
  catalog evidence (absent/unreadable/unexpected) means "no verdict" and routing proceeds as before.
  `jev_effort_router_status` reports offending grid entries under `grid_unavailable`.

## [0.1.0] - 2026-09-22

First release.

### Added

- `llm_request` middleware that asks TypeSafe Jev (`typesafe/jev-1.13` over OpenRouter's Decisions API) for
  a model and a reasoning-effort level on the first provider request of every user turn, and rewrites the
  outgoing provider kwargs accordingly.
- Six-model routing grid, keyed by position, with the criteria sent to Jev as `{"1": "<model>: <profile>"}`.
- Per-family reasoning-effort translation table: never escalates, never invents a level, omits the field
  rather than risk a 400. Kimi K3's `medium` maps to `high` per its documented vocabulary.
- Confidence guard (default `0.5`): a distrusted answer degrades to the configured default model and
  effort, and the turn still goes out routed but flagged.
- One decision per user turn, memoised on `turn_id` so a turn's whole tool loop replays it without
  disturbing the prompt cache. `route_per_turn: false` routes once per session instead.
- Durable JSONL audit trail under `plugin-data/jev-router/routes.jsonl`: choice, probabilities,
  confidence, alternatives, latency, degradation reasons and turn/session identifiers. Skipped turns are
  recorded with their reason.
- Fail-open on every path: timeout, HTTP error, malformed answer, low confidence, off-grid model, unknown
  provider or API mode all leave the request byte-identical to a plugin-disabled run.
- Operator surfaces: `/jev-router` slash command, `hermes jev-router` CLI family (`status`, `route`,
  `grid`, `tail`, `reset`), and the `jev_effort_router_status` / `jev_effort_router_route` agent tools.
- 74 tests, driving the real middleware callback against a stub Decisions API with no network access.

[0.3.0]: https://github.com/AlphaPerseii3000/jev-effort-router/releases/tag/v0.3.0
[0.2.2]: https://github.com/AlphaPerseii3000/jev-effort-router/releases/tag/v0.2.2
[0.2.1]: https://github.com/AlphaPerseii3000/jev-effort-router/releases/tag/v0.2.1
[0.2.0]: https://github.com/AlphaPerseii3000/jev-effort-router/releases/tag/v0.2.0
[0.1.1]: https://github.com/AlphaPerseii3000/jev-router/releases/tag/v0.1.1
[0.1.0]: https://github.com/AlphaPerseii3000/jev-router/releases/tag/v0.1.0
