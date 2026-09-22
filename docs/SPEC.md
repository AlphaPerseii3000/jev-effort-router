---
id: SPEC-jev-router
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
  routes every turn without any further configuration beyond an OpenRouter API key.
  - **success:** `hermes plugins enable jev-router` followed by `hermes plugins list` shows the plugin
    enabled with its declared middleware/hook/tool surface; `hermes plugins doctor <repo>` exits 0.

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
  - **success:** `/jev-router status` and `hermes jev-router status` both report enabled state, model,
  endpoint host, threshold, and the N most recent routes; `hermes jev-router route "refactor the payment
  module"` prints Jev's model/effort answer plus latency and exits non-zero on a routing failure.

- **CAP-7** — Routing degrades to "do nothing" on every surface or configuration it does not own, instead
  of perturbing it.
  - **success:** for each of — non-`ollama-cloud` provider, a model absent from the option grid, and an
    auxiliary/compression/MoA call — the plugin leaves the payload untouched (no `model` and no
    `reasoning_effort` mutation) and appends a skip entry to the audit file naming the trigger.

## Constraints

- Ships as a standalone Hermes plugin in its own public GitHub repository. It installs through
  `hermes plugins install <repo>` and must never modify Hermes core: no edits to `run_agent.py`, `cli.py`,
  `gateway/run.py`, `hermes_cli/main.py`, or any in-tree file.
- Routing rides `llm_request` middleware (`hermes_cli/middleware.py`, `hermes.middleware.v1`). That kind
  exists in hermes-agent as installed here (0.21.4); a plugin written against it registers with a warning
  instead of failing on an older Hermes, so the fallback is a no-op, never a crash.
- The API key is read from `OPENROUTER_API_KEY`; it is never written to `config.yaml`, never logged, and
  never included in an audit record.
- Network failure, HTTP status, or malformed Jev response must never raise into the turn. The middleware
  timeout is host-owned (`plugins.hook_callback_timeout`, default 30 s and fail-open), so the plugin
  enforces its own much shorter budget (default 2.0 s) and returns `None` on any error.
- The chosen model must exist in the Ollama:cloud catalog of the running profile; a model id outside the
  configured grid is rejected and treated as low confidence.
- The option list sent to Jev stays short. A longer Choice list measurably dilutes decision quality, so
  the grid is the six benchmarked models and grows only on evidence.
- One route per user turn: the decision is taken at the first request of the turn and reused for its
  remainder, so the system prompt and prompt cache are not disturbed mid-turn.
- Effort levels are translated through a per-family mapping table, never sent as a generic parameter:
  families that reject a level must be clamped or the field omitted (see routing-grid.md).
- Python standard library only in the hot path; the plugin declares `httpx` as a dependency rather than
  reaching for a vendored HTTP client.
- Repository is public with an open-source license (MIT) and a README that lets a stranger install,
  configure, and verify it.

## Non-goals

- **Not provider-agnostic.** Ollama:Cloud is the only provider this plugin serves: the grid is six
  Ollama:Cloud models, the effort table is written for that profile's vocabulary, and the middleware is
  gated on `provider: ollama-cloud`. Supporting another provider means a new grid, a new effort table and
  a review of the routing logic — a different feature, not a config change.
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
- The plugin keys effort translation off the provider profile name `ollama-cloud`; operators using a
  differently-named profile pointing at `ollama.com` are out of scope for the first release.
- Ollama:cloud is the only provider the routing grid was benchmarked on, so the plugin is a no-op
  elsewhere by design rather than by omission.

## Open Questions

- Should `reasoning_effort` for models the Ollama:cloud wire cannot express be *omitted* (server default)
  or clamped to the nearest weaker level? Current contract: clamp low/medium/high onto the family's
  accepted set, and omit rather than send a level the family would reject.
- Is a per-turn route on *every* message the right default, or should the plugin expose
  `route_once_per_session` alongside it? Current contract ships per-turn only; the toggle is deferred
  until the log shows it is wanted.
- Should the grid's model ids be validated live against `GET https://ollama.com/v1/models` at startup,
  or is config-time validation enough? Current contract: config-time against the grid, with a skip entry
  in the audit file when the live catalog disagrees.
- Does the operator want the Desktop settings form seeded with the six-model grid, or an empty grid the
  operator fills in? Current contract: ship the grid as the default so the plugin works out of the box.
