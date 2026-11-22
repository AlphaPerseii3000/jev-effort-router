---
id: SPEC-jev-effort-router
companions:
  - routing-grid.md
  - jev-decisions-api.md
  - integration-surface.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete,
> preservation-validated contract for what to build, test, and validate.

# Jev Router — decision-model routing for Hermes Agent

## Why

**An opportunity to capture.** TypeSafe's Jev (`typesafe/jev-1.13`, first of the "System One" models) makes a
typed, calibrated decision — choice, score, or yes/no probability — in ~270 ms p50 and $0.042/M input
tokens with free output. That is fast and cheap enough to run on *every* user turn, which turns model
selection from a static config value into a per-message routing decision. A Hermes operator running six
benchmarked Ollama:cloud models (routing-grid.md) currently has to guess one model and one reasoning-effort
level for the whole session, and re-guess by hand when the conversation changes shape from a one-line
question to a multi-file code change. Jev closes that gap at negligible cost, and keeps every gram of
reasoning and generation in the main model — it only steers.

## Capabilities

- **CAP-1** — An operator can install and enable the plugin in one command, and a fresh Hermes session
  routes every turn without any further configuration beyond an OpenRouter API key (or OpenCode Console API key when using `decider: jev_console`).
  - **success:** `hermes plugins enable jev-effort-router` followed by `hermes plugins list` shows the plugin
    enabled with its declared middleware/hook/tool surface; `hermes plugins doctor <repo>` exits 0.

- **CAP-1b** — Decider pluggability and route catalogs: The operator can configure `decider: jev_console` to use the SystemOne protocol (`POST https://opencode.ai/inference/systemone/v1/systemone`) authenticated via `OPENCODE_CONSOLE_API_KEY`. The Console decider decides; generation is served by the `opencode-go` provider, whose catalog is capped to the single authorized normal model (`deepseek-v4.1-flash`) while this decider is active, or by the opt-in `commandcode` provider, capped to its namespaced id (`deepseek/deepseek-v4.1-flash`). The `opencode-console` route itself is pinned to the one generation id its own catalog carries (`deepseek-v4.1-flash`). No generation catalog carries a second model.
  - **success:** When `decider: jev_console` is configured, Jev decisions are fetched via SystemOne wire format, supporting primary model (`jev-1.13-free`) and fallback model iteration (`jev-1.13`), failing open gracefully on errors, isolating memo replays across providers and deciders, and offering no model outside the active route's catalog. The native `anthropic` route is routable on the same terms: it has its own catalog (`ANTHROPIC_GRID`) and its own wire (`output_config.effort`), and routing it never moves a turn between it and the subscription route.

- **CAP-2** — On the first LLM request of each user turn, the effective provider kwargs are rewritten so
  that `model` is Jev's chosen model and `reasoning_effort` is Jev's chosen level, mapped onto that
  model's real wire vocabulary.
  - **success:** with a stub Jev endpoint returning `deepseek-v4.1-flash` + `high`, the captured provider
    kwargs for request 1 show exactly those values; with a stub returning `kimi-k3` + `low`, the captured
    kwargs show `kimi-k3` and the effort value Kimi's wire accepts.

- **CAP-3** — The routing decision covers the whole turn: follow-up requests inside the same tool loop
  keep the model and effort chosen at the first request.
  - **success:** with a stub that returns a different model on every call, all provider kwargs captured
    within one user turn carry the *first* answer; the next user turn picks up a new answer.

- **CAP-4** — When Jev is slow, unreachable, returns an error, or answers with confidence below the
  configured threshold, the turn proceeds unchanged on the statically configured model and effort, and
  the degradation is visible in the log.
  - **success:** with the Jev endpoint pointed at a closed port (and separately, at a stub returning
    `confidence: 0.1`), the captured provider kwargs are byte-identical to a run with the plugin disabled,
    the turn completes, and one WARNING/INFO line names the reason.

- **CAP-5** — Every decision is durably auditable: one JSONL record per route carrying the plugin/model
  versions, the chosen model and effort with their probabilities and confidence, the alternatives, the
  latency, the fallback reason, and the identifiers of the turn it applied to.
  - **success:** after three turns, the audit file holds exactly three records, each with a non-empty
    `choices`, a numeric `confidence`, and a `turn_id`/`session_id` matching those of the captured
    provider requests.

- **CAP-6** — An operator can see the routing state and the recent decisions from inside a Hermes session
  and from the shell, and can exercise the router end-to-end without running a full turn.
  - **success:** `/jev-effort-router status` and `hermes jev-effort-router status` both report enabled state, model,
  endpoint host, threshold, and the N most recent routes; `hermes jev-effort-router route "refactor the payment
  module"` prints Jev's model/effort answer plus latency and exits non-zero on a routing failure.

- **CAP-7** — Routing degrades to "do nothing" on every surface or configuration it does not own, instead
  of perturbing it.
  - **success:** for each of — provider not listed in `routable_providers` (by default non-`ollama-cloud`), a model absent from the option grid, and an
    auxiliary/compression/MoA call — the plugin leaves the payload untouched (no `model` and no
    `reasoning_effort` mutation) and appends a skip entry to the audit file naming the trigger.

## Constraints

- Ships as a standalone Hermes plugin in its own public GitHub repository. It installs through
  `hermes plugins install <repo>` and must never modify Hermes core: no edits to `run_agent.py`, `cli.py`,
  `gateway/run.py`, `hermes_cli/main.py`, or any in-tree file.
- Routing rides `llm_request` middleware (`hermes_cli/middleware.py`, `hermes.middleware.v1`). That kind
  exists in hermes-agent as installed here (0.21.4); a plugin written against it registers with a warning
  instead of failing on an older Hermes, so the fallback is a no-op, never a crash.
- The API key is read from the configured environment variable (`OPENROUTER_API_KEY` for Jev by default, or
  `decider_api_key_env` such as `OPENCODE_GO_API_KEY` for OpenAI-compatible deciders); it is never written to `config.yaml`, never logged, and
  never included in an audit record.
- Network failure, HTTP status, or malformed Jev/decider response must never raise into the turn. The middleware
  timeout is host-owned (`plugins.hook_callback_timeout`, default 30 s and fail-open), so the plugin
  enforces its own much shorter budget (default 2.0 s) and returns `None` on any error.
- The chosen model must exist in the active provider's catalog (if a catalog is available); a model id outside the
  configured grid is rejected and treated as low confidence.
- Model ids are sent exactly as the route's catalog declares them: there is no context-window variant to
  normalise. The route that declared one (`[1m]`, on the subscription route) was removed in 0.8.0, and the
  native `anthropic` route requests its 1M window with the `context-1m-2025-08-07` beta header instead of
  the id, so an unexpected `[1m]` there remains a `model_not_in_grid` skip.
- The option list sent to Jev stays short. A longer Choice list measurably dilutes decision quality, so
  the grid is the six benchmarked models and grows only on evidence.
- Routing interval: one route per user turn by default (`route_per_turn: true`), where the decision is taken
  at the first request of the turn and reused for its remainder. When `route_per_turn: false`, routing decisions
  are taken once per session.
- Effort levels are translated through a per-family mapping table, never sent as a generic parameter:
  a family's declared vocabulary decides, and a level it does not accept is omitted rather than guessed —
  unless the operator declared explicit `min`/`max` bounds for that model, in which case the level is
  clamped onto the bound so the declared ceiling always reaches the wire (see routing-grid.md). The
  audit keeps the asked level in `effort_requested` beside the applied `effort`, so the clamp is visible
  rather than inferred.
- An effort question is asked once, before the model is chosen. When any model in the grid declares
  bounds, the question offers the **union** of the models' levels (each answer is clamped per model);
  otherwise it offers only the levels every model accepts verbatim, so an answer is never offered and
  then dropped.
- Python standard library only in the hot path; the plugin declares `httpx` as a dependency rather than
  reaching for a vendored HTTP client.
- Repository is public with an open-source license (MIT) and a README that lets a stranger install,
  configure, and verify it.

## Non-goals

- **Provider-agnostic routing.** Ollama:Cloud is the default provider this plugin serves (`routable_providers: ["ollama-cloud"]`). In the v0.4.0 fork, operators can declare additional providers under `routable_providers`, supplying a custom `grids` and `effort_maps` entry for each. Unconfigured non-Ollama providers omit reasoning effort rather than guessing.
- Not an evaluation loop. Scoring the quality of the generated answer after the fact is a separate
  decision type and a separate feature.
- No model or reasoning routing of *auxiliary* calls (titling, compression, MoA, vision, approvals) —
  only the main turn's provider requests.
- No provider switching. The plugin chooses among models of the already-configured provider; it never
  moves a turn to a different provider or base URL.
- No cost/accounting optimisation, no learned or adaptive routing policy, no feedback from outcome into
  the grid. The grid is hand-maintained and validated by benchmarks, not by this plugin.
- Not a general "decision" toolkit — no API for third parties to ask Jev arbitrary questions through
  this plugin.
- No Hermes core contribution. If a capability is missing, it is requested upstream and the plugin
  degrades meanwhile; it is not patched into core from here.
- No UI beyond the status output, the audit file, and the Desktop settings form generated from
  `config_schema`.

## Success signal

A Hermes user with an OpenRouter key installs the plugin, and in a session that moves from a one-line
question to a code refactor to a long-context analysis, the audit file shows Jev choosing three different
models/effort levels while the configured model was never touched, no turn failed, and a kill-switch run
with the plugin disabled produced byte-identical requests to a healthy routed run whose Jev answers match
the configured model.

## Assumptions

- The host middleware contract is as installed in this profile (hermes-agent 0.21.4): `register_middleware("llm_request", cb)`,
  callback returns `{"request": {...}}`, receives `request`, `original_request`, plus
  `task_id`/`turn_id`/`request_id`-style context kwargs, and the host swallows callback exceptions.
- Jev is reached over OpenRouter's Decisions API (`POST https://openrouter.ai/api/alpha/decisions`,
  `typesafe/jev-1.13`), reachable with the operator's `OPENROUTER_API_KEY` and prepaid credits; no
  TypeSafe key is needed.
- The Jev request/response shapes behave as documented in `jev-decisions-api.md`; the exact wire
  contract was verified against OpenRouter's model page, tutorial, and Decisions API reference.
- The plugin keys effort translation off the provider profile name `ollama-cloud` by default; in this v0.4.0 fork, operators can declare custom `effort_maps` for each routable provider.
- Ollama:cloud is the default provider the routing grid was benchmarked on; other providers can be configured via `routable_providers`, `grids`, and `effort_maps`.

## Open Questions

- Should `reasoning_effort` for models the Ollama:cloud wire cannot express be *omitted* (server default)
  or clamped to the nearest weaker level? Resolved for **declared** routes: clamp only where the operator
  declared `min`/`max` bounds for the model (`effort.resolve_bounded_effort`), omit everywhere else — a
  level a declaration does not carry is never invented. The legacy `ollama-cloud` family table keeps its
  nearest-weaker clamp for compatibility.
- Is a per-turn route on *every* message the right default, or should the plugin expose
  `route_once_per_session` alongside it? Implemented via `route_per_turn`: defaults to `true` (per turn), and when set to `false` routes once per session.
- Should the grid's model ids be validated live against `GET https://ollama.com/v1/models` at startup,
  or is config-time validation enough? Current contract: config-time against the grid, with a skip entry
  in the audit file when the live catalog disagrees.
- Does the operator want the Desktop settings form seeded with the six-model grid, or an empty grid the
  operator fills in? Current contract: ship the grid as the default so the plugin works out of the box.
