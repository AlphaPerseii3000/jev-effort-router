# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `jev_router_status` and `jev_router_route` now accept the arguments dict the host's tool registry
  passes positionally (`handler(args, **context)`). Both were declared `handler(recent=5)` /
  `handler(task="", context="")`, so the arguments dict was bound to the first parameter and every
  call that carried a parameter failed with
  `TypeError: int() argument must be a string, a bytes-like object or a real number, not 'dict'`.
  Reproduced through `tools/registry.py::dispatch`: the empty-arguments call worked, which is how it
  hid. Covered by tests that dispatch through a host-shaped call.
- `plugin.yaml` no longer declares `provides_middleware:` — the manifest schema has no such field, so
  it only produced an `unknown manifest field(s) ignored` warning on every load. The middleware is
  wired in code (`ctx.register_middleware("llm_request", ...)`); a regression test now asserts every
  manifest key is one the installed Hermes understands.
- The grid entry `nemotron-3-nano` is now `nemotron-3-nano:30b`, the id the provider's own catalog
  uses. The bare name produced `HTTP 404: model "nemotron-3-nano" not found` and failed the whole
  turn — the one outcome the fail-open design exists to prevent.
- New `catalog.py` cross-checks a decision against the provider's cached model list (read from the
  host's `ollama_cloud_models_cache.json`, no credential and no socket) before the chosen model is
  put on the wire. A model the catalog proves absent is refused: the turn keeps the configured model
  and the refusal is recorded as `model_not_in_provider_catalog` with the model Jev picked. No
  catalog evidence (absent/unreadable/unexpected) means "no verdict" and routing proceeds as before.
  `jev_router_status` reports offending grid entries under `grid_unavailable`.

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
  `grid`, `tail`, `reset`), and the `jev_router_status` / `jev_router_route` agent tools.
- 74 tests, driving the real middleware callback against a stub Decisions API with no network access.

[0.1.0]: https://github.com/AlphaPerseii3000/jev-router/releases/tag/v0.1.0
